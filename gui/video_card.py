import io
import threading
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk
from typing import Callable, Optional

import requests
from PIL import Image, ImageDraw, ImageTk

from models.video import Video
from utils.formatter import format_count
from utils.trend import get_trend_level, get_video_trend_score


class VideoCard(ttk.Frame):
    _THUMB_MIN_W = 300
    _THUMB_MIN_H = 520
    _CORNER_RADIUS = 14

    def __init__(
        self,
        master: tk.Misc,
        video: Video,
        checked: bool = True,
        on_toggle: Optional[Callable[[Video, bool], None]] = None,
        on_open: Optional[Callable[[Video], None]] = None,
        trend_threshold: float = 0.8,
        **kwargs,
    ) -> None:
        super().__init__(master, padding=0, style="Card.TFrame", **kwargs)
        self.video = video
        self._on_toggle = on_toggle
        self._on_open = on_open
        self._trend_threshold = trend_threshold

        self._thumb_image: Optional[ImageTk.PhotoImage] = None
        self._source_image: Optional[Image.Image] = None
        self._last_size: tuple[int, int] = (0, 0)
        self._render_retry = 0
        self._thumbnail_started = False
        self._resize_job: str | None = None

        container = ttk.Frame(self, style="CardInner.TFrame", padding=8)
        container.pack(fill=tk.BOTH, expand=True)

        thumb_frame = tk.Frame(container, bg="#121826", bd=0, highlightthickness=1, highlightbackground="#2b3345")
        # Keep a stable preview region so the card does not collapse before image load.
        thumb_frame.configure(width=self._THUMB_MIN_W, height=self._THUMB_MIN_H)
        thumb_frame.pack_propagate(False)
        thumb_frame.pack(fill=tk.BOTH, expand=True)
        self._thumb_frame = thumb_frame

        self._thumb_label = ttk.Label(thumb_frame, cursor="hand2", style="VideoThumb.TLabel")
        self._thumb_label.pack(fill=tk.BOTH, expand=True)
        self._thumb_frame.bind("<Configure>", self._on_thumb_configure)

        self._checked = tk.BooleanVar(value=checked)
        check = ttk.Checkbutton(thumb_frame, text="Select", variable=self._checked, command=self._handle_toggle)
        check.place(relx=0.02, rely=0.04, anchor="nw")

        if self.video.is_downloaded:
            downloaded_badge = tk.Label(
                thumb_frame,
                text="Downloaded",
                bg="#12331f",
                fg="#9ff0b8",
                padx=8,
                pady=2,
                font=("Segoe UI", 8, "bold"),
            )
            downloaded_badge.place(relx=0.98, rely=0.04, anchor="ne")

        if self._is_new_video():
            new_badge = tk.Label(
                thumb_frame,
                text="NEW",
                bg="#7b1f4a",
                fg="#ffe3ee",
                padx=8,
                pady=2,
                font=("Segoe UI", 8, "bold"),
            )
            new_badge.place(relx=0.98, rely=0.12, anchor="ne")

        self._build_trend_badge(thumb_frame)

        stats_bar = tk.Frame(thumb_frame, bg="#0b1020")
        stats_bar.place(relx=0.0, rely=1.0, anchor="sw", relwidth=1.0)

        left_stats = tk.Label(
            stats_bar,
            text=f"▶ {format_count(video.view_count)}" if video.view_count is not None else "▶ -",
            bg="#0b1020",
            fg="#d7def0",
            padx=8,
            pady=4,
            font=("Segoe UI", 9, "bold"),
        )
        left_stats.pack(side=tk.LEFT)

        right_parts = []
        if video.upload_time:
            right_parts.append(video.upload_time.strftime("%d-%m-%Y"))

        right_stats = tk.Label(
            stats_bar,
            text=" | ".join(right_parts) if right_parts else "",
            bg="#0b1020",
            fg="#9fb0d3",
            padx=6,
            pady=4,
            font=("Segoe UI", 8, "bold"),
        )
        right_stats.pack(side=tk.RIGHT)

        for widget in (self, container, thumb_frame, self._thumb_label, left_stats, right_stats):
            widget.bind("<Button-1>", self._on_preview_clicked)

        # Thumbnail is lazy-loaded when card enters viewport.

    def _on_thumb_configure(self, _event: tk.Event) -> None:  # type: ignore[override]
        if self._resize_job:
            self.after_cancel(self._resize_job)
        # Debounce resize events to avoid re-render noise while scrolling.
        self._resize_job = self.after(50, self._render_thumbnail)

    def _render_thumbnail(self) -> None:
        if self._source_image is None:
            return

        w = max(self._THUMB_MIN_W, self._thumb_frame.winfo_width())
        h = max(self._THUMB_MIN_H, self._thumb_frame.winfo_height())

        # Skip placeholder size and retry shortly while layout settles.
        if w < 30 or h < 30:
            if self._render_retry < 10:
                self._render_retry += 1
                self.after(60, self._render_thumbnail)
            return
        self._render_retry = 0
        old_w, old_h = self._last_size
        if old_w and old_h and abs(old_w - w) < 4 and abs(old_h - h) < 4:
            return

        self._last_size = (w, h)
        img = self._source_image.resize((w, h), Image.LANCZOS).convert("RGBA")
        mask = Image.new("L", (w, h), 0)
        draw = ImageDraw.Draw(mask)
        draw.rounded_rectangle((0, 0, w - 1, h - 1), radius=self._CORNER_RADIUS, fill=255)
        img.putalpha(mask)
        self._thumb_image = ImageTk.PhotoImage(img)
        self._thumb_label.configure(image=self._thumb_image)

    def ensure_thumbnail_loaded(self) -> None:
        if self._thumbnail_started:
            return
        self._thumbnail_started = True
        self._load_thumbnail_async()

    def _load_thumbnail_async(self) -> None:
        if not self.video.thumbnail_url:
            return

        def _load() -> None:
            try:
                resp = requests.get(self.video.thumbnail_url, timeout=10)
                resp.raise_for_status()
                img = Image.open(io.BytesIO(resp.content)).convert("RGB")

                # Crop to 9:16 before dynamic resize to fill the card area.
                w, h = img.size
                target_ratio = 9 / 16
                current_ratio = w / h if h else target_ratio
                if current_ratio > target_ratio:
                    new_w = int(h * target_ratio)
                    left = (w - new_w) // 2
                    img = img.crop((left, 0, left + new_w, h))
                elif current_ratio < target_ratio:
                    new_h = int(w / target_ratio)
                    top = (h - new_h) // 2
                    img = img.crop((0, top, w, top + new_h))
            except Exception:
                return

            def _apply() -> None:
                self._source_image = img
                self._last_size = (0, 0)
                self._render_retry = 0
                self._render_thumbnail()

            self.after(0, _apply)

        threading.Thread(target=_load, daemon=True).start()

    def _is_new_video(self) -> bool:
        if not self.video.upload_time:
            return False
        return self.video.upload_time.date() == datetime.now().date()

    def _build_trend_badge(self, thumb_frame: tk.Frame) -> None:
        score = get_video_trend_score(self.video)
        if score is None or score < self._trend_threshold:
            return

        level = get_trend_level(score)
        palette = {
            "Viral": ("#6f1d1b", "#ffe0dc"),
            "Trending": ("#8a5a00", "#ffe8bf"),
            "Potential": ("#0e5f59", "#d6fff4"),
            "Normal": ("#294d8f", "#dce8ff"),
        }
        bg, fg = palette.get(level, ("#294d8f", "#dce8ff"))

        badge = tk.Label(
            thumb_frame,
            text=level.upper(),
            bg=bg,
            fg=fg,
            padx=8,
            pady=2,
            cursor="hand2",
            font=("Segoe UI", 8, "bold"),
        )
        badge.place(relx=0.98, rely=0.20, anchor="ne")
        badge.bind("<Button-1>", self._show_trend_explanation)

    def _show_trend_explanation(self, _event: tk.Event) -> str:
        score = get_video_trend_score(self.video)
        if score is None:
            messagebox.showinfo("Trend Score", "Not enough data to calculate a trend score for this video.")
            return "break"

        hours = 1.0
        if self.video.upload_time:
            hours = max((datetime.now() - self.video.upload_time).total_seconds() / 3600.0, 1.0)

        messagebox.showinfo(
            "Trend Score",
            (
                "Formula:\n"
                "0.4 * Velocity + 0.25 * Engagement + 0.2 * Share Rate + 0.15 * Comment Rate\n\n"
                f"Views: {format_count(self.video.view_count) or '-'}\n"
                f"Likes: {format_count(self.video.like_count) or '-'}\n"
                f"Comments: {format_count(self.video.comment_count) or '-'}\n"
                f"Shares: {format_count(self.video.share_count) or '-'}\n"
                f"Hours: {hours:.1f}\n\n"
                f"Score: {score:.2f}\n"
                f"Level: {get_trend_level(score)}\n"
                f"Threshold: {self._trend_threshold:.2f}\n\n"
                "Guide:\n"
                "< 0.4: Cold\n"
                "0.4 - 0.6: Normal\n"
                "0.6 - 0.8: Potential\n"
                "> 0.8: Trending\n"
                "> 0.9: Viral"
            ),
        )
        return "break"

    def _on_preview_clicked(self, _event: tk.Event) -> None:  # type: ignore[override]
        if self._on_open:
            self._on_open(self.video)

    def _handle_toggle(self) -> None:
        if self._on_toggle:
            self._on_toggle(self.video, self._checked.get())
