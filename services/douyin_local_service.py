from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from playwright.async_api import BrowserContext, Page, async_playwright

from models.video import Video


class DouyinLocalService:
    _DOUYIN_HOME = "https://www.douyin.com/"
    _DETAIL_API = "https://www.douyin.com/aweme/v1/web/aweme/detail/"

    def __init__(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        self._user_data_dir = project_root / "browser_data" / "douyin_app_user"
        self._user_data_dir.mkdir(parents=True, exist_ok=True)
        self._assets_dir = project_root / "assets" / "douyin"
        self._sign_script_path = self._assets_dir / "douyin.js"
        self._stealth_script_path = self._assets_dir / "stealth.min.js"

    def configure_cookie_file(self, cookie_file: str | None) -> None:
        del cookie_file

    @staticmethod
    def is_douyin_url(url: str) -> bool:
        lowered = (url or "").lower()
        return "douyin.com/" in lowered or "iesdouyin.com/" in lowered or "v.douyin.com/" in lowered

    def has_login_session(self) -> bool:
        return self._user_data_dir.exists() and any(self._user_data_dir.iterdir())

    def login(self, timeout_seconds: int = 240) -> bool:
        return asyncio.run(self._login_async(timeout_seconds))

    def fetch_video(self, url: str) -> Video:
        return asyncio.run(self._fetch_video_async(url))

    async def _login_async(self, timeout_seconds: int) -> bool:
        self._ensure_assets()
        async with async_playwright() as playwright:
            context = await self._launch_context(playwright, headless=False)
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                await page.goto(self._DOUYIN_HOME, wait_until="domcontentloaded", timeout=60000)
                deadline = time.time() + max(30, timeout_seconds)
                while time.time() < deadline:
                    if await self._is_logged_in(context, page):
                        return True
                    await asyncio.sleep(2)
                return False
            finally:
                await context.close()

    async def _fetch_video_async(self, original_url: str) -> Video:
        self._ensure_assets()
        async with async_playwright() as playwright:
            context = await self._launch_context(playwright, headless=False)
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                await page.goto(self._DOUYIN_HOME, wait_until="domcontentloaded", timeout=60000)

                if not await self._is_logged_in(context, page):
                    raise RuntimeError("Douyin login is required. Please open Settings and click 'Login to Douyin' first.")

                detail_id = await self._resolve_detail_id(page, original_url)
                if not detail_id:
                    raise RuntimeError("Could not resolve a valid Douyin video ID for this URL.")

                aweme_detail = await self._request_video_detail(context, page, detail_id)
                if not aweme_detail:
                    raise RuntimeError(
                        "Douyin returned an empty video detail. Your login session may have expired, so please login again."
                    )

                return self._video_from_aweme_detail(aweme_detail, original_url)
            finally:
                await context.close()

    async def _launch_context(self, playwright: Any, headless: bool) -> BrowserContext:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(self._user_data_dir),
            accept_downloads=False,
            headless=headless,
            viewport={"width": 1920, "height": 1080},
        )
        if self._stealth_script_path.exists():
            await context.add_init_script(path=str(self._stealth_script_path))
        return context

    async def _is_logged_in(self, context: BrowserContext, page: Page) -> bool:
        try:
            local_storage = await page.evaluate("() => Object.assign({}, window.localStorage)")
            if str(local_storage.get("HasUserLogin") or "") == "1":
                return True
        except Exception:
            pass

        cookie_dict = self._cookie_dict(await context.cookies())
        return str(cookie_dict.get("LOGIN_STATUS") or "") == "1"

    async def _resolve_detail_id(self, page: Page, url: str) -> str:
        text = (url or "").strip()
        if not text:
            return ""
        if text.isdigit():
            return text

        if "modal_id=" in text:
            match = re.search(r"modal_id=(\d+)", text)
            if match:
                return match.group(1)

        match = re.search(r"/video/(\d+)", text)
        if match:
            return match.group(1)

        if "v.douyin.com" in text or "iesdouyin.com" in text:
            try:
                await page.goto(text, wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(1)
                return await self._resolve_detail_id(page, page.url)
            except Exception as exc:
                raise RuntimeError(f"Could not resolve the Douyin short link: {exc}") from exc

        return ""

    async def _request_video_detail(self, context: BrowserContext, page: Page, detail_id: str) -> dict[str, Any]:
        cookie_dict = self._cookie_dict(await context.cookies())
        cookie_str = ";".join(f"{key}={value}" for key, value in cookie_dict.items())
        local_storage = await page.evaluate("() => Object.assign({}, window.localStorage)")
        user_agent = await page.evaluate("() => navigator.userAgent")

        params = {
            "device_platform": "webapp",
            "aid": "6383",
            "channel": "channel_pc_web",
            "version_code": "190600",
            "version_name": "19.6.0",
            "update_version_code": "170400",
            "pc_client_type": "1",
            "cookie_enabled": "true",
            "browser_language": "zh-CN",
            "browser_platform": "MacIntel",
            "browser_name": "Chrome",
            "browser_version": "125.0.0.0",
            "browser_online": "true",
            "engine_name": "Blink",
            "engine_version": "109.0",
            "os_name": "Mac OS",
            "os_version": "10.15.7",
            "cpu_core_num": "8",
            "device_memory": "8",
            "platform": "PC",
            "screen_width": "2560",
            "screen_height": "1440",
            "effective_type": "4g",
            "round_trip_time": "50",
            "webid": self._generate_web_id(),
            "msToken": str(local_storage.get("xmst") or cookie_dict.get("msToken") or ""),
            "aweme_id": detail_id,
        }
        encoded = urlencode(params)
        params["a_bogus"] = await self._sign_detail(page, encoded, user_agent)

        headers = {
            "User-Agent": user_agent,
            "Cookie": cookie_str,
            "Host": "www.douyin.com",
            "Referer": "https://www.douyin.com/",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json;charset=UTF-8",
        }

        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            response = await client.get(self._DETAIL_API, params=params, headers=headers)
            status = response.status_code
            text = response.text.strip()

        if status != 200:
            raise RuntimeError(f"Douyin detail request failed with HTTP {status}.")
        if not text or text == "blocked":
            raise RuntimeError(
                f"Douyin blocked the current request. "
                f"HTTP {status}, page URL: {page.url}, response preview: {text[:120] or '<empty>'}"
            )
        try:
            data = json.loads(text)
        except Exception as exc:
            raise RuntimeError("Douyin returned a non-JSON response for the video detail request.") from exc
        if not isinstance(data, dict):
            raise RuntimeError("Douyin returned an unexpected response shape.")
        return data.get("aweme_detail") or {}

    async def _sign_detail(self, page: Page, encoded_params: str, user_agent: str) -> str:
        await self._ensure_sign_script(page)
        try:
            result = await page.evaluate(
                "([params, ua]) => window.__codex_sign_datail(params, ua)",
                [encoded_params, user_agent],
            )
            return str(result or "")
        except Exception as exc:
            raise RuntimeError(f"Could not generate the Douyin request signature: {exc}") from exc

    async def _ensure_sign_script(self, page: Page) -> None:
        try:
            ready = await page.evaluate("() => typeof window.__codex_sign_datail === 'function'")
        except Exception:
            ready = False
        if ready:
            return

        source = self._sign_script_path.read_text(encoding="utf-8", errors="ignore")
        await page.add_script_tag(
            content=(
                source
                + "\nwindow.__codex_sign_datail = function(params, ua) { return sign_datail(params, ua); };"
            )
        )

    def _ensure_assets(self) -> None:
        if not self._sign_script_path.exists():
            raise RuntimeError("Missing Douyin signing asset: assets/douyin/douyin.js")
        if not self._stealth_script_path.exists():
            raise RuntimeError("Missing Playwright stealth asset: assets/douyin/stealth.min.js")

    @staticmethod
    def _cookie_dict(cookies: list[dict[str, Any]]) -> dict[str, str]:
        result: dict[str, str] = {}
        for cookie in cookies:
            name = str(cookie.get("name") or "").strip()
            value = str(cookie.get("value") or "")
            if name:
                result[name] = value
        return result

    @staticmethod
    def _extract_browser_version(user_agent: str) -> str:
        match = re.search(r"Chrome/([\d.]+)", user_agent or "")
        if match:
            return match.group(1)
        return "125.0.0.0"

    @staticmethod
    def _generate_web_id() -> str:
        import uuid
        return uuid.uuid4().hex[:19]

    def _video_from_aweme_detail(self, data: dict[str, Any], original_url: str) -> Video:
        statistics = data.get("statistics") or {}
        author = data.get("author") or {}
        music = data.get("music") or {}
        video_item = data.get("video") or {}

        upload_time = None
        create_time = data.get("create_time")
        if isinstance(create_time, (int, float)):
            upload_time = datetime.fromtimestamp(create_time)

        thumbnail_url = self._pick_last_url(
            (video_item.get("raw_cover") or {}).get("url_list"),
            (video_item.get("origin_cover") or {}).get("url_list"),
            (author.get("avatar_thumb") or {}).get("url_list"),
        )
        media_url = self._pick_last_url(
            (video_item.get("play_addr_h264") or {}).get("url_list"),
            (video_item.get("play_addr_256") or {}).get("url_list"),
            (video_item.get("play_addr") or {}).get("url_list"),
        )

        tags = []
        for item in data.get("text_extra") or []:
            if isinstance(item, dict):
                tag_name = str(item.get("hashtag_name") or item.get("tag_name") or "").strip()
                if tag_name:
                    tags.append(tag_name)
            elif isinstance(item, str) and item.strip():
                tags.append(item.strip())

        video_id = str(data.get("aweme_id") or "").strip()
        return Video(
            id=video_id or self._extract_digits(original_url),
            title=str(data.get("desc") or video_id or "Douyin Video"),
            url=f"https://www.douyin.com/video/{video_id}" if video_id else original_url,
            platform="douyin",
            media_url=media_url,
            description=data.get("desc") or None,
            duration=self._duration_to_seconds(data.get("duration")),
            thumbnail_url=thumbnail_url,
            author=author.get("nickname") or None,
            uploader=author.get("uid") or author.get("sec_uid") or None,
            view_count=self._to_int(statistics.get("play_count")),
            like_count=self._to_int(statistics.get("digg_count")),
            comment_count=self._to_int(statistics.get("comment_count")),
            share_count=self._to_int(statistics.get("share_count")),
            bookmark_count=self._to_int(statistics.get("collect_count")),
            upload_time=upload_time,
            tags=tuple(tags),
            music_title=music.get("title") or None,
        )

    @staticmethod
    def _pick_last_url(*lists: Any) -> str | None:
        for item in lists:
            if isinstance(item, list) and item:
                for value in reversed(item):
                    if isinstance(value, str) and value.strip():
                        return value.strip()
        return None

    @staticmethod
    def _to_int(value: Any) -> int | None:
        if value in (None, ""):
            return None
        try:
            return int(float(value))
        except Exception:
            return None

    @staticmethod
    def _duration_to_seconds(value: Any) -> int | None:
        if isinstance(value, (int, float)):
            milliseconds = float(value)
            if milliseconds > 1000:
                return int(milliseconds / 1000)
            return int(milliseconds)
        return None

    @staticmethod
    def _extract_digits(text: str) -> str:
        return "".join(ch for ch in str(text) if ch.isdigit())
