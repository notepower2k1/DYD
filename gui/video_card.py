import io
import threading
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk
from typing import Callable, Optional

import requests
from PIL import Image, ImageDraw, ImageTk

from models.video import Video
from utils.formatter import format_count, format_duration
from utils.trend import get_trend_level, get_video_trend_score


class VideoCard(ttk.Frame):
    _THUMB_MIN_W = 300
    _THUMB_MIN_H = 520
    _CORNER_RADIUS = 14
    _THUMB_BG = "#121826"

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
        self._retry_job: str | None = None
        self._apply_job: str | None = None

        container = ttk.Frame(self, style="CardInner.TFrame", padding=8)
        container.pack(fill=tk.BOTH, expand=True)

        thumb_frame = tk.Frame(
            container,
            bg=self._THUMB_BG,
            bd=0,
            highlightthickness=1,
            highlightbackground="#2b3345",
        )
        # Keep a stable preview region so the card does not collapse before image load.
        thumb_frame.configure(width=self._THUMB_MIN_W, height=self._THUMB_MIN_H)
        thumb_frame.pack_propagate(False)
        thumb_frame.pack(fill=tk.BOTH, expand=True)
        self._thumb_frame = thumb_frame

        self._thumb_label = tk.Label(thumb_frame, cursor="hand2", bg=self._THUMB_BG, bd=0, highlightthickness=0)
        self._thumb_label.pack(fill=tk.BOTH, expand=True)
        self._thumb_frame.bind("<Configure>", self._on_thumb_configure, add="+")
        self._thumb_label.bind("<Configure>", self._on_thumb_configure, add="+")

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

        left_icon = "♥" if (video.platform or "").lower() == "douyin" else "▶"
        left_value = video.like_count if (video.platform or "").lower() == "douyin" else video.view_count

        left_stats = tk.Label(
            stats_bar,
            text=f"{left_icon} {self._format_optional_count(left_value)}",
            bg="#0b1020",
            fg="#d7def0",
            padx=8,
            pady=4,
            font=("Segoe UI", 9, "bold"),
        )
        left_stats.pack(side=tk.LEFT)

        right_parts = []
        duration_text = format_duration(video.duration)
        if duration_text:
            right_parts.append(duration_text)
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

        self.bind("<Destroy>", self._on_destroy, add="+")

        # Thumbnail is lazy-loaded when card enters viewport.

    def _is_widget_alive(self) -> bool:
        try:
            return bool(self.winfo_exists() and self._thumb_frame.winfo_exists() and self._thumb_label.winfo_exists())
        except Exception:
            return False

    def _cancel_job(self, job_name: str) -> None:
        job_id = getattr(self, job_name, None)
        if not job_id:
            return
        try:
            self.after_cancel(job_id)
        except Exception:
            pass
        setattr(self, job_name, None)

    def _on_destroy(self, _event: tk.Event) -> None:  # type: ignore[override]
        self._cancel_job("_resize_job")
        self._cancel_job("_retry_job")
        self._cancel_job("_apply_job")

    def _on_thumb_configure(self, _event: tk.Event) -> None:  # type: ignore[override]
        if not self._is_widget_alive() or self._source_image is None:
            return
        self._cancel_job("_resize_job")
        self._resize_job = self.after(50, self._render_thumbnail)

    def _render_thumbnail(self) -> None:
        self._resize_job = None
        if self._source_image is None:
            return
        if not self._is_widget_alive():
            return

        w = max(self._thumb_frame.winfo_width(), self._THUMB_MIN_W)
        h = max(self._thumb_frame.winfo_height(), self._THUMB_MIN_H)

        # Skip placeholder size and retry shortly while layout settles.
        if w < 30 or h < 30:
            if self._render_retry < 10:
                self._render_retry += 1
                self._cancel_job("_retry_job")
                self._retry_job = self.after(60, self._render_thumbnail)
            return
        self._cancel_job("_retry_job")
        self._render_retry = 0
        old_w, old_h = self._last_size
        if old_w and old_h and abs(old_w - w) < 4 and abs(old_h - h) < 4:
            return

        self._last_size = (w, h)
        img = self._source_image.resize((w, h), Image.LANCZOS).convert("RGB")
        mask = Image.new("L", (w, h), 0)
        draw = ImageDraw.Draw(mask)
        draw.rounded_rectangle((0, 0, w - 1, h - 1), radius=self._CORNER_RADIUS, fill=255)
        composed = Image.new("RGB", (w, h), self._THUMB_BG)
        composed.paste(img, (0, 0), mask)
        self._thumb_image = ImageTk.PhotoImage(composed)
        self._thumb_label.configure(image=self._thumb_image)
        self._thumb_label.image = self._thumb_image

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
                self._apply_job = None
                if not self._is_widget_alive():
                    return
                self._source_image = img
                self._last_size = (0, 0)
                self._render_retry = 0
                self._render_thumbnail()

            if not self._is_widget_alive():
                return
            self._apply_job = self.after(0, _apply)

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

        is_douyin = (self.video.platform or "").lower() == "douyin"
        metric_lines = (
            f"Likes: {self._format_optional_count(self.video.like_count)}\n"
            f"Comments: {self._format_optional_count(self.video.comment_count)}\n"
            f"Shares: {self._format_optional_count(self.video.share_count)}\n"
        )
        if not is_douyin:
            metric_lines = (
                f"Views: {self._format_optional_count(self.video.view_count)}\n"
                + metric_lines
            )
        formula_text = (
            "0.45 * Interaction Velocity + 0.2 * Share Mix + 0.2 * Comment Mix + 0.15 * Like Mix"
            if is_douyin
            else "0.4 * Velocity + 0.25 * Engagement + 0.2 * Share Rate + 0.15 * Comment Rate"
        )

        messagebox.showinfo(
            "Trend Score",
            (
                "Formula:\n"
                f"{formula_text}\n\n"
                f"{metric_lines}"
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

    @staticmethod
    def _format_optional_count(value: int | None) -> str:
        if value is None:
            return "-"
        return format_count(value)
