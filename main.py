import os
import sqlite3
import shutil
import uuid
import asyncio
import inspect
from pathlib import Path
from datetime import datetime

import flet as ft
import flet_audio as fta
from mutagen import File as MutagenFile


# ============================================================
# REDA MUSIC V2
# 100% LOCAL - NO SERVER - NO LOGIN - NO ACCOUNT
# Android/Desktop + Flet + SQLite + Flet Audio + Mutagen
#
# Features:
# - One-click import: choose a song -> it is saved immediately
# - Offline playback from local storage
# - Cross-platform local playback with flet-audio (fresh Audio service per song)
# - Search
# - Favorites
# - Recently played
# - Playlists
# - Previous / Next
# - Shuffle / Repeat
# - Volume
# - Progress bar + seek
# - Embedded cover art
# - Safe SQLite transactions
# - No cloud, no internet, no authentication
# ============================================================


APP_NAME = "REDA MUSIC"

# Flet 0.86+ uses this writable app-private directory on Android.
BASE_DIR = Path(
    os.environ.get("FLET_APP_STORAGE_DATA")
    or (Path.home() / ".reda_music")
)
AUDIO_DIR = BASE_DIR / "audios"
COVER_DIR = BASE_DIR / "covers"
DB_PATH = BASE_DIR / "reda_music_v3.db"

# Bundled asset: assets/images/reda_chess.jpg
HERO_ASSET = "images/reda_chess.jpg"

SUPPORTED_AUDIO = [
    "mp3",
    "wav",
    "aac",
    "m4a",
    "flac",
    "ogg",
    "wma",
]


# ============================================================
# HYBRID LOCAL AUDIO PLAYER
# ============================================================
# LOCAL AUDIO ENGINE — FLET AUDIO ONLY
# ============================================================
# No Windows MCI. Each song gets a fresh flet-audio Audio service.

class LocalAudioPlayer:
    """
    REDA MUSIC local audio engine.

    IMPORTANT:
    - No Windows MCI.
    - A fresh flet-audio Audio service is created for every selected file.
    - The source is passed in the Audio constructor, matching the official
      Flet Audio usage pattern and avoiding stale native-player state.
    - The old service is released before a new one is attached.
    """

    def __init__(self, page: ft.Page):
        self.page = page
        self._audio = None
        self.current_path = None
        self.is_open = False
        self.is_playing = False
        self._duration = 0.0
        self._volume = 1.0
        self._loaded_event = None

    async def _dispose_current(self):
        old = self._audio
        self._audio = None
        if old is not None:
            try:
                await old.release()
            except Exception:
                pass
            try:
                if old in self.page.services:
                    self.page.services.remove(old)
            except Exception:
                pass

    async def close(self):
        await self._dispose_current()
        self.current_path = None
        self.is_open = False
        self.is_playing = False
        self._duration = 0.0

    async def open(self, path):
        path = Path(path).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"ملف الصوت غير موجود: {path}")

        await self._dispose_current()

        # Use a fresh Audio service with src already assigned. This is the
        # same construction pattern shown in the official Flet Audio docs.
        self._loaded_event = asyncio.Event()

        audio = fta.Audio(
            src=str(path),
            autoplay=False,
            volume=self._volume,
            balance=0,
            release_mode=fta.ReleaseMode.STOP,
            on_loaded=self._on_loaded,
            on_state_change=self._on_state_change,
        )
        self.page.services.append(audio)
        self._audio = audio

        # Push the newly created service/source to the native client before
        # invoking play().
        try:
            self.page.update()
        except Exception:
            pass

        self.current_path = path
        self.is_open = True
        self.is_playing = False
        self._duration = 0.0

    def _on_loaded(self, e):
        if self._loaded_event is not None:
            self._loaded_event.set()
        print("REDA MUSIC: audio loaded")

    def _on_state_change(self, e):
        try:
            state = str(e.state)
            self.is_playing = state.endswith("PLAYING")
        except Exception:
            pass

    async def play(self):
        if not self.is_open or self.current_path is None or self._audio is None:
            raise RuntimeError("لم يتم فتح ملف صوتي.")

        print(f"REDA MUSIC: waiting for audio load: {self.current_path}")
        if self._loaded_event is not None:
            try:
                await asyncio.wait_for(self._loaded_event.wait(), timeout=12.0)
            except asyncio.TimeoutError:
                print("REDA MUSIC: audio load event timed out; trying play anyway")

        print(f"REDA MUSIC: play request: {self.current_path}")
        await self._audio.play()
        self.is_playing = True

    async def pause(self):
        if self._audio is not None and self.is_open:
            await self._audio.pause()
        self.is_playing = False

    async def resume(self):
        if self._audio is None or not self.is_open:
            raise RuntimeError("لم يتم فتح ملف صوتي.")
        await self._audio.resume()
        self.is_playing = True

    async def stop(self):
        if self._audio is not None and self.is_open:
            try:
                await self._audio.pause()
            except Exception:
                pass
        self.is_playing = False

    async def seek(self, seconds):
        if self._audio is None or not self.is_open:
            return
        await self._audio.seek(ft.Duration(seconds=float(seconds)))

    async def position(self):
        if self._audio is None or not self.is_open:
            return 0.0
        try:
            value = await self._audio.get_current_position()
            return float(duration_to_seconds(value))
        except Exception:
            return 0.0

    async def length(self):
        if self._audio is None or not self.is_open:
            return self._duration
        if self._duration:
            return self._duration
        try:
            value = await self._audio.get_duration()
            self._duration = float(duration_to_seconds(value))
        except Exception:
            pass
        return self._duration

    def set_duration(self, seconds):
        self._duration = max(0.0, float(seconds or 0))

    def set_volume(self, value):
        self._volume = max(0.0, min(1.0, float(value)))
        if self._audio is not None:
            try:
                self._audio.volume = self._volume
            except Exception:
                pass


# ============================================================
# STORAGE
# ============================================================

def ensure_storage():
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    COVER_DIR.mkdir(parents=True, exist_ok=True)


def connect_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db():
    ensure_storage()

    conn = connect_db()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS songs (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                artist TEXT NOT NULL DEFAULT 'غير معروف',
                album TEXT NOT NULL DEFAULT '',
                duration INTEGER NOT NULL DEFAULT 0,
                file_path TEXT NOT NULL UNIQUE,
                cover_path TEXT NOT NULL DEFAULT '',
                is_favorite INTEGER NOT NULL DEFAULT 0,
                added_at TEXT NOT NULL,
                last_played TEXT NOT NULL DEFAULT ''
            )
            """
        )

        # ----------------------------------------------------
        # Database migration
        # ----------------------------------------------------
        # Older REDA MUSIC versions may already have created
        # the same database file with a smaller songs table.
        # CREATE TABLE IF NOT EXISTS does NOT modify an existing
        # table, so we add any missing columns safely here.
        existing_columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(songs)"
            ).fetchall()
        }

        migrations = {
            "title": (
                "ALTER TABLE songs "
                "ADD COLUMN title TEXT NOT NULL DEFAULT ''"
            ),
            "artist": (
                "ALTER TABLE songs "
                "ADD COLUMN artist TEXT NOT NULL DEFAULT 'غير معروف'"
            ),
            "album": (
                "ALTER TABLE songs "
                "ADD COLUMN album TEXT NOT NULL DEFAULT ''"
            ),
            "duration": (
                "ALTER TABLE songs "
                "ADD COLUMN duration INTEGER NOT NULL DEFAULT 0"
            ),
            "file_path": (
                "ALTER TABLE songs "
                "ADD COLUMN file_path TEXT NOT NULL DEFAULT ''"
            ),
            "cover_path": (
                "ALTER TABLE songs "
                "ADD COLUMN cover_path TEXT NOT NULL DEFAULT ''"
            ),
            "is_favorite": (
                "ALTER TABLE songs "
                "ADD COLUMN is_favorite INTEGER NOT NULL DEFAULT 0"
            ),
            "added_at": (
                "ALTER TABLE songs "
                "ADD COLUMN added_at TEXT NOT NULL DEFAULT ''"
            ),
            "last_played": (
                "ALTER TABLE songs "
                "ADD COLUMN last_played TEXT NOT NULL DEFAULT ''"
            ),
        }

        for column, sql in migrations.items():
            if column not in existing_columns:
                conn.execute(sql)
                existing_columns.add(column)

        # Some very old databases may have rows with an empty
        # added_at value after migration. Fill it so ordering works.
        conn.execute(
            """
            UPDATE songs
            SET added_at = ?
            WHERE added_at IS NULL OR added_at = ''
            """,
            (
                datetime.now().isoformat(
                    timespec="seconds"
                ),
            ),
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS playlists (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS playlist_songs (
                playlist_id TEXT NOT NULL,
                song_id TEXT NOT NULL,
                position INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (playlist_id, song_id),
                FOREIGN KEY (playlist_id)
                    REFERENCES playlists(id)
                    ON DELETE CASCADE,
                FOREIGN KEY (song_id)
                    REFERENCES songs(id)
                    ON DELETE CASCADE
            )
            """
        )

        conn.commit()
    finally:
        conn.close()


def validate_database_schema():
    """Fail early with a readable error if the local schema is incomplete."""
    required = {
        "id", "title", "artist", "album", "duration",
        "file_path", "cover_path", "is_favorite",
        "added_at", "last_played",
    }

    conn = connect_db()
    try:
        actual = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(songs)"
            ).fetchall()
        }
    finally:
        conn.close()

    missing = sorted(required - actual)
    if missing:
        raise RuntimeError(
            "قاعدة بيانات REDA MUSIC غير مكتملة. "
            "الأعمدة الناقصة: " + ", ".join(missing)
        )


def rows_to_dict(rows):
    keys = [
        "id",
        "title",
        "artist",
        "album",
        "duration",
        "file_path",
        "cover_path",
        "is_favorite",
        "added_at",
        "last_played",
    ]
    return [dict(zip(keys, row)) for row in rows]


def get_songs(mode="all", search=""):
    conn = connect_db()
    try:
        query = """
            SELECT id, title, artist, album, duration,
                   file_path, cover_path, is_favorite,
                   added_at, last_played
            FROM songs
            WHERE 1=1
        """
        params = []

        if mode == "favorites":
            query += " AND is_favorite = 1"

        if mode == "recent":
            query += " AND last_played <> ''"

        if search.strip():
            query += """
                AND (
                    title LIKE ?
                    OR artist LIKE ?
                    OR album LIKE ?
                )
            """
            q = f"%{search.strip()}%"
            params.extend([q, q, q])

        if mode == "recent":
            query += " ORDER BY last_played DESC"
        else:
            query += " ORDER BY added_at DESC"

        return rows_to_dict(conn.execute(query, params).fetchall())
    finally:
        conn.close()


def get_song(song_id):
    conn = connect_db()
    try:
        row = conn.execute(
            """
            SELECT id, title, artist, album, duration,
                   file_path, cover_path, is_favorite,
                   added_at, last_played
            FROM songs
            WHERE id = ?
            """,
            (song_id,),
        ).fetchone()
        return rows_to_dict([row])[0] if row else None
    finally:
        conn.close()


def get_all_songs():
    return get_songs("all")


def get_playlists():
    conn = connect_db()
    try:
        rows = conn.execute(
            """
            SELECT p.id, p.name, COUNT(ps.song_id)
            FROM playlists p
            LEFT JOIN playlist_songs ps
                ON ps.playlist_id = p.id
            GROUP BY p.id
            ORDER BY p.created_at DESC
            """
        ).fetchall()
        return rows
    finally:
        conn.close()


def get_playlist_songs(playlist_id):
    conn = connect_db()
    try:
        rows = conn.execute(
            """
            SELECT s.id, s.title, s.artist, s.album, s.duration,
                   s.file_path, s.cover_path, s.is_favorite,
                   s.added_at, s.last_played
            FROM songs s
            INNER JOIN playlist_songs ps
                ON ps.song_id = s.id
            WHERE ps.playlist_id = ?
            ORDER BY ps.position ASC, s.added_at DESC
            """,
            (playlist_id,),
        ).fetchall()
        return rows_to_dict(rows)
    finally:
        conn.close()


def create_playlist(name):
    name = (name or "").strip()
    if not name:
        return False, "اكتب اسم القائمة."

    conn = connect_db()
    try:
        conn.execute(
            """
            INSERT INTO playlists (id, name, created_at)
            VALUES (?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                name,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        return True, "تم إنشاء القائمة."
    except sqlite3.IntegrityError:
        return False, "اسم القائمة موجود بالفعل."
    finally:
        conn.close()


def add_song_to_playlist(playlist_id, song_id):
    conn = connect_db()
    try:
        existing = conn.execute(
            """
            SELECT 1 FROM playlist_songs
            WHERE playlist_id = ? AND song_id = ?
            """,
            (playlist_id, song_id),
        ).fetchone()

        if existing:
            return False

        position = conn.execute(
            """
            SELECT COALESCE(MAX(position), -1) + 1
            FROM playlist_songs
            WHERE playlist_id = ?
            """,
            (playlist_id,),
        ).fetchone()[0]

        conn.execute(
            """
            INSERT INTO playlist_songs
                (playlist_id, song_id, position)
            VALUES (?, ?, ?)
            """,
            (playlist_id, song_id, position),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def remove_song_from_playlist(playlist_id, song_id):
    conn = connect_db()
    try:
        conn.execute(
            """
            DELETE FROM playlist_songs
            WHERE playlist_id = ? AND song_id = ?
            """,
            (playlist_id, song_id),
        )
        conn.commit()
    finally:
        conn.close()


def delete_playlist(playlist_id):
    conn = connect_db()
    try:
        conn.execute(
            "DELETE FROM playlists WHERE id = ?",
            (playlist_id,),
        )
        conn.commit()
    finally:
        conn.close()


def toggle_favorite_db(song_id):
    conn = connect_db()
    try:
        row = conn.execute(
            "SELECT is_favorite FROM songs WHERE id = ?",
            (song_id,),
        ).fetchone()

        if not row:
            return

        value = 0 if row[0] else 1

        conn.execute(
            """
            UPDATE songs
            SET is_favorite = ?
            WHERE id = ?
            """,
            (value, song_id),
        )
        conn.commit()
    finally:
        conn.close()


def mark_played(song_id):
    conn = connect_db()
    try:
        conn.execute(
            """
            UPDATE songs
            SET last_played = ?
            WHERE id = ?
            """,
            (
                datetime.now().isoformat(timespec="seconds"),
                song_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def delete_song_db(song_id):
    conn = connect_db()
    try:
        row = conn.execute(
            """
            SELECT file_path, cover_path
            FROM songs
            WHERE id = ?
            """,
            (song_id,),
        ).fetchone()

        conn.execute(
            "DELETE FROM songs WHERE id = ?",
            (song_id,),
        )
        conn.commit()
        return row
    finally:
        conn.close()


# ============================================================
# METADATA
# ============================================================

def format_duration(seconds):
    seconds = max(0, int(seconds or 0))
    return f"{seconds // 60}:{seconds % 60:02d}"


def duration_to_seconds(value):
    if value is None:
        return 0

    # Current Flet Duration exposes in_seconds.
    if hasattr(value, "in_seconds"):
        try:
            return int(value.in_seconds)
        except Exception:
            pass

    # Compatibility with datetime.timedelta-like values.
    if hasattr(value, "total_seconds"):
        try:
            return int(value.total_seconds())
        except Exception:
            pass

    # Some event payloads may expose milliseconds.
    if hasattr(value, "in_milliseconds"):
        try:
            return int(value.in_milliseconds / 1000)
        except Exception:
            pass

    try:
        return int(value)
    except Exception:
        return 0


def read_metadata(file_path):
    path = Path(file_path)

    title = path.stem
    artist = "غير معروف"
    album = ""
    duration = 0
    cover_bytes = None

    try:
        audio = MutagenFile(str(path), easy=False)

        if audio and getattr(audio, "info", None):
            duration = int(
                getattr(audio.info, "length", 0) or 0
            )

        tags = getattr(audio, "tags", None) if audio else None

        if not tags:
            return (
                title,
                artist,
                album,
                duration,
                cover_bytes,
            )

        def get_tag(keys):
            for key in keys:
                try:
                    if key in tags:
                        value = tags[key]

                        if isinstance(value, (list, tuple)):
                            return (
                                str(value[0])
                                if value
                                else ""
                            )

                        return str(value)
                except Exception:
                    continue

            return ""

        title = (
            get_tag(
                [
                    "TIT2",
                    "\xa9nam",
                    "title",
                ]
            )
            or title
        )

        artist = (
            get_tag(
                [
                    "TPE1",
                    "\xa9ART",
                    "artist",
                ]
            )
            or artist
        )

        album = (
            get_tag(
                [
                    "TALB",
                    "\xa9alb",
                    "album",
                ]
            )
            or ""
        )

        for key in tags.keys():
            if str(key).startswith("APIC"):
                try:
                    cover_bytes = tags[key].data
                    break
                except Exception:
                    pass

        if cover_bytes is None:
            try:
                if "covr" in tags:
                    cover_bytes = bytes(tags["covr"][0])
            except Exception:
                pass

    except Exception:
        # Metadata failure should NEVER prevent importing the song.
        pass

    return (
        title.strip() or path.stem,
        artist.strip() or "غير معروف",
        album.strip(),
        duration,
        cover_bytes,
    )


def save_cover(song_id, cover_bytes):
    if not cover_bytes:
        return ""

    cover_path = COVER_DIR / f"{song_id}.jpg"

    try:
        cover_path.write_bytes(cover_bytes)
        return str(cover_path)
    except Exception:
        return ""


def safe_filename(original_name):
    extension = Path(original_name).suffix.lower()

    if extension not in {
        f".{x}" for x in SUPPORTED_AUDIO
    }:
        extension = ".mp3"

    return f"{uuid.uuid4().hex}{extension}"


# ============================================================
# APP
# ============================================================

def main(page: ft.Page):
    page.title = APP_NAME
    page.theme_mode = ft.ThemeMode.DARK
    page.bgcolor = "#0B0B0D"
    page.padding = 0
    page.spacing = 0
    page.rtl = True

    # Never force desktop window dimensions on Android.

    init_db()
    validate_database_schema()

    state = {
        "view": "home",
        "search": "",
        "queue": [],
        "queue_index": -1,
        "current_song_id": None,
        "is_playing": False,
        "shuffle": False,
        "repeat": False,
        "volume": 1.0,
        "position": 0,
        "duration": 0,
        "playlist_id": None,
    }

    # --------------------------------------------------------
    # Cross-platform services
    # --------------------------------------------------------
    audio = LocalAudioPlayer(page)

    # --------------------------------------------------------
    # REDA MUSIC - PREMIUM CHESS IDENTITY UI
    # --------------------------------------------------------
    # Bundled hero image. Flet serves files relative to assets_dir.
    # Keep this as a relative asset path for desktop + Android builds.
    HERO_IMAGE = "images/reda_chess.jpg"

    state.update({
        "view": "home",
        "search": "",
        "queue": [],
        "queue_index": -1,
        "current_song_id": None,
        "is_playing": False,
        "shuffle": False,
        "repeat": False,
        "volume": 1.0,
        "position": 0,
        "duration": 0,
        "playlist_id": None,
    })

    GREEN = "#1DB954"
    GREEN_SOFT = "#1DB95422"
    BG = "#08090A"
    SURFACE = "#111214"
    SURFACE_2 = "#17191B"
    BORDER = "#292C2F"
    MUTED = "#8D9296"
    GOLD = "#D9B36C"
    WHITE = "#F5F5F3"

    # Main content area. The image is loaded only if it exists next
    # to the Python file, so the app still opens safely without it.
    content_area = ft.Container(
        expand=True,
        padding=ft.Padding.only(
            left=22, right=22, top=18, bottom=12
        ),
    )

    # ---------------------------
    # Player controls
    # ---------------------------
    player_title = ft.Text(
        "لم يتم اختيار أغنية",
        color=WHITE,
        size=13,
        weight=ft.FontWeight.BOLD,
        no_wrap=True,
        overflow=ft.TextOverflow.ELLIPSIS,
    )
    player_artist = ft.Text(
        "REDA MUSIC",
        color=MUTED,
        size=10,
        no_wrap=True,
        overflow=ft.TextOverflow.ELLIPSIS,
    )
    player_cover = ft.Container(
        width=52,
        height=52,
        bgcolor="#24272A",
        border_radius=12,
        alignment=ft.Alignment(0, 0),
        content=ft.Icon(
            ft.Icons.MUSIC_NOTE_ROUNDED,
            color=GREEN,
            size=25,
        ),
    )
    position_text = ft.Text("0:00", color=MUTED, size=9)
    duration_text = ft.Text("0:00", color=MUTED, size=9)

    progress = ft.Slider(
        min=0,
        max=1,
        value=0,
        expand=True,
        active_color=GREEN,
        inactive_color="#34373A",
        thumb_color=GREEN,
    )
    volume_slider = ft.Slider(
        min=0,
        max=1,
        value=1,
        width=95,
        active_color=GREEN,
        inactive_color="#34373A",
        thumb_color=GREEN,
    )
    play_pause_button = ft.IconButton(
        icon=ft.Icons.PLAY_CIRCLE_FILLED,
        icon_color=WHITE,
        icon_size=38,
        tooltip="تشغيل / إيقاف مؤقت",
    )
    shuffle_button = ft.IconButton(
        icon=ft.Icons.SHUFFLE_ROUNDED,
        icon_color="#74797D",
        tooltip="تشغيل عشوائي",
    )
    repeat_button = ft.IconButton(
        icon=ft.Icons.REPEAT_ROUNDED,
        icon_color="#74797D",
        tooltip="تكرار",
    )

    player_bar = ft.Container(
        visible=False,
        bgcolor="#101113F5",
        border=ft.Border.only(
            top=ft.BorderSide(1, BORDER)
        ),
        padding=ft.Padding.symmetric(
            horizontal=18,
            vertical=10,
        ),
    )

    # ---------------------------
    # Small helpers
    # ---------------------------
    def message(text, success=False):
        try:
            page.show_dialog(
                ft.SnackBar(
                    content=ft.Text(text, color="white"),
                    bgcolor=(
                        "#147A3A" if success else "#B3261E"
                    ),
                    duration=2600,
                )
            )
        except Exception:
            print(text)

    def cover_widget(song, size=58, radius=14):
        raw = song.get("cover_path") or ""
        if raw and Path(raw).is_file():
            return ft.Container(
                width=size,
                height=size,
                border_radius=radius,
                clip_behavior=ft.ClipBehavior.HARD_EDGE,
                content=ft.Image(
                    src=raw,
                    width=size,
                    height=size,
                    fit="cover",
                ),
            )
        return ft.Container(
            width=size,
            height=size,
            bgcolor="#24272A",
            border_radius=radius,
            alignment=ft.Alignment(0, 0),
            content=ft.Icon(
                ft.Icons.MUSIC_NOTE_ROUNDED,
                color=GREEN,
                size=max(22, int(size * 0.42)),
            ),
        )

    def stat_card(number, label, icon):
        return ft.Container(
            bgcolor=SURFACE,
            border=ft.Border.all(1, BORDER),
            border_radius=16,
            padding=14,
            expand=True,
            content=ft.Row(
                [
                    ft.Container(
                        width=42,
                        height=42,
                        bgcolor=GREEN_SOFT,
                        border_radius=12,
                        alignment=ft.Alignment(0, 0),
                        content=ft.Icon(
                            icon,
                            color=GREEN,
                            size=21,
                        ),
                    ),
                    ft.Column(
                        [
                            ft.Text(
                                str(number),
                                color=WHITE,
                                size=17,
                                weight=ft.FontWeight.BOLD,
                            ),
                            ft.Text(
                                label,
                                color=MUTED,
                                size=9,
                            ),
                        ],
                        spacing=1,
                    ),
                ],
                spacing=10,
            ),
        )

    # ---------------------------
    # Native cross-platform file picker
    # ---------------------------
    file_picker = ft.FilePicker()
    page.services.append(file_picker)

    async def pick_audio_files():
        return await file_picker.pick_files(
            dialog_title="اختار الأغاني لإضافتها إلى REDA MUSIC",
            allow_multiple=True,
            file_type=ft.FilePickerFileType.CUSTOM,
            allowed_extensions=SUPPORTED_AUDIO,
            # Android/iOS require file bytes when a filesystem path
            # is not exposed by the native picker.
            with_data=True,
        )

    # ---------------------------
    # Import songs
    # ---------------------------
    async def import_songs(e=None):
        try:
            files = await pick_audio_files()
        except Exception as ex:
            message(f"تعذر فتح اختيار الملفات: {ex}")
            return

        if not files:
            return

        imported = 0
        skipped = 0
        errors = []

        for selected in files:
            source_path = None
            destination = None
            try:
                selected_name = getattr(selected, "name", "song.mp3")
                selected_bytes = getattr(selected, "bytes", None)
                selected_path = getattr(selected, "path", None)

                if selected_bytes:
                    temp_source = AUDIO_DIR / (
                        f".import_{uuid.uuid4().hex}"
                        f"{Path(selected_name).suffix.lower()}"
                    )
                    temp_source.write_bytes(selected_bytes)
                    source_path = temp_source
                elif selected_path:
                    source_path = Path(selected_path)

                if source_path is None or not source_path.is_file():
                    raise RuntimeError(
                        "تعذر قراءة الملف المحدد من مدير الملفات."
                    )

                title, artist, album, duration, cover = (
                    read_metadata(str(source_path))
                )

                song_id = str(uuid.uuid4())
                destination = AUDIO_DIR / safe_filename(selected_name)

                if selected_bytes:
                    destination.write_bytes(selected_bytes)
                else:
                    shutil.copy2(
                        str(source_path),
                        str(destination),
                    )

                if (
                    not destination.is_file()
                    or destination.stat().st_size <= 0
                ):
                    raise RuntimeError(
                        "فشل نسخ ملف الأغنية أو الملف فارغ."
                    )

                with destination.open("rb") as check:
                    if not check.read(16):
                        raise RuntimeError(
                            "تعذر قراءة ملف الأغنية بعد الحفظ."
                        )

                cover_path = save_cover(song_id, cover)

                conn = connect_db()
                try:
                    conn.execute(
                        """
                        INSERT INTO songs (
                            id, title, artist, album, duration,
                            file_path, cover_path, is_favorite,
                            added_at, last_played
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, '')
                        """,
                        (
                            song_id,
                            title,
                            artist,
                            album,
                            duration,
                            str(destination),
                            cover_path,
                            datetime.now().isoformat(
                                timespec="seconds"
                            ),
                        ),
                    )
                    conn.commit()
                finally:
                    conn.close()

                if not get_song(song_id):
                    raise RuntimeError(
                        "تم حفظ الملف لكن فشل تسجيله."
                    )

                imported += 1

                try:
                    if (
                        source_path
                        and source_path.name.startswith(".import_")
                        and source_path.is_file()
                    ):
                        source_path.unlink()
                except Exception:
                    pass

            except Exception as ex:
                skipped += 1
                errors.append(
                    f"{selected_name}: {ex}"
                )
                try:
                    if destination and destination.is_file():
                        destination.unlink()
                except Exception:
                    pass
                try:
                    if (
                        source_path
                        and source_path.name.startswith(".import_")
                        and source_path.is_file()
                    ):
                        source_path.unlink()
                except Exception:
                    pass

        if imported:
            message(
                f"تمت إضافة {imported} أغنية إلى مكتبتك ✓"
                + (
                    f" وتم تخطي {skipped}."
                    if skipped
                    else ""
                ),
                success=True,
            )
        else:
            message(
                "لم تتم إضافة أي أغنية."
                + (
                    f" {errors[0]}"
                    if errors
                    else ""
                )
            )

        state["search"] = ""
        state["view"] = "library"
        library_view()

    # ---------------------------
    # Playback
    # ---------------------------
    def resolve_song_file(song):
        raw = str(song.get("file_path") or "").strip()
        if raw:
            path = Path(raw)
            if path.is_file():
                return path

        filename = Path(raw).name
        if filename:
            candidates = list(AUDIO_DIR.glob(filename))
            if candidates:
                return candidates[0]

        # Last-resort repair by title/extension is deliberately
        # conservative: never guess a different song.
        return None

    def update_player_ui():
        has_song = bool(state["current_song_id"])
        player_bar.visible = has_song

        if not has_song:
            return

        song = get_song(state["current_song_id"])
        if not song:
            return

        player_title.value = song["title"]
        player_artist.value = song["artist"] or "غير معروف"

        raw_cover = song.get("cover_path") or ""
        if raw_cover and Path(raw_cover).is_file():
            player_cover.content = ft.Image(
                src=raw_cover,
                width=52,
                height=52,
                fit="cover",
            )
        else:
            player_cover.content = ft.Icon(
                ft.Icons.MUSIC_NOTE_ROUNDED,
                color=GREEN,
                size=25,
            )

        duration = max(
            0,
            int(state["duration"] or song["duration"] or 0),
        )
        position = max(
            0,
            min(int(state["position"]), duration or 999999999),
        )

        position_text.value = format_duration(position)
        duration_text.value = format_duration(duration)
        progress.max = max(1, duration)
        progress.value = min(position, max(1, duration))

        play_pause_button.icon = (
            ft.Icons.PAUSE_CIRCLE_FILLED
            if state["is_playing"]
            else ft.Icons.PLAY_CIRCLE_FILLED
        )
        shuffle_button.icon_color = (
            GREEN if state["shuffle"] else "#74797D"
        )
        repeat_button.icon_color = (
            GREEN if state["repeat"] else "#74797D"
        )
        volume_slider.value = state["volume"]

    async def play_song(song):
        if not song:
            return

        path = resolve_song_file(song)
        if path is None or not path.is_file():
            message(
                "ملف الأغنية غير موجود في تخزين REDA MUSIC."
            )
            return

        try:
            print(f"REDA MUSIC: opening audio: {path}")
            await audio.open(path)
            audio.set_volume(state["volume"])
            print("REDA MUSIC: source sent to native audio player; starting playback")
            await audio.play()
            print("REDA MUSIC: playback started")

            state["current_song_id"] = song["id"]
            state["is_playing"] = True

            native_duration = int(song["duration"] or 0)
            state["duration"] = native_duration
            audio.set_duration(state["duration"])
            state["position"] = 0

            stored_path = str(song.get("file_path") or "")
            if stored_path != str(path):
                conn = connect_db()
                try:
                    conn.execute(
                        "UPDATE songs SET file_path=? WHERE id=?",
                        (str(path), song["id"]),
                    )
                    conn.commit()
                finally:
                    conn.close()

            mark_played(song["id"])
            update_player_ui()
            page.update()

        except Exception as ex:
            state["is_playing"] = False
            update_player_ui()
            page.update()
            message(
                f"تعذر تشغيل الأغنية: {type(ex).__name__}: {ex}"
            )

    def set_queue(songs, index):
        state["queue"] = list(songs)
        state["queue_index"] = index

    async def play_from_list(songs, index):
        if not songs:
            return
        set_queue(songs, index)
        await play_song(songs[index])

    async def next_song():
        queue = state["queue"] or get_all_songs()
        state["queue"] = queue
        if not queue:
            return

        if state["shuffle"]:
            import random
            index = random.randrange(len(queue))
        else:
            index = (
                state["queue_index"] + 1
            ) % len(queue)

        state["queue_index"] = index
        await play_song(queue[index])

    async def previous_song():
        queue = state["queue"] or get_all_songs()
        state["queue"] = queue
        if not queue:
            return

        index = (
            state["queue_index"] - 1
        ) % len(queue)
        state["queue_index"] = index
        await play_song(queue[index])

    async def toggle_play_pause(e=None):
        if not state["current_song_id"]:
            return
        try:
            if state["is_playing"]:
                await audio.pause()
                state["is_playing"] = False
            else:
                await audio.resume()
                state["is_playing"] = True

            update_player_ui()
            page.update()
        except Exception as ex:
            message(f"تعذر تغيير حالة التشغيل: {ex}")

    async def stop_audio(e=None):
        try:
            await audio.stop()
            await audio.close()
        except Exception:
            pass

        state["current_song_id"] = None
        state["is_playing"] = False
        state["position"] = 0
        state["duration"] = 0
        update_player_ui()
        page.update()

    async def on_progress_change(e):
        if not state["current_song_id"]:
            return

        target = int(float(e.control.value or 0))
        try:
            await audio.seek(target)
            state["position"] = target
            update_player_ui()
            page.update()
        except Exception as ex:
            message(f"تعذر تغيير موضع التشغيل: {ex}")

    def change_volume(e):
        value = float(e.control.value or 0)
        state["volume"] = value
        audio.set_volume(value)

    async def audio_monitor():
        while True:
            try:
                if state["current_song_id"] and audio.is_open:
                    position = int(await audio.position())
                    duration = int(state["duration"] or 0)

                    state["position"] = position

                    if (
                        state["is_playing"]
                        and duration > 0
                        and position >= max(0, duration - 1)
                    ):
                        state["is_playing"] = False
                        if state["repeat"]:
                            current = get_song(
                                state["current_song_id"]
                            )
                            if current:
                                await play_song(current)
                        else:
                            await next_song()
                    else:
                        update_player_ui()
                        page.update()
            except Exception:
                pass

            await asyncio.sleep(0.5)

    progress.on_change = lambda e: page.run_task(on_progress_change, e)
    volume_slider.on_change = change_volume
    play_pause_button.on_click = toggle_play_pause

    shuffle_button.on_click = lambda e: (
        state.update(
            {"shuffle": not state["shuffle"]}
        ),
        update_player_ui(),
        page.update(),
    )
    repeat_button.on_click = lambda e: (
        state.update(
            {"repeat": not state["repeat"]}
        ),
        update_player_ui(),
        page.update(),
    )

    # ---------------------------
    # Song actions
    # ---------------------------
    def toggle_favorite(song):
        toggle_favorite_db(song["id"])
        refresh_current_view()

    def open_playlist_picker(song):
        playlists = get_playlists()

        if not playlists:
            message("أنشئ Playlist أولًا.")
            return

        controls = []
        for playlist_id, name, count in playlists:
            controls.append(
                ft.ListTile(
                    leading=ft.Icon(
                        ft.Icons.QUEUE_MUSIC_ROUNDED,
                        color=GREEN,
                    ),
                    title=ft.Text(name, color=WHITE),
                    subtitle=ft.Text(
                        f"{count} أغنية",
                        color=MUTED,
                    ),
                    on_click=lambda e,
                    pid=playlist_id,
                    pname=name: (
                        add_song_to_playlist(
                            pid, song["id"]
                        ),
                        page.pop_dialog(),
                        message(
                            f"تمت إضافة الأغنية إلى {pname} ✓",
                            success=True,
                        ),
                    ),
                )
            )

        page.show_dialog(
            ft.AlertDialog(
                modal=True,
                title=ft.Text(
                    "إضافة إلى Playlist",
                    color=WHITE,
                ),
                content=ft.Column(
                    controls,
                    tight=True,
                    scroll=ft.ScrollMode.AUTO,
                ),
            )
        )

    def refresh_current_view():
        view = state.get("view", "home")
        if view == "favorites":
            favorites_view()
        elif view == "recent":
            recent_view()
        elif view == "playlists":
            if state.get("playlist_id"):
                playlist_detail_view(
                    state["playlist_id"]
                )
            else:
                playlists_view()
        elif view == "library":
            library_view()
        else:
            home_view()

    def delete_song(song):
        async def do_delete():
            try:
                if state["current_song_id"] == song["id"]:
                    await stop_audio()

                files = delete_song_db(song["id"])
                for raw_path in files or []:
                    if not raw_path:
                        continue
                    path = Path(raw_path)
                    if path.is_file():
                        try:
                            path.unlink()
                        except Exception as ex:
                            print(
                                "Could not remove file:",
                                path,
                                ex,
                            )

                state["queue"] = [
                    item for item in state["queue"]
                    if item["id"] != song["id"]
                ]

                if state["queue_index"] >= len(state["queue"]):
                    state["queue_index"] = (
                        len(state["queue"]) - 1
                    )

                refresh_current_view()
                message(
                    "تم حذف الأغنية نهائيًا من الجهاز ✓",
                    success=True,
                )
            except Exception as ex:
                message(f"تعذر حذف الأغنية: {ex}")

        asyncio.create_task(do_delete())

    # ---------------------------
    # Song cards / lists
    # ---------------------------
    async def song_click(e, song):
        """Play the clicked song directly."""
        try:
            print(f"REDA MUSIC: clicked song id={song.get('id')} title={song.get('title')}")
            print(f"REDA MUSIC: stored file_path={song.get('file_path')}")
            resolved = resolve_song_file(song)
            print(f"REDA MUSIC: resolved audio path={resolved}")

            songs = get_songs("all")
            if not songs:
                songs = [song]
            index = next(
                (i for i, item in enumerate(songs) if item["id"] == song["id"]),
                0,
            )
            set_queue(songs, index)
            await play_song(songs[index])
        except Exception as ex:
            message(f"تعذر تشغيل الأغنية: {type(ex).__name__}: {ex}")

    def song_tile(song, playlist_id=None, compact=False):
        subtitle = song["artist"] or "غير معروف"
        if song["album"]:
            subtitle += f" • {song['album']}"
        if song["duration"]:
            subtitle += (
                f" • {format_duration(song['duration'])}"
            )

        menu_items = [
            ft.PopupMenuItem(
                content=(
                    "إزالة من القائمة"
                    if playlist_id
                    else "إضافة إلى Playlist"
                ),
                icon=(
                    ft.Icons.REMOVE_CIRCLE_OUTLINE
                    if playlist_id
                    else ft.Icons.PLAYLIST_ADD
                ),
                on_click=lambda e,
                s=song,
                pid=playlist_id: (
                    remove_song_from_playlist(
                        pid, s["id"]
                    )
                    if pid
                    else open_playlist_picker(s)
                ),
            ),
            ft.PopupMenuItem(
                content="حذف من الجهاز",
                icon=ft.Icons.DELETE_OUTLINE,
                on_click=lambda e,
                s=song: delete_song(s),
            ),
        ]

        return ft.Container(
            bgcolor=(
                "#1A1D1F"
                if state["current_song_id"] == song["id"]
                else SURFACE
            ),
            border=ft.Border.all(
                1,
                (
                    "#315A40"
                    if state["current_song_id"] == song["id"]
                    else BORDER
                ),
            ),
            border_radius=16,
            padding=8 if compact else 10,
            margin=5,
            on_click=lambda e, s=song: page.run_task(song_click, e, s),
            content=ft.Row(
                [
                    cover_widget(
                        song,
                        48 if compact else 58,
                        12,
                    ),
                    ft.Column(
                        [
                            ft.Text(
                                song["title"],
                                color=WHITE,
                                size=13 if compact else 14,
                                weight=ft.FontWeight.BOLD,
                                no_wrap=True,
                                overflow=ft.TextOverflow.ELLIPSIS,
                            ),
                            ft.Text(
                                subtitle,
                                color=MUTED,
                                size=9 if compact else 10,
                                no_wrap=True,
                                overflow=ft.TextOverflow.ELLIPSIS,
                            ),
                        ],
                        spacing=3,
                        expand=True,
                    ),
                    ft.Container(
                        bgcolor="#0E5E2C33",
                        border_radius=9,
                        padding=ft.Padding.symmetric(
                            horizontal=8,
                            vertical=5,
                        ),
                        content=ft.Text(
                            "OFFLINE",
                            color=GREEN,
                            size=8,
                            weight=ft.FontWeight.BOLD,
                        ),
                    ),
                    ft.IconButton(
                        icon=(
                            ft.Icons.FAVORITE_ROUNDED
                            if song["is_favorite"]
                            else ft.Icons.FAVORITE_BORDER_ROUNDED
                        ),
                        icon_color=(
                            "#FF4F6D"
                            if song["is_favorite"]
                            else "#74797D"
                        ),
                        tooltip="المفضلة",
                        on_click=lambda e,
                        s=song: toggle_favorite(s),
                    ),
                    ft.PopupMenuButton(
                        items=menu_items,
                    ),
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=9,
            ),
        )

    def songs_list(songs, playlist_id=None):
        if not songs:
            return ft.Container(
                expand=True,
                alignment=ft.Alignment(0, 0),
                content=ft.Column(
                    [
                        ft.Icon(
                            ft.Icons.MUSIC_OFF_ROUNDED,
                            size=62,
                            color="#34383B",
                        ),
                        ft.Text(
                            "مكتبتك لسه فاضية",
                            color=WHITE,
                            size=17,
                            weight=ft.FontWeight.BOLD,
                        ),
                        ft.Text(
                            "أضف أول أغنية وخلي REDA MUSIC تبدأ.",
                            color=MUTED,
                            size=10,
                        ),
                        ft.Button(
                            content="إضافة أغنية",
                            icon=ft.Icons.ADD_ROUNDED,
                            on_click=import_songs,
                        ),
                    ],
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=8,
                ),
            )

        state["queue"] = list(songs)
        return ft.ListView(
            controls=[
                song_tile(song, playlist_id)
                for song in songs
            ],
            expand=True,
            spacing=0,
        )

    def mini_card(song):
        return ft.Container(
            width=210,
            bgcolor=SURFACE,
            border=ft.Border.all(1, BORDER),
            border_radius=18,
            padding=10,
            on_click=lambda e, s=song: page.run_task(song_click, e, s),
            content=ft.Column(
                [
                    cover_widget(song, 190, 14),
                    ft.Text(
                        song["title"],
                        color=WHITE,
                        size=12,
                        weight=ft.FontWeight.BOLD,
                        no_wrap=True,
                        overflow=ft.TextOverflow.ELLIPSIS,
                    ),
                    ft.Text(
                        song["artist"] or "غير معروف",
                        color=MUTED,
                        size=9,
                        no_wrap=True,
                        overflow=ft.TextOverflow.ELLIPSIS,
                    ),
                ],
                spacing=6,
            ),
        )

    async def play_all_click(e=None):
        songs = get_songs("all")
        if songs:
            await play_from_list(songs, 0)
        else:
            await import_songs(e)

    # ---------------------------
    # Home
    # ---------------------------
    def home_view():
        state["view"] = "home"
        all_songs = get_songs("all")
        recent = get_songs("recent")[:8]
        favorites = get_songs("favorites")
        playlists = get_playlists()

        hero_content = [
            ft.Container(
                bgcolor="#00000099",
                border_radius=24,
                padding=24,
                content=ft.Column(
                    [
                        ft.Row(
                            [
                                ft.Container(
                                    bgcolor="#D9B36C22",
                                    border_radius=10,
                                    padding=ft.Padding.symmetric(
                                        horizontal=10,
                                        vertical=6,
                                    ),
                                    content=ft.Text(
                                        "CHESS • MUSIC • OFFLINE",
                                        color=GOLD,
                                        size=8,
                                        weight=ft.FontWeight.BOLD,
                                    ),
                                ),
                            ]
                        ),
                        ft.Text(
                            "REDA MUSIC",
                            color=WHITE,
                            size=31,
                            weight=ft.FontWeight.BOLD,
                        ),
                        ft.Text(
                            "موسيقاك. مكتبتك. على جهازك.",
                            color="#E7E7E3",
                            size=15,
                            weight=ft.FontWeight.W_500,
                        ),
                        ft.Text(
                            "استمتع بأغانيك المفضلة بدون حساب وبدون إنترنت.",
                            color="#BFC3C4",
                            size=10,
                        ),
                        ft.Row(
                            [
                                ft.Button(
                                    content="تشغيل الموسيقى",
                                    icon=ft.Icons.PLAY_ARROW_ROUNDED,
                                    on_click=play_all_click,
                                ),
                                ft.Button(
                                    content="إضافة أغنية",
                                    icon=ft.Icons.ADD_ROUNDED,
                                    on_click=import_songs,
                                ),
                            ],
                            spacing=8,
                        ),
                    ],
                    spacing=8,
                ),
            )
        ]

        if HERO_IMAGE:
            hero = ft.Container(
                height=280,
                border_radius=24,
                clip_behavior=ft.ClipBehavior.HARD_EDGE,
                content=ft.Stack(
                    [
                        ft.Image(
                            src=HERO_IMAGE,
                            expand=True,
                            fit="cover",
                        ),
                        ft.Container(
                            expand=True,
                            bgcolor="#05050555",
                        ),
                        ft.Container(
                            expand=True,
                            padding=18,
                            content=ft.Column(
                                hero_content,
                                alignment=ft.MainAxisAlignment.END,
                            ),
                        ),
                    ]
                ),
            )
        else:
            hero = ft.Container(
                height=280,
                bgcolor="#171A1C",
                border_radius=24,
                padding=24,
                content=hero_content[0],
            )

        recent_controls = (
            [
                mini_card(song)
                for song in recent
            ]
            if recent
            else [
                ft.Container(
                    padding=18,
                    content=ft.Text(
                        "لا توجد أغاني تم تشغيلها مؤخرًا.",
                        color=MUTED,
                    ),
                )
            ]
        )

        content_area.content = ft.Column(
            [
                ft.Row(
                    [
                        ft.Column(
                            [
                                ft.Text(
                                    "صباح الخير 👋",
                                    color=MUTED,
                                    size=10,
                                ),
                                ft.Text(
                                    "جاهز للموسيقى؟",
                                    color=WHITE,
                                    size=22,
                                    weight=ft.FontWeight.BOLD,
                                ),
                            ],
                            spacing=2,
                            expand=True,
                        ),
                        ft.Container(
                            bgcolor=GREEN_SOFT,
                            border_radius=12,
                            padding=ft.Padding.symmetric(
                                horizontal=12,
                                vertical=8,
                            ),
                            content=ft.Row(
                                [
                                    ft.Icon(
                                        ft.Icons.OFFLINE_BOLT_ROUNDED,
                                        color=GREEN,
                                        size=17,
                                    ),
                                    ft.Text(
                                        "Offline",
                                        color=GREEN,
                                        size=9,
                                        weight=ft.FontWeight.BOLD,
                                    ),
                                ],
                                spacing=5,
                            ),
                        ),
                    ]
                ),
                hero,
                ft.Row(
                    [
                        stat_card(
                            len(all_songs),
                            "أغنية",
                            ft.Icons.MUSIC_NOTE_ROUNDED,
                        ),
                        stat_card(
                            len(favorites),
                            "مفضلة",
                            ft.Icons.FAVORITE_ROUNDED,
                        ),
                        stat_card(
                            len(playlists),
                            "قائمة تشغيل",
                            ft.Icons.QUEUE_MUSIC_ROUNDED,
                        ),
                    ],
                    spacing=10,
                ),
                ft.Row(
                    [
                        ft.Text(
                            "تم تشغيلها مؤخرًا",
                            color=WHITE,
                            size=17,
                            weight=ft.FontWeight.BOLD,
                        ),
                        ft.Container(expand=True),
                        ft.Button(
                            content="عرض الكل",
                            on_click=lambda e: (
                                state.update(
                                    {"view": "recent"}
                                ),
                                recent_view(),
                            ),
                        ),
                    ]
                ),
                ft.Row(
                    recent_controls,
                    spacing=10,
                    scroll=ft.ScrollMode.AUTO,
                ),
            ],
            expand=True,
            spacing=12,
            scroll=ft.ScrollMode.AUTO,
        )
        page.update()

    # ---------------------------
    # Library
    # ---------------------------
    def library_view():
        state["view"] = "library"
        songs = get_songs(
            "all",
            state["search"],
        )

        search_field = ft.TextField(
            hint_text="ابحث عن أغنية، فنان أو ألبوم...",
            prefix_icon=ft.Icons.SEARCH_ROUNDED,
            value=state["search"],
            height=48,
            bgcolor=SURFACE,
            border_color=BORDER,
            focused_border_color=GREEN,
            color=WHITE,
            hint_style=ft.TextStyle(color="#696E72"),
            on_submit=lambda e: (
                state.update(
                    {"search": e.control.value or ""}
                ),
                library_view(),
            ),
        )

        content_area.content = ft.Column(
            [
                ft.Row(
                    [
                        ft.Column(
                            [
                                ft.Text(
                                    "مكتبتي",
                                    color=WHITE,
                                    size=26,
                                    weight=ft.FontWeight.BOLD,
                                ),
                                ft.Text(
                                    f"{len(songs)} أغنية محفوظة على جهازك",
                                    color=MUTED,
                                    size=9,
                                ),
                            ],
                            spacing=2,
                            expand=True,
                        ),
                        ft.Button(
                            content="إضافة أغنية",
                            icon=ft.Icons.ADD_ROUNDED,
                            on_click=import_songs,
                        ),
                    ]
                ),
                search_field,
                ft.Row(
                    [
                        ft.Button(
                            content="الكل",
                            on_click=lambda e: (
                                state.update(
                                    {"search": ""}
                                ),
                                library_view(),
                            ),
                        ),
                        ft.Button(
                            content="❤️ المفضلة",
                            on_click=lambda e: (
                                state.update(
                                    {"view": "favorites"}
                                ),
                                favorites_view(),
                            ),
                        ),
                        ft.Button(
                            content="🕘 الأخيرة",
                            on_click=lambda e: (
                                state.update(
                                    {"view": "recent"}
                                ),
                                recent_view(),
                            ),
                        ),
                    ],
                    spacing=6,
                ),
                ft.Divider(
                    height=1,
                    color=BORDER,
                ),
                songs_list(songs),
            ],
            expand=True,
            spacing=9,
        )
        page.update()

    def favorites_view():
        state["view"] = "favorites"
        songs = get_songs("favorites")

        content_area.content = ft.Column(
            [
                ft.Row(
                    [
                        ft.Column(
                            [
                                ft.Text(
                                    "المفضلة",
                                    color=WHITE,
                                    size=26,
                                    weight=ft.FontWeight.BOLD,
                                ),
                                ft.Text(
                                    f"{len(songs)} أغنية أعجبتك",
                                    color=MUTED,
                                    size=9,
                                ),
                            ],
                            spacing=2,
                            expand=True,
                        ),
                        ft.Button(
                            content="إضافة",
                            icon=ft.Icons.ADD_ROUNDED,
                            on_click=import_songs,
                        ),
                    ]
                ),
                songs_list(songs),
            ],
            expand=True,
            spacing=9,
        )
        page.update()

    def recent_view():
        state["view"] = "recent"
        songs = get_songs("recent")

        content_area.content = ft.Column(
            [
                ft.Row(
                    [
                        ft.Column(
                            [
                                ft.Text(
                                    "آخر تشغيل",
                                    color=WHITE,
                                    size=26,
                                    weight=ft.FontWeight.BOLD,
                                ),
                                ft.Text(
                                    "أغانيك التي استمعت إليها مؤخرًا",
                                    color=MUTED,
                                    size=9,
                                ),
                            ],
                            spacing=2,
                            expand=True,
                        ),
                        ft.Button(
                            content="إضافة",
                            icon=ft.Icons.ADD_ROUNDED,
                            on_click=import_songs,
                        ),
                    ]
                ),
                songs_list(songs),
            ],
            expand=True,
            spacing=9,
        )
        page.update()

    # ---------------------------
    # Playlists
    # ---------------------------
    def playlists_view():
        state["view"] = "playlists"
        state["playlist_id"] = None
        playlists = get_playlists()

        def create_new_playlist(e=None):
            name_field = ft.TextField(
                label="اسم قائمة التشغيل",
                autofocus=True,
            )

            def save(e):
                ok, msg = create_playlist(
                    name_field.value
                )
                page.pop_dialog()
                message(msg, success=ok)
                if ok:
                    playlists_view()

            dialog = ft.AlertDialog(
                modal=True,
                title=ft.Text(
                    "قائمة تشغيل جديدة",
                    color=WHITE,
                ),
                content=name_field,
                actions=[
                    ft.Button(
                        content="إلغاء",
                        on_click=lambda e: page.pop_dialog(),
                    ),
                    ft.Button(
                        content="إنشاء",
                        on_click=save,
                    ),
                ],
            )
            page.show_dialog(dialog)

        controls = [
            ft.Row(
                [
                    ft.Column(
                        [
                            ft.Text(
                                "قوائم التشغيل",
                                color=WHITE,
                                size=26,
                                weight=ft.FontWeight.BOLD,
                            ),
                            ft.Text(
                                "رتّب موسيقاك بطريقتك",
                                color=MUTED,
                                size=9,
                            ),
                        ],
                        spacing=2,
                        expand=True,
                    ),
                    ft.Button(
                        content="قائمة جديدة",
                        icon=ft.Icons.ADD_ROUNDED,
                        on_click=create_new_playlist,
                    ),
                ]
            )
        ]

        if not playlists:
            controls.append(
                ft.Container(
                    expand=True,
                    alignment=ft.Alignment(0, 0),
                    content=ft.Column(
                        [
                            ft.Icon(
                                ft.Icons.QUEUE_MUSIC_ROUNDED,
                                size=65,
                                color="#34383B",
                            ),
                            ft.Text(
                                "لا توجد قوائم تشغيل",
                                color=WHITE,
                                size=17,
                                weight=ft.FontWeight.BOLD,
                            ),
                            ft.Text(
                                "أنشئ أول قائمة لك.",
                                color=MUTED,
                                size=10,
                            ),
                        ],
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        spacing=6,
                    ),
                )
            )
        else:
            for playlist_id, name, count in playlists:
                controls.append(
                    ft.Container(
                        bgcolor=SURFACE,
                        border=ft.Border.all(1, BORDER),
                        border_radius=18,
                        padding=12,
                        on_click=lambda e,
                        pid=playlist_id: (
                            state.update(
                                {"playlist_id": pid}
                            ),
                            playlist_detail_view(pid),
                        ),
                        content=ft.Row(
                            [
                                ft.Container(
                                    width=54,
                                    height=54,
                                    bgcolor="#2B2112",
                                    border_radius=14,
                                    alignment=ft.Alignment(0, 0),
                                    content=ft.Icon(
                                        ft.Icons.QUEUE_MUSIC_ROUNDED,
                                        color=GOLD,
                                        size=25,
                                    ),
                                ),
                                ft.Column(
                                    [
                                        ft.Text(
                                            name,
                                            color=WHITE,
                                            size=14,
                                            weight=ft.FontWeight.BOLD,
                                        ),
                                        ft.Text(
                                            f"{count} أغنية",
                                            color=MUTED,
                                            size=9,
                                        ),
                                    ],
                                    spacing=3,
                                    expand=True,
                                ),
                                ft.Icon(
                                    ft.Icons.CHEVRON_LEFT_ROUNDED,
                                    color="#666B6F",
                                ),
                                ft.IconButton(
                                    icon=ft.Icons.DELETE_OUTLINE,
                                    icon_color="#77777F",
                                    tooltip="حذف",
                                    on_click=lambda e,
                                    pid=playlist_id: (
                                        delete_playlist(pid),
                                        playlists_view(),
                                    ),
                                ),
                            ],
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                    )
                )

        content_area.content = ft.Column(
            controls,
            expand=True,
            spacing=8,
            scroll=ft.ScrollMode.AUTO,
        )
        page.update()

    def playlist_detail_view(playlist_id):
        playlist_rows = [
            row for row in get_playlists()
            if row[0] == playlist_id
        ]

        if not playlist_rows:
            playlists_view()
            return

        state["view"] = "playlists"
        state["playlist_id"] = playlist_id
        playlist_name = playlist_rows[0][1]
        songs = get_playlist_songs(playlist_id)

        content_area.content = ft.Column(
            [
                ft.Row(
                    [
                        ft.IconButton(
                            icon=ft.Icons.ARROW_BACK_ROUNDED,
                            icon_color=WHITE,
                            on_click=lambda e: playlists_view(),
                        ),
                        ft.Column(
                            [
                                ft.Text(
                                    playlist_name,
                                    color=WHITE,
                                    size=22,
                                    weight=ft.FontWeight.BOLD,
                                ),
                                ft.Text(
                                    f"{len(songs)} أغنية",
                                    color=MUTED,
                                    size=9,
                                ),
                            ],
                            spacing=1,
                            expand=True,
                        ),
                    ]
                ),
                songs_list(
                    songs,
                    playlist_id,
                ),
            ],
            expand=True,
            spacing=9,
        )
        page.update()

    # ---------------------------
    # Top navigation
    # ---------------------------
    def nav_button(label, icon, view_name, callback):
        active = state["view"] == view_name
        return ft.Button(
            content=label,
            icon=icon,
            style=ft.ButtonStyle(
                bgcolor=(
                    GREEN_SOFT if active else "transparent"
                ),
                color=(
                    GREEN if active else "#A1A5A8"
                ),
            ),
            on_click=callback,
        )

    navigation = ft.Row(
        [
            nav_button(
                "الرئيسية",
                ft.Icons.HOME_ROUNDED,
                "home",
                lambda e: home_view(),
            ),
            nav_button(
                "مكتبتي",
                ft.Icons.LIBRARY_MUSIC_ROUNDED,
                "library",
                lambda e: library_view(),
            ),
            nav_button(
                "المفضلة",
                ft.Icons.FAVORITE_ROUNDED,
                "favorites",
                lambda e: favorites_view(),
            ),
            nav_button(
                "آخر تشغيل",
                ft.Icons.HISTORY_ROUNDED,
                "recent",
                lambda e: recent_view(),
            ),
            nav_button(
                "Playlists",
                ft.Icons.QUEUE_MUSIC_ROUNDED,
                "playlists",
                lambda e: playlists_view(),
            ),
        ],
        spacing=4,
        scroll=ft.ScrollMode.AUTO,
    )

    # ---------------------------
    # Player
    # ---------------------------
    player_bar.content = ft.Column(
        [
            ft.Row(
                [
                    player_cover,
                    ft.Column(
                        [
                            player_title,
                            player_artist,
                        ],
                        spacing=2,
                        expand=True,
                    ),
                    ft.IconButton(
                        icon=ft.Icons.STOP_CIRCLE_OUTLINED,
                        icon_color="#8B9094",
                        tooltip="إيقاف",
                        on_click=stop_audio,
                    ),
                ],
                spacing=10,
            ),
            ft.Row(
                [
                    position_text,
                    progress,
                    duration_text,
                ],
                spacing=7,
            ),
            ft.Row(
                [
                    shuffle_button,
                    ft.IconButton(
                        icon=ft.Icons.SKIP_PREVIOUS_ROUNDED,
                        icon_color=WHITE,
                        icon_size=23,
                        tooltip="السابق",
                        on_click=lambda e: page.run_task(previous_song),
                    ),
                    play_pause_button,
                    ft.IconButton(
                        icon=ft.Icons.SKIP_NEXT_ROUNDED,
                        icon_color=WHITE,
                        icon_size=23,
                        tooltip="التالي",
                        on_click=lambda e: page.run_task(next_song),
                    ),
                    repeat_button,
                    ft.Icon(
                        ft.Icons.VOLUME_UP_ROUNDED,
                        size=18,
                        color="#74797D",
                    ),
                    volume_slider,
                ],
                alignment=ft.MainAxisAlignment.CENTER,
                spacing=3,
            ),
        ],
        spacing=2,
    )

    # ---------------------------
    # Header
    # ---------------------------
    header = ft.Container(
        bgcolor="#0B0C0DF2",
        border=ft.Border.only(
            bottom=ft.BorderSide(1, BORDER)
        ),
        padding=ft.Padding.symmetric(
            horizontal=22,
            vertical=12,
        ),
        content=ft.Row(
            [
                ft.Row(
                    [
                        ft.Container(
                            width=42,
                            height=42,
                            bgcolor="#1B241D",
                            border_radius=13,
                            alignment=ft.Alignment(0, 0),
                            content=ft.Icon(
                                ft.Icons.MUSIC_NOTE_ROUNDED,
                                color=GREEN,
                                size=23,
                            ),
                        ),
                        ft.Column(
                            [
                                ft.Text(
                                    "REDA MUSIC",
                                    color=WHITE,
                                    size=15,
                                    weight=ft.FontWeight.BOLD,
                                ),
                                ft.Text(
                                    "LOCAL MUSIC PLAYER",
                                    color="#666B6F",
                                    size=7,
                                    weight=ft.FontWeight.BOLD,
                                ),
                            ],
                            spacing=0,
                        ),
                    ],
                    spacing=9,
                    expand=True,
                ),
                navigation,
                ft.Button(
                    content="إضافة",
                    icon=ft.Icons.ADD_ROUNDED,
                    on_click=import_songs,
                ),
            ],
            spacing=12,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
    )

    # Start the background audio monitor only once.
    page.run_task(audio_monitor)

    page.add(
        ft.SafeArea(
            expand=True,
            content=ft.Column(
                [
                    header,
                    content_area,
                    player_bar,
                ],
                expand=True,
                spacing=0,
            ),
        )
    )

    home_view()


if __name__ == "__main__":
    ft.run(main, assets_dir="assets")
