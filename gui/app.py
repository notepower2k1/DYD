from __future__ import annotations

import os
import shutil
import tempfile
import time
import tkinter as tk
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, List
from urllib.parse import quote

from models.profile import Profile
from models.video import Video
from services.download_history_service import DownloadHistoryService
from services.download_service import DownloadService
from services.session_cache_service import SessionCacheService
from services.tiktok_service import TikTokService
from utils.formatter import format_count, format_duration
from utils.trend import get_video_trend_score
from utils.threading import run_in_thread

from .components import LabeledEntry, PrimaryButton, ScrollableFrame
from .video_grid import VideoGrid

try:
    import vlc  # type: ignore[import-not-found]
except Exception:
    vlc = None


class TikTokDownloaderApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root

        self.tiktok_service = TikTokService()
        self._platform_states = self._build_platform_states()
        self.output_dir = self._platform_states["tiktok"]["output_dir"]
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.history_service = self._platform_states["tiktok"]["history_service"]
        self.session_cache = SessionCacheService(Path.cwd() / "session_cache.json")
        self._workspace_caches = {
            "tiktok": SessionCacheService(Path.cwd() / "session_cache_tiktok.json"),
            "douyin": SessionCacheService(Path.cwd() / "session_cache_douyin.json"),
        }

        self.profile_videos: List[Video] = []
        self.multi_videos: List[Video] = []
        self._profile_all_videos: List[Video] = []
        self._multi_all_videos: List[Video] = []
        self.bookmark_videos: List[Video] = []
        self._bookmark_all_videos: List[Video] = []
        self.search_videos: List[Video] = []
        self._search_all_videos: List[Video] = []
        self._search_offset = 0
        self._search_has_more = False

        self.profile: Profile | None = None
        self._profile_url: str | None = None
        self._next_start = 1
        self._page_size = 20
        self._profile_has_more = False
        self._loading_profile_page = False
        self._settings_popup: tk.Toplevel | None = None
        self._video_popup: tk.Toplevel | None = None
        self._download_queue: list[dict[str, Any]] = []
        self._queue_id_seq = 1
        self._queue_running = False
        self.platform_var = tk.StringVar(value="tiktok")
        self.platform_choice_var = tk.StringVar(value="")
        self._build_settings_controls()
        self._apply_douyin_backend_settings(save=False)

        self._build_styles()
        self._build_ui()
        self._refresh_queue_tab()
        self._refresh_downloaded_tab()
        self._restore_last_session()
        self.root.protocol("WM_DELETE_WINDOW", self._on_app_close)

    def _build_platform_states(self) -> dict[str, dict[str, Any]]:
        base_download_dir = Path.cwd() / "downloads"
        states: dict[str, dict[str, Any]] = {}
        for platform in ("tiktok", "douyin"):
            output_dir = base_download_dir / platform
            output_dir.mkdir(parents=True, exist_ok=True)
            states[platform] = {
                "output_dir": output_dir,
                "history_service": DownloadHistoryService(Path.cwd() / f"download_history_{platform}.json"),
                "download_queue": [],
                "queue_id_seq": 1,
                "queue_running": False,
                "profile_url": "",
                "profile": None,
                "profile_videos": [],
                "multi_links": [],
                "multi_videos": [],
                "bookmarked_videos": [],
                "search_keyword": "",
                "search_videos": [],
                "search_options": {},
                "search_offset": 0,
                "search_has_more": False,
                "active_tab": "profile",
                "profile_has_more": False,
                "next_start": 1,
            }
        return states

    def _build_styles(self) -> None:
        self.root.configure(bg="#eef2f9")

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure("App.TFrame", background="#eef2f9")
        style.configure("Panel.TFrame", background="#ffffff", relief="flat")
        style.configure("Panel.TLabelframe", background="#ffffff")
        style.configure("Panel.TLabelframe.Label", background="#ffffff", foreground="#1d2a44", font=("Segoe UI", 10, "bold"))
        style.configure("Muted.TLabel", background="#ffffff", foreground="#6b7a99", font=("Segoe UI", 10))

        style.configure("Primary.TButton", foreground="#ffffff", background="#1e5eff", borderwidth=0, padding=(14, 8), font=("Segoe UI", 10, "bold"))
        style.map("Primary.TButton", background=[("active", "#1949c7")])

        style.configure("Secondary.TButton", foreground="#1e5eff", background="#e9f0ff", borderwidth=0, padding=(12, 7), font=("Segoe UI", 10, "bold"))
        style.map("Secondary.TButton", background=[("active", "#d9e6ff")])

        style.configure("Card.TFrame", background="#ffffff", relief="solid", borderwidth=1)
        style.configure("CardInner.TFrame", background="#ffffff")
        style.configure("VideoThumb.TLabel", background="#121826")

        style.configure("Status.TLabel", background="#dde6f7", foreground="#1b2a45", padding=(10, 7), font=("Segoe UI", 9))

    def _build_ui(self) -> None:
        self.home_frame = ttk.Frame(self.root, padding=24, style="App.TFrame")
        self.home_frame.pack(fill=tk.BOTH, expand=True)

        self.main_frame = ttk.Frame(self.root, padding=14, style="App.TFrame")

        self._build_platform_home(self.home_frame)

        topbar = ttk.Frame(self.main_frame, padding=10, style="Panel.TFrame")
        topbar.pack(side=tk.TOP, fill=tk.X, pady=(0, 10))

        self.current_platform_title_var = tk.StringVar(value="")
        ttk.Label(
            topbar,
            textvariable=self.current_platform_title_var,
            background="#ffffff",
            foreground="#1d2a44",
            font=("Segoe UI", 11, "bold"),
        ).pack(side=tk.LEFT)

        self.platform_hint_var = tk.StringVar(value="")
        ttk.Label(topbar, textvariable=self.platform_hint_var, style="Muted.TLabel").pack(side=tk.LEFT, padx=(12, 0))

        ttk.Button(
            topbar,
            text="Change Platform",
            command=self._show_platform_home,
            style="Secondary.TButton",
        ).pack(side=tk.RIGHT)

        self.notebook = ttk.Notebook(self.main_frame)
        self.notebook.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.notebook.bind("<<NotebookTabChanged>>", lambda _e: self._save_session_cache())

        self.profile_tab = ttk.Frame(self.notebook, style="Panel.TFrame")
        self.multi_tab = ttk.Frame(self.notebook, style="Panel.TFrame")
        self.search_tab = ttk.Frame(self.notebook, style="Panel.TFrame")
        self.bookmark_tab = ttk.Frame(self.notebook, style="Panel.TFrame")
        self.downloads_tab = ttk.Frame(self.notebook, style="Panel.TFrame")

        self.notebook.add(self.profile_tab, text="Profile")
        self.notebook.add(self.multi_tab, text="Multi-link")
        self.notebook.add(self.search_tab, text="Search")
        self.notebook.add(self.bookmark_tab, text="Bookmarks")
        self.notebook.add(self.downloads_tab, text="Downloads")

        self._build_profile_tab()
        self._build_multi_tab()
        self._build_search_tab()
        self._build_bookmark_tab()
        self._build_downloads_tab()

        footer = ttk.Frame(self.main_frame, padding=10, style="Panel.TFrame")
        footer.pack(side=tk.TOP, fill=tk.X, pady=(10, 0))

        self.selected_var = tk.StringVar(value="Selected 0 video(s).")
        ttk.Label(footer, textvariable=self.selected_var, style="Muted.TLabel").pack(side=tk.LEFT)

        right_actions = ttk.Frame(footer, style="Panel.TFrame")
        right_actions.pack(side=tk.RIGHT)

        self.download_btn = ttk.Button(right_actions, text="Queue Selected", command=self.on_queue_selected, style="Secondary.TButton")
        self.download_btn.pack(side=tk.LEFT, padx=(0, 8))

        ttk.Button(right_actions, text="Settings", command=self._open_settings_popup, style="Secondary.TButton").pack(side=tk.LEFT)

        self.status_var = tk.StringVar(value="Choose a tab and fetch videos.")
        ttk.Label(footer, textvariable=self.status_var, style="Muted.TLabel", anchor=tk.W).pack(side=tk.RIGHT, padx=(0, 14))
        self._refresh_platform_ui(save=False)

    def _build_platform_home(self, master: ttk.Frame) -> None:
        hero = ttk.Frame(master, padding=18, style="Panel.TFrame")
        hero.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            hero,
            text="Choose a Platform",
            background="#ffffff",
            foreground="#1d2a44",
            font=("Segoe UI", 18, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            hero,
            text="Open only the workspace you need. Each platform keeps its own download folder and download history.",
            style="Muted.TLabel",
            wraplength=760,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(6, 18))

        cards = ttk.Frame(hero, style="Panel.TFrame")
        cards.pack(fill=tk.X)

        self._build_platform_card(cards, "TikTok", "Profile tools, multi-link fetch, downloads", True, lambda: self._enter_platform("tiktok")).pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 10))
        self._build_platform_card(cards, "Douyin", "Profile tools, multi-link fetch, downloads", True, lambda: self._enter_platform("douyin")).pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=10)
        self._build_platform_card(cards, "Xiaohongshu", "Coming soon", False, None).pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10, 0))

    def _build_platform_card(self, master: ttk.Frame, title: str, subtitle: str, enabled: bool, command) -> ttk.Frame:
        card = ttk.Frame(master, padding=16, style="Panel.TFrame")
        ttk.Label(card, text=title, background="#ffffff", foreground="#1d2a44", font=("Segoe UI", 13, "bold")).pack(anchor="w")
        ttk.Label(card, text=subtitle, style="Muted.TLabel", wraplength=220, justify=tk.LEFT).pack(anchor="w", pady=(8, 18))
        if enabled:
            ttk.Button(card, text=f"Open {title}", command=command, style="Primary.TButton").pack(anchor="w")
        else:
            ttk.Button(card, text="Coming Soon", state=tk.DISABLED, style="Secondary.TButton").pack(anchor="w")
        return card

    def _build_profile_tab(self) -> None:
        controls_panel = ttk.Frame(self.profile_tab, padding=10, style="Panel.TFrame")
        controls_panel.pack(side=tk.TOP, fill=tk.X)

        self.profile_url_input = LabeledEntry(
            controls_panel,
            text="Single TikTok profile URL",
        )
        self.profile_url_input.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 10))

        self.fetch_profile_btn = PrimaryButton(controls_panel, text="Fetch Profile Videos", command=self.on_fetch_profile)
        self.fetch_profile_btn.pack(side=tk.LEFT)

        self._build_video_tools(
            self.profile_tab,
            "profile",
            self._apply_profile_filters,
            self._reset_profile_filters,
            lambda: self.profile_grid.select_all(),
            lambda: self.profile_grid.clear_selection(),
        )

        profile_panel = ttk.Frame(self.profile_tab, padding=10, style="Panel.TFrame")
        profile_panel.pack(side=tk.TOP, fill=tk.X, pady=(10, 0))

        self.profile_name_var = tk.StringVar(value="")
        self.profile_extra_var = tk.StringVar(value="")

        self.profile_avatar_label = ttk.Label(profile_panel, width=4)
        self.profile_avatar_label.pack(side=tk.LEFT, padx=(0, 10))
        self._profile_avatar_img: tk.PhotoImage | None = None

        profile_text = ttk.Frame(profile_panel, style="Panel.TFrame")
        profile_text.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.profile_name_label = ttk.Label(profile_text, textvariable=self.profile_name_var, font=("Segoe UI", 11, "bold"), background="#ffffff", foreground="#1d2a44")
        self.profile_name_label.pack(side=tk.TOP, anchor="w")

        self.profile_extra_label = ttk.Label(profile_text, textvariable=self.profile_extra_var, style="Muted.TLabel")
        self.profile_extra_label.pack(side=tk.TOP, anchor="w")

        self.profile_grid_container = ScrollableFrame(self.profile_tab, style="Panel.TFrame")
        self.profile_grid_container.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=(10, 0))

        self.profile_grid = VideoGrid(
            self.profile_grid_container.container,
            on_selection_change=self._on_selection_change,
            on_open_video=self._open_video_details,
            on_quick_download=self._queue_single_video,
            on_bookmark=self._toggle_bookmark,
            on_load_more=None,
            columns=5,
            trend_threshold=self._get_trend_threshold(),
            style="Panel.TFrame",
        )
        self.profile_grid.pack(fill=tk.BOTH, expand=True)
        self.profile_grid_container.set_on_viewport_changed(self.profile_grid.update_visible_range)

        self.profile_loading_more_var = tk.StringVar(value="")
        self.profile_loading_more_label = ttk.Label(
            self.profile_tab,
            textvariable=self.profile_loading_more_var,
            style="Muted.TLabel",
            anchor="center",
        )
        self.profile_loading_more_label.pack(side=tk.TOP, fill=tk.X, pady=(6, 0))

        self.load_more_profile_btn = ttk.Button(
            self.profile_tab,
            text="Load More",
            command=self.on_load_more_profile,
            style="Secondary.TButton",
        )
        self.load_more_profile_btn.pack(side=tk.TOP, pady=(6, 0))
        self._show_profile_welcome_state()

    def _build_multi_tab(self) -> None:
        controls_panel = ttk.Frame(self.multi_tab, padding=10, style="Panel.TFrame")
        controls_panel.pack(side=tk.TOP, fill=tk.X)

        left = ttk.Frame(controls_panel, style="Panel.TFrame")
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 10))

        self.multi_links_label = ttk.Label(
            left,
            text="Multiple TikTok video URLs (space/newline separated)",
            style="Muted.TLabel",
        )
        self.multi_links_label.pack(side=tk.TOP, anchor="w", pady=(0, 4))
        self.multi_links_text = tk.Text(left, height=4, wrap="word", font=("Segoe UI", 10), relief="solid", borderwidth=1)
        self.multi_links_text.pack(side=tk.TOP, fill=tk.X, expand=True)

        self.fetch_links_btn = ttk.Button(controls_panel, text="Fetch Multi Links", command=self.on_fetch_multi_links, style="Secondary.TButton")
        self.fetch_links_btn.pack(side=tk.LEFT)

        self._build_video_tools(
            self.multi_tab,
            "multi",
            self._apply_multi_filters,
            self._reset_multi_filters,
            lambda: self.multi_grid.select_all(),
            lambda: self.multi_grid.clear_selection(),
        )

        self.multi_grid_container = ScrollableFrame(self.multi_tab, style="Panel.TFrame")
        self.multi_grid_container.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=(10, 0))

        self.multi_grid = VideoGrid(
            self.multi_grid_container.container,
            on_selection_change=self._on_selection_change,
            on_open_video=self._open_video_details,
            on_quick_download=self._queue_single_video,
            on_bookmark=self._toggle_bookmark,
            on_load_more=None,
            columns=5,
            trend_threshold=self._get_trend_threshold(),
            style="Panel.TFrame",
        )
        self.multi_grid.pack(fill=tk.BOTH, expand=True)
        self.multi_grid_container.set_on_viewport_changed(self.multi_grid.update_visible_range)

    def _build_search_tab(self) -> None:
        controls_panel = ttk.Frame(self.search_tab, padding=10, style="Panel.TFrame")
        controls_panel.pack(side=tk.TOP, fill=tk.X)

        self.search_keyword_var = tk.StringVar(value="")
        self.search_keyword_input = LabeledEntry(
            controls_panel,
            text="Keyword",
        )
        self.search_keyword_input.entry.configure(textvariable=self.search_keyword_var)
        self.search_keyword_input.entry_var = self.search_keyword_var
        self.search_keyword_input.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 10))

        self.search_btn = ttk.Button(
            controls_panel,
            text="Search",
            command=self.on_search_videos,
            style="Secondary.TButton",
        )
        self.search_btn.pack(side=tk.LEFT)

        load_more_slot = ttk.Frame(controls_panel, style="Panel.TFrame")
        load_more_slot.pack(side=tk.LEFT, padx=(8, 0))
        self.search_load_more_btn = ttk.Button(
            load_more_slot,
            text="Load More",
            command=self.on_load_more_search,
            style="Secondary.TButton",
            state=tk.DISABLED,
        )
        self.search_load_more_btn.pack(side=tk.LEFT)

        options_panel = ttk.Frame(self.search_tab, padding=(10, 0, 10, 6), style="Panel.TFrame")
        options_panel.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(options_panel, text="Search Options (Before Fetch)", style="Muted.TLabel").pack(side=tk.LEFT)

        self.search_sort_var = tk.StringVar(value="综合排序")
        self.search_time_var = tk.StringVar(value="不限")
        self.search_duration_var = tk.StringVar(value="不限")
        self.search_scope_var = tk.StringVar(value="不限")

        ttk.Label(options_panel, text="Sort", style="Muted.TLabel").pack(side=tk.LEFT, padx=(12, 0))
        ttk.Combobox(
            options_panel,
            textvariable=self.search_sort_var,
            state="readonly",
            width=10,
            values=("综合排序", "最新发布", "最多点赞"),
        ).pack(side=tk.LEFT, padx=(6, 10))

        ttk.Label(options_panel, text="Time", style="Muted.TLabel").pack(side=tk.LEFT)
        ttk.Combobox(
            options_panel,
            textvariable=self.search_time_var,
            state="readonly",
            width=10,
            values=("不限", "一天内", "一周内", "半年内"),
        ).pack(side=tk.LEFT, padx=(6, 10))

        ttk.Label(options_panel, text="Duration", style="Muted.TLabel").pack(side=tk.LEFT)
        ttk.Combobox(
            options_panel,
            textvariable=self.search_duration_var,
            state="readonly",
            width=10,
            values=("不限", "< 1分钟", "1–5分钟"),
        ).pack(side=tk.LEFT, padx=(6, 10))

        ttk.Label(options_panel, text="Scope", style="Muted.TLabel").pack(side=tk.LEFT)
        ttk.Combobox(
            options_panel,
            textvariable=self.search_scope_var,
            state="readonly",
            width=12,
            values=("不限", "关注的人", "最近看过", "还未看过"),
        ).pack(side=tk.LEFT, padx=(6, 0))

        ttk.Label(
            self.search_tab,
            text="Result Filters (After Fetch)",
            style="Muted.TLabel",
            padding=(10, 4, 0, 0),
        ).pack(side=tk.TOP, anchor="w")

        self._build_video_tools(
            self.search_tab,
            "search",
            self._apply_search_filters,
            self._reset_search_filters,
            lambda: self.search_grid.select_all(),
            lambda: self.search_grid.clear_selection(),
        )

        self.search_grid_container = ScrollableFrame(self.search_tab, style="Panel.TFrame")
        self.search_grid_container.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=(10, 0))

        self.search_grid = VideoGrid(
            self.search_grid_container.container,
            on_selection_change=self._on_selection_change,
            on_open_video=self._open_video_details,
            on_quick_download=self._queue_single_video,
            on_bookmark=self._toggle_bookmark,
            on_load_more=None,
            columns=5,
            trend_threshold=self._get_trend_threshold(),
            style="Panel.TFrame",
        )
        self.search_grid.pack(fill=tk.BOTH, expand=True)
        self.search_grid_container.set_on_viewport_changed(self.search_grid.update_visible_range)

    def _build_bookmark_tab(self) -> None:
        header = ttk.Frame(self.bookmark_tab, padding=10, style="Panel.TFrame")
        header.pack(side=tk.TOP, fill=tk.X)

        ttk.Label(
            header,
            text="Bookmarked Videos",
            background="#ffffff",
            foreground="#1d2a44",
            font=("Segoe UI", 11, "bold"),
        ).pack(side=tk.LEFT)
        ttk.Label(
            header,
            text="Mark videos with ★ to keep them for later.",
            style="Muted.TLabel",
        ).pack(side=tk.LEFT, padx=(12, 0))

        self.bookmark_grid_container = ScrollableFrame(self.bookmark_tab, style="Panel.TFrame")
        self.bookmark_grid_container.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=(10, 0))

        self.bookmark_grid = VideoGrid(
            self.bookmark_grid_container.container,
            on_selection_change=self._on_selection_change,
            on_open_video=self._open_video_details,
            on_quick_download=self._queue_single_video,
            on_bookmark=self._toggle_bookmark,
            on_load_more=None,
            columns=5,
            trend_threshold=self._get_trend_threshold(),
            style="Panel.TFrame",
        )
        self.bookmark_grid.pack(fill=tk.BOTH, expand=True)
        self.bookmark_grid_container.set_on_viewport_changed(self.bookmark_grid.update_visible_range)

    def _build_downloads_tab(self) -> None:
        top = ttk.Frame(self.downloads_tab, padding=10, style="Panel.TFrame")
        top.pack(side=tk.TOP, fill=tk.X)

        ttk.Button(top, text="Refresh History", command=self._refresh_downloaded_tab, style="Secondary.TButton").pack(side=tk.LEFT)
        ttk.Button(top, text="Open File", command=self._open_selected_downloaded_file, style="Secondary.TButton").pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(top, text="Clear Completed Queue", command=self._clear_completed_queue, style="Secondary.TButton").pack(side=tk.LEFT, padx=(8, 0))

        queue_panel = ttk.Frame(self.downloads_tab, padding=10, style="Panel.TFrame")
        queue_panel.pack(side=tk.TOP, fill=tk.BOTH, expand=False)

        queue_header = ttk.Frame(queue_panel, style="Panel.TFrame")
        queue_header.pack(fill=tk.X)
        ttk.Label(queue_header, text="Download Queue", background="#ffffff", foreground="#1d2a44", font=("Segoe UI", 11, "bold")).pack(side=tk.LEFT)
        self.queue_status_var = tk.StringVar(value="Queue is empty.")
        ttk.Label(queue_header, textvariable=self.queue_status_var, style="Muted.TLabel").pack(side=tk.RIGHT)

        self.queue_progress_var = tk.DoubleVar(value=0.0)
        self.queue_progress = ttk.Progressbar(queue_panel, maximum=100, variable=self.queue_progress_var)
        self.queue_progress.pack(fill=tk.X, pady=(8, 8))

        queue_columns = ("title", "profile", "status", "progress")
        self.queue_tree = ttk.Treeview(queue_panel, columns=queue_columns, show="headings", height=7)
        self.queue_tree.heading("title", text="Title")
        self.queue_tree.heading("profile", text="Profile")
        self.queue_tree.heading("status", text="Status")
        self.queue_tree.heading("progress", text="Progress")
        self.queue_tree.column("title", width=360, anchor="w")
        self.queue_tree.column("profile", width=120, anchor="w")
        self.queue_tree.column("status", width=140, anchor="w")
        self.queue_tree.column("progress", width=120, anchor="center")
        self.queue_tree.pack(fill=tk.BOTH, expand=True)

        separator = ttk.Separator(self.downloads_tab, orient="horizontal")
        separator.pack(fill=tk.X, padx=10, pady=(0, 4))

        history_panel = ttk.Frame(self.downloads_tab, padding=10, style="Panel.TFrame")
        history_panel.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        ttk.Label(history_panel, text="Downloaded History", background="#ffffff", foreground="#1d2a44", font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(0, 8))

        columns = ("downloaded_at", "profile", "title", "file_path")
        self.downloaded_tree = ttk.Treeview(history_panel, columns=columns, show="headings")
        self.downloaded_tree.heading("downloaded_at", text="Downloaded At")
        self.downloaded_tree.heading("profile", text="Profile")
        self.downloaded_tree.heading("title", text="Title")
        self.downloaded_tree.heading("file_path", text="File Path")

        self.downloaded_tree.column("downloaded_at", width=170, anchor="w")
        self.downloaded_tree.column("profile", width=120, anchor="w")
        self.downloaded_tree.column("title", width=260, anchor="w")
        self.downloaded_tree.column("file_path", width=520, anchor="w")

        self.downloaded_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.downloaded_tree.bind("<Double-1>", lambda _e: self._open_selected_downloaded_file())

        scrollbar = ttk.Scrollbar(history_panel, orient="vertical", command=self.downloaded_tree.yview)
        self.downloaded_tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

    def _build_settings_controls(self) -> None:
        self.batch_size_var = tk.StringVar(value="20")
        self.trend_threshold_var = tk.StringVar(value="0.80")
        self.douyin_login_status_var = tk.StringVar(value="Douyin login is not ready.")
        self.douyin_backend_enabled_var = tk.BooleanVar(value=True)
        self.douyin_backend_autostart_var = tk.BooleanVar(value=True)
        self.douyin_backend_url_var = tk.StringVar(value="http://127.0.0.1:5555")
        self.douyin_backend_token_var = tk.StringVar(value="")
        self.douyin_backend_endpoint_var = tk.StringVar(value="/douyin/detail")
        self.douyin_backend_command_var = tk.StringVar(value="")

    def _show_profile_welcome_state(self) -> None:
        is_douyin = self.platform_var.get().strip().lower() == "douyin"
        self.profile_name_var.set("Discover a Douyin profile" if is_douyin else "Discover a TikTok profile")
        self.profile_extra_var.set(
            "Paste a Douyin profile URL or sec_user_id to load videos, or continue from the last cached session."
            if is_douyin
            else "Paste a profile URL to load videos, or continue from the last cached session."
        )
        self.profile_avatar_label.configure(text="@", image="", anchor="center", background="#dfe8f7", foreground="#6a7da6", font=("Segoe UI", 18, "bold"))
        self._profile_avatar_img = None
        self.profile_grid.show_loading("Loading workspace...")
        self.root.after(
            220,
            lambda: self.profile_grid.show_placeholder(
                "Paste a Douyin profile URL to load videos."
                if is_douyin
                else "Paste a TikTok profile URL to load videos."
            ) if not self.profile_videos else None,
        )
        self._refresh_load_more_button()

    def _refresh_load_more_button(self) -> None:
        if not hasattr(self, "load_more_profile_btn"):
            return
        if self._loading_profile_page:
            self.load_more_profile_btn.configure(state="disabled", text="Loading...")
            return
        if self._profile_has_more and self._profile_url:
            self.load_more_profile_btn.configure(state="normal", text="Load More")
        else:
            self.load_more_profile_btn.configure(state="disabled", text="No More Videos")

    def _build_video_tools(
        self,
        master: ttk.Frame,
        prefix: str,
        apply_command,
        reset_command,
        select_all_command,
        clear_selection_command,
    ) -> None:
        setattr(self, f"{prefix}_sort_key_var", tk.StringVar(value="upload_time"))
        setattr(self, f"{prefix}_sort_order_var", tk.StringVar(value="desc"))
        setattr(self, f"{prefix}_min_views_var", tk.StringVar(value=""))
        setattr(self, f"{prefix}_min_likes_var", tk.StringVar(value=""))
        setattr(self, f"{prefix}_min_comments_var", tk.StringVar(value=""))
        setattr(self, f"{prefix}_min_trend_var", tk.StringVar(value=""))
        setattr(self, f"{prefix}_max_age_days_var", tk.StringVar(value=""))

        tools = ttk.Frame(master, padding=(10, 0, 10, 6), style="Panel.TFrame")
        tools.pack(side=tk.TOP, fill=tk.X)
        setattr(self, f"{prefix}_tools_frame", tools)

        ttk.Button(tools, text="Select All", style="Secondary.TButton", command=select_all_command).pack(side=tk.LEFT)
        ttk.Button(tools, text="Unselect All", style="Secondary.TButton", command=clear_selection_command).pack(side=tk.LEFT, padx=(8, 12))

        ttk.Label(tools, text="Sort", style="Muted.TLabel").pack(side=tk.LEFT)
        sort_key_combo = ttk.Combobox(
            tools,
            textvariable=getattr(self, f"{prefix}_sort_key_var"),
            state="readonly",
            width=14,
            values=("upload_time", "view_count", "like_count", "comment_count", "trend_score"),
        )
        sort_key_combo.pack(side=tk.LEFT, padx=(6, 6))
        setattr(self, f"{prefix}_sort_key_combo", sort_key_combo)
        ttk.Combobox(
            tools,
            textvariable=getattr(self, f"{prefix}_sort_order_var"),
            state="readonly",
            width=7,
            values=("desc", "asc"),
        ).pack(side=tk.LEFT, padx=(0, 12))

        for key, label, attr_name, width in (
            ("views", "Min views", f"{prefix}_min_views_var", 9),
            ("likes", "Min likes", f"{prefix}_min_likes_var", 9),
            ("comments", "Min comments", f"{prefix}_min_comments_var", 10),
            ("trend", "Min trend", f"{prefix}_min_trend_var", 7),
            ("age", "Max age days", f"{prefix}_max_age_days_var", 8),
        ):
            label_widget = ttk.Label(tools, text=label, style="Muted.TLabel")
            label_widget.pack(side=tk.LEFT)
            entry_widget = ttk.Entry(tools, textvariable=getattr(self, attr_name), width=width)
            entry_widget.pack(side=tk.LEFT, padx=(6, 10))
            setattr(self, f"{prefix}_{key}_label_widget", label_widget)
            setattr(self, f"{prefix}_{key}_entry_widget", entry_widget)

        ttk.Button(tools, text="Apply", style="Secondary.TButton", command=apply_command).pack(side=tk.LEFT)
        ttk.Button(tools, text="Reset", style="Secondary.TButton", command=reset_command).pack(side=tk.LEFT, padx=(8, 0))

    def _open_settings_popup(self) -> None:
        if self._settings_popup is not None and self._settings_popup.winfo_exists():
            self._settings_popup.lift()
            self._settings_popup.focus_force()
            return
        self._update_douyin_login_status()

        popup = tk.Toplevel(self.root)
        popup.title("Settings")
        popup.transient(self.root)
        popup.resizable(False, False)
        popup.configure(bg="#eef2f9")
        popup.grab_set()
        popup.focus_force()
        self._settings_popup = popup

        body = ttk.Frame(popup, padding=14, style="App.TFrame")
        body.pack(fill=tk.BOTH, expand=True)

        panel = ttk.Frame(body, padding=12, style="Panel.TFrame")
        panel.pack(fill=tk.BOTH, expand=True)

        batch_row = ttk.Frame(panel, style="Panel.TFrame")
        batch_row.pack(fill=tk.X)
        ttk.Label(batch_row, text="Batch size", style="Muted.TLabel").pack(side=tk.LEFT)
        self.batch_size_combo = ttk.Combobox(
            batch_row,
            textvariable=self.batch_size_var,
            state="readonly",
            width=6,
            values=("10", "20", "50"),
        )
        self.batch_size_combo.pack(side=tk.RIGHT)

        trend_row = ttk.Frame(panel, style="Panel.TFrame")
        trend_row.pack(fill=tk.X, pady=(12, 0))
        ttk.Label(trend_row, text="Trend threshold", style="Muted.TLabel").pack(side=tk.LEFT)
        trend_combo = ttk.Combobox(
            trend_row,
            textvariable=self.trend_threshold_var,
            state="readonly",
            width=6,
            values=("0.60", "0.70", "0.80", "0.90"),
        )
        trend_combo.pack(side=tk.RIGHT)
        trend_combo.bind("<<ComboboxSelected>>", lambda _e: self._apply_trend_threshold())

        login_row = ttk.Frame(panel, style="Panel.TFrame")
        login_row.pack(fill=tk.X, pady=(12, 0))
        ttk.Label(login_row, text="Douyin login", style="Muted.TLabel").pack(anchor="w")
        ttk.Label(
            login_row,
            text=(
                "Use Playwright to open a real Douyin browser session, then log in there once. "
                "The app will keep and reuse that local session for future Douyin fetches."
            ),
            style="Muted.TLabel",
            wraplength=620,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(4, 8))
        ttk.Label(
            login_row,
            textvariable=self.douyin_login_status_var,
            style="Muted.TLabel",
            wraplength=620,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(0, 8))
        ttk.Button(
            login_row,
            text="Login to Douyin",
            style="Secondary.TButton",
            command=self._login_to_douyin,
        ).pack(side=tk.LEFT)
        ttk.Button(
            login_row,
            text="Show Douyin Browser",
            style="Secondary.TButton",
            command=self._show_douyin_browser,
        ).pack(side=tk.LEFT, padx=(8, 0))

        backend_row = ttk.Frame(panel, style="Panel.TFrame")
        backend_row.pack(fill=tk.X, pady=(16, 0))
        ttk.Label(backend_row, text="Douyin engine", style="Muted.TLabel").pack(anchor="w")
        ttk.Checkbutton(
            backend_row,
            text="Use embedded Douyin Playwright engine in Multi-link",
            variable=self.douyin_backend_enabled_var,
            command=self._apply_douyin_backend_settings,
        ).pack(anchor="w", pady=(4, 8))

        ttk.Label(
            backend_row,
            text=(
                "Douyin links now use an embedded Playwright-based engine inside this app. "
                "No separate backend API process is required."
            ),
            style="Muted.TLabel",
            wraplength=620,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(8, 0))

        folder_row = ttk.Frame(panel, style="Panel.TFrame")
        folder_row.pack(fill=tk.X, pady=(16, 0))
        ttk.Label(folder_row, text="Download folder", style="Muted.TLabel").pack(anchor="w")
        folder_var = tk.StringVar(value=str(self.output_dir))
        ttk.Label(
            folder_row,
            textvariable=folder_var,
            background="#ffffff",
            foreground="#1d2a44",
            wraplength=520,
            justify=tk.LEFT,
        ).pack(anchor="w", pady=(4, 8))
        ttk.Button(
            folder_row,
            text="Change Folder",
            style="Secondary.TButton",
            command=lambda: self.on_change_dir(folder_var),
        ).pack(anchor="w")

        footer = ttk.Frame(panel, style="Panel.TFrame")
        footer.pack(fill=tk.X, pady=(14, 0))
        ttk.Button(footer, text="Close", style="Secondary.TButton", command=self._close_settings_popup).pack(side=tk.RIGHT)

        self._center_popup(popup, 760, 520)
        popup.protocol("WM_DELETE_WINDOW", self._close_settings_popup)

    def run(self) -> None:
        self.root.mainloop()

    def _enter_platform(self, platform: str) -> None:
        self._set_platform(platform, save=True)
        self.home_frame.pack_forget()
        self.main_frame.pack(fill=tk.BOTH, expand=True)
        self.status_var.set(f"{self._platform_display_name()} workspace is ready.")

    def _show_platform_home(self) -> None:
        self._sync_platform_state()
        self._save_session_cache()
        self.main_frame.pack_forget()
        self.home_frame.pack(fill=tk.BOTH, expand=True)

    def _set_platform(self, platform: str, save: bool = True, sync_current: bool = True) -> None:
        if sync_current:
            self._sync_platform_state()
        platform = "douyin" if str(platform).lower() == "douyin" else "tiktok"
        self.platform_var.set(platform)
        self._apply_platform_state(platform)
        self._refresh_platform_ui(save=save)

    def _sync_platform_state(self) -> None:
        platform = self.platform_var.get().strip().lower() or "tiktok"
        state = self._platform_states.get(platform)
        if not state:
            return
        state["output_dir"] = self.output_dir
        state["history_service"] = self.history_service
        state["download_queue"] = self._download_queue
        state["queue_id_seq"] = self._queue_id_seq
        state["queue_running"] = self._queue_running
        state["profile_url"] = self._profile_url or ""
        state["profile"] = {
            "username": getattr(self.profile, "username", None),
            "display_name": getattr(self.profile, "display_name", None),
            "avatar_url": getattr(self.profile, "avatar_url", None),
            "follower_count": getattr(self.profile, "follower_count", None),
            "following_count": getattr(self.profile, "following_count", None),
            "like_count": getattr(self.profile, "like_count", None),
            "video_count": getattr(self.profile, "video_count", None),
        } if self.profile else None
        state["profile_videos"] = [video.to_dict() for video in self._profile_all_videos]
        state["multi_links"] = self._parse_multi_links()
        state["multi_videos"] = [video.to_dict() for video in self._multi_all_videos]
        state["bookmarked_videos"] = [video.to_dict() for video in self._bookmark_all_videos]
        state["search_keyword"] = self.search_keyword_var.get().strip() if hasattr(self, "search_keyword_var") else ""
        state["search_videos"] = [video.to_dict() for video in self._search_all_videos]
        state["search_options"] = self._build_search_options() if hasattr(self, "search_sort_var") else {}
        state["search_offset"] = int(self._search_offset)
        state["search_has_more"] = bool(self._search_has_more)
        state["active_tab"] = self._active_mode()
        state["profile_has_more"] = bool(self._profile_has_more)
        state["next_start"] = int(self._next_start)

    def _apply_platform_state(self, platform: str) -> None:
        state = self._platform_states.get(platform)
        if not state:
            return
        self.output_dir = state["output_dir"]
        self.history_service = state["history_service"]
        self._download_queue = state["download_queue"]
        self._queue_id_seq = state["queue_id_seq"]
        self._queue_running = state["queue_running"]
        self._profile_url = str(state.get("profile_url") or "")
        profile_data = state.get("profile")
        self.profile = None
        if isinstance(profile_data, dict):
            self.profile = Profile(
                username=profile_data.get("username") or "",
                display_name=profile_data.get("display_name") or "",
                avatar_url=profile_data.get("avatar_url") or "",
                follower_count=profile_data.get("follower_count"),
                following_count=profile_data.get("following_count"),
                like_count=profile_data.get("like_count"),
                video_count=profile_data.get("video_count"),
            )
        self._profile_has_more = bool(state.get("profile_has_more"))
        self._next_start = int(state.get("next_start") or 1)
        profile_rows = state.get("profile_videos") or []
        multi_rows = state.get("multi_videos") or []
        bookmark_rows = state.get("bookmarked_videos") or []
        search_rows = state.get("search_videos") or []
        self._bookmark_all_videos = self._dedupe_videos([Video.from_dict(row) for row in bookmark_rows if isinstance(row, dict)])
        for video in self._bookmark_all_videos:
            video.is_bookmarked = True
        self._profile_all_videos = self._dedupe_videos([Video.from_dict(row) for row in profile_rows if isinstance(row, dict)])
        self._multi_all_videos = self._dedupe_videos([Video.from_dict(row) for row in multi_rows if isinstance(row, dict)])
        self._search_all_videos = self._dedupe_videos([Video.from_dict(row) for row in search_rows if isinstance(row, dict)])
        self.profile_videos = self._apply_video_controls("profile", self._profile_all_videos)
        self.multi_videos = self._apply_video_controls("multi", self._multi_all_videos)
        self.bookmark_videos = list(self._bookmark_all_videos)
        self.search_videos = self._apply_video_controls("search", self._search_all_videos)
        self.profile_url_input.set(self._profile_url)
        if hasattr(self, "search_keyword_var"):
            self.search_keyword_var.set(str(state.get("search_keyword") or ""))
        search_options = state.get("search_options") or {}
        if isinstance(search_options, dict):
            try:
                self.search_sort_var.set(str(search_options.get("sort_label") or "综合排序"))
                self.search_time_var.set(str(search_options.get("time_label") or "不限"))
                self.search_duration_var.set(str(search_options.get("duration_label") or "不限"))
                self.search_scope_var.set(str(search_options.get("scope_label") or "不限"))
            except Exception:
                pass
        self._search_offset = int(state.get("search_offset") or 0)
        self._search_has_more = bool(state.get("search_has_more"))
        self.multi_links_text.delete("1.0", tk.END)
        multi_links = state.get("multi_links") or []
        if isinstance(multi_links, list) and multi_links:
            self.multi_links_text.insert("1.0", "\n".join(str(link) for link in multi_links))
        self._refresh_queue_tab()
        self._refresh_downloaded_tab()
        self.history_service.apply_status(self._profile_all_videos)
        self.history_service.apply_status(self._multi_all_videos)
        self.history_service.apply_status(self._bookmark_all_videos)
        self.history_service.apply_status(self._search_all_videos)
        self.profile_grid.set_videos(self.profile_videos, has_more=self._profile_has_more)
        self.multi_grid.set_videos(self.multi_videos, has_more=False)
        self.bookmark_grid.set_videos(self.bookmark_videos, has_more=False)
        self.search_grid.set_videos(self.search_videos, has_more=False)
        self.profile_grid_container.scroll_to_top()
        self.multi_grid_container.scroll_to_top()
        self.bookmark_grid_container.scroll_to_top()
        self.search_grid_container.scroll_to_top()
        self.profile_grid_container.refresh_viewport()
        self.multi_grid_container.refresh_viewport()
        self.bookmark_grid_container.refresh_viewport()
        self.search_grid_container.refresh_viewport()
        self._refresh_search_load_more_button()
        if self.profile:
            self._update_profile_header()
        elif not self.profile_videos:
            self._show_profile_welcome_state()
        else:
            self.profile_name_var.set("")
            self.profile_extra_var.set("")
            self.profile_avatar_label.configure(image="", text="")
            self._profile_avatar_img = None
        active_tab = str(state.get("active_tab") or "profile")
        if active_tab == "multi":
            self.notebook.select(self.multi_tab)
        elif active_tab == "search":
            self.notebook.select(self.search_tab)
        elif active_tab == "bookmarks":
            self.notebook.select(self.bookmark_tab)
        elif active_tab in {"downloads", "downloaded"}:
            self.notebook.select(self.downloads_tab)
        else:
            self.notebook.select(self.profile_tab)

    def _platform_display_name(self) -> str:
        return "Douyin" if self.platform_var.get().strip().lower() == "douyin" else "TikTok"

    def _refresh_platform_ui(self, save: bool = True) -> None:
        platform = self.platform_var.get().strip().lower() or "tiktok"
        is_douyin = platform == "douyin"
        self.current_platform_title_var.set("Douyin Workspace" if is_douyin else "TikTok Workspace")

        self.platform_hint_var.set(
            "Douyin mode: profile and video tools."
            if is_douyin
            else "TikTok mode: profile and video tools."
        )

        self.profile_url_input.label.configure(
            text="Single Douyin profile URL" if is_douyin else "Single TikTok profile URL"
        )
        self.fetch_profile_btn.configure(
            text="Fetch Douyin Profile Videos" if is_douyin else "Fetch Profile Videos"
        )
        self.multi_links_label.configure(
            text="Multiple Douyin video URLs (space/newline separated)"
            if is_douyin
            else "Multiple TikTok video URLs (space/newline separated)"
        )
        self.fetch_links_btn.configure(
            text="Fetch Douyin Links" if is_douyin else "Fetch TikTok Links"
        )
        if not self.profile_videos:
            self._show_profile_welcome_state()
        self._refresh_video_tool_ui("profile", is_douyin)
        self._refresh_video_tool_ui("multi", is_douyin)
        self._refresh_video_tool_ui("search", is_douyin)

        try:
            self.notebook.add(self.profile_tab, text="Profile")
        except Exception:
            pass
        self.notebook.insert(0, self.profile_tab)
        if not self.profile_videos and not self.multi_videos and not self.bookmark_videos and not self.search_videos:
            self.status_var.set(
                "Douyin mode is active. Choose Profile or Multi-link."
                if is_douyin
                else "TikTok mode is active. Choose Profile or Multi-link."
            )

        if save:
            self._save_session_cache()

    def _refresh_video_tool_ui(self, prefix: str, is_douyin: bool) -> None:
        sort_combo = getattr(self, f"{prefix}_sort_key_combo", None)
        if sort_combo is not None:
            sort_values = ("upload_time", "like_count", "comment_count", "trend_score") if is_douyin else (
                "upload_time",
                "view_count",
                "like_count",
                "comment_count",
                "trend_score",
            )
            try:
                sort_combo.configure(values=sort_values)
            except Exception:
                pass
            sort_var = getattr(self, f"{prefix}_sort_key_var")
            if is_douyin and sort_var.get().strip() == "view_count":
                sort_var.set("like_count")

        views_label = getattr(self, f"{prefix}_views_label_widget", None)
        views_entry = getattr(self, f"{prefix}_views_entry_widget", None)
        likes_label = getattr(self, f"{prefix}_likes_label_widget", None)
        if views_label is not None:
            if is_douyin:
                if views_label.winfo_manager():
                    views_label.pack_forget()
            elif not views_label.winfo_manager():
                if likes_label is not None and likes_label.winfo_manager():
                    views_label.pack(side=tk.LEFT, before=likes_label)
                else:
                    views_label.pack(side=tk.LEFT)
        if views_entry is not None:
            if is_douyin:
                if views_entry.winfo_manager():
                    views_entry.pack_forget()
                getattr(self, f"{prefix}_min_views_var").set("")
            elif not views_entry.winfo_manager():
                if likes_label is not None and likes_label.winfo_manager():
                    views_entry.pack(side=tk.LEFT, padx=(6, 10), before=likes_label)
                else:
                    views_entry.pack(side=tk.LEFT, padx=(6, 10))

    def _active_mode(self) -> str:
        current = self.notebook.select()
        if current == str(self.profile_tab):
            return "profile"
        if current == str(self.multi_tab):
            return "multi"
        if current == str(self.search_tab):
            return "search"
        if current == str(self.bookmark_tab):
            return "bookmarks"
        return "downloads"

    def _set_loading(self, loading: bool) -> None:
        state = tk.DISABLED if loading else tk.NORMAL
        self.fetch_profile_btn.configure(state=state)
        self.fetch_links_btn.configure(state=state)
        self.download_btn.configure(state=state)
        if hasattr(self, "search_btn"):
            self.search_btn.configure(state=state)
        if hasattr(self, "search_load_more_btn"):
            self.search_load_more_btn.configure(state=tk.DISABLED if loading else self.search_load_more_btn["state"])
        combo = getattr(self, "batch_size_combo", None)
        if combo is not None:
            try:
                if combo.winfo_exists():
                    combo.configure(state="disabled" if loading else "readonly")
            except Exception:
                self.batch_size_combo = None  # type: ignore[assignment]

        if loading:
            self.status_var.set("Working... please wait.")
        self.root.update_idletasks()
        
    def _on_selection_change(self, _selected: List[Video]) -> None:
        mode = self._active_mode()
        if mode == "profile":
            count = len(self.profile_grid.get_selected())
        elif mode == "multi":
            count = len(self.multi_grid.get_selected())
        elif mode == "search":
            count = len(self.search_grid.get_selected())
        elif mode == "bookmarks":
            count = len(self.bookmark_grid.get_selected())
        else:
            count = 0
        self.selected_var.set(f"Selected {count} video(s).")

    def _parse_multi_links(self) -> List[str]:
        raw = self.multi_links_text.get("1.0", tk.END)
        if not raw:
            return []
        return [p.strip() for p in raw.replace("\n", " ").split(" ") if p.strip()]

    @staticmethod
    def _video_identity(video: Video) -> str:
        return (video.id or video.url or "").strip()

    def _merge_video(self, base: Video, incoming: Video) -> Video:
        for attr in (
            "title",
            "url",
            "platform",
            "media_url",
            "description",
            "duration",
            "thumbnail_url",
            "author",
            "uploader",
            "view_count",
            "like_count",
            "comment_count",
            "share_count",
            "bookmark_count",
            "upload_time",
            "music_title",
            "is_bookmarked",
        ):
            current_value = getattr(base, attr)
            if current_value in (None, "", 0):
                incoming_value = getattr(incoming, attr)
                if incoming_value not in (None, "", 0):
                    setattr(base, attr, incoming_value)

        if not base.image_urls and incoming.image_urls:
            base.image_urls = incoming.image_urls
        if not base.tags and incoming.tags:
            base.tags = incoming.tags
        if incoming.is_downloaded and not base.is_downloaded:
            base.is_downloaded = True
        if not base.downloaded_path and incoming.downloaded_path:
            base.downloaded_path = incoming.downloaded_path
        return base

    def _toggle_bookmark(self, video: Video, bookmarked: bool) -> None:
        key = self._video_identity(video)
        if not key:
            return

        video.is_bookmarked = bookmarked
        self.history_service.apply_status([video])
        if bookmarked:
            existing = {self._video_identity(v) for v in self._bookmark_all_videos}
            if key not in existing:
                self._bookmark_all_videos.append(video)
        else:
            self._bookmark_all_videos = [v for v in self._bookmark_all_videos if self._video_identity(v) != key]

        for source in (self._profile_all_videos, self._multi_all_videos):
            for item in source:
                if self._video_identity(item) == key:
                    item.is_bookmarked = bookmarked

        self._bookmark_all_videos = self._dedupe_videos(self._bookmark_all_videos)
        self.bookmark_videos = list(self._bookmark_all_videos)
        if getattr(self, "bookmark_grid", None) is not None:
            self.bookmark_grid.set_videos(self.bookmark_videos, has_more=False)
            self.bookmark_grid_container.refresh_viewport()
        self._save_session_cache()

    def _dedupe_videos(self, videos: List[Video]) -> List[Video]:
        ordered: list[Video] = []
        by_key: dict[str, Video] = {}
        bookmark_keys = {self._video_identity(v) for v in self._bookmark_all_videos}
        for video in videos:
            if not video:
                continue
            key = self._video_identity(video)
            if not key:
                continue
            if key in bookmark_keys:
                video.is_bookmarked = True
            if key in by_key:
                self._merge_video(by_key[key], video)
                continue
            by_key[key] = video
            ordered.append(video)
        return ordered

    def _get_page_size(self) -> int:
        try:
            v = int(self.batch_size_var.get().strip())
        except Exception:
            return 20
        if v in (10, 20, 50):
            return v
        return 20

    def _get_trend_threshold(self) -> float:
        try:
            value = float(self.trend_threshold_var.get().strip())
        except Exception:
            return 0.8
        if 0.3 <= value <= 1.0:
            return value
        return 0.8

    def _apply_trend_threshold(self) -> None:
        threshold = self._get_trend_threshold()
        self.profile_grid.set_trend_threshold(threshold)
        self.multi_grid.set_trend_threshold(threshold)
        if self._profile_all_videos:
            self.profile_videos = self._apply_video_controls("profile", self._profile_all_videos)
            self.profile_grid.set_videos(self.profile_videos, has_more=self._profile_has_more)
            self.profile_grid_container.refresh_viewport()
        if self._multi_all_videos:
            self.multi_videos = self._apply_video_controls("multi", self._multi_all_videos)
            self.multi_grid.set_videos(self.multi_videos, has_more=False)
            self.multi_grid_container.refresh_viewport()
        self._save_session_cache()

    def _update_douyin_login_status(self) -> None:
        if self.tiktok_service.has_douyin_login_session():
            self.douyin_login_status_var.set("Douyin login session is available.")
        else:
            self.douyin_login_status_var.set("Douyin login is required before fetching Douyin videos.")

    def _login_to_douyin(self) -> None:
        self.douyin_login_status_var.set("Opening Douyin login window...")
        self.status_var.set("Opening Douyin login window...")
        self._do_douyin_login()

    def _apply_douyin_backend_settings(self, save: bool = True) -> None:
        self.tiktok_service.configure_douyin_backend(True, "", "", "")
        if save:
            self._save_session_cache()

    def _is_douyin_backend_healthy(self) -> bool:
        return True

    def _auto_start_douyin_backend_on_launch(self) -> None:
        return

    def _start_douyin_backend_if_needed(self, show_error: bool) -> bool:
        del show_error
        return True

    def _wait_for_douyin_backend(self, timeout_ms: int = 12000) -> bool:
        del timeout_ms
        return True

    def _ensure_douyin_backend_ready(self, urls: List[str]) -> bool:
        del urls
        return True

    @run_in_thread
    def _do_douyin_login(self) -> None:
        try:
            ok = self.tiktok_service.login_to_douyin(timeout_seconds=240)
        except Exception as exc:  # noqa: BLE001
            self.root.after(0, lambda exc=exc: self._handle_douyin_login_error(exc))
            return
        self.root.after(0, lambda: self._handle_douyin_login_result(ok))

    def _handle_douyin_login_result(self, ok: bool) -> None:
        self._update_douyin_login_status()
        if ok:
            self.status_var.set("Douyin login session is ready.")
            messagebox.showinfo("Douyin Login", "Douyin login completed. You can fetch Douyin videos now.")
        else:
            self.status_var.set("Douyin login was not completed.")
            messagebox.showwarning(
                "Douyin Login",
                "The login window was closed before a valid Douyin session was detected.",
            )

    def _handle_douyin_login_error(self, exc: Exception) -> None:
        self._update_douyin_login_status()
        self.status_var.set("Could not start the Douyin login flow.")
        messagebox.showerror("Douyin Login", str(exc))

    @run_in_thread
    def _show_douyin_browser(self) -> None:
        try:
            self.tiktok_service.show_douyin_browser()
        except Exception as exc:  # noqa: BLE001
            self.root.after(0, lambda exc=exc: messagebox.showerror("Douyin Browser", str(exc)))
            return
        self.root.after(0, self._handle_douyin_browser_shown)

    def _handle_douyin_browser_shown(self) -> None:
        self.status_var.set("Douyin browser is now visible.")
        self.douyin_login_status_var.set("Douyin browser is visible. Log in there if needed.")

    def _parse_int_filter(self, value: str) -> int | None:
        text = value.strip()
        if not text:
            return None
        try:
            return max(0, int(float(text)))
        except Exception:
            return None

    def _parse_float_filter(self, value: str) -> float | None:
        text = value.strip()
        if not text:
            return None
        try:
            return max(0.0, float(text))
        except Exception:
            return None

    def _apply_video_controls(self, prefix: str, source_videos: List[Video]) -> List[Video]:
        sort_key = getattr(self, f"{prefix}_sort_key_var").get().strip() or "upload_time"
        sort_order = getattr(self, f"{prefix}_sort_order_var").get().strip() or "desc"
        min_views = self._parse_int_filter(getattr(self, f"{prefix}_min_views_var").get())
        min_likes = self._parse_int_filter(getattr(self, f"{prefix}_min_likes_var").get())
        min_comments = self._parse_int_filter(getattr(self, f"{prefix}_min_comments_var").get())
        min_trend = self._parse_float_filter(getattr(self, f"{prefix}_min_trend_var").get())
        max_age_days = self._parse_float_filter(getattr(self, f"{prefix}_max_age_days_var").get())
        is_douyin = self.platform_var.get().strip().lower() == "douyin"
        if is_douyin:
            min_views = None
            if sort_key == "view_count":
                sort_key = "like_count"

        filtered = []
        now = __import__("datetime").datetime.now()
        for video in source_videos:
            if min_views is not None and (video.view_count or 0) < min_views:
                continue
            if min_likes is not None and (video.like_count or 0) < min_likes:
                continue
            if min_comments is not None and (video.comment_count or 0) < min_comments:
                continue
            score = get_video_trend_score(video, now=now)
            if min_trend is not None and (score or 0.0) < min_trend:
                continue
            if max_age_days is not None:
                if not video.upload_time:
                    continue
                age_days = max((now - video.upload_time).total_seconds() / 86400.0, 0.0)
                if age_days > max_age_days:
                    continue
            filtered.append(video)

        def _sort_value(video: Video):
            if sort_key == "view_count":
                return video.view_count or 0
            if sort_key == "like_count":
                return video.like_count or 0
            if sort_key == "comment_count":
                return video.comment_count or 0
            if sort_key == "trend_score":
                return get_video_trend_score(video, now=now) or 0.0
            return video.upload_time or datetime.min

        reverse = sort_order != "asc"
        return sorted(filtered, key=_sort_value, reverse=reverse)

    def _apply_profile_filters(self) -> None:
        self.profile_videos = self._apply_video_controls("profile", self._profile_all_videos)
        self.profile_grid.set_videos(self.profile_videos, has_more=self._profile_has_more)
        self.profile_grid_container.refresh_viewport()
        self.status_var.set(f"Showing {len(self.profile_videos)} filtered profile video(s).")

    def _reset_profile_filters(self) -> None:
        for attr in ("min_views", "min_likes", "min_comments", "min_trend", "max_age_days"):
            getattr(self, f"profile_{attr}_var").set("")
        self.profile_sort_key_var.set("upload_time")
        self.profile_sort_order_var.set("desc")
        self._apply_profile_filters()

    def _apply_multi_filters(self) -> None:
        self.multi_videos = self._apply_video_controls("multi", self._multi_all_videos)
        self.multi_grid.set_videos(self.multi_videos, has_more=False)
        self.multi_grid_container.refresh_viewport()
        self.status_var.set(f"Showing {len(self.multi_videos)} filtered multi-link video(s).")

    def _apply_search_filters(self) -> None:
        self.search_videos = self._apply_video_controls("search", self._search_all_videos)
        self.search_grid.set_videos(self.search_videos, has_more=False)
        self.search_grid_container.refresh_viewport()
        self.status_var.set(f"Showing {len(self.search_videos)} filtered search video(s).")

    def _reset_multi_filters(self) -> None:
        for attr in ("min_views", "min_likes", "min_comments", "min_trend", "max_age_days"):
            getattr(self, f"multi_{attr}_var").set("")
        self.multi_sort_key_var.set("upload_time")
        self.multi_sort_order_var.set("desc")
        self._apply_multi_filters()

    def _reset_search_filters(self) -> None:
        for attr in ("min_views", "min_likes", "min_comments", "min_trend", "max_age_days"):
            getattr(self, f"search_{attr}_var").set("")
        self.search_sort_key_var.set("upload_time")
        self.search_sort_order_var.set("desc")
        self._apply_search_filters()

    def _center_popup(self, popup: tk.Toplevel, width: int, height: int) -> None:
        self.root.update_idletasks()
        root_x = self.root.winfo_rootx()
        root_y = self.root.winfo_rooty()
        root_w = max(self.root.winfo_width(), 1)
        root_h = max(self.root.winfo_height(), 1)
        x = root_x + max((root_w - width) // 2, 0)
        y = root_y + max((root_h - height) // 2, 0)
        popup.geometry(f"{width}x{height}+{x}+{y}")

    def _close_settings_popup(self) -> None:
        self._page_size = self._get_page_size()
        self._apply_trend_threshold()
        self._apply_douyin_backend_settings()
        self._save_session_cache()
        popup = self._settings_popup
        self._settings_popup = None
        if popup is None or not popup.winfo_exists():
            return
        try:
            popup.grab_release()
        except Exception:
            pass
        popup.destroy()

    def on_fetch_profile(self) -> None:
        is_douyin = self.platform_var.get().strip().lower() == "douyin"
        url = self.profile_url_input.get().strip()
        if not url:
            messagebox.showwarning("Missing URL", "Please enter one profile URL.")
            return
        if is_douyin and "douyin.com" not in url.lower() and "iesdouyin.com" not in url.lower() and "v.douyin.com" not in url.lower() and not url.startswith("MS4w"):
            messagebox.showwarning(
                "Wrong Platform",
                "Douyin mode is active.\n\nPlease enter a Douyin profile URL or sec_user_id.",
            )
            return
        if not is_douyin and "douyin.com" in url.lower():
            messagebox.showwarning(
                "Wrong Platform",
                "TikTok mode is active.\n\nPlease switch to Douyin mode for Douyin profiles.",
            )
            return
        if is_douyin and not self.tiktok_service.has_douyin_login_session():
            messagebox.showwarning(
                "Douyin Login Required",
                "Please open Settings and click 'Login to Douyin' before fetching Douyin profiles.",
            )
            return

        self._profile_url = url
        self._page_size = self._get_page_size()
        self.profile_videos = []
        self._profile_all_videos = []
        self._next_start = 1
        self._profile_has_more = False
        self._loading_profile_page = False
        self.profile_loading_more_var.set("")
        self.profile_grid.show_skeleton(self._page_size)
        self._refresh_load_more_button()

        self.profile = Profile(username=self._extract_username_from_url(url) if not is_douyin else url)
        self.profile_name_var.set(self.profile.username and f"@{self.profile.username}" or "Profile")
        self.profile_extra_var.set("Loading profile metadata...")
        self.profile_avatar_label.configure(image="", text="")
        self._profile_avatar_img = None

        self._set_loading(True)
        self.status_var.set(
            f"Fetching first {self._page_size} Douyin profile videos..."
            if is_douyin
            else f"Fetching first {self._page_size} profile videos..."
        )
        self._do_search_profile(url, self._next_start)

    def on_fetch_multi_links(self) -> None:
        urls = self._parse_multi_links()
        if not urls:
            platform_name = "Douyin" if self.platform_var.get().strip().lower() == "douyin" else "TikTok"
            messagebox.showwarning("Missing URLs", f"Please enter at least one {platform_name} video URL.")
            return

        is_douyin_mode = self.platform_var.get().strip().lower() == "douyin"
        wrong_urls = [
            url for url in urls
            if ("douyin.com" in url.lower() or "iesdouyin.com" in url.lower() or "v.douyin.com" in url.lower()) != is_douyin_mode
        ]
        if wrong_urls:
            expected = "Douyin" if is_douyin_mode else "TikTok"
            messagebox.showwarning(
                "Wrong Platform",
                f"{expected} mode is active.\n\nPlease paste only {expected} video links in this mode.",
            )
            return
        if is_douyin_mode and not self.tiktok_service.has_douyin_login_session():
            messagebox.showwarning(
                "Douyin Login Required",
                "Please open Settings and click 'Login to Douyin' before fetching Douyin videos.",
            )
            return

        self.multi_videos = []
        self._multi_all_videos = []
        self.multi_grid.show_skeleton(8)

        self._set_loading(True)
        self.status_var.set(
            "Fetching metadata from Douyin links..."
            if is_douyin_mode
            else "Fetching metadata from TikTok links..."
        )
        self._do_search_multi(urls)

    def on_search_videos(self) -> None:
        keyword = (self.search_keyword_var.get() or "").strip()
        if not keyword:
            messagebox.showwarning("Missing Keyword", "Please enter a keyword to search.")
            return

        platform = self.platform_var.get().strip().lower() or "tiktok"

        self.search_videos = []
        self._search_all_videos = []
        self._search_offset = 0
        self._search_has_more = False
        self.search_grid.show_skeleton(8)
        self._set_loading(True)
        platform_label = "Douyin" if platform == "douyin" else "TikTok"
        self.status_var.set(f"Searching {platform_label} for '{keyword}'...")
        self._do_search_keyword(keyword, platform, reset=True)

    def on_load_more_search(self) -> None:
        if not self._search_has_more:
            return
        keyword = (self.search_keyword_var.get() or "").strip()
        if not keyword:
            return
        platform = self.platform_var.get().strip().lower() or "tiktok"
        if platform != "douyin":
            messagebox.showinfo("Unavailable", "Load more is currently supported only for Douyin search.")
            return
        self.search_grid.show_tail_skeleton(4)
        self._set_loading(True)
        self.status_var.set("Loading more search results...")
        self._do_search_keyword(keyword, platform, reset=False)

    @run_in_thread
    def _do_search_keyword(self, keyword: str, platform: str, reset: bool) -> None:
        try:
            options = self._build_search_options()
            videos, has_more, next_offset = self.tiktok_service.search_by_keyword(
                keyword,
                platform=platform,
                offset=self._search_offset if not reset else 0,
                count=self._get_page_size(),
                options=options,
            )
        except Exception as exc:  # noqa: BLE001
            self.root.after(0, lambda exc=exc: self._handle_search_error(exc))
            return
        self.root.after(0, lambda: self._handle_search_success_search(videos, keyword, has_more, next_offset, reset))

    def _handle_search_success_search(
        self,
        videos: List[Video],
        keyword: str,
        has_more: bool,
        next_offset: int,
        reset: bool,
    ) -> None:
        self._set_loading(False)
        videos = self._dedupe_videos(videos)
        if not videos:
            self.status_var.set("No videos found.")
            if reset:
                self.search_grid.set_videos([], has_more=False)
            self.search_grid_container.scroll_to_top()
            self._search_has_more = False
            self._refresh_search_load_more_button()
            self._sync_platform_state()
            self._save_session_cache()
            return

        self.history_service.apply_status(videos)
        if reset:
            self._search_all_videos = self._dedupe_videos(list(videos))
        else:
            self._search_all_videos = self._dedupe_videos(self._search_all_videos + list(videos))
        self.search_videos = self._apply_video_controls("search", self._search_all_videos)
        if reset:
            self.search_grid.set_videos(self.search_videos, has_more=False)
            self.search_grid_container.scroll_to_top()
        else:
            self.search_grid.set_videos(self.search_videos, has_more=False)
        self.search_grid_container.refresh_viewport()
        self._search_has_more = bool(has_more)
        self._search_offset = int(next_offset or 0)
        self._refresh_search_load_more_button()
        self.status_var.set(
            f"Found {len(self._search_all_videos)} video(s) for '{keyword}'."
            + (" More available." if self._search_has_more else "")
        )
        self._sync_platform_state()
        self._persist_platform_workspace_cache(self.platform_var.get())
        self._save_session_cache()

    def _build_search_url(self, keyword: str) -> str:
        term = quote(keyword.strip())
        return f"https://www.tiktok.com/search?q={term}"

    def _build_search_options(self) -> dict[str, Any]:
        sort_map = {
            "综合排序": 0,
            "最新发布": 2,
            "最多点赞": 1,
        }
        time_map = {
            "不限": 0,
            "一天内": 1,
            "一周内": 7,
            "半年内": 180,
        }
        duration_map = {
            "不限": "",
            "< 1分钟": "0-1",
            "1–5分钟": "1-5",
        }
        scope_map = {
            "不限": "",
            "关注的人": "follow",
            "最近看过": "history",
            "还未看过": "unwatch",
        }
        return {
            "sort_label": self.search_sort_var.get().strip(),
            "time_label": self.search_time_var.get().strip(),
            "duration_label": self.search_duration_var.get().strip(),
            "scope_label": self.search_scope_var.get().strip(),
            "sort_type": sort_map.get(self.search_sort_var.get().strip(), 0),
            "publish_time": time_map.get(self.search_time_var.get().strip(), 0),
            "duration": duration_map.get(self.search_duration_var.get().strip(), ""),
            "scope": scope_map.get(self.search_scope_var.get().strip(), ""),
        }

    def _refresh_search_load_more_button(self) -> None:
        if not hasattr(self, "search_load_more_btn"):
            return
        state = tk.NORMAL if self._search_has_more else tk.DISABLED
        self.search_load_more_btn.configure(state=state)

    @run_in_thread
    def _do_search_multi(self, urls: List[str]) -> None:
        try:
            videos = self.tiktok_service.fetch_videos(urls)
        except Exception as exc:  # noqa: BLE001
            self.root.after(0, lambda exc=exc: self._handle_search_error(exc))
            return

        self.root.after(0, lambda: self._handle_search_success_multi(videos))

    @run_in_thread
    def _do_search_profile(self, url: str, start: int) -> None:
        try:
            videos, has_more, profile = self.tiktok_service.fetch_videos_paged(url, start, self._page_size)
        except Exception as exc:  # noqa: BLE001
            self.root.after(0, lambda exc=exc: self._handle_search_error(exc))
            return

        self.root.after(0, lambda: self._handle_search_success_profile(url, start, videos, has_more, profile))

    def _handle_search_error(self, exc: Exception) -> None:
        self._loading_profile_page = False
        self.profile_grid.clear_tail_skeleton()
        self.profile_loading_more_var.set("")
        self._refresh_load_more_button()
        self._set_loading(False)
        if self._active_mode() == "search":
            self.search_grid.clear_tail_skeleton()
            self._search_has_more = False
            self._refresh_search_load_more_button()
        messagebox.showerror("Request Failed", f"Could not fetch TikTok / Douyin data.\n\nDetails: {exc}")

    def _handle_search_success_multi(self, videos: List[Video]) -> None:
        self._set_loading(False)
        videos = self._dedupe_videos(videos)
        if not videos:
            self.status_var.set("No videos found.")
            self.multi_grid.set_videos([], has_more=False)
            self.multi_grid_container.scroll_to_top()
            self._sync_platform_state()
            self._save_session_cache()
            return

        self.history_service.apply_status(videos)
        self._multi_all_videos = self._dedupe_videos(list(videos))
        self.multi_videos = self._apply_video_controls("multi", self._multi_all_videos)
        self.multi_grid.set_videos(self.multi_videos, has_more=False)
        self.multi_grid_container.scroll_to_top()
        self.multi_grid_container.refresh_viewport()
        self.status_var.set(f"Found {len(videos)} videos from multiple links.")
        self._sync_platform_state()
        self._persist_platform_workspace_cache(self.platform_var.get())
        self._save_session_cache()

    def _handle_search_success_profile(self, url: str, start: int, videos: List[Video], has_more: bool, profile: Profile | None) -> None:
        self._loading_profile_page = False
        self.profile_grid.clear_tail_skeleton()
        self.profile_loading_more_var.set("")
        self._refresh_load_more_button()
        self._set_loading(False)
        videos = self._dedupe_videos(videos)
        if not videos and start == 1:
            self.status_var.set("No videos found.")
            self.profile_grid.set_videos([], has_more=False)
            self.profile_grid_container.scroll_to_top()
            self._sync_platform_state()
            self._save_session_cache()
            return

        if start == 1:
            if profile is not None:
                self.profile = profile
                if not self.profile.username and self._profile_url:
                    self.profile.username = self._extract_username_from_url(self._profile_url)
            elif self.profile is None and self._profile_url:
                self.profile = Profile(username=self._extract_username_from_url(self._profile_url))
            self._update_profile_header()

        self.history_service.apply_status(videos)

        if start == 1:
            self._profile_all_videos = self._dedupe_videos(list(videos))
            self.profile_videos = self._apply_video_controls("profile", self._profile_all_videos)
            self.profile_grid.set_videos(self.profile_videos, has_more=has_more)
            self.profile_grid_container.scroll_to_top()
            self.profile_grid_container.refresh_viewport()
        else:
            self._profile_all_videos = self._dedupe_videos(self._profile_all_videos + list(videos))
            self.profile_videos = self._apply_video_controls("profile", self._profile_all_videos)
            self.profile_grid.set_videos(self.profile_videos, has_more=has_more)
            self.profile_grid_container.refresh_viewport()

        known_total = None
        if self.profile and self.profile.video_count is not None:
            known_total = max(int(self.profile.video_count), len(self._profile_all_videos))

        self._profile_has_more = has_more
        if known_total is not None:
            self._profile_has_more = len(self._profile_all_videos) < known_total

        if self._profile_has_more:
            self._next_start = start + len(videos)
        else:
            self._next_start = max(1, len(self._profile_all_videos) + 1)
        self._refresh_load_more_button()

        total_text = str(known_total) if known_total is not None else ("?" if self._profile_has_more else str(len(self._profile_all_videos)))
        shown_count = len(self.profile_videos)
        self.status_var.set(f"Loaded {shown_count} profile video(s). Showing {shown_count}/{total_text}.")
        self._sync_platform_state()
        self._persist_platform_workspace_cache(self.platform_var.get())
        self._save_session_cache()

    def on_load_more_profile(self) -> None:
        if not self._profile_url:
            return
        if not self._profile_has_more:
            return
        if getattr(self, "_loading_profile_page", False):
            return

        self._loading_profile_page = True
        self.profile_grid.show_tail_skeleton(max(2, min(self._page_size // 2, 12)))
        self.profile_loading_more_var.set("Loading more videos...")
        self._refresh_load_more_button()
        self._set_loading(True)
        self.status_var.set(f"Loading {self._page_size} more profile videos...")
        self._do_search_profile(self._profile_url, self._next_start)

    def _update_profile_header(self) -> None:
        if not self.profile:
            self.profile_name_var.set("")
            self.profile_extra_var.set("")
            self.profile_avatar_label.configure(image="", text="")
            self._profile_avatar_img = None
            return

        p = self.profile
        self.profile_name_var.set(p.display_name or (p.username and f"@{p.username}") or "Profile")

        parts: list[str] = []
        if p.username:
            parts.append(f"@{p.username}")
        if p.follower_count is not None:
            parts.append(f"Followers: {p.follower_count:,}")
        if p.following_count is not None:
            parts.append(f"Following: {p.following_count:,}")
        if p.like_count is not None:
            parts.append(f"Likes: {p.like_count:,}")
        if p.video_count is not None:
            parts.append(f"Videos: {p.video_count:,}")

        self.profile_extra_var.set("  |  ".join(parts) if parts else "Profile metadata is limited, but videos can still be fetched.")

        if p.avatar_url:
            self._load_profile_avatar_async(p.avatar_url)

    @staticmethod
    def _extract_username_from_url(url: str) -> str:
        try:
            at = url.split("@", 1)[1]
        except Exception:
            return ""
        username = at.split("/", 1)[0]
        username = username.split("?", 1)[0]
        return username.strip()

    @staticmethod
    def _safe_folder_name(name: str) -> str:
        invalid = '<>:"/\\|?*'
        cleaned = "".join("_" if c in invalid else c for c in name).strip().strip(".")
        return cleaned or "profile"

    def _load_profile_avatar_async(self, url: str) -> None:
        import io
        import threading

        import requests
        from PIL import Image, ImageTk

        def _load() -> None:
            try:
                resp = requests.get(url, timeout=10)
                resp.raise_for_status()
                img = Image.open(io.BytesIO(resp.content))
                img = img.resize((64, 64), Image.LANCZOS)
                photo = ImageTk.PhotoImage(img)
            except Exception:
                return

            def _apply() -> None:
                self._profile_avatar_img = photo
                self.profile_avatar_label.configure(image=self._profile_avatar_img, text="")

            self.root.after(0, _apply)

        threading.Thread(target=_load, daemon=True).start()

    def _open_video_details(self, video: Video) -> None:
        if self._video_popup is not None and self._video_popup.winfo_exists():
            self._close_video_popup(self._video_popup)

        popup = tk.Toplevel(self.root)
        popup.title("Video Details")
        popup.configure(bg="#eef2f9")
        popup.transient(self.root)
        self._center_popup(popup, 980, 760)
        popup.grab_set()
        popup.focus_force()
        self._video_popup = popup

        main = ttk.Frame(popup, padding=14, style="App.TFrame")
        main.pack(fill=tk.BOTH, expand=True)

        top = ttk.Frame(main, style="Panel.TFrame", padding=12)
        top.pack(fill=tk.BOTH, expand=False)

        preview_holder = tk.Frame(top, bg="#121826", width=320, height=560, highlightthickness=1, highlightbackground="#2b3345")
        preview_holder.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 14))
        preview_holder.pack_propagate(False)

        preview_label = ttk.Label(preview_holder, style="VideoThumb.TLabel")
        preview_label.place(relx=0.0, rely=0.0, relwidth=1.0, relheight=1.0)
        player_host = tk.Frame(preview_holder, bg="#000000")
        player_host.place(relx=0.0, rely=0.0, relwidth=1.0, relheight=1.0)
        player_host.lower()
        popup._detail_preview_image = None  # type: ignore[attr-defined]
        popup._vlc_instance = None  # type: ignore[attr-defined]
        popup._vlc_player = None  # type: ignore[attr-defined]
        popup._vlc_media = None  # type: ignore[attr-defined]
        popup._preview_label = preview_label  # type: ignore[attr-defined]
        popup._player_host = player_host  # type: ignore[attr-defined]
        popup._temp_media_path = None  # type: ignore[attr-defined]
        popup._temp_media_dir = None  # type: ignore[attr-defined]
        popup._prepared_video_id = None  # type: ignore[attr-defined]
        popup._volume_var = tk.IntVar(value=100)  # type: ignore[attr-defined]
        popup._current_media_source = None  # type: ignore[attr-defined]
        popup._playback_state = "idle"  # type: ignore[attr-defined]
        popup._video = video  # type: ignore[attr-defined]

        right = ttk.Frame(top, style="Panel.TFrame")
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        title_text = video.title or "Untitled"
        ttk.Label(
            right,
            text=title_text,
            background="#ffffff",
            foreground="#1d2a44",
            font=("Segoe UI", 14, "bold"),
            wraplength=560,
            justify=tk.LEFT,
        ).pack(anchor="w")

        is_douyin = (video.platform or "").lower() == "douyin"
        stats_rows = [
            ("Likes", self._format_optional_count(video.like_count)),
            ("Comments", self._format_optional_count(video.comment_count)),
            ("Shares", self._format_optional_count(video.share_count)),
            ("Upload date", video.upload_time.strftime("%d-%m-%Y") if video.upload_time else "-"),
            ("Duration", format_duration(video.duration) or "-"),
            ("Author", video.author or video.uploader or "-"),
            ("Music", video.music_title or "-"),
            ("Downloaded", "Yes" if video.is_downloaded else "No"),
        ]
        if not is_douyin:
            stats_rows.insert(0, ("Views", self._format_optional_count(video.view_count)))

        if video.bookmark_count is not None:
            stats_rows.insert(4, ("Saves", self._format_optional_count(video.bookmark_count)))

        for label, value in stats_rows:
            row = ttk.Frame(right, style="Panel.TFrame")
            row.pack(fill=tk.X, pady=(8 if label in {"Views", "Likes"} else 4, 0))
            ttk.Label(row, text=f"{label}:", style="Muted.TLabel").pack(side=tk.LEFT)
            ttk.Label(
                row,
                text=value,
                background="#ffffff",
                foreground="#1d2a44",
                font=("Segoe UI", 10, "bold"),
            ).pack(side=tk.LEFT, padx=(8, 0))

        if video.image_urls and not video.media_url:
            row = ttk.Frame(right, style="Panel.TFrame")
            row.pack(fill=tk.X, pady=(4, 0))
            ttk.Label(row, text="Type:", style="Muted.TLabel").pack(side=tk.LEFT)
            ttk.Label(
                row,
                text=f"Image gallery ({len(video.image_urls)} images)",
                background="#ffffff",
                foreground="#1d2a44",
                font=("Segoe UI", 10, "bold"),
            ).pack(side=tk.LEFT, padx=(8, 0))

        if video.tags:
            tags_row = ttk.Frame(right, style="Panel.TFrame")
            tags_row.pack(fill=tk.X, pady=(8, 0))
            ttk.Label(tags_row, text="Hashtags:", style="Muted.TLabel").pack(side=tk.LEFT)
            ttk.Label(
                tags_row,
                text=" ".join(f"#{tag.lstrip('#')}" for tag in video.tags),
                background="#ffffff",
                foreground="#1d2a44",
                font=("Segoe UI", 10, "bold"),
                wraplength=520,
                justify=tk.LEFT,
            ).pack(side=tk.LEFT, padx=(8, 0))

        if video.description:
            ttk.Label(right, text="Description", style="Muted.TLabel").pack(anchor="w", pady=(10, 4))
            description = tk.Text(right, height=8, wrap="word", font=("Segoe UI", 10), relief="solid", borderwidth=1)
            description.pack(fill=tk.BOTH, expand=True)
            description.insert("1.0", video.description)
            description.configure(state="disabled")

        actions = ttk.Frame(main, style="Panel.TFrame", padding=12)
        actions.pack(fill=tk.X, pady=(10, 0))

        watch_status_var = tk.StringVar(value="Prepare to watch this video.")
        ttk.Label(actions, textvariable=watch_status_var, style="Muted.TLabel").pack(side=tk.LEFT)

        controls_wrap = ttk.Frame(actions, style="Panel.TFrame")
        controls_wrap.pack(side=tk.RIGHT)

        volume_box = ttk.Frame(controls_wrap, style="Panel.TFrame")
        volume_box.pack(side=tk.RIGHT, padx=(8, 0))

        ttk.Label(volume_box, text="Volume", style="Muted.TLabel").pack(side=tk.LEFT, padx=(0, 6))
        volume_scale = tk.Scale(
            volume_box,
            from_=0,
            to=150,
            orient=tk.HORIZONTAL,
            showvalue=False,
            resolution=5,
            length=120,
            bg="#ffffff",
            fg="#1d2a44",
            troughcolor="#d9e6ff",
            highlightthickness=0,
            bd=0,
            variable=popup._volume_var,  # type: ignore[attr-defined]
            command=lambda _value: self._set_popup_volume(popup, watch_status_var),
        )
        volume_scale.pack(side=tk.LEFT)

        volume_value_label = ttk.Label(volume_box, textvariable=popup._volume_var, style="Muted.TLabel", width=4)  # type: ignore[attr-defined]
        volume_value_label.pack(side=tk.LEFT, padx=(4, 0))

        controls_box = ttk.Frame(controls_wrap, style="Panel.TFrame")
        controls_box.pack(side=tk.RIGHT)

        for idx, width in enumerate((92, 48, 48, 48, 112)):
            controls_box.grid_columnconfigure(idx, minsize=width)

        watch_slot = ttk.Frame(controls_box, style="Panel.TFrame", width=92, height=34)
        watch_slot.grid(row=0, column=0, padx=(0, 8))
        watch_slot.grid_propagate(False)
        watch_btn = ttk.Button(watch_slot, text="Prepare", style="Primary.TButton")
        watch_btn.configure(command=lambda: self._watch_video(video, popup, watch_status_var, watch_btn))
        watch_btn.place(relx=0.0, rely=0.0, relwidth=1.0, relheight=1.0)

        pause_slot = ttk.Frame(controls_box, style="Panel.TFrame", width=48, height=34)
        pause_slot.grid(row=0, column=1, padx=(0, 8))
        pause_slot.grid_propagate(False)
        pause_btn = ttk.Button(pause_slot, text="⏸", width=4, style="Secondary.TButton", command=lambda: self._pause_video_playback(popup, watch_status_var))
        pause_btn.place(relx=0.0, rely=0.0, relwidth=1.0, relheight=1.0)

        resume_slot = ttk.Frame(controls_box, style="Panel.TFrame", width=48, height=34)
        resume_slot.grid(row=0, column=2, padx=(0, 8))
        resume_slot.grid_propagate(False)
        resume_btn = ttk.Button(resume_slot, text="⏯", width=4, style="Secondary.TButton", command=lambda: self._resume_video_playback(popup, watch_status_var))
        resume_btn.place(relx=0.0, rely=0.0, relwidth=1.0, relheight=1.0)

        replay_slot = ttk.Frame(controls_box, style="Panel.TFrame", width=48, height=34)
        replay_slot.grid(row=0, column=3, padx=(0, 8))
        replay_slot.grid_propagate(False)
        replay_btn = ttk.Button(replay_slot, text="🔁", width=4, style="Secondary.TButton", command=lambda: self._replay_video_playback(popup, video, watch_status_var))
        replay_btn.place(relx=0.0, rely=0.0, relwidth=1.0, relheight=1.0)

        open_link_slot = ttk.Frame(controls_box, style="Panel.TFrame", width=112, height=34)
        open_link_slot.grid(row=0, column=4)
        open_link_slot.grid_propagate(False)
        open_link_btn = ttk.Button(open_link_slot, text="🔗", style="Secondary.TButton", command=lambda: self._open_video_link(video))
        open_link_btn.place(relx=0.0, rely=0.0, relwidth=1.0, relheight=1.0)

        popup._watch_btn = watch_btn  # type: ignore[attr-defined]
        popup._pause_btn = pause_btn  # type: ignore[attr-defined]
        popup._resume_btn = resume_btn  # type: ignore[attr-defined]
        popup._replay_btn = replay_btn  # type: ignore[attr-defined]

        self._update_popup_controls(popup)

        self._load_video_preview_async(video, preview_label, popup)
        popup.protocol("WM_DELETE_WINDOW", lambda: self._close_video_popup(popup))

    def _load_video_preview_async(self, video: Video, label: ttk.Label, popup: tk.Toplevel) -> None:
        import io
        import threading

        import requests
        from PIL import Image, ImageDraw, ImageTk

        if not video.thumbnail_url:
            return

        def _load() -> None:
            try:
                resp = requests.get(video.thumbnail_url, timeout=10)
                resp.raise_for_status()
                img = Image.open(io.BytesIO(resp.content)).convert("RGB")
                target_ratio = 9 / 16
                w, h = img.size
                current_ratio = w / h if h else target_ratio
                if current_ratio > target_ratio:
                    new_w = int(h * target_ratio)
                    left = (w - new_w) // 2
                    img = img.crop((left, 0, left + new_w, h))
                elif current_ratio < target_ratio:
                    new_h = int(w / target_ratio)
                    top = (h - new_h) // 2
                    img = img.crop((0, top, w, top + new_h))

                img = img.resize((320, 560))
                rgba = img.convert("RGBA")
                mask = Image.new("L", rgba.size, 0)
                draw = ImageDraw.Draw(mask)
                draw.rounded_rectangle((0, 0, rgba.size[0] - 1, rgba.size[1] - 1), radius=18, fill=255)
                rgba.putalpha(mask)
                photo = ImageTk.PhotoImage(rgba)
            except Exception:
                return

            def _apply() -> None:
                if not popup.winfo_exists():
                    return
                popup._detail_preview_image = photo  # type: ignore[attr-defined]
                label.configure(image=photo)

            self.root.after(0, _apply)

        threading.Thread(target=_load, daemon=True).start()

    def _watch_video(
        self,
        video: Video,
        popup: tk.Toplevel,
        status_var: tk.StringVar,
        button: ttk.Button | None = None,
    ) -> None:
        if vlc is None:
            status_var.set("python-vlc is not available. Install dependency and ensure VLC/libvlc is installed.")
            return
        if video.image_urls and not video.media_url:
            status_var.set("This Douyin post is an image gallery, so it can be downloaded but not played as a video.")
            return

        popup._playback_state = "preparing"  # type: ignore[attr-defined]
        self._update_popup_controls(popup)
        if button is not None:
            button.configure(state="disabled")
        status_var.set("Preparing local preview for playback...")

        def _resolve() -> None:
            source = None
            error_message = ""
            if video.downloaded_path:
                local_file = Path(video.downloaded_path)
                if local_file.exists():
                    source = str(local_file)
            if not source:
                cached_path = getattr(popup, "_temp_media_path", None)
                prepared_video_id = getattr(popup, "_prepared_video_id", None)
                if isinstance(cached_path, str) and prepared_video_id == video.id:
                    cached_file = Path(cached_path)
                    if cached_file.exists():
                        source = str(cached_file)
            if not source:
                temp_dir = Path(tempfile.mkdtemp(prefix="dyd_preview_"))
                try:
                    old_temp_dir = getattr(popup, "_temp_media_dir", None)
                    if isinstance(old_temp_dir, str) and old_temp_dir:
                        shutil.rmtree(old_temp_dir, ignore_errors=True)
                    source = str(self.tiktok_service.download_video(video, temp_dir))
                    popup._temp_media_path = source  # type: ignore[attr-defined]
                    popup._temp_media_dir = str(temp_dir)  # type: ignore[attr-defined]
                    popup._prepared_video_id = video.id  # type: ignore[attr-defined]
                except Exception as exc:
                    error_message = str(exc or "")
                    try:
                        shutil.rmtree(temp_dir, ignore_errors=True)
                    except Exception:
                        pass
                    source = None

            def _apply() -> None:
                if button is not None:
                    button.configure(state="normal")
                if not popup.winfo_exists():
                    return
                if not source:
                    popup._playback_state = "idle"  # type: ignore[attr-defined]
                    self._update_popup_controls(popup)
                    status_var.set(error_message or "Could not prepare this video.")
                    return
                source_path = Path(source)
                if not source_path.exists():
                    popup._playback_state = "idle"  # type: ignore[attr-defined]
                    self._update_popup_controls(popup)
                    status_var.set("The prepared media file was not created.")
                    return
                if source_path.is_file() and source_path.stat().st_size == 0:
                    popup._playback_state = "idle"  # type: ignore[attr-defined]
                    self._update_popup_controls(popup)
                    status_var.set("The prepared media file is empty.")
                    return
                popup._prepared_video_id = video.id  # type: ignore[attr-defined]
                self._play_video_in_popup(popup, video, source, status_var)

            self.root.after(0, _apply)

        import threading

        threading.Thread(target=_resolve, daemon=True).start()

    def _watch_douyin_in_browser(
        self,
        video: Video,
        popup: tk.Toplevel,
        status_var: tk.StringVar,
        button: ttk.Button | None = None,
    ) -> None:
        popup._playback_state = "preparing"  # type: ignore[attr-defined]
        self._update_popup_controls(popup)
        if button is not None:
            button.configure(state="disabled")
        status_var.set("Opening this Douyin post in the logged-in browser session...")

        def _open() -> None:
            error_message = ""
            try:
                self.tiktok_service.open_douyin_video_in_browser(video.url or "")
            except Exception as exc:  # noqa: BLE001
                error_message = str(exc or "")

            def _apply() -> None:
                if button is not None:
                    button.configure(state="normal")
                if not popup.winfo_exists():
                    return
                if error_message:
                    popup._playback_state = "idle"  # type: ignore[attr-defined]
                    popup._current_media_source = None  # type: ignore[attr-defined]
                    self._update_popup_controls(popup)
                    status_var.set(error_message)
                    return
                popup._playback_state = "playing"  # type: ignore[attr-defined]
                popup._current_media_source = video.url or "douyin-browser"  # type: ignore[attr-defined]
                self._update_popup_controls(popup)
                status_var.set("Opened directly in the Douyin browser session.")

            self.root.after(0, _apply)

        import threading

        threading.Thread(target=_open, daemon=True).start()

    def _play_video_in_popup(
        self,
        popup: tk.Toplevel,
        video: Video,
        source: str,
        status_var: tk.StringVar,
    ) -> None:
        if vlc is None:
            status_var.set("python-vlc is not available.")
            return

        try:
            if getattr(popup, "_vlc_player", None) is None:
                instance = self._create_vlc_instance()
                if instance is None:
                    popup._playback_state = "idle"  # type: ignore[attr-defined]
                    self._update_popup_controls(popup)
                    status_var.set("Could not initialize VLC. Please check VLC/libvlc installation on this machine.")
                    return
                player = instance.media_player_new()
                popup._vlc_instance = instance  # type: ignore[attr-defined]
                popup._vlc_player = player  # type: ignore[attr-defined]
            else:
                player = popup._vlc_player  # type: ignore[attr-defined]
                try:
                    player.stop()
                except Exception:
                    pass

            host = popup._player_host  # type: ignore[attr-defined]
            popup.update_idletasks()
            host.update_idletasks()
            media = popup._vlc_instance.media_new(source)  # type: ignore[attr-defined]
            self._apply_vlc_media_options(media)
            popup._vlc_media = media  # type: ignore[attr-defined]
            player.set_media(media)

            handle = host.winfo_id()
            if os.name == "nt":
                player.set_hwnd(handle)
            else:
                try:
                    player.set_xwindow(handle)
                except Exception:
                    pass

            static_preview_mode = self._should_keep_static_preview(video)
            if static_preview_mode:
                self._show_preview_surface(popup)
            else:
                self._show_player_surface(popup)

            def _start_playback() -> None:
                try:
                    player.play()
                    self._apply_popup_volume(popup)
                    popup._playback_state = "playing"  # type: ignore[attr-defined]
                    self._update_popup_controls(popup)
                    if static_preview_mode:
                        status_var.set("Playing audio while keeping the original image preview.")
                    else:
                        status_var.set("Playing video inside popup.")
                except Exception as exc:
                    popup._playback_state = "idle"  # type: ignore[attr-defined]
                    self._update_popup_controls(popup)
                    status_var.set(f"Could not start VLC playback: {exc}")

            self.root.after(120, _start_playback)
            popup._current_media_source = source  # type: ignore[attr-defined]
        except Exception as exc:
            popup._playback_state = "idle"  # type: ignore[attr-defined]
            self._update_popup_controls(popup)
            status_var.set(f"Could not start VLC playback: {exc}")

    @staticmethod
    def _apply_vlc_media_options(media) -> None:
        media_options = (
            ":avcodec-hw=none",
            ":codec=avcodec",
            ":vout=wingdi",
            ":network-caching=1500",
            ":file-caching=1000",
        )
        for option in media_options:
            try:
                media.add_option(option)
            except Exception:
                continue

    def _create_vlc_instance(self):
        if vlc is None:
            return None
        option_sets = [
            (
                "--no-video-title-show",
                "--avcodec-hw=none",
                "--network-caching=1500",
                "--file-caching=1000",
                "--vout=wingdi",
            ),
            (
                "--no-video-title-show",
                "--avcodec-hw=none",
                "--network-caching=1500",
                "--file-caching=1000",
            ),
            ("--no-video-title-show",),
            tuple(),
        ]
        for options in option_sets:
            try:
                instance = vlc.Instance(*options)
                if instance is not None:
                    return instance
            except Exception:
                continue
        return None

    def _show_player_surface(self, popup: tk.Toplevel) -> None:
        host = popup._player_host  # type: ignore[attr-defined]
        preview = popup._preview_label  # type: ignore[attr-defined]
        preview.place_forget()
        host.place(relx=0.0, rely=0.0, relwidth=1.0, relheight=1.0)
        host.lift()
        host.update_idletasks()

    def _show_preview_surface(self, popup: tk.Toplevel) -> None:
        host = popup._player_host  # type: ignore[attr-defined]
        preview = popup._preview_label  # type: ignore[attr-defined]
        host.lower()
        preview.place(relx=0.0, rely=0.0, relwidth=1.0, relheight=1.0)
        preview.lift()

    @staticmethod
    def _should_keep_static_preview(video: Video) -> bool:
        return (video.platform or "").lower() == "douyin" and bool(video.image_urls)

    def _pause_video_playback(self, popup: tk.Toplevel, status_var: tk.StringVar) -> None:
        player = getattr(popup, "_vlc_player", None)
        if player is None:
            status_var.set("No active VLC player.")
            return
        try:
            player.pause()
            popup._playback_state = "paused"  # type: ignore[attr-defined]
            self._update_popup_controls(popup)
            status_var.set("Playback paused.")
        except Exception as exc:
            status_var.set(f"Could not pause playback: {exc}")

    def _resume_video_playback(self, popup: tk.Toplevel, status_var: tk.StringVar) -> None:
        player = getattr(popup, "_vlc_player", None)
        if player is None:
            status_var.set("No active VLC player.")
            return
        try:
            player.play()
            self._apply_popup_volume(popup)
            popup._playback_state = "playing"  # type: ignore[attr-defined]
            self._update_popup_controls(popup)
            status_var.set("Playback resumed.")
        except Exception as exc:
            status_var.set(f"Could not resume playback: {exc}")

    def _replay_video_playback(self, popup: tk.Toplevel, video: Video, status_var: tk.StringVar) -> None:
        source = getattr(popup, "_current_media_source", None)
        if isinstance(source, str) and source:
            status_var.set("Replaying video from the beginning...")
            popup._playback_state = "preparing"  # type: ignore[attr-defined]
            self._update_popup_controls(popup)
            self._play_video_in_popup(popup, video, source, status_var)
            return
        self._watch_video(video, popup, status_var)

    def _stop_video_playback(self, popup: tk.Toplevel, status_var: tk.StringVar) -> None:
        try:
            self._teardown_vlc_player(popup)
            popup._current_media_source = None  # type: ignore[attr-defined]
            popup._playback_state = "idle"  # type: ignore[attr-defined]
            self._update_popup_controls(popup)
            self._show_preview_surface(popup)
            status_var.set("Playback stopped.")
        except Exception as exc:
            status_var.set(f"Could not stop playback: {exc}")

    def _set_popup_volume(self, popup: tk.Toplevel, status_var: tk.StringVar) -> None:
        try:
            self._apply_popup_volume(popup)
        except Exception as exc:
            status_var.set(f"Could not change volume: {exc}")

    @staticmethod
    def _format_optional_count(value: int | None) -> str:
        if value is None:
            return "-"
        return format_count(value)

    def _update_popup_controls(self, popup: tk.Toplevel) -> None:
        watch_btn = getattr(popup, "_watch_btn", None)
        pause_btn = getattr(popup, "_pause_btn", None)
        resume_btn = getattr(popup, "_resume_btn", None)
        replay_btn = getattr(popup, "_replay_btn", None)
        state = getattr(popup, "_playback_state", "idle")
        has_media = bool(getattr(popup, "_current_media_source", None))

        if watch_btn is not None:
            watch_state = "disabled" if state == "preparing" or has_media else "normal"
            watch_btn.configure(state=watch_state)

        if pause_btn is not None:
            pause_btn.configure(state="normal" if state == "playing" else "disabled")

        if resume_btn is not None:
            resume_btn.configure(state="normal" if state == "paused" else "disabled")

        if replay_btn is not None:
            replay_btn.configure(state="normal" if has_media else "disabled")

    def _save_session_cache(self) -> None:
        self._sync_platform_state()
        self._persist_platform_workspace_cache(self.platform_var.get().strip().lower() or "tiktok")
        payload = {
            "platform": self.platform_var.get().strip() or "tiktok",
            "platform_output_dirs": {
                name: str(state["output_dir"])
                for name, state in self._platform_states.items()
            },
            "page_size": self._page_size,
            "trend_threshold": self._get_trend_threshold(),
            "douyin_backend_enabled": bool(self.douyin_backend_enabled_var.get()),
            "douyin_backend_autostart": bool(self.douyin_backend_autostart_var.get()),
            "douyin_backend_url": self.douyin_backend_url_var.get().strip(),
            "douyin_backend_token": self.douyin_backend_token_var.get().strip(),
            "douyin_backend_endpoint": self.douyin_backend_endpoint_var.get().strip(),
            "douyin_backend_command": self.douyin_backend_command_var.get().strip(),
            "platform_sessions": {
                name: self._build_platform_session_payload(state)
                for name, state in self._platform_states.items()
            },
        }
        try:
            self.session_cache.save(payload)
        except Exception:
            pass
        for name, state in self._platform_states.items():
            cache = self._workspace_caches.get(name)
            if cache is None:
                continue
            try:
                cache.save(self._build_platform_session_payload(state))
            except Exception:
                continue

    @staticmethod
    def _build_platform_session_payload(state: dict[str, Any]) -> dict[str, Any]:
        return {
            "profile_url": state.get("profile_url") or "",
            "profile": state.get("profile"),
            "profile_videos": state.get("profile_videos") or [],
            "multi_links": state.get("multi_links") or [],
            "multi_videos": state.get("multi_videos") or [],
            "bookmarked_videos": state.get("bookmarked_videos") or [],
            "search_keyword": state.get("search_keyword") or "",
            "search_videos": state.get("search_videos") or [],
            "search_options": state.get("search_options") or {},
            "search_offset": int(state.get("search_offset") or 0),
            "search_has_more": bool(state.get("search_has_more")),
            "active_tab": state.get("active_tab") or "profile",
            "profile_has_more": bool(state.get("profile_has_more")),
            "next_start": int(state.get("next_start") or 1),
        }

    def _persist_platform_workspace_cache(self, platform: str) -> None:
        platform = "douyin" if str(platform).strip().lower() == "douyin" else "tiktok"
        state = self._platform_states.get(platform)
        cache = self._workspace_caches.get(platform)
        if state is None or cache is None:
            return
        try:
            cache.save(self._build_platform_session_payload(state))
        except Exception:
            pass

    def _restore_last_session(self) -> None:
        data = self.session_cache.load()
        try:
            platform = str(data.get("platform") or "tiktok").strip().lower()
            output_dirs = data.get("platform_output_dirs") or {}
            if isinstance(output_dirs, dict):
                for name, folder in output_dirs.items():
                    if name in self._platform_states and isinstance(folder, str) and folder.strip():
                        path = Path(folder)
                        path.mkdir(parents=True, exist_ok=True)
                        self._platform_states[name]["output_dir"] = path
            platform_sessions = data.get("platform_sessions") or {}
            if isinstance(platform_sessions, dict):
                for name, session in platform_sessions.items():
                    if name in self._platform_states and isinstance(session, dict):
                        self._platform_states[name]["profile_url"] = str(session.get("profile_url") or "")
                        self._platform_states[name]["profile"] = session.get("profile") if isinstance(session.get("profile"), dict) else None
                        self._platform_states[name]["profile_videos"] = session.get("profile_videos") if isinstance(session.get("profile_videos"), list) else []
                        self._platform_states[name]["multi_links"] = session.get("multi_links") if isinstance(session.get("multi_links"), list) else []
                        self._platform_states[name]["multi_videos"] = session.get("multi_videos") if isinstance(session.get("multi_videos"), list) else []
                        self._platform_states[name]["bookmarked_videos"] = session.get("bookmarked_videos") if isinstance(session.get("bookmarked_videos"), list) else []
                        self._platform_states[name]["search_keyword"] = str(session.get("search_keyword") or "")
                        self._platform_states[name]["search_videos"] = session.get("search_videos") if isinstance(session.get("search_videos"), list) else []
                        self._platform_states[name]["search_options"] = session.get("search_options") if isinstance(session.get("search_options"), dict) else {}
                        self._platform_states[name]["search_offset"] = int(session.get("search_offset") or 0)
                        self._platform_states[name]["search_has_more"] = bool(session.get("search_has_more"))
                        self._platform_states[name]["active_tab"] = str(session.get("active_tab") or "profile")
                        self._platform_states[name]["profile_has_more"] = bool(session.get("profile_has_more"))
                        self._platform_states[name]["next_start"] = int(session.get("next_start") or 1)
            for name, cache in self._workspace_caches.items():
                session = cache.load()
                if not session:
                    continue
                self._platform_states[name]["profile_url"] = str(session.get("profile_url") or "")
                self._platform_states[name]["profile"] = session.get("profile") if isinstance(session.get("profile"), dict) else None
                self._platform_states[name]["profile_videos"] = session.get("profile_videos") if isinstance(session.get("profile_videos"), list) else []
                self._platform_states[name]["multi_links"] = session.get("multi_links") if isinstance(session.get("multi_links"), list) else []
                self._platform_states[name]["multi_videos"] = session.get("multi_videos") if isinstance(session.get("multi_videos"), list) else []
                self._platform_states[name]["bookmarked_videos"] = session.get("bookmarked_videos") if isinstance(session.get("bookmarked_videos"), list) else []
                self._platform_states[name]["search_keyword"] = str(session.get("search_keyword") or "")
                self._platform_states[name]["search_videos"] = session.get("search_videos") if isinstance(session.get("search_videos"), list) else []
                self._platform_states[name]["search_options"] = session.get("search_options") if isinstance(session.get("search_options"), dict) else {}
                self._platform_states[name]["search_offset"] = int(session.get("search_offset") or 0)
                self._platform_states[name]["search_has_more"] = bool(session.get("search_has_more"))
                self._platform_states[name]["active_tab"] = str(session.get("active_tab") or "profile")
                self._platform_states[name]["profile_has_more"] = bool(session.get("profile_has_more"))
                self._platform_states[name]["next_start"] = int(session.get("next_start") or 1)
            page_size = int(data.get("page_size") or 20)
            if page_size in (10, 20, 50):
                self._page_size = page_size
                self.batch_size_var.set(str(page_size))
            trend_threshold = float(data.get("trend_threshold") or 0.8)
            if 0.3 <= trend_threshold <= 1.0:
                self.trend_threshold_var.set(f"{trend_threshold:.2f}")
            self.douyin_backend_enabled_var.set(bool(data.get("douyin_backend_enabled", True)))
            self.douyin_backend_autostart_var.set(bool(data.get("douyin_backend_autostart", True)))
            self.douyin_backend_url_var.set(str(data.get("douyin_backend_url") or "http://127.0.0.1:5555").strip())
            self.douyin_backend_token_var.set(str(data.get("douyin_backend_token") or "").strip())
            self.douyin_backend_endpoint_var.set(
                str(data.get("douyin_backend_endpoint") or "/douyin/detail").strip()
            )
            self.douyin_backend_command_var.set(str(data.get("douyin_backend_command") or "").strip())
            self._apply_douyin_backend_settings(save=False)
            self._update_douyin_login_status()
            has_legacy_session = any(
                key in data
                for key in ("profile_url", "profile", "profile_videos", "multi_links", "multi_videos", "active_tab")
            )
            if not platform_sessions and has_legacy_session:
                legacy_session = {
                    "profile_url": str(data.get("profile_url") or ""),
                    "profile": data.get("profile") if isinstance(data.get("profile"), dict) else None,
                    "profile_videos": data.get("profile_videos") if isinstance(data.get("profile_videos"), list) else [],
                    "multi_links": data.get("multi_links") if isinstance(data.get("multi_links"), list) else [],
                    "multi_videos": data.get("multi_videos") if isinstance(data.get("multi_videos"), list) else [],
                    "active_tab": str(data.get("active_tab") or "profile"),
                    "profile_has_more": False,
                    "next_start": 1,
                }
                self._platform_states[platform].update(legacy_session)
            self._set_platform(platform, save=False, sync_current=False)
            self._apply_trend_threshold()

            if self.profile_videos or self.multi_videos or self._profile_url:
                self.status_var.set("Restored the last session from local cache.")
        except Exception:
            self._update_douyin_login_status()
            fallback_platform = self.platform_var.get().strip().lower() or "tiktok"
            self._set_platform(fallback_platform, save=False, sync_current=False)
            self._show_profile_welcome_state()
            self.status_var.set("Could not fully restore the last session cache.")

    def _on_app_close(self) -> None:
        try:
            self._save_session_cache()
        except Exception:
            pass
        popup = self._video_popup
        if popup is not None and popup.winfo_exists():
            self._close_video_popup(popup)
        settings_popup = self._settings_popup
        if settings_popup is not None and settings_popup.winfo_exists():
            self._close_settings_popup()
        try:
            self.tiktok_service.close()
        except Exception:
            pass
        self.root.destroy()

    def _apply_popup_volume(self, popup: tk.Toplevel) -> None:
        player = getattr(popup, "_vlc_player", None)
        if player is None:
            return
        volume_var = getattr(popup, "_volume_var", None)
        if volume_var is None:
            return
        try:
            volume = int(volume_var.get())
        except Exception:
            volume = 100
        volume = max(0, min(volume, 150))
        player.audio_set_volume(volume)

    def _close_video_popup(self, popup: tk.Toplevel) -> None:
        if self._video_popup is popup:
            self._video_popup = None
        try:
            popup.withdraw()
        except Exception:
            pass
        try:
            self._teardown_vlc_player(popup)
        except Exception:
            pass
        temp_dir = getattr(popup, "_temp_media_dir", None)
        if isinstance(temp_dir, str) and temp_dir:
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception:
                pass
        try:
            popup.grab_release()
        except Exception:
            pass
        self.root.after(120, lambda: popup.winfo_exists() and popup.destroy())

    def _teardown_vlc_player(self, popup: tk.Toplevel) -> None:
        player = getattr(popup, "_vlc_player", None)
        media = getattr(popup, "_vlc_media", None)
        instance = getattr(popup, "_vlc_instance", None)
        if player is not None:
            try:
                player.audio_set_volume(0)
            except Exception:
                pass
            try:
                player.stop()
            except Exception:
                pass
            try:
                if os.name == "nt":
                    player.set_hwnd(0)
            except Exception:
                pass
            try:
                player.set_media(None)
            except Exception:
                pass
        if media is not None:
            try:
                media.release()
            except Exception:
                pass
        if player is not None:
            try:
                player.release()
            except Exception:
                pass
        if instance is not None:
            try:
                instance.release()
            except Exception:
                pass
        popup._vlc_media = None  # type: ignore[attr-defined]
        popup._vlc_player = None  # type: ignore[attr-defined]
        popup._vlc_instance = None  # type: ignore[attr-defined]

    @staticmethod
    def _open_video_link(video: Video) -> None:
        if video.url:
            webbrowser.open(video.url)

    def on_change_dir(self, folder_var: tk.StringVar | None = None) -> None:
        new_dir = filedialog.askdirectory(
            title="Choose download folder",
            initialdir=str(self.output_dir),
        )
        if not new_dir:
            return

        self.output_dir = Path(new_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        if folder_var is not None:
            folder_var.set(str(self.output_dir))
        self.status_var.set(f"Download folder changed to {self.output_dir}")
        self._save_session_cache()

    def on_queue_selected(self) -> None:
        mode = self._active_mode()
        if mode == "profile":
            selected = self.profile_grid.get_selected()
        elif mode == "multi":
            selected = self.multi_grid.get_selected()
        elif mode == "search":
            selected = self.search_grid.get_selected()
        elif mode == "bookmarks":
            selected = self.bookmark_grid.get_selected()
        else:
            messagebox.showinfo("Unavailable", "Please select videos in Profile, Multi-link, Search, or Bookmarks tab.")
            return

        if not selected:
            messagebox.showinfo("No Selection", "Select at least one video to queue.")
            return

        self._queue_videos(selected)

    def _queue_single_video(self, video: Video) -> None:
        self._queue_videos([video])

    def _queue_videos(self, videos: List[Video]) -> None:
        if not videos:
            return

        target_dir = self.output_dir
        profile_username: str | None = None
        mode = self._active_mode()
        if mode == "profile" and self.profile and self.profile.username:
            profile_username = self.profile.username
            target_dir = self.output_dir / self._safe_folder_name(profile_username)

        added = 0
        existing_keys = {
            (str(item.get("video_id") or ""), str(item.get("url") or ""))
            for item in self._download_queue
        }
        for video in videos:
            key = ((video.id or "").strip(), (video.url or "").strip())
            if key in existing_keys:
                continue
            queue_item = {
                "queue_id": str(self._queue_id_seq),
                "video": video,
                "video_id": video.id,
                "url": video.url,
                "title": self._short_title(video.title or "Untitled"),
                "profile_username": profile_username or "",
                "target_dir": target_dir,
                "status": "Queued",
                "progress": 0.0,
                "file_path": "",
                "error": "",
            }
            self._queue_id_seq += 1
            self._download_queue.append(queue_item)
            existing_keys.add(key)
            added += 1

        if added == 0:
            self.status_var.set("Selected videos are already in the queue.")
            self.notebook.select(self.downloads_tab)
            return

        self._refresh_queue_tab()
        self.notebook.select(self.downloads_tab)
        self.status_var.set(f"Queued {added} video(s) for download.")
        self._start_download_queue()

    def _refresh_queue_tab(self) -> None:
        if not hasattr(self, "queue_tree"):
            return
        for iid in self.queue_tree.get_children():
            self.queue_tree.delete(iid)

        for item in self._download_queue:
            progress_text = f"{int(item.get('progress', 0.0))}%"
            self.queue_tree.insert(
                "",
                "end",
                iid=str(item["queue_id"]),
                values=(
                    str(item.get("title") or ""),
                    str(item.get("profile_username") or ""),
                    str(item.get("status") or ""),
                    progress_text,
                ),
            )

        if not self._download_queue:
            self.queue_status_var.set("Queue is empty.")
            self.queue_progress_var.set(0.0)
            return

        total = len(self._download_queue)
        completed = sum(1 for item in self._download_queue if str(item.get("status")) == "Completed")
        active = next((item for item in self._download_queue if str(item.get("status")) == "Downloading"), None)
        if active is not None:
            self.queue_status_var.set(f"Downloading: {active.get('title') or 'Video'}")
            self.queue_progress_var.set(float(active.get("progress") or 0.0))
        else:
            self.queue_status_var.set(f"Queue items: {total} | Completed: {completed}")
            self.queue_progress_var.set(100.0 if total and completed == total else 0.0)

    def _clear_completed_queue(self) -> None:
        self._download_queue = [
            item for item in self._download_queue
            if str(item.get("status")) not in {"Completed", "Failed"}
        ]
        self._refresh_queue_tab()

    def _start_download_queue(self) -> None:
        if self._queue_running:
            return
        next_item = next((item for item in self._download_queue if str(item.get("status")) == "Queued"), None)
        if next_item is None:
            self._queue_running = False
            self.status_var.set("Download queue is idle.")
            self._refresh_queue_tab()
            return

        self._queue_running = True
        next_item["status"] = "Downloading"
        next_item["progress"] = 0.0
        self._refresh_queue_tab()
        self.status_var.set(f"Downloading queued video: {next_item.get('title') or 'Video'}")
        self._process_queue_item(next_item)

    @run_in_thread
    def _process_queue_item(self, queue_item: dict[str, Any]) -> None:
        target_dir = Path(queue_item["target_dir"])
        video = queue_item["video"]

        def _hook(data: dict) -> None:
            status = str(data.get("status") or "")
            if status == "downloading":
                downloaded = float(data.get("downloaded_bytes") or 0.0)
                total = float(data.get("total_bytes") or data.get("total_bytes_estimate") or 0.0)
                percent = (downloaded / total * 100.0) if total > 0 else float(queue_item.get("progress") or 0.0)
                self.root.after(0, lambda: self._update_queue_progress(str(queue_item["queue_id"]), percent))
            elif status == "finished":
                self.root.after(0, lambda: self._update_queue_progress(str(queue_item["queue_id"]), 100.0))

        try:
            path = self.tiktok_service.download_video(video, target_dir, progress_hook=_hook)
        except Exception as exc:  # noqa: BLE001
            self.root.after(0, lambda exc=exc: self._handle_queue_item_error(str(queue_item["queue_id"]), exc))
            return

        self.root.after(0, lambda: self._handle_queue_item_success(str(queue_item["queue_id"]), path))

    def _update_queue_progress(self, queue_id: str, percent: float) -> None:
        item = self._find_queue_item(queue_id)
        if item is None:
            return
        item["progress"] = max(0.0, min(percent, 100.0))
        self._refresh_queue_tab()

    def _handle_queue_item_error(self, queue_id: str, exc: Exception) -> None:
        item = self._find_queue_item(queue_id)
        if item is None:
            self._queue_running = False
            self._start_download_queue()
            return
        item["status"] = "Failed"
        item["error"] = str(exc)
        item["progress"] = 0.0
        self._queue_running = False
        self._refresh_queue_tab()
        self.status_var.set(f"Download failed: {item.get('title') or 'Video'}")
        self._start_download_queue()

    def _handle_queue_item_success(self, queue_id: str, path: Path) -> None:
        item = self._find_queue_item(queue_id)
        if item is None:
            self._queue_running = False
            self._start_download_queue()
            return

        item["status"] = "Completed"
        item["progress"] = 100.0
        item["file_path"] = str(path)
        self.history_service.add_record(
            item["video"],
            path,
            profile_username=str(item.get("profile_username") or "") or None,
        )

        self._mark_video_downloaded(item["video"], path)
        self.history_service.apply_status(self._profile_all_videos)
        self.history_service.apply_status(self._multi_all_videos)
        self._refresh_downloaded_tab()
        self._save_session_cache()
        self._queue_running = False
        self._refresh_queue_tab()
        self.status_var.set(f"Downloaded: {item.get('title') or 'Video'}")
        if not any(str(entry.get("status")) == "Queued" for entry in self._download_queue):
            self._refresh_video_tables_after_queue()
        self._start_download_queue()

    def _mark_video_downloaded(self, video: Video, path: Path) -> None:
        target_key = self._video_identity(video)
        for source in (self._profile_all_videos, self._multi_all_videos):
            for row in source:
                if self._video_identity(row) != target_key:
                    continue
                row.is_downloaded = True
                row.downloaded_path = str(path)

    def _refresh_video_tables_after_queue(self) -> None:
        self.profile_videos = self._apply_video_controls("profile", self._profile_all_videos)
        self.multi_videos = self._apply_video_controls("multi", self._multi_all_videos)
        self.profile_grid.set_videos(self.profile_videos, has_more=self._profile_has_more)
        self.multi_grid.set_videos(self.multi_videos, has_more=False)

    @staticmethod
    def _short_title(text: str, max_length: int = 56) -> str:
        compact = " ".join(str(text or "").split())
        if len(compact) <= max_length:
            return compact or "Untitled"
        return compact[: max_length - 3].rstrip() + "..."

    def _find_queue_item(self, queue_id: str) -> dict[str, Any] | None:
        for item in self._download_queue:
            if str(item.get("queue_id")) == str(queue_id):
                return item
        return None

    def _refresh_downloaded_tab(self) -> None:
        for iid in self.downloaded_tree.get_children():
            self.downloaded_tree.delete(iid)

        records = self.history_service.list_records()
        for i, row in enumerate(records):
            downloaded_at = str(row.get("downloaded_at") or "")
            profile = str(row.get("profile_username") or "")
            title = str(row.get("title") or "")
            file_path = str(row.get("file_path") or "")
            self.downloaded_tree.insert("", "end", iid=str(i), values=(downloaded_at, profile, title, file_path))

    def _open_selected_downloaded_file(self) -> None:
        selected = self.downloaded_tree.selection()
        if not selected:
            return

        values = self.downloaded_tree.item(selected[0], "values")
        if len(values) < 4:
            return

        file_path = str(values[3])
        p = Path(file_path)
        if not p.exists():
            messagebox.showwarning("Missing File", f"File not found:\n{file_path}")
            return

        try:
            os.startfile(str(p))  # type: ignore[attr-defined]
        except Exception:
            messagebox.showinfo("Open File", str(p))
