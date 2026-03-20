import tkinter as tk
from tkinter import ttk
from typing import Callable, Dict, Iterable, List, Optional, Set

from models.video import Video

from .video_card import VideoCard


class VideoGrid(ttk.Frame):
    def __init__(
        self,
        master: tk.Misc,
        on_selection_change: Callable[[List[Video]], None],
        on_open_video: Optional[Callable[[Video], None]] = None,
        on_load_more: Optional[Callable[[], None]] = None,
        columns: int = 3,
        trend_threshold: float = 0.8,
        **kwargs,
    ) -> None:
        super().__init__(master, **kwargs)
        self._on_selection_change = on_selection_change
        self._on_open_video = on_open_video
        self._on_load_more = on_load_more
        self._trend_threshold = trend_threshold
        self._cards: Dict[str, VideoCard] = {}
        self._selected_ids: Set[str] = set()
        self._columns = max(1, columns)
        self._rendered_count = 0

        self._last_viewport_top = 0
        self._last_viewport_bottom = 0

        self._skeleton_widgets: List[tk.Widget] = []
        self._tail_skeleton_widgets: List[tk.Widget] = []

        self._col_min_size = VideoCard._THUMB_MIN_W + 22
        for col in range(self._columns):
            # Let columns expand to fill horizontal space; cards stay fixed-size inside each column.
            self.columnconfigure(col, weight=1, minsize=self._col_min_size)

    def _clear_all(self) -> None:
        for child in self.winfo_children():
            child.destroy()
        self._cards.clear()
        self._rendered_count = 0
        self._clear_skeletons()
        self._clear_tail_skeleton()

    def _clear_skeletons(self) -> None:
        for w in self._skeleton_widgets:
            w.destroy()
        self._skeleton_widgets = []

    def _clear_tail_skeleton(self) -> None:
        for w in self._tail_skeleton_widgets:
            w.destroy()
        self._tail_skeleton_widgets = []

    def clear_tail_skeleton(self) -> None:
        self._clear_tail_skeleton()

    def show_skeleton(self, count: int = 8) -> None:
        self._clear_all()

        for idx in range(max(1, count)):
            row = idx // self._columns
            col = idx % self._columns

            card = ttk.Frame(self, style="Card.TFrame", padding=8)
            card.grid(row=row, column=col, sticky="n", padx=4, pady=6)

            block = tk.Frame(card, bg="#e4ebf7", width=VideoCard._THUMB_MIN_W, height=VideoCard._THUMB_MIN_H)
            block.pack(fill=tk.BOTH, expand=True)
            block.pack_propagate(False)

            top_row = tk.Frame(block, bg="#e4ebf7")
            top_row.pack(side=tk.TOP, fill=tk.X, padx=10, pady=10)

            check_stub = tk.Frame(top_row, bg="#cfdcf4", width=70, height=22)
            check_stub.pack(side=tk.LEFT)
            check_stub.pack_propagate(False)

            badge_stub = tk.Frame(top_row, bg="#d8e2f6", width=54, height=20)
            badge_stub.pack(side=tk.RIGHT)
            badge_stub.pack_propagate(False)

            bar = tk.Frame(block, bg="#d3dff4", height=24)
            bar.pack(side=tk.BOTTOM, fill=tk.X)

            left_stub = tk.Frame(bar, bg="#bfcfea", width=88, height=10)
            left_stub.pack(side=tk.LEFT, padx=8, pady=7)
            left_stub.pack_propagate(False)

            right_stub = tk.Frame(bar, bg="#c8d6ee", width=74, height=10)
            right_stub.pack(side=tk.RIGHT, padx=8, pady=7)
            right_stub.pack_propagate(False)

            self._skeleton_widgets.append(card)

    def show_tail_skeleton(self, count: int = 4) -> None:
        self._clear_tail_skeleton()
        start_idx = self._rendered_count
        for idx in range(max(1, count)):
            absolute_idx = start_idx + idx
            row = absolute_idx // self._columns
            col = absolute_idx % self._columns

            card = ttk.Frame(self, style="Card.TFrame", padding=8)
            card.grid(row=row, column=col, sticky="n", padx=4, pady=6)

            block = tk.Frame(card, bg="#e4ebf7", width=VideoCard._THUMB_MIN_W, height=VideoCard._THUMB_MIN_H)
            block.pack(fill=tk.BOTH, expand=True)
            block.pack_propagate(False)

            top_row = tk.Frame(block, bg="#e4ebf7")
            top_row.pack(side=tk.TOP, fill=tk.X, padx=10, pady=10)

            check_stub = tk.Frame(top_row, bg="#cfdcf4", width=70, height=22)
            check_stub.pack(side=tk.LEFT)
            check_stub.pack_propagate(False)

            bar = tk.Frame(block, bg="#d3dff4", height=24)
            bar.pack(side=tk.BOTTOM, fill=tk.X)

            left_stub = tk.Frame(bar, bg="#bfcfea", width=88, height=10)
            left_stub.pack(side=tk.LEFT, padx=8, pady=7)
            left_stub.pack_propagate(False)

            self._tail_skeleton_widgets.append(card)

    def set_videos(self, videos: Iterable[Video], has_more: bool = False) -> None:
        del has_more  # Infinite scroll is controlled by container callbacks.
        video_list = list(videos)
        valid_ids = {video.id for video in video_list if video.id}
        self._selected_ids.intersection_update(valid_ids)
        self._clear_all()

        if not video_list:
            empty = ttk.Label(self, text="No videos to display yet.", style="Muted.TLabel")
            empty.grid(row=0, column=0, columnspan=self._columns, sticky="ew", padx=10, pady=20)
            self._fire_selection_changed()
            return

        self._append_cards(video_list, default_checked=False)
        self._fire_selection_changed()
        self.update_visible_range(self._last_viewport_top, self._last_viewport_bottom)

    def append_videos(self, videos: Iterable[Video], has_more: bool = False) -> None:
        del has_more  # Infinite scroll is controlled by container callbacks.
        video_list = [v for v in videos if v and v.id and v.id not in self._cards]
        self._clear_tail_skeleton()

        if video_list:
            self._append_cards(video_list, default_checked=False)

        self._fire_selection_changed()
        self.update_visible_range(self._last_viewport_top, self._last_viewport_bottom)

    def _append_cards(self, videos: List[Video], default_checked: bool) -> None:
        self._clear_skeletons()
        for idx, video in enumerate(videos):
            absolute_idx = self._rendered_count + idx
            row = absolute_idx // self._columns
            col = absolute_idx % self._columns

            checked = video.id in self._selected_ids or default_checked
            if checked:
                self._selected_ids.add(video.id)

            card = VideoCard(
                self,
                video,
                checked=checked,
                on_toggle=self._handle_toggle,
                on_open=self._on_open_video,
                trend_threshold=self._trend_threshold,
            )
            card.grid(row=row, column=col, sticky="n", padx=4, pady=6)
            self._cards[video.id] = card

        self._rendered_count += len(videos)

    def update_visible_range(self, viewport_top: int, viewport_bottom: int) -> None:
        self._last_viewport_top = viewport_top
        self._last_viewport_bottom = viewport_bottom

        if not self._cards:
            return
        self.update_idletasks()

        top_guard = viewport_top - 140
        bottom_guard = viewport_bottom + 140

        for card in self._cards.values():
            y = card.winfo_y()
            h = card.winfo_height() or 360
            if (y + h) >= top_guard and y <= bottom_guard:
                card.ensure_thumbnail_loaded()

    def _handle_toggle(self, video: Video, checked: bool) -> None:
        if checked:
            self._selected_ids.add(video.id)
        else:
            self._selected_ids.discard(video.id)
        self._fire_selection_changed()

    def get_selected(self) -> List[Video]:
        videos: List[Video] = []
        for vid, card in self._cards.items():
            if vid in self._selected_ids:
                videos.append(card.video)
        return videos

    def _fire_selection_changed(self) -> None:
        self._on_selection_change(self.get_selected())

    def set_trend_threshold(self, trend_threshold: float) -> None:
        self._trend_threshold = trend_threshold

    def select_all(self) -> None:
        for vid in self._cards:
            self._selected_ids.add(vid)
        self.set_videos([card.video for card in self._cards.values()])

    def clear_selection(self) -> None:
        self._selected_ids.clear()
        self.set_videos([card.video for card in self._cards.values()])
