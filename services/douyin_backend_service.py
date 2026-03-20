from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import requests

from models.video import Video


@dataclass
class DouyinBackendConfig:
    enabled: bool = False
    base_url: str = "http://127.0.0.1:5555"
    token: str = ""
    detail_endpoint: str = "/api/v1/douyin/web/fetch_one_video_v2"


class DouyinBackendService:
    def __init__(self) -> None:
        self._config = DouyinBackendConfig()
        self._cookie_file: str | None = None

    def configure(self, enabled: bool, base_url: str, token: str = "", detail_endpoint: str = "") -> None:
        self._config = DouyinBackendConfig(
            enabled=bool(enabled),
            base_url=(base_url or "http://127.0.0.1:5555").strip().rstrip("/"),
            token=(token or "").strip(),
            detail_endpoint=self._normalize_endpoint(detail_endpoint),
        )

    def configure_cookie_file(self, cookie_file: str | None) -> None:
        self._cookie_file = cookie_file.strip() if cookie_file else None

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    @staticmethod
    def is_douyin_url(url: str) -> bool:
        lowered = (url or "").lower()
        return "douyin.com/" in lowered or "iesdouyin.com/" in lowered or "v.douyin.com/" in lowered

    def fetch_video(self, url: str) -> Video:
        if not self._config.enabled:
            raise RuntimeError("Douyin backend is disabled. Enable it in Settings first.")

        detail_id = self.extract_detail_id(url)
        if not detail_id:
            raise RuntimeError("Could not extract a Douyin video ID from this URL.")

        endpoint = self._config.base_url + self._config.detail_endpoint
        headers = {"Accept": "application/json"}
        if self._config.token:
            headers["token"] = self._config.token
        request_payload = {
            "cookie": self._read_cookie_header(),
            "proxy": "",
            "source": False,
            "detail_id": detail_id,
        }

        try:
            resp = requests.post(
                endpoint,
                json=request_payload,
                headers=headers,
                timeout=60,
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            raise RuntimeError(f"Could not reach the local Douyin backend at {endpoint}: {exc}") from exc

        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            data = payload.get("data")
            if self._looks_like_backend_video(data):
                return self._video_from_backend_data(data, original_url=url)

        detail = self._locate_video_detail(payload)
        if not isinstance(detail, dict):
            summary = self._summarize_payload(payload)
            raise RuntimeError(
                "Douyin backend responded, but the video detail payload was not recognized. "
                f"Payload summary: {summary}"
            )

        return self._video_from_detail(detail, original_url=url)

    @staticmethod
    def extract_detail_id(url: str) -> str:
        for marker in ("/video/", "modal_id=", "aweme_id="):
            if marker in url:
                tail = url.split(marker, 1)[1]
                digits = []
                for ch in tail:
                    if ch.isdigit():
                        digits.append(ch)
                    else:
                        break
                if digits:
                    return "".join(digits)
        return ""

    @staticmethod
    def _normalize_endpoint(endpoint: str) -> str:
        cleaned = (endpoint or "").strip()
        if not cleaned:
            return "/douyin/detail"
        if not cleaned.startswith("/"):
            cleaned = "/" + cleaned
        return cleaned

    def _read_cookie_header(self) -> str:
        if not self._cookie_file:
            return ""
        try:
            cookie_pairs: list[str] = []
            with open(self._cookie_file, "r", encoding="utf-8", errors="ignore") as fh:
                for raw_line in fh:
                    line = raw_line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split("\t")
                    if len(parts) >= 7 and parts[5] and parts[6]:
                        cookie_pairs.append(f"{parts[5]}={parts[6]}")
            return "; ".join(cookie_pairs)
        except Exception:
            return ""

    def _video_from_detail(self, detail: dict[str, Any], original_url: str) -> Video:
        timestamp = self._to_int(
            detail.get("create_time"),
            self._deep_get(detail, "createTime"),
        )
        upload_time = datetime.fromtimestamp(timestamp) if timestamp else None
        stats = self._pick_dict(detail, "statistics", "stats")
        author = self._pick_dict(detail, "author")
        music = self._pick_dict(detail, "music")
        video_block = self._pick_dict(detail, "video")

        title = self._first_str(
            detail.get("desc"),
            detail.get("title"),
            detail.get("description"),
            f"Douyin Video {detail.get('aweme_id') or detail.get('id') or ''}".strip(),
        ) or "Douyin Video"

        return Video(
            id=str(detail.get("aweme_id") or detail.get("id") or self.extract_detail_id(original_url) or ""),
            title=title,
            url=self._first_str(
                self._deep_get(detail, "share_info.share_url"),
                detail.get("share_url"),
                original_url,
            ) or original_url,
            platform="douyin",
            media_url=self._extract_media_url(detail),
            description=self._first_str(detail.get("desc"), detail.get("description")),
            duration=self._extract_duration_seconds(detail, video_block),
            thumbnail_url=self._extract_thumbnail_url(detail, video_block),
            author=self._first_str(
                author.get("nickname") if author else None,
                author.get("unique_id") if author else None,
                author.get("short_id") if author else None,
            ),
            uploader=self._first_str(
                author.get("sec_uid") if author else None,
                author.get("uid") if author else None,
            ),
            view_count=self._to_int(stats.get("play_count") if stats else None, detail.get("view_count")),
            like_count=self._to_int(stats.get("digg_count") if stats else None, detail.get("like_count")),
            comment_count=self._to_int(stats.get("comment_count") if stats else None, detail.get("comment_count")),
            share_count=self._to_int(stats.get("share_count") if stats else None, detail.get("share_count")),
            bookmark_count=self._to_int(stats.get("collect_count") if stats else None, detail.get("bookmark_count")),
            upload_time=upload_time,
            tags=tuple(self._extract_tags(detail)),
            music_title=self._first_str(
                music.get("title") if music else None,
                detail.get("music_title"),
            ),
        )

    def _video_from_backend_data(self, data: dict[str, Any], original_url: str) -> Video:
        upload_time = None
        create_time = self._first_str(data.get("create_time"))
        if create_time:
            for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    upload_time = datetime.strptime(create_time, pattern)
                    break
                except Exception:
                    continue

        downloads = data.get("downloads")
        media_url = None
        if isinstance(downloads, list):
            for item in downloads:
                if isinstance(item, str) and item.strip():
                    media_url = item.strip()
                    break
        elif isinstance(downloads, str) and downloads.strip():
            media_url = downloads.strip()

        return Video(
            id=str(data.get("id") or self.extract_detail_id(original_url) or ""),
            title=self._first_str(data.get("desc"), data.get("id")) or "Douyin Video",
            url=self._first_str(data.get("share_url"), original_url) or original_url,
            platform="douyin",
            media_url=media_url,
            description=self._first_str(data.get("desc")),
            duration=self._duration_to_seconds(data.get("duration")),
            thumbnail_url=self._first_str(data.get("static_cover"), data.get("dynamic_cover")),
            author=self._first_str(data.get("nickname")),
            uploader=self._first_str(data.get("uid"), data.get("sec_uid")),
            view_count=self._to_int(data.get("play_count"), data.get("view_count")),
            like_count=self._to_int(data.get("digg_count"), data.get("like_count")),
            comment_count=self._to_int(data.get("comment_count")),
            share_count=self._to_int(data.get("share_count")),
            bookmark_count=self._to_int(data.get("collect_count")),
            upload_time=upload_time,
            tags=tuple(str(tag) for tag in (data.get("text_extra") or []) if isinstance(tag, str) and tag.strip()),
            music_title=self._first_str(data.get("music_title")),
        )

    def _locate_video_detail(self, payload: Any) -> dict[str, Any] | None:
        if isinstance(payload, dict):
            for key in (
                "aweme_detail",
                "aweme_details",
                "aweme_list",
                "video_detail",
                "item_info",
                "item_list",
                "post_info",
                "detail",
                "data",
                "result",
                "response",
            ):
                value = payload.get(key)
                if isinstance(value, dict):
                    if any(k in value for k in ("aweme_id", "video", "statistics", "desc", "create_time")):
                        return value
                    nested = self._locate_video_detail(value)
                    if nested:
                        return nested
                if isinstance(value, list):
                    for item in value:
                        nested = self._locate_video_detail(item)
                        if nested:
                            return nested
            if any(key in payload for key in ("aweme_id", "video", "statistics", "desc", "create_time")):
                return payload
            for value in payload.values():
                nested = self._locate_video_detail(value)
                if nested:
                    return nested
        if isinstance(payload, list):
            for item in payload:
                nested = self._locate_video_detail(item)
                if nested:
                    return nested
        return None

    @staticmethod
    def _pick_dict(data: dict[str, Any], *keys: str) -> dict[str, Any]:
        for key in keys:
            value = data.get(key)
            if isinstance(value, dict):
                return value
        return {}

    @staticmethod
    def _deep_get(data: Any, dotted_key: str) -> Any:
        current = data
        for part in dotted_key.split("."):
            if not isinstance(current, dict):
                return None
            current = current.get(part)
        return current

    @staticmethod
    def _first_str(*values: Any) -> str | None:
        for value in values:
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _to_int(*values: Any) -> int | None:
        for value in values:
            if value is None or value == "":
                continue
            try:
                return int(float(value))
            except Exception:
                continue
        return None

    def _extract_duration_seconds(self, detail: dict[str, Any], video_block: dict[str, Any]) -> int | None:
        raw = self._to_int(
            detail.get("duration"),
            video_block.get("duration"),
            self._deep_get(video_block, "duration"),
        )
        if raw is None:
            return None
        return raw // 1000 if raw > 1000 else raw

    def _extract_thumbnail_url(self, detail: dict[str, Any], video_block: dict[str, Any]) -> str | None:
        for candidate in (
            self._extract_url_list(video_block.get("cover")),
            self._extract_url_list(video_block.get("origin_cover")),
            self._extract_url_list(detail.get("cover")),
            self._extract_url_list(detail.get("dynamic_cover")),
        ):
            if candidate:
                return candidate
        return None

    def _extract_media_url(self, detail: dict[str, Any]) -> str | None:
        for candidate in (
            self._extract_url_list(self._deep_get(detail, "video.play_addr")),
            self._extract_url_list(self._deep_get(detail, "video.download_addr")),
            self._extract_url_list(self._deep_get(detail, "video.bit_rate")),
            self._first_str(
                detail.get("nwm_video_url"),
                detail.get("video_url"),
                detail.get("play_url"),
                detail.get("download_url"),
            ),
        ):
            if candidate:
                return candidate
        return None

    def _extract_tags(self, detail: dict[str, Any]) -> list[str]:
        tags: list[str] = []
        text_extra = detail.get("text_extra")
        if isinstance(text_extra, list):
            for item in text_extra:
                if not isinstance(item, dict):
                    continue
                tag = self._first_str(item.get("hashtag_name"), item.get("tag_name"), item.get("name"))
                if tag and tag not in tags:
                    tags.append(tag)
        for candidate in detail.get("tags") or []:
            if isinstance(candidate, str) and candidate not in tags:
                tags.append(candidate)
        return tags

    def _extract_url_list(self, value: Any) -> str | None:
        if isinstance(value, dict):
            url_list = value.get("url_list")
            if isinstance(url_list, list):
                for item in url_list:
                    if isinstance(item, str) and item.strip():
                        return item.strip()
            for nested in value.values():
                candidate = self._extract_url_list(nested)
                if candidate:
                    return candidate
        if isinstance(value, list):
            for item in value:
                candidate = self._extract_url_list(item)
                if candidate:
                    return candidate
        if isinstance(value, str) and value.strip().startswith("http"):
            return value.strip()
        return None

    @staticmethod
    def _looks_like_backend_video(data: dict[str, Any]) -> bool:
        return any(key in data for key in ("downloads", "static_cover", "desc", "digg_count", "nickname"))

    @staticmethod
    def _duration_to_seconds(value: Any) -> int | None:
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str) and value.strip():
            parts = value.strip().split(":")
            try:
                parts_int = [int(part) for part in parts]
            except Exception:
                return None
            if len(parts_int) == 3:
                return parts_int[0] * 3600 + parts_int[1] * 60 + parts_int[2]
            if len(parts_int) == 2:
                return parts_int[0] * 60 + parts_int[1]
            if len(parts_int) == 1:
                return parts_int[0]
        return None

    def _summarize_payload(self, payload: Any) -> str:
        if isinstance(payload, dict):
            keys = list(payload.keys())[:12]
            fragments = [f"top-level keys={keys}"]
            for key in keys[:6]:
                value = payload.get(key)
                if isinstance(value, dict):
                    nested_keys = list(value.keys())[:8]
                    fragments.append(f"{key} keys={nested_keys}")
                elif isinstance(value, list):
                    fragments.append(f"{key} list_len={len(value)}")
                else:
                    fragments.append(f"{key}={type(value).__name__}")
            return "; ".join(fragments)
        if isinstance(payload, list):
            return f"list payload with {len(payload)} item(s)"
        return f"payload type={type(payload).__name__}"
