import tkinter as tk

from gui.app import TikTokDownloaderApp


def main() -> None:
    root = tk.Tk()
    app = TikTokDownloaderApp(root)
    root.title("DYD Workspace")
    # Open maximized and keep a fixed window size.
    try:
        root.state("zoomed")  # Windows
    except Exception:
        root.attributes("-zoomed", True)
    root.resizable(False, False)
    app.run()


if __name__ == "__main__":
    main()
