from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional


@dataclass
class Video:
    id: str
    title: str
    url: str
    platform: Optional[str] = None
    media_url: Optional[str] = None
    description: Optional[str] = None
    duration: Optional[int] = None
    thumbnail_url: Optional[str] = None
    author: Optional[str] = None
    uploader: Optional[str] = None
    view_count: Optional[int] = None
    like_count: Optional[int] = None
    comment_count: Optional[int] = None
    share_count: Optional[int] = None
    bookmark_count: Optional[int] = None
    upload_time: Optional[datetime] = None
    tags: tuple[str, ...] = ()
    music_title: Optional[str] = None
    is_downloaded: bool = False
    downloaded_path: Optional[str] = None

    @classmethod
    def from_ydl(cls, info: Dict[str, Any]) -> "Video":
        # yt-dlp TikTok info thường có các field:
        # like_count, comment_count, repost_count/share_count, wish_count/bookmark_count, timestamp
        ts = info.get("timestamp")
        upload_time = datetime.fromtimestamp(ts) if ts else None

        return cls(
            id=str(info.get("id") or ""),
            title=str(info.get("title") or "Untitled"),
            url=str(info.get("webpage_url") or info.get("original_url") or ""),
            platform=cls._infer_platform(info),
            media_url=cls._first_non_empty(
                info.get("url"),
                info.get("play_addr"),
                info.get("download_addr"),
            ),
            description=info.get("description") or None,
            duration=info.get("duration"),
            thumbnail_url=info.get("thumbnail"),
            author=(info.get("creator") or info.get("uploader") or None),
            uploader=info.get("uploader") or None,
            view_count=info.get("view_count"),
            like_count=info.get("like_count"),
            comment_count=info.get("comment_count"),
            share_count=info.get("repost_count") or info.get("share_count"),
            bookmark_count=info.get("wish_count") or info.get("bookmark_count"),
            upload_time=upload_time,
            tags=tuple(info.get("tags") or ()),
            music_title=info.get("track") or info.get("album") or None,
        )

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Video":
        upload_raw = data.get("upload_time")
        upload_time = None
        if isinstance(upload_raw, str) and upload_raw:
            try:
                upload_time = datetime.fromisoformat(upload_raw)
            except Exception:
                upload_time = None

        tags = data.get("tags") or ()
        if isinstance(tags, list):
            tags = tuple(str(tag) for tag in tags)
        elif isinstance(tags, tuple):
            tags = tuple(str(tag) for tag in tags)
        else:
            tags = ()

        return cls(
            id=str(data.get("id") or ""),
            title=str(data.get("title") or "Untitled"),
            url=str(data.get("url") or ""),
            platform=data.get("platform") or None,
            media_url=data.get("media_url") or None,
            description=data.get("description") or None,
            duration=data.get("duration"),
            thumbnail_url=data.get("thumbnail_url") or None,
            author=data.get("author") or None,
            uploader=data.get("uploader") or None,
            view_count=data.get("view_count"),
            like_count=data.get("like_count"),
            comment_count=data.get("comment_count"),
            share_count=data.get("share_count"),
            bookmark_count=data.get("bookmark_count"),
            upload_time=upload_time,
            tags=tags,
            music_title=data.get("music_title") or None,
            is_downloaded=bool(data.get("is_downloaded")),
            downloaded_path=data.get("downloaded_path") or None,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "url": self.url,
            "platform": self.platform,
            "media_url": self.media_url,
            "description": self.description,
            "duration": self.duration,
            "thumbnail_url": self.thumbnail_url,
            "author": self.author,
            "uploader": self.uploader,
            "view_count": self.view_count,
            "like_count": self.like_count,
            "comment_count": self.comment_count,
            "share_count": self.share_count,
            "bookmark_count": self.bookmark_count,
            "upload_time": self.upload_time.isoformat() if self.upload_time else None,
            "tags": list(self.tags),
            "music_title": self.music_title,
            "is_downloaded": self.is_downloaded,
            "downloaded_path": self.downloaded_path,
        }

    @staticmethod
    def _first_non_empty(*values: Any) -> Optional[str]:
        for value in values:
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @classmethod
    def _infer_platform(cls, info: Dict[str, Any]) -> Optional[str]:
        webpage_url = str(info.get("webpage_url") or info.get("original_url") or "").lower()
        extractor = str(info.get("extractor_key") or info.get("extractor") or "").lower()
        if "douyin" in webpage_url or "douyin" in extractor:
            return "douyin"
        if "tiktok" in webpage_url or "tiktok" in extractor:
            return "tiktok"
        return None

