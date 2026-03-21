import io
import threading
import tkinter as tk
from datetime import datetime
from tkinter import ttk
from typing import Callable, Dict, Iterable, List, Optional, Set

import requests
from PIL import Image, ImageTk

from models.video import Video
from utils.formatter import format_count
from utils.trend import get_trend_level, get_video_trend_score


class _VideoTableRow(ttk.Frame):
    _PREVIEW_W = 56
    _PREVIEW_H = 100
    _PREVIEW_BG = "#121826"
    _PREVIEW_BORDER = "#2b3345"

    def __init__(
        self,
        master: tk.Misc,
        video: Video,
        checked: bool,
        trend_threshold: float,
        on_toggle: Callable[[Video, bool], None],
        on_bookmark: Optional[Callable[[Video, bool], None]],
        on_open: Optional[Callable[[Video], None]],
        on_quick_download: Optional[Callable[[Video], None]],
        **kwargs,
    ) -> None:
        super().__init__(master, style="Panel.TFrame", padding=(6, 4), **kwargs)
        self.video = video
        self._on_toggle = on_toggle
        self._on_bookmark = on_bookmark
        self._on_open = on_open
        self._on_quick_download = on_quick_download
        self._thumb_image: ImageTk.PhotoImage | None = None

        self.columnconfigure(1, minsize=72)
        self.columnconfigure(2, weight=1, minsize=120)
        self.columnconfigure(3, minsize=70)
        self.columnconfigure(4, minsize=98)
        self.columnconfigure(5, minsize=92)
        self.columnconfigure(6, minsize=86)
        self.columnconfigure(7, minsize=120)

        self._checked = tk.BooleanVar(value=checked)
        ttk.Checkbutton(self, variable=self._checked, command=self._handle_toggle).grid(
            row=0, column=0, padx=(0, 8), sticky="w"
        )

        preview_frame = tk.Frame(
            self,
            bg=self._PREVIEW_BG,
            bd=0,
            highlightthickness=1,
            highlightbackground=self._PREVIEW_BORDER,
            width=self._PREVIEW_W,
            height=self._PREVIEW_H,
        )
        preview_frame.grid(row=0, column=1, padx=(0, 10), sticky="w")
        preview_frame.grid_propagate(False)

        preview_holder = tk.Label(
            preview_frame,
            bg=self._PREVIEW_BG,
            bd=0,
            highlightthickness=0,
            cursor="hand2",
        )
        preview_holder.place(relx=0.0, rely=0.0, relwidth=1.0, relheight=1.0)
        preview_holder.bind("<Button-1>", self._handle_open)
        preview_frame.bind("<Button-1>", self._handle_open)
        self._preview_frame = preview_frame
        self._preview_holder = preview_holder

        platform = (video.platform or "").lower()
        metric_value = video.like_count if platform in {"douyin", "xhs"} else video.view_count
        ttk.Label(
            self,
            text=self._format_metric(metric_value),
            style="Muted.TLabel",
            anchor="w",
            justify=tk.LEFT,
        ).grid(row=0, column=2, padx=(0, 10), sticky="w")

        media_type = "Image" if video.image_urls and not video.media_url else "Video"
        ttk.Label(
            self,
            text=media_type,
            style="Muted.TLabel",
            anchor="center",
        ).grid(row=0, column=3, padx=(0, 10), sticky="w")

        ttk.Label(
            self,
            text=video.upload_time.strftime("%d-%m-%Y") if video.upload_time else "-",
            style="Muted.TLabel",
            anchor="w",
        ).grid(row=0, column=4, padx=(0, 10), sticky="w")

        download_text = "Downloaded" if video.is_downloaded else "Download"
        download_state = "disabled" if video.is_downloaded else "normal"
        ttk.Button(
            self,
            text=download_text,
            command=self._handle_quick_download,
            style="Secondary.TButton",
            state=download_state,
        ).grid(row=0, column=5, padx=(0, 10), sticky="w")

        self._bookmarked = tk.BooleanVar(value=bool(video.is_bookmarked))
        bookmark_btn = ttk.Button(
            self,
            text="★" if self._bookmarked.get() else "☆",
            command=self._handle_bookmark,
            style="Secondary.TButton",
            width=3,
        )
        bookmark_btn.grid(row=0, column=6, padx=(0, 10), sticky="w")
        self._bookmark_btn = bookmark_btn

        trend_text, trend_bg, trend_fg = self._trend_style(video, trend_threshold)
        trend_label = tk.Label(
            self,
            text=trend_text,
            bg=trend_bg,
            fg=trend_fg,
            padx=8,
            pady=2,
            anchor="w",
            font=("Segoe UI", 8, "bold"),
            bd=0,
            highlightthickness=0,
        )
        trend_label.grid(row=0, column=7, sticky="w")

        separator = ttk.Separator(self, orient="horizontal")
        separator.grid(row=1, column=0, columnspan=8, sticky="ew", pady=(6, 0))

        self._load_thumbnail_async()

    def _handle_toggle(self) -> None:
        self._on_toggle(self.video, self._checked.get())

    def set_checked(self, checked: bool) -> None:
        self._checked.set(checked)

    def _handle_open(self, _event: tk.Event) -> None:  # type: ignore[override]
        if self._on_open:
            self._on_open(self.video)

    def _handle_quick_download(self) -> None:
        if self._on_quick_download:
            self._on_quick_download(self.video)

    def _handle_bookmark(self) -> None:
        current = not self._bookmarked.get()
        self._bookmarked.set(current)
        try:
            self._bookmark_btn.configure(text="★" if current else "☆")
        except Exception:
            pass
        self.video.is_bookmarked = current
        if self._on_bookmark:
            self._on_bookmark(self.video, current)

    def _load_thumbnail_async(self) -> None:
        if not self.video.thumbnail_url:
            return

        def _load() -> None:
            try:
                response = requests.get(self.video.thumbnail_url, timeout=10)
                response.raise_for_status()
                image = Image.open(io.BytesIO(response.content)).convert("RGB")
                image = self._prepare_preview(image)
                photo = ImageTk.PhotoImage(image)
            except Exception:
                return

            def _apply() -> None:
                if not self.winfo_exists():
                    return
                self._thumb_image = photo
                self._preview_holder.after(45, lambda: self._apply_preview_image(photo))

            self.after(0, _apply)

        threading.Thread(target=_load, daemon=True).start()

    def _apply_preview_image(self, photo: ImageTk.PhotoImage) -> None:
        if not self.winfo_exists():
            return
        self._thumb_image = photo
        self._preview_holder.configure(image=photo, text="")

    @classmethod
    def _prepare_preview(cls, image: Image.Image) -> Image.Image:
        target_ratio = cls._PREVIEW_W / cls._PREVIEW_H
        width, height = image.size
        current_ratio = width / height if height else target_ratio

        if current_ratio > target_ratio:
            new_width = int(height * target_ratio)
            left = max((width - new_width) // 2, 0)
            image = image.crop((left, 0, left + new_width, height))
        elif current_ratio < target_ratio:
            new_height = int(width / target_ratio)
            top = max((height - new_height) // 2, 0)
            image = image.crop((0, top, width, top + new_height))

        return image.resize((cls._PREVIEW_W, cls._PREVIEW_H), Image.LANCZOS)

    @staticmethod
    def _format_metric(value: int | None) -> str:
        if value is None:
            return "-"
        return format_count(value)

    @staticmethod
    def _trend_style(video: Video, trend_threshold: float) -> tuple[str, str, str]:
        score = get_video_trend_score(video, now=datetime.now())
        if score is None or score < trend_threshold:
            return "-", "#ffffff", "#6b7a99"

        level = get_trend_level(score)
        palette = {
            "Potential": ("POTENTIAL", "#0e5f59", "#d6fff4"),
            "Trending": ("TRENDING", "#8a5a00", "#ffe8bf"),
            "Viral": ("VIRAL", "#6f1d1b", "#ffe0dc"),
            "Normal": ("NORMAL", "#294d8f", "#dce8ff"),
        }
        return palette.get(level, (level.upper(), "#294d8f", "#dce8ff"))


class VideoGrid(ttk.Frame):
    def __init__(
        self,
        master: tk.Misc,
        on_selection_change: Callable[[List[Video]], None],
        on_open_video: Optional[Callable[[Video], None]] = None,
        on_load_more: Optional[Callable[[], None]] = None,
        on_quick_download: Optional[Callable[[Video], None]] = None,
        on_bookmark: Optional[Callable[[Video, bool], None]] = None,
        columns: int = 3,
        trend_threshold: float = 0.8,
        **kwargs,
    ) -> None:
        del columns, on_load_more
        super().__init__(master, **kwargs)
        self._on_selection_change = on_selection_change
        self._on_open_video = on_open_video
        self._on_quick_download = on_quick_download
        self._on_bookmark = on_bookmark
        self._trend_threshold = trend_threshold
        self._selected_ids: Set[str] = set()
        self._rows: Dict[str, _VideoTableRow] = {}
        self._videos: Dict[str, Video] = {}
        self._ordered_ids: List[str] = []
        self._skeleton_widgets: List[tk.Widget] = []
        self._tail_skeleton_widgets: List[tk.Widget] = []
        self._loading_bar: ttk.Progressbar | None = None

        self.columnconfigure(1, minsize=72)
        self.columnconfigure(2, weight=1, minsize=120)
        self.columnconfigure(3, minsize=70)
        self.columnconfigure(4, minsize=98)
        self.columnconfigure(5, minsize=92)
        self.columnconfigure(6, minsize=86)
        self.columnconfigure(7, minsize=120)

    @staticmethod
    def _dedupe_videos(videos: Iterable[Video]) -> List[Video]:
        ordered: List[Video] = []
        seen: Set[str] = set()
        for video in videos:
            if not video:
                continue
            key = (video.id or video.url or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            ordered.append(video)
        return ordered

    def _clear_all(self) -> None:
        if self._loading_bar is not None:
            try:
                self._loading_bar.stop()
            except Exception:
                pass
            self._loading_bar = None
        for child in self.winfo_children():
            child.destroy()
        self._rows.clear()
        self._videos.clear()
        self._ordered_ids = []
        self._clear_skeletons()
        self._clear_tail_skeleton()

    def _clear_skeletons(self) -> None:
        for widget in self._skeleton_widgets:
            widget.destroy()
        self._skeleton_widgets = []

    def _clear_tail_skeleton(self) -> None:
        for widget in self._tail_skeleton_widgets:
            widget.destroy()
        self._tail_skeleton_widgets = []

    def clear_tail_skeleton(self) -> None:
        self._clear_tail_skeleton()

    def show_placeholder(self, message: str) -> None:
        self._clear_all()
        empty = ttk.Label(self, text=message, style="Muted.TLabel")
        empty.grid(row=0, column=0, columnspan=8, sticky="ew", padx=10, pady=20)

    def show_loading(self, message: str = "Loading...") -> None:
        self._clear_all()
        holder = ttk.Frame(self, style="Panel.TFrame", padding=(10, 18))
        holder.grid(row=0, column=0, columnspan=8, sticky="ew")
        ttk.Label(holder, text=message, style="Muted.TLabel").pack(anchor="center", pady=(0, 8))
        bar = ttk.Progressbar(holder, mode="indeterminate", length=260)
        bar.pack(anchor="center")
        bar.start(10)
        self._loading_bar = bar

    def show_skeleton(self, count: int = 8) -> None:
        del count
        self.show_loading("Loading videos...")

    def show_tail_skeleton(self, count: int = 4) -> None:
        del count
        self._clear_tail_skeleton()
        if self._loading_bar is None:
            footer = ttk.Frame(self, style="Panel.TFrame", padding=(10, 8))
            footer.grid(row=max(len(self._ordered_ids) + 1, 1), column=0, columnspan=8, sticky="ew")
            ttk.Label(footer, text="Loading more videos...", style="Muted.TLabel").pack(anchor="center", pady=(0, 6))
            bar = ttk.Progressbar(footer, mode="indeterminate", length=220)
            bar.pack(anchor="center")
            bar.start(10)
            self._tail_skeleton_widgets.append(footer)

    def set_videos(self, videos: Iterable[Video], has_more: bool = False) -> None:
        del has_more
        video_list = self._dedupe_videos(videos)
        valid_ids = {(video.id or video.url or "").strip() for video in video_list}
        self._selected_ids.intersection_update(valid_ids)
        self._clear_all()
        self._build_header()

        if not video_list:
            empty = ttk.Label(self, text="No videos to display yet.", style="Muted.TLabel")
            empty.grid(row=1, column=0, columnspan=8, sticky="ew", padx=10, pady=20)
            self._fire_selection_changed()
            return

        self._append_rows(video_list)
        self._fire_selection_changed()

    def append_videos(self, videos: Iterable[Video], has_more: bool = False) -> None:
        del has_more
        deduped = self._dedupe_videos(videos)
        new_items = []
        for video in deduped:
            key = (video.id or video.url or "").strip()
            if not key or key in self._rows:
                continue
            new_items.append(video)
        self._clear_tail_skeleton()
        if new_items:
            self._append_rows(new_items)
        self._fire_selection_changed()

    def _build_header(self) -> None:
        headers = ("", "Preview", "Views / Likes", "Type", "Upload Date", "Quick Download", "Bookmark", "Trending")
        for col, text in enumerate(headers):
            ttk.Label(
                self,
                text=text,
                background="#ffffff",
                foreground="#1d2a44",
                font=("Segoe UI", 9, "bold"),
                anchor="w",
            ).grid(row=0, column=col, padx=(6 if col == 0 else 0, 10), pady=(4, 8), sticky="w")

    def _append_rows(self, videos: List[Video]) -> None:
        self._clear_skeletons()
        for video in videos:
            key = (video.id or video.url or "").strip()
            if not key:
                continue
            checked = key in self._selected_ids
            row_index = len(self._ordered_ids) + 1
            row = _VideoTableRow(
                self,
                video=video,
                checked=checked,
                trend_threshold=self._trend_threshold,
                on_toggle=self._handle_toggle,
                on_bookmark=self._on_bookmark,
                on_open=self._on_open_video,
                on_quick_download=self._on_quick_download,
            )
            row.grid(row=row_index, column=0, columnspan=8, sticky="ew")
            self._rows[key] = row
            self._videos[key] = video
            self._ordered_ids.append(key)

    def update_visible_range(self, viewport_top: int, viewport_bottom: int) -> None:
        del viewport_top, viewport_bottom

    def _handle_toggle(self, video: Video, checked: bool) -> None:
        key = (video.id or video.url or "").strip()
        if not key:
            return
        if checked:
            self._selected_ids.add(key)
        else:
            self._selected_ids.discard(key)
        self._fire_selection_changed()

    def get_selected(self) -> List[Video]:
        return [self._videos[key] for key in self._ordered_ids if key in self._selected_ids and key in self._videos]

    def _fire_selection_changed(self) -> None:
        self._on_selection_change(self.get_selected())

    def set_trend_threshold(self, trend_threshold: float) -> None:
        self._trend_threshold = trend_threshold

    def select_all(self) -> None:
        for key, row in self._rows.items():
            self._selected_ids.add(key)
            row.set_checked(True)
        self._fire_selection_changed()

    def clear_selection(self) -> None:
        self._selected_ids.clear()
        for row in self._rows.values():
            row.set_checked(False)
        self._fire_selection_changed()
