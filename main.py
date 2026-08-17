import asyncio
import hashlib
import os
import re
import shutil
import sqlite3
from pathlib import Path

import flet as ft
import flet_audio as fta
from mutagen import File as MutagenFile


APP_NAME = "REDA MUSIC"
DB_NAME = "music.db"
MUSIC_DIR_NAME = "music"
AUDIO_EXTENSIONS = ["mp3", "m4a", "aac", "wav", "ogg", "flac", "opus"]


def safe_filename(name: str) -> str:
    name = Path(name).name
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    return name[:180] or "song"


def format_seconds(value) -> str:
    try:
        total = max(0, int(float(value)))
    except Exception:
        total = 0
    m, s = divmod(total, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def metadata_from_bytes(data: bytes, filename: str):
    title = Path(filename).stem
    artist = "غير معروف"
    album = "غير معروف"
    cover = None
    duration = 0.0

    try:
        meta = MutagenFile(data)
        if meta:
            def first(key, default):
                try:
                    val = meta.get(key)
                    if val:
                        if hasattr(val, "text"):
                            val = val.text
                        if isinstance(val, (list, tuple)):
                            return str(val[0])
                        return str(val)
                except Exception:
                    pass
                return default

            title = first("TIT2", first("©nam", title))
            artist = first("TPE1", first("©ART", artist))
            album = first("TALB", first("©alb", album))
            try:
                duration = float(getattr(meta.info, "length", 0) or 0)
            except Exception:
                duration = 0.0

            # Common MP3 cover extraction.
            try:
                if hasattr(meta, "tags") and meta.tags:
                    for tag in meta.tags.values():
                        if hasattr(tag, "data") and isinstance(tag.data, bytes):
                            cover = tag.data
                            break
            except Exception:
                pass
    except Exception:
        pass

    return title, artist, album, duration, cover


class LocalMusicApp:
    def __init__(self, page: ft.Page):
        self.page = page
        self.data_dir = Path(os.environ.get("FLET_APP_STORAGE_DATA", ".")).resolve()
        self.music_dir = self.data_dir / MUSIC_DIR_NAME
        self.music_dir.mkdir(parents=True, exist_ok=True)

        self.db_path = self.data_dir / DB_NAME
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.init_db()

        self.songs = []
        self.current_song_id = None
        self.current_bytes = None
        self.is_playing = False
        self.position = 0.0
        self.duration = 0.0
        self.search_text = ""
        self.filter_mode = "all"

        self.file_picker = ft.FilePicker()
        self.page.services.append(self.file_picker)

        self.audio = fta.Audio(
            src=None,
            autoplay=False,
            volume=1.0,
            release_mode=fta.ReleaseMode.STOP,
            on_position_change=self.on_position_change,
            on_duration_change=self.on_duration_change,
            on_state_change=self.on_state_change,
        )
        self.page.services.append(self.audio)

        self.title_text = ft.Text("لا توجد أغنية", size=15, weight=ft.FontWeight.BOLD)
        self.artist_text = ft.Text("اختر أغنية للتشغيل", size=12, color="#9BA0AA")
        self.position_text = ft.Text("0:00", size=11, color="#9BA0AA")
        self.duration_text = ft.Text("0:00", size=11, color="#9BA0AA")
        self.progress = ft.Slider(
            min=0,
            max=1,
            value=0,
            expand=True,
            on_change=self.on_seek,
        )

        self.list_view = ft.ListView(expand=True, spacing=8, padding=8)
        self.search_field = ft.TextField(
            hint_text="ابحث عن أغنية أو فنان...",
            prefix_icon=ft.Icons.SEARCH,
            text_align=ft.TextAlign.RIGHT,
            border_radius=14,
            on_change=self.on_search,
            dense=True,
        )

        self.status = ft.Text("", size=11, color="#2AC769")

        self.build_ui()
        self.load_songs()

    def init_db(self):
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS songs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                stored_name TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                artist TEXT NOT NULL,
                album TEXT NOT NULL,
                duration REAL DEFAULT 0,
                favorite INTEGER DEFAULT 0,
                added_at TEXT DEFAULT CURRENT_TIMESTAMP,
                last_played_at TEXT
            )
            """
        )
        self.conn.commit()

    def build_ui(self):
        self.page.title = APP_NAME
        self.page.theme_mode = ft.ThemeMode.DARK
        self.page.bgcolor = "#08090D"
        self.page.padding = 0
        self.page.spacing = 0
        self.page.rtl = True

        header = ft.Container(
            padding=ft.padding.symmetric(horizontal=16, vertical=10),
            bgcolor="#0C0D14",
            border=ft.border.only(bottom=ft.BorderSide(1, "#242632")),
            content=ft.Row(
                controls=[
                    ft.Container(
                        width=42,
                        height=42,
                        border_radius=13,
                        bgcolor="#102D1D",
                        alignment=ft.alignment.center,
                        content=ft.Icon(ft.Icons.MUSIC_NOTE, color="#20C968", size=25),
                    ),
                    ft.Column(
                        controls=[
                            ft.Text(APP_NAME, size=18, weight=ft.FontWeight.BOLD),
                            ft.Text("LOCAL MUSIC PLAYER", size=8, color="#7D818B"),
                        ],
                        spacing=0,
                    ),
                    ft.Container(expand=True),
                    ft.Button(
                        content="إضافة",
                        icon=ft.Icons.ADD,
                        on_click=self.pick_files,
                    ),
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

        hero = ft.Container(
            height=155,
            margin=ft.margin.only(left=12, right=12, top=12),
            border_radius=20,
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
            content=ft.Stack(
                controls=[
                    ft.Image(
                        src="reda_chess.jpg",
                        fit=ft.ImageFit.COVER,
                        width=float("inf"),
                        height=155,
                    ),
                    ft.Container(
                        width=float("inf"),
                        height=155,
                        bgcolor="#9906090D",
                    ),
                    ft.Container(
                        padding=18,
                        content=ft.Column(
                            controls=[
                                ft.Text(
                                    "REDA MUSIC",
                                    size=28,
                                    weight=ft.FontWeight.BOLD,
                                ),
                                ft.Text(
                                    "مكتبتك الموسيقية • بدون حساب • بدون سيرفر",
                                    size=12,
                                    color="#E5E7EB",
                                ),
                                ft.Text(
                                    "كل أغانيك محفوظة على الجهاز وتعمل بدون إنترنت.",
                                    size=11,
                                    color="#B8BCC5",
                                ),
                            ],
                            alignment=ft.MainAxisAlignment.END,
                            spacing=4,
                        ),
                    ),
                ]
            ),
        )

        filters = ft.Row(
            controls=[
                self.filter_button("الكل", "all"),
                self.filter_button("المفضلة ❤️", "favorite"),
                self.filter_button("الأخيرة 🕘", "recent"),
            ],
            alignment=ft.MainAxisAlignment.END,
            spacing=7,
        )

        library_header = ft.Container(
            padding=ft.padding.only(left=14, right=14, top=12, bottom=6),
            content=ft.Row(
                controls=[
                    ft.Column(
                        controls=[
                            ft.Text("مكتبتي", size=22, weight=ft.FontWeight.BOLD),
                            ft.Text("أغانيك محفوظة على الجهاز", size=10, color="#777C87"),
                        ],
                        spacing=0,
                    ),
                    ft.Container(expand=True),
                    self.status,
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

        self.content = ft.Column(
            controls=[
                header,
                ft.Container(
                    padding=ft.padding.symmetric(horizontal=12, vertical=8),
                    content=self.search_field,
                ),
                hero,
                library_header,
                ft.Container(
                    padding=ft.padding.symmetric(horizontal=12, vertical=4),
                    content=filters,
                ),
                self.list_view,
                self.player_bar(),
            ],
            expand=True,
            spacing=0,
        )

        self.page.add(ft.SafeArea(content=self.content, expand=True))

    def filter_button(self, label, mode):
        return ft.Button(
            content=label,
            on_click=lambda e, m=mode: self.set_filter(m),
        )

    def player_bar(self):
        return ft.Container(
            bgcolor="#0D0F1A",
            padding=ft.padding.symmetric(horizontal=14, vertical=8),
            border=ft.border.only(top=ft.BorderSide(1, "#252836")),
            content=ft.Column(
                controls=[
                    ft.Row(
                        controls=[
                            ft.Container(
                                width=45,
                                height=45,
                                border_radius=12,
                                bgcolor="#20232B",
                                alignment=ft.alignment.center,
                                content=ft.Icon(
                                    ft.Icons.MUSIC_NOTE,
                                    color="#20C968",
                                ),
                            ),
                            ft.Column(
                                controls=[self.title_text, self.artist_text],
                                spacing=0,
                                expand=True,
                            ),
                            ft.IconButton(
                                icon=ft.Icons.SKIP_PREVIOUS,
                                on_click=self.previous_song,
                            ),
                            ft.IconButton(
                                icon=ft.Icons.PLAY_ARROW,
                                selected_icon=ft.Icons.PAUSE,
                                selected=self.is_playing,
                                on_click=self.toggle_play,
                            ),
                            ft.IconButton(
                                icon=ft.Icons.SKIP_NEXT,
                                on_click=self.next_song,
                            ),
                        ],
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Row(
                        controls=[
                            self.position_text,
                            self.progress,
                            self.duration_text,
                        ],
                        spacing=6,
                    ),
                ],
                spacing=2,
            ),
        )

    async def pick_files(self, e):
        try:
            files = await self.file_picker.pick_files(
                dialog_title="اختر الأغاني",
                allow_multiple=True,
                with_data=True,
                file_type=ft.FilePickerFileType.CUSTOM,
                allowed_extensions=AUDIO_EXTENSIONS,
            )
            if not files:
                return

            added = 0
            for picked in files:
                data = picked.bytes
                if data is None and picked.path:
                    data = Path(picked.path).read_bytes()
                if not data:
                    continue

                digest = hashlib.sha1(data).hexdigest()
                ext = Path(picked.name).suffix.lower() or ".mp3"
                stored_name = f"{digest}{ext}"
                target = self.music_dir / stored_name

                if not target.exists():
                    target.write_bytes(data)

                title, artist, album, duration, _ = metadata_from_bytes(
                    data, picked.name
                )

                self.conn.execute(
                    """
                    INSERT OR IGNORE INTO songs
                    (filename, stored_name, title, artist, album, duration)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        picked.name,
                        stored_name,
                        title,
                        artist,
                        album,
                        duration,
                    ),
                )
                added += 1

            self.conn.commit()
            self.status.value = f"تم حفظ {added} أغنية على الجهاز"
            self.load_songs()
            self.page.update()
        except Exception as ex:
            self.status.value = f"تعذر إضافة الأغنية: {ex}"
            self.status.color = "#FF5B5B"
            self.page.update()

    def load_songs(self):
        order = "added_at DESC"
        where = ""
        params = []

        if self.filter_mode == "favorite":
            where = "WHERE favorite=1"
        elif self.filter_mode == "recent":
            where = "WHERE last_played_at IS NOT NULL"
            order = "last_played_at DESC"

        rows = self.conn.execute(
            f"SELECT * FROM songs {where} ORDER BY {order}"
        ).fetchall()

        if self.search_text.strip():
            q = self.search_text.strip().lower()
            rows = [
                r
                for r in rows
                if q in r["title"].lower()
                or q in r["artist"].lower()
                or q in r["album"].lower()
                or q in r["filename"].lower()
            ]

        self.songs = rows
        self.list_view.controls.clear()

        if not rows:
            self.list_view.controls.append(
                ft.Container(
                    padding=35,
                    alignment=ft.alignment.center,
                    content=ft.Column(
                        controls=[
                            ft.Icon(ft.Icons.MUSIC_OFF, size=42, color="#4B4F59"),
                            ft.Text(
                                "مفيش أغاني هنا لسه",
                                size=16,
                                weight=ft.FontWeight.BOLD,
                            ),
                            ft.Text(
                                "اضغط «إضافة» واختر ملفات MP3 أو M4A أو WAV...",
                                size=11,
                                color="#777C87",
                                text_align=ft.TextAlign.CENTER,
                            ),
                        ],
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                )
            )
        else:
            for row in rows:
                self.list_view.controls.append(self.song_card(row))

        self.page.update()

    def song_card(self, row):
        favorite_icon = ft.Icons.FAVORITE if row["favorite"] else ft.Icons.FAVORITE_BORDER
        path = self.music_dir / row["stored_name"]
        exists = path.exists()

        return ft.Container(
            padding=ft.padding.symmetric(horizontal=10, vertical=8),
            border_radius=15,
            bgcolor="#111216",
            border=ft.border.all(1, "#25272E"),
            content=ft.Row(
                controls=[
                    ft.Container(
                        width=48,
                        height=48,
                        border_radius=13,
                        bgcolor="#20242A",
                        alignment=ft.alignment.center,
                        content=ft.Icon(
                            ft.Icons.MUSIC_NOTE,
                            color="#20C968",
                            size=26,
                        ),
                    ),
                    ft.Column(
                        controls=[
                            ft.Text(
                                row["title"],
                                size=14,
                                weight=ft.FontWeight.BOLD,
                                max_lines=1,
                                overflow=ft.TextOverflow.ELLIPSIS,
                            ),
                            ft.Text(
                                f'{row["artist"]} • {format_seconds(row["duration"])}',
                                size=10,
                                color="#777C87",
                                max_lines=1,
                            ),
                            ft.Text(
                                "OFFLINE • محفوظ على الجهاز"
                                if exists
                                else "الملف غير موجود",
                                size=9,
                                color="#20C968" if exists else "#FF5B5B",
                            ),
                        ],
                        spacing=1,
                        expand=True,
                    ),
                    ft.IconButton(
                        icon=favorite_icon,
                        icon_color="#FF4F68" if row["favorite"] else "#858995",
                        on_click=lambda e, sid=row["id"]: self.toggle_favorite(sid),
                    ),
                    ft.PopupMenuButton(
                        items=[
                            ft.PopupMenuItem(
                                content=ft.Text("حذف الأغنية"),
                                on_click=lambda e, sid=row["id"]: self.delete_song(sid),
                            )
                        ],
                    ),
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            on_click=lambda e, sid=row["id"]: self.play_song(sid),
        )

    def on_search(self, e):
        self.search_text = e.control.value or ""
        self.load_songs()

    def set_filter(self, mode):
        self.filter_mode = mode
        self.load_songs()

    def get_song(self, song_id):
        return self.conn.execute(
            "SELECT * FROM songs WHERE id=?", (song_id,)
        ).fetchone()

    async def play_song(self, song_id):
        row = self.get_song(song_id)
        if not row:
            return

        path = self.music_dir / row["stored_name"]
        if not path.exists():
            self.status.value = "ملف الأغنية غير موجود على الجهاز"
            self.status.color = "#FF5B5B"
            self.page.update()
            return

        try:
            data = path.read_bytes()
            self.current_song_id = song_id
            self.current_bytes = data
            self.duration = float(row["duration"] or 0)
            self.position = 0

            # flet-audio supports raw bytes as an audio source.
            self.audio.src = data
            await self.audio.play()

            self.conn.execute(
                "UPDATE songs SET last_played_at=CURRENT_TIMESTAMP WHERE id=?",
                (song_id,),
            )
            self.conn.commit()

            self.title_text.value = row["title"]
            self.artist_text.value = row["artist"]
            self.progress.value = 0
            self.progress.max = max(self.duration, 1)
            self.duration_text.value = format_seconds(self.duration)
            self.position_text.value = "0:00"
            self.is_playing = True
            self.status.value = "يعمل بدون إنترنت"
            self.status.color = "#20C968"
            self.load_songs()
        except Exception as ex:
            self.is_playing = False
            self.status.value = f"تعذر تشغيل الأغنية: {ex}"
            self.status.color = "#FF5B5B"
            self.page.update()

    async def toggle_play(self, e=None):
        if not self.current_song_id:
            if self.songs:
                await self.play_song(self.songs[0]["id"])
            return

        try:
            if self.is_playing:
                await self.audio.pause()
                self.is_playing = False
            else:
                await self.audio.resume()
                self.is_playing = True
            self.page.update()
        except Exception as ex:
            self.status.value = f"تعذر التحكم في التشغيل: {ex}"
            self.status.color = "#FF5B5B"
            self.page.update()

    async def next_song(self, e=None):
        if not self.songs:
            return
        ids = [r["id"] for r in self.songs]
        if self.current_song_id in ids:
            idx = ids.index(self.current_song_id)
            idx = (idx + 1) % len(ids)
        else:
            idx = 0
        await self.play_song(ids[idx])

    async def previous_song(self, e=None):
        if not self.songs:
            return
        ids = [r["id"] for r in self.songs]
        if self.current_song_id in ids:
            idx = ids.index(self.current_song_id)
            idx = (idx - 1) % len(ids)
        else:
            idx = 0
        await self.play_song(ids[idx])

    async def on_seek(self, e):
        if not self.current_song_id or self.duration <= 0:
            return
        try:
            self.position = float(e.control.value)
            await self.audio.seek(ft.Duration(seconds=self.position))
        except Exception:
            pass

    async def on_position_change(self, e):
        try:
            pos = e.position.total_seconds()
        except Exception:
            try:
                pos = float(e.position) / 1000
            except Exception:
                return

        self.position = max(0, pos)
        self.progress.value = self.position
        self.position_text.value = format_seconds(self.position)
        self.page.update()

    async def on_duration_change(self, e):
        try:
            duration = e.duration.total_seconds()
        except Exception:
            try:
                duration = float(e.duration) / 1000
            except Exception:
                return

        self.duration = max(0, duration)
        self.progress.max = max(self.duration, 1)
        self.duration_text.value = format_seconds(self.duration)
        self.page.update()

    async def on_state_change(self, e):
        try:
            if e.state == fta.AudioState.COMPLETED:
                self.is_playing = False
                await self.next_song()
            elif e.state == fta.AudioState.PLAYING:
                self.is_playing = True
            elif e.state in (
                fta.AudioState.PAUSED,
                fta.AudioState.STOPPED,
            ):
                self.is_playing = False
            self.page.update()
        except Exception:
            pass

    def toggle_favorite(self, song_id):
        self.conn.execute(
            "UPDATE songs SET favorite=CASE favorite WHEN 1 THEN 0 ELSE 1 END WHERE id=?",
            (song_id,),
        )
        self.conn.commit()
        self.load_songs()

    async def delete_song(self, song_id):
        row = self.get_song(song_id)
        if not row:
            return

        if self.current_song_id == song_id:
            try:
                await self.audio.pause()
                await self.audio.release()
            except Exception:
                pass
            self.current_song_id = None
            self.current_bytes = None
            self.is_playing = False
            self.title_text.value = "لا توجد أغنية"
            self.artist_text.value = "اختر أغنية للتشغيل"

        path = self.music_dir / row["stored_name"]
        try:
            if path.exists():
                path.unlink()
        except Exception:
            pass

        self.conn.execute("DELETE FROM songs WHERE id=?", (song_id,))
        self.conn.commit()
        self.status.value = "تم حذف الأغنية من الجهاز"
        self.status.color = "#20C968"
        self.load_songs()

    async def close(self):
        try:
            await self.audio.release()
        except Exception:
            pass
        self.conn.close()


async def main(page: ft.Page):
    app = LocalMusicApp(page)


if __name__ == "__main__":
    ft.run(main, assets_dir="assets")
