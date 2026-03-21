from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable, List, Optional
from urllib.parse import urlparse

import httpx
import requests
from yt_dlp import YoutubeDL

from models.video import Video


class DownloadService:
    _MAX_FILENAME_STEM = 80

    def __init__(
        self,
        output_dir: Path,
        cookie_file: str | None = None,
        video_resolver: Optional[Callable[[Video], Video]] = None,
    ) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.cookie_file = cookie_file.strip() if cookie_file else None
        self.video_resolver = video_resolver

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
        safe_title = self._sanitize_title(title, fallback=video.id or "video")
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
            ),
            "Accept": "*/*",
        }
        with httpx.Client(timeout=60, follow_redirects=True, headers=headers) as client:
            resp = client.get(media_url)
            resp.raise_for_status()
            content_type = str(resp.headers.get("Content-Type") or "").lower()
            if "text/html" in content_type:
                raise RuntimeError("The direct media URL resolved to an HTML page instead of video content.")

            total = int(resp.headers.get("Content-Length") or len(resp.content) or 0)
            if progress_hook is not None:
                progress_hook(
                    {
                        "status": "downloading",
                        "downloaded_bytes": 0,
                        "total_bytes": total,
                    }
                )
            output_path.write_bytes(resp.content)
            if progress_hook is not None:
                progress_hook(
                    {
                        "status": "downloading",
                        "downloaded_bytes": len(resp.content),
                        "total_bytes": total,
                    }
                )

        if progress_hook is not None:
            progress_hook({"status": "finished", "filename": str(output_path)})
        return output_path

    def _download_image_gallery(self, video: Video, progress_hook: Optional[Callable[[dict], None]] = None) -> Path:
        if not video.image_urls:
            raise RuntimeError("This post does not include downloadable image URLs.")

        title = (video.title or video.id or "gallery").strip()
        safe_title = self._sanitize_title(title, fallback=video.id or "gallery")
        if not safe_title:
            safe_title = video.id or "gallery"

        output_dir = self.output_dir / safe_title
        output_dir.mkdir(parents=True, exist_ok=True)

        total = len(video.image_urls)
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            )
        }
        for index, image_url in enumerate(video.image_urls, start=1):
            parsed = urlparse(image_url)
            suffix = Path(parsed.path).suffix.lower() or ".jpg"
            image_path = output_dir / f"{index:02d}{suffix}"
            with requests.get(image_url, timeout=60, headers=headers) as resp:
                resp.raise_for_status()
                image_path.write_bytes(resp.content)
            if progress_hook is not None:
                progress_hook(
                    {
                        "status": "downloading",
                        "downloaded_bytes": index,
                        "total_bytes": total,
                    }
                )

        if progress_hook is not None:
            progress_hook({"status": "finished", "filename": str(output_dir)})
        return output_dir

    def download_video(self, video: Video, progress_hook: Optional[Callable[[dict], None]] = None) -> Path:
        is_douyin = video.platform == "douyin" or "douyin.com/" in (video.url or "")
        if self.video_resolver is not None:
            try:
                refreshed = self.video_resolver(video)
                refreshed.is_downloaded = video.is_downloaded
                refreshed.downloaded_path = video.downloaded_path
                video = refreshed
            except Exception:
                pass

        if is_douyin:
            if video.media_url:
                return self._download_from_media_url(video, progress_hook)
            if video.image_urls:
                return self._download_image_gallery(video, progress_hook)

        ydl = self._build_ydl(progress_hook)
        try:
            info = ydl.extract_info(video.url, download=True)
            filename = ydl.prepare_filename(info)
            return Path(filename)
        except Exception:
            if video.media_url:
                return self._download_from_media_url(video, progress_hook)
            if video.image_urls:
                return self._download_image_gallery(video, progress_hook)
            raise

    def download_videos(self, videos: Iterable[Video]) -> List[Path]:
        paths: List[Path] = []

        for video in videos:
            paths.append(self.download_video(video))

        return paths

    @classmethod
    def _sanitize_title(cls, title: str, fallback: str) -> str:
        safe_title = "".join(ch for ch in title if ch not in '<>:"/\\|?*').strip().rstrip(".")
        safe_title = " ".join(safe_title.split())
        if len(safe_title) > cls._MAX_FILENAME_STEM:
            safe_title = safe_title[: cls._MAX_FILENAME_STEM].rstrip(" ._-")
        return safe_title or fallback

