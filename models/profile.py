from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class Profile:
    username: str
    display_name: Optional[str] = None
    avatar_url: Optional[str] = None
    follower_count: Optional[int] = None
    following_count: Optional[int] = None
    like_count: Optional[int] = None
    video_count: Optional[int] = None

    @staticmethod
    def _to_int(value: Any) -> Optional[int]:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        try:
            text = str(value).strip().replace(",", "")
            if not text:
                return None
            return int(float(text))
        except Exception:
            return None

    @classmethod
    def from_ydl(cls, info: Dict[str, Any]) -> "Profile":
        # yt-dlp TikTok playlist info may contain uploader_* fields.
        return cls(
            username=str(info.get("uploader_id") or info.get("uploader") or ""),
            display_name=info.get("uploader") or None,
            avatar_url=info.get("uploader_avatar") or None,
            follower_count=cls._to_int(info.get("uploader_follower_count")),
            following_count=cls._to_int(info.get("uploader_following_count")),
            like_count=cls._to_int(info.get("uploader_like_count")),
            video_count=cls._to_int(info.get("playlist_count") or info.get("n_entries")),
        )

    @classmethod
    def from_user_info(cls, user_info: Dict[str, Any], username_hint: str = "") -> "Profile | None":
        """Parse format: {"user": {...}, "stats": {...}}."""
        try:
            user = user_info.get("user") or {}
            stats = user_info.get("stats") or {}
            username = str(user.get("uniqueId") or username_hint or "").strip()
            display_name = user.get("nickname") or None
            avatar_url = user.get("avatarLarger") or user.get("avatarMedium") or user.get("avatarThumb") or None

            if not username and not display_name:
                return None

            return cls(
                username=username,
                display_name=display_name,
                avatar_url=avatar_url,
                follower_count=cls._to_int(stats.get("followerCount")),
                following_count=cls._to_int(stats.get("followingCount")),
                like_count=cls._to_int(stats.get("heartCount") or stats.get("diggCount")),
                video_count=cls._to_int(stats.get("videoCount")),
            )
        except Exception:
            return None

    @classmethod
    def from_sigi_state(cls, sigi: Dict[str, Any], username_hint: str = "") -> "Profile | None":
        """Parse profile data from TikTok page payloads (SIGI_STATE and newer variants)."""
        try:
            user_module = sigi.get("UserModule") or {}
            users = user_module.get("users") or {}
            stats = user_module.get("stats") or {}

            key = ""
            if username_hint:
                for k, u in users.items():
                    if str(u.get("uniqueId") or "").lower() == username_hint.lower():
                        key = k
                        break
            if not key and users:
                key = next(iter(users.keys()))
            if key:
                u = users.get(key) or {}
                s = stats.get(key) or {}
                username = str(u.get("uniqueId") or username_hint or "")
                display_name = u.get("nickname") or None
                avatar_url = u.get("avatarLarger") or u.get("avatarMedium") or u.get("avatarThumb") or None

                return cls(
                    username=username,
                    display_name=display_name,
                    avatar_url=avatar_url,
                    follower_count=cls._to_int(s.get("followerCount")),
                    following_count=cls._to_int(s.get("followingCount")),
                    like_count=cls._to_int(s.get("heartCount") or s.get("diggCount")),
                    video_count=cls._to_int(s.get("videoCount")),
                )
        except Exception:
            pass

        # Fallback for newer formats (e.g. __UNIVERSAL_DATA_FOR_REHYDRATION__ payloads)
        try:
            def _walk(obj: Any) -> "Profile | None":
                if isinstance(obj, dict):
                    if "userInfo" in obj and isinstance(obj.get("userInfo"), dict):
                        p = cls.from_user_info(obj["userInfo"], username_hint=username_hint)
                        if p:
                            return p
                    if "user" in obj and "stats" in obj:
                        p = cls.from_user_info(obj, username_hint=username_hint)
                        if p:
                            return p
                    for v in obj.values():
                        found = _walk(v)
                        if found:
                            return found
                elif isinstance(obj, list):
                    for v in obj:
                        found = _walk(v)
                        if found:
                            return found
                return None

            return _walk(sigi)
        except Exception:
            return None
