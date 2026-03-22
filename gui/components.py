import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional


class LabeledEntry(ttk.Frame):
    def __init__(
        self,
        master: tk.Misc,
        text: str,
        placeholder: str | None = None,
        **kwargs,
    ) -> None:
        kwargs.setdefault("style", "Panel.TFrame")
        super().__init__(master, **kwargs)
        self.label = ttk.Label(self, text=text, style="Muted.TLabel")
        self.entry_var = tk.StringVar()
        self.entry = ttk.Entry(self, textvariable=self.entry_var, style="App.TEntry")

        self.label.pack(side=tk.TOP, anchor=tk.W, padx=2, pady=(0, 4))
        self.entry.pack(side=tk.TOP, fill=tk.X, expand=True)

        if placeholder:
            self.entry.insert(0, placeholder)

    def get(self) -> str:
        return self.entry_var.get().strip()

    def set(self, value: str) -> None:
        self.entry_var.set(value)


class PrimaryButton(ttk.Button):
    def __init__(self, master: tk.Misc, text: str, command: Optional[Callable] = None, **kwargs) -> None:
        super().__init__(master, text=text, command=command, style="Primary.TButton", **kwargs)


class ScrollableFrame(ttk.Frame):
    def __init__(self, master: tk.Misc, **kwargs) -> None:
        kwargs.setdefault("style", "Panel.TFrame")
        super().__init__(master, **kwargs)

        canvas = tk.Canvas(self, borderwidth=0, highlightthickness=0, bg="#fffdfa")
        v_scroll = ttk.Scrollbar(self, orient="vertical")
        self._inner = ttk.Frame(canvas, style="Panel.TFrame")

        self._on_viewport_changed: Optional[Callable[[int, int], None]] = None
        self._on_near_bottom: Optional[Callable[[], None]] = None
        self._near_bottom_threshold = 600
        self._viewport_job: str | None = None

        def _on_inner_configure(_: tk.Event) -> None:  # type: ignore[type-arg]
            canvas.configure(scrollregion=canvas.bbox("all"))
            self._schedule_viewport_update()

        self._inner.bind("<Configure>", _on_inner_configure)

        window = canvas.create_window((0, 0), window=self._inner, anchor="nw")

        def _on_canvas_configure(event: tk.Event) -> None:  # type: ignore[type-arg]
            canvas.itemconfig(window, width=event.width)
            self._schedule_viewport_update()

        canvas.bind("<Configure>", _on_canvas_configure)

        def _yview(*args: str) -> None:
            canvas.yview(*args)
            self._schedule_viewport_update()

        v_scroll.configure(command=_yview)

        def _on_mousewheel(event: tk.Event) -> None:  # type: ignore[type-arg]
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
            self._schedule_viewport_update()

        canvas.bind("<Enter>", lambda _: canvas.bind_all("<MouseWheel>", _on_mousewheel))
        canvas.bind("<Leave>", lambda _: canvas.unbind_all("<MouseWheel>"))

        def _on_y_scroll(first: str, last: str) -> None:
            v_scroll.set(first, last)
            self._schedule_viewport_update()

        canvas.configure(yscrollcommand=_on_y_scroll)

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        v_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        self._canvas = canvas
        self._window = window

    @property
    def container(self) -> ttk.Frame:
        return self._inner

    def set_on_viewport_changed(self, callback: Optional[Callable[[int, int], None]]) -> None:
        self._on_viewport_changed = callback
        self._schedule_viewport_update()

    def set_on_near_bottom(self, callback: Optional[Callable[[], None]], threshold_px: int = 600) -> None:
        self._on_near_bottom = callback
        self._near_bottom_threshold = max(80, threshold_px)

    def refresh_viewport(self) -> None:
        self._schedule_viewport_update(force=True)

    def scroll_to_top(self) -> None:
        self._canvas.yview_moveto(0.0)
        self._schedule_viewport_update(force=True)

    def _schedule_viewport_update(self, force: bool = False) -> None:
        if force:
            if self._viewport_job:
                try:
                    self.after_cancel(self._viewport_job)
                except Exception:
                    pass
                self._viewport_job = None
            self._flush_viewport_update()
            return
        if self._viewport_job:
            return
        self._viewport_job = self.after(16, self._flush_viewport_update)

    def _flush_viewport_update(self) -> None:
        self._viewport_job = None
        self._notify_viewport_changed()
        self._check_near_bottom()

    def _notify_viewport_changed(self) -> None:
        if not self._on_viewport_changed:
            return
        top = int(self._canvas.canvasy(0))
        bottom = int(top + self._canvas.winfo_height())
        self._on_viewport_changed(top, bottom)

    def _check_near_bottom(self) -> None:
        if not self._on_near_bottom:
            return
        region = self._canvas.bbox("all")
        if not region:
            return
        total_height = region[3] - region[1]
        if total_height <= 0:
            return

        current_bottom = int(self._canvas.canvasy(0) + self._canvas.winfo_height())
        if current_bottom >= (total_height - self._near_bottom_threshold):
            self._on_near_bottom()
