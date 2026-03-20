from __future__ import annotations

from typing import Any, Dict, List, Tuple

import json
import re

import requests
from yt_dlp import YoutubeDL

from models.profile import Profile
from models.video import Video
from services.douyin_local_service import DouyinLocalService


class TikTokService:
    def __init__(self) -> None:
        self._cookie_file: str | None = None
        self._douyin_local = DouyinLocalService()
        self._ydl = self._create_ydl()

    def _base_opts(self) -> Dict[str, Any]:
        opts: Dict[str, Any] = {
            "quiet": True,
            "skip_download": True,
            "nocheckcertificate": True,
            "http_headers": {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9,vi;q=0.8",
            },
        }
        if self._cookie_file:
            opts["cookiefile"] = self._cookie_file
        return opts

    def configure_cookie_file(self, cookie_file: str | None) -> None:
        self._cookie_file = cookie_file.strip() if cookie_file else None
        self._ydl = self._create_ydl()

    def has_douyin_login_session(self) -> bool:
        return self._douyin_local.has_login_session()

    def login_to_douyin(self, timeout_seconds: int = 240) -> bool:
        return self._douyin_local.login(timeout_seconds=timeout_seconds)

    def configure_douyin_backend(
        self,
        enabled: bool,
        base_url: str,
        token: str = "",
        detail_endpoint: str = "",
    ) -> None:
        del enabled, base_url, token, detail_endpoint

    def _create_ydl(self, extra_opts: Dict[str, Any] | None = None) -> YoutubeDL:
        opts = self._base_opts()
        if extra_opts:
            opts.update(extra_opts)

        try:
            return YoutubeDL({**opts, "impersonate": "chrome"})
        except Exception:
            return YoutubeDL(opts)

    def fetch_videos(self, urls: list[str]) -> List[Video]:
        videos: List[Video] = []
        for url in urls:
            url = url.strip()
            if not url:
                continue
            if self._douyin_local.is_douyin_url(url):
                videos.append(self._douyin_local.fetch_video(url))
                continue
            info = self._ydl.extract_info(url, download=False)
            if "_type" in info and info["_type"] == "playlist":
                for entry in info.get("entries") or []:
                    if not entry:
                        continue
                    videos.append(Video.from_ydl(entry))
            else:
                videos.append(Video.from_ydl(info))
        return videos

    def fetch_videos_paged(self, url: str, start: int, count: int) -> Tuple[List[Video], bool, Profile | None]:
        """
        Fetch videos using true pagination for profile/playlist URL.

        start: 1-based index in playlist/profile.
        count: number of videos to fetch in this page.
        Return: (videos, has_more, profile).
        """
        url = url.strip()
        if not url:
            return [], False, None

        end = start + count - 1
        opts = {
            "playliststart": start,
            "playlistend": end,
        }
        ydl = self._create_ydl(opts)

        info = ydl.extract_info(url, download=False)

        videos: List[Video] = []
        profile: Profile | None = None
        if "_type" in info and info["_type"] == "playlist":
            profile = Profile.from_ydl(info)
            for entry in info.get("entries") or []:
                if not entry:
                    continue
                videos.append(Video.from_ydl(entry))
        else:
            # Not a playlist/profile URL, likely a single video URL.
            videos.append(Video.from_ydl(info))

        # If yt-dlp metadata is missing/partial, try scraping from HTML on first page.
        if start == 1 and (profile is None or (profile and not profile.avatar_url and not profile.follower_count)):
            scraped = self.fetch_profile_from_web(url)
            if scraped:
                if profile is None:
                    profile = scraped
                else:
                    profile.username = profile.username or scraped.username
                    profile.display_name = profile.display_name or scraped.display_name
                    profile.avatar_url = profile.avatar_url or scraped.avatar_url
                    profile.follower_count = profile.follower_count or scraped.follower_count
                    profile.following_count = profile.following_count or scraped.following_count
                    profile.like_count = profile.like_count or scraped.like_count
                    profile.video_count = profile.video_count or scraped.video_count

        has_more = len(videos) == count
        return videos, has_more, profile

    def _extract_profile_from_json_blob(self, blob: str, username_hint: str = "") -> Profile | None:
        try:
            data = json.loads(blob)
        except Exception:
            return None
        return Profile.from_sigi_state(data, username_hint=username_hint)

    def _extract_profile_from_html(self, html: str, username_hint: str = "") -> Profile | None:
        patterns = [
            r'<script[^>]+id="SIGI_STATE"[^>]*>(\{.*?\})</script>',
            r'<script[^>]+id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(\{.*?\})</script>',
            r"__UNIVERSAL_DATA_FOR_REHYDRATION__\s*=\s*(\{.*?\});",
            r'<script[^>]+id="__NEXT_DATA__"[^>]*>(\{.*?\})</script>',
        ]

        for pattern in patterns:
            m = re.search(pattern, html, flags=re.DOTALL)
            if not m:
                continue
            p = self._extract_profile_from_json_blob(m.group(1), username_hint=username_hint)
            if p:
                return p

        return None

    def fetch_profile_from_web(self, url: str) -> Profile | None:
        """
        Fetch profile info by parsing TikTok HTML payloads.
        Can fail due to region/captcha/anti-bot; returns None on failure.
        """
        username_hint = ""
        if "@" in url:
            try:
                username_hint = url.split("@", 1)[1].split("/", 1)[0].split("?", 1)[0]
            except Exception:
                username_hint = ""

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
        }

        try:
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            html = resp.text
        except Exception:
            return None

        return self._extract_profile_from_html(html, username_hint=username_hint)
