from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable, List, Optional
from urllib.parse import urlparse

import requests
from yt_dlp import YoutubeDL

from models.video import Video


class DownloadService:
    def __init__(self, output_dir: Path, cookie_file: str | None = None) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.cookie_file = cookie_file.strip() if cookie_file else None

    def _build_ydl(self, progress_hook: Optional[Callable[[dict], None]] = None) -> YoutubeDL:
        out_tpl = str(self.output_dir / "%(title)s.%(ext)s")
        opts = {
            "outtmpl": out_tpl,
            "nocheckcertificate": True,
        }
        if self.cookie_file:
            opts["cookiefile"] = self.cookie_file
        if progress_hook is not None:
            opts["progress_hooks"] = [progress_hook]
        return YoutubeDL(opts)

    def _build_output_path(self, video: Video, media_url: str = "") -> Path:
        title = (video.title or video.id or "video").strip()
        safe_title = "".join(ch for ch in title if ch not in '<>:"/\\|?*').strip().rstrip(".")
        if not safe_title:
            safe_title = video.id or "video"

        ext = ".mp4"
        parsed = urlparse(media_url)
        suffix = Path(parsed.path).suffix.lower()
        if suffix and len(suffix) <= 5:
            ext = suffix
        return self.output_dir / f"{safe_title}{ext}"

    def _download_from_media_url(self, video: Video, progress_hook: Optional[Callable[[dict], None]] = None) -> Path:
        media_url = (video.media_url or "").strip()
        if not media_url:
            raise RuntimeError("This video does not include a direct media URL.")

        output_path = self._build_output_path(video, media_url)
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            )
        }
        with requests.get(media_url, stream=True, timeout=60, headers=headers) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("Content-Length") or 0)
            downloaded = 0
            if progress_hook is not None:
                progress_hook(
                    {
                        "status": "downloading",
                        "downloaded_bytes": 0,
                        "total_bytes": total,
                    }
                )
            with output_path.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=1024 * 128):
                    if not chunk:
                        continue
                    fh.write(chunk)
                    downloaded += len(chunk)
                    if progress_hook is not None:
                        progress_hook(
                            {
                                "status": "downloading",
                                "downloaded_bytes": downloaded,
                                "total_bytes": total,
                            }
                        )

        if progress_hook is not None:
            progress_hook({"status": "finished", "filename": str(output_path)})
        return output_path

    def download_video(self, video: Video, progress_hook: Optional[Callable[[dict], None]] = None) -> Path:
        if video.media_url:
            return self._download_from_media_url(video, progress_hook)
        ydl = self._build_ydl(progress_hook)
        info = ydl.extract_info(video.url, download=True)
        filename = ydl.prepare_filename(info)
        return Path(filename)

    def download_videos(self, videos: Iterable[Video]) -> List[Path]:
        paths: List[Path] = []

        for video in videos:
            paths.append(self.download_video(video))

        return paths

