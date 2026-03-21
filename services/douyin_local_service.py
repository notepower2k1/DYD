from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any, Callable
from urllib.parse import urlencode

import httpx
from playwright.async_api import BrowserContext, Page, async_playwright

from models.profile import Profile
from models.video import Video


class DouyinLocalService:
    _DOUYIN_HOME = "https://www.douyin.com/"
    _DETAIL_API = "https://www.douyin.com/aweme/v1/web/aweme/detail/"
    _USER_PROFILE_API = "/aweme/v1/web/user/profile/other/"
    _USER_POST_API = "/aweme/v1/web/aweme/post/"

    def __init__(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        self._user_data_dir = project_root / "browser_data" / "douyin_app_user"
        self._user_data_dir.mkdir(parents=True, exist_ok=True)
        self._assets_dir = project_root / "assets" / "douyin"
        self._debug_log_path = project_root / "douyin_debug.log"
        self._sign_script_path = self._assets_dir / "douyin.js"
        self._stealth_script_path = self._assets_dir / "stealth.min.js"
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: Thread | None = None
        self._loop_ready = Event()
        self._operation_lock = Lock()
        self._playwright: Any | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._window_hidden = False

    def configure_cookie_file(self, cookie_file: str | None) -> None:
        del cookie_file

    @staticmethod
    def is_douyin_url(url: str) -> bool:
        lowered = (url or "").lower()
        return "douyin.com/" in lowered or "iesdouyin.com/" in lowered or "v.douyin.com/" in lowered

    def has_login_session(self) -> bool:
        return self._user_data_dir.exists() and any(self._user_data_dir.iterdir())

    def login(self, timeout_seconds: int = 240) -> bool:
        with self._operation_lock:
            return bool(self._run_coro(self._login_async(timeout_seconds)))

    def fetch_video(self, url: str) -> Video:
        with self._operation_lock:
            result = self._run_coro(self._fetch_video_async(url))
        if not isinstance(result, Video):
            raise RuntimeError("Douyin fetch did not return a valid video object.")
        return result

    def fetch_profile_videos_paged(self, url: str, start: int, count: int) -> tuple[list[Video], bool, Profile | None]:
        with self._operation_lock:
            result = self._run_coro(self._fetch_profile_videos_paged_async(url, start, count))
        if not isinstance(result, tuple) or len(result) != 3:
            raise RuntimeError("Douyin profile fetch did not return a valid result.")
        return result

    def download_video(
        self,
        video: Video,
        output_dir: Path,
        progress_hook: Callable[[dict], None] | None = None,
    ) -> Path:
        with self._operation_lock:
            result = self._run_coro(self._download_video_async(video, output_dir, progress_hook))
        if not isinstance(result, Path):
            raise RuntimeError("Douyin download did not return a valid path.")
        return result

    def show_browser(self) -> None:
        with self._operation_lock:
            self._run_coro(self._show_browser_async())

    def hide_browser(self) -> None:
        with self._operation_lock:
            self._run_coro(self._hide_browser_async())

    def open_video_in_browser(self, url: str) -> None:
        with self._operation_lock:
            self._run_coro(self._open_video_in_browser_async(url))

    def close(self) -> None:
        with self._operation_lock:
            self._close_locked()

    def _close_locked(self) -> None:
        if self._loop is None:
            return
        try:
            self._run_coro(self._close_async())
        except Exception:
            pass
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._loop_thread is not None:
            self._loop_thread.join(timeout=3)
        self._loop = None
        self._loop_thread = None
        self._loop_ready.clear()

    async def _login_async(self, timeout_seconds: int) -> bool:
        self._ensure_assets()
        context, page = await self._ensure_browser_session(keep_visible=True)
        await self._ensure_home_ready(page)
        deadline = time.time() + max(30, timeout_seconds)
        while time.time() < deadline:
            if await self._is_logged_in(context, page):
                await self._move_window_offscreen(page)
                return True
            await asyncio.sleep(2)
        return False

    async def _fetch_video_async(self, original_url: str) -> Video:
        self._ensure_assets()
        context, page = await self._ensure_browser_session(keep_visible=False)
        await self._move_window_offscreen(page)
        await self._ensure_home_ready(page)

        if not await self._is_logged_in(context, page):
            raise RuntimeError("Douyin login is required. Please open Settings and click 'Login to Douyin' first.")

        detail_id = await self._resolve_detail_id(page, original_url)
        if not detail_id:
            raise RuntimeError("Could not resolve a valid Douyin video ID for this URL.")
        self._debug("fetch.resolve", input_url=original_url, page_url=page.url, detail_id=detail_id)

        referer_url = (page.url or "").strip()
        if not referer_url or "douyin.com/video/" not in referer_url:
            referer_url = (original_url or "").strip()
        aweme_detail = await self._request_video_detail(context, page, detail_id, referer_url=referer_url)
        if not aweme_detail:
            raise RuntimeError(
                "Douyin returned an empty video detail. Your login session may have expired, so please login again."
            )
        video = self._video_from_aweme_detail(aweme_detail, original_url)
        self._debug(
            "fetch.video",
            detail_id=video.id,
            media_url=video.media_url or "",
            image_count=len(video.image_urls),
            duration=video.duration,
            author=video.author or "",
            title=(video.title or "")[:120],
        )
        return video

    async def _fetch_profile_videos_paged_async(
        self,
        original_url: str,
        start: int,
        count: int,
    ) -> tuple[list[Video], bool, Profile | None]:
        self._ensure_assets()
        context, page = await self._ensure_browser_session(keep_visible=False)
        await self._move_window_offscreen(page)
        await self._ensure_home_ready(page)

        if not await self._is_logged_in(context, page):
            raise RuntimeError("Douyin login is required. Please open Settings and click 'Login to Douyin' first.")

        sec_user_id = await self._resolve_sec_user_id(page, original_url)
        if not sec_user_id:
            raise RuntimeError("Could not resolve a valid Douyin profile ID for this URL.")

        profile_payload = await self._request_user_profile(context, page, sec_user_id)
        profile = self._profile_from_user_info(profile_payload, original_url, sec_user_id)

        offset = max(0, int(start) - 1)
        target_count = max(1, int(count))
        collected: list[Video] = []
        skipped = 0
        max_cursor = "0"
        has_more = True

        while len(collected) < target_count and has_more:
            posts_payload = await self._request_user_posts(context, page, sec_user_id, max_cursor, target_count)
            aweme_list = posts_payload.get("aweme_list") or []
            if not isinstance(aweme_list, list):
                aweme_list = []
            has_more = bool(posts_payload.get("has_more"))
            max_cursor = str(posts_payload.get("max_cursor") or "0")

            if skipped + len(aweme_list) <= offset:
                skipped += len(aweme_list)
                if not has_more:
                    break
                continue

            start_index = max(0, offset - skipped)
            for aweme in aweme_list[start_index:]:
                if not isinstance(aweme, dict):
                    continue
                collected.append(self._video_from_aweme_detail(aweme, original_url))
                if len(collected) >= target_count:
                    break
            skipped += len(aweme_list)
            if not has_more:
                break

        self._debug(
            "profile.fetch",
            profile_input=original_url,
            sec_user_id=sec_user_id,
            start=start,
            count=count,
            returned=len(collected),
            has_more=has_more,
        )
        return collected, has_more, profile

    async def _download_video_async(
        self,
        video: Video,
        output_dir: Path,
        progress_hook: Callable[[dict], None] | None = None,
    ) -> Path:
        self._ensure_assets()
        context, page = await self._ensure_browser_session(keep_visible=False)
        await self._move_window_offscreen(page)
        await self._ensure_home_ready(page)

        if not await self._is_logged_in(context, page):
            raise RuntimeError("Douyin login is required before download.")

        refreshed_video = video
        if not refreshed_video.media_url and not refreshed_video.image_urls:
            refreshed_video = await self._fetch_video_async(video.url)
        refreshed_video.is_downloaded = video.is_downloaded
        refreshed_video.downloaded_path = video.downloaded_path
        self._debug(
            "download.refresh",
            detail_id=refreshed_video.id,
            media_url=refreshed_video.media_url or "",
            image_count=len(refreshed_video.image_urls),
            output_dir=str(output_dir),
        )

        output_dir.mkdir(parents=True, exist_ok=True)
        if refreshed_video.image_urls and not refreshed_video.media_url:
            return await self._download_gallery_async(page, refreshed_video, output_dir, progress_hook)
        if not refreshed_video.media_url:
            raise RuntimeError(
                f"Could not resolve a playable Douyin media URL for this post. "
                f"See {self._debug_log_path.name} for details."
            )
        try:
            return await self._download_media_async(page, refreshed_video, output_dir, progress_hook)
        except Exception as exc:
            self._debug(
                "download.media.error",
                detail_id=refreshed_video.id,
                error=exc,
            )
            raise

    async def _show_browser_async(self) -> None:
        self._ensure_assets()
        _context, page = await self._ensure_browser_session(keep_visible=True)
        await self._ensure_home_ready(page)

    async def _hide_browser_async(self) -> None:
        self._ensure_assets()
        _context, page = await self._ensure_browser_session(keep_visible=False)
        await self._move_window_offscreen(page)

    async def _open_video_in_browser_async(self, url: str) -> None:
        self._ensure_assets()
        context, page = await self._ensure_browser_session(keep_visible=True)
        await self._ensure_home_ready(page)
        if not await self._is_logged_in(context, page):
            raise RuntimeError("Douyin login is required. Please open Settings and click 'Login to Douyin' first.")

        target = (url or "").strip()
        if not target:
            raise RuntimeError("Missing Douyin URL to open in browser.")

        await page.goto(target, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(0.6)
        try:
            await page.bring_to_front()
        except Exception:
            pass
        self._window_hidden = False

    def _run_coro(self, coro: Any) -> Any:
        self._ensure_loop()
        if self._loop is None:
            raise RuntimeError("Could not initialize the Douyin background loop.")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()

    def _ensure_loop(self) -> None:
        if self._loop is not None and self._loop_thread is not None and self._loop_thread.is_alive():
            return

        self._loop_ready.clear()

        def _runner() -> None:
            loop = asyncio.new_event_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)
            self._loop_ready.set()
            loop.run_forever()
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

        self._loop_thread = Thread(target=_runner, daemon=True, name="douyin-playwright-loop")
        self._loop_thread.start()
        self._loop_ready.wait(timeout=5)

    async def _ensure_browser_session(self, keep_visible: bool) -> tuple[BrowserContext, Page]:
        if self._context is not None and self._page is not None:
            try:
                _ = self._context.pages
                _ = self._page.url
                if keep_visible:
                    await self._restore_window(self._page)
                else:
                    await self._move_window_offscreen(self._page)
                return self._context, self._page
            except Exception:
                self._context = None
                self._page = None
        if self._context is not None and self._page is None:
            try:
                self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
                _ = self._page.url
                if keep_visible:
                    await self._restore_window(self._page)
                else:
                    await self._move_window_offscreen(self._page)
                return self._context, self._page
            except Exception:
                self._context = None
                self._page = None

        self._playwright = await async_playwright().start()
        self._context = await self._launch_persistent_context()
        if self._stealth_script_path.exists():
            await self._context.add_init_script(path=str(self._stealth_script_path))
        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        if keep_visible:
            await self._restore_window(self._page)
        else:
            await self._move_window_offscreen(self._page)
        return self._context, self._page

    async def _ensure_home_ready(self, page: Page) -> None:
        try:
            current_url = page.url or ""
        except Exception:
            current_url = ""
        if current_url.startswith("https://www.douyin.com/"):
            return
        await page.goto(self._DOUYIN_HOME, wait_until="domcontentloaded", timeout=60000)

    async def _close_async(self) -> None:
        if self._page is not None:
            try:
                await self._page.close()
            except Exception:
                pass
        self._page = None
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:
                pass
        self._context = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
        self._playwright = None
        self._window_hidden = False

    async def _launch_persistent_context(self) -> BrowserContext:
        user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
        launch_kwargs = {
            "user_data_dir": str(self._user_data_dir),
            "accept_downloads": False,
            "headless": True,
            "viewport": {"width": 1920, "height": 1080},
            "device_scale_factor": 1,
            "ignore_default_args": ["--enable-automation"],
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--window-size=1920,1080",
            ],
            "user_agent": user_agent,
        }
        browser_type = self._playwright.chromium  # type: ignore[union-attr]
        attempts: tuple[dict[str, Any], ...] = (
            {"channel": "chrome"},
            {"channel": "msedge"},
            {},
        )
        last_exc: Exception | None = None
        for extra in attempts:
            try:
                context = await browser_type.launch_persistent_context(**launch_kwargs, **extra)
                self._debug("browser.launch", channel=extra.get("channel", "chromium"))
                return context
            except Exception as exc:
                last_exc = exc
                self._debug("browser.launch_error", channel=extra.get("channel", "chromium"), error=exc)
                continue
        raise RuntimeError(f"Could not launch a compatible browser for Douyin: {last_exc}")

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

    async def _resolve_sec_user_id(self, page: Page, url: str) -> str:
        text = (url or "").strip()
        if not text:
            return ""
        if text.startswith("MS4w"):
            return text

        match = re.search(r"/user/([^/?#]+)", text)
        if match:
            return match.group(1).strip()

        if "v.douyin.com" in text or "iesdouyin.com" in text:
            try:
                await page.goto(text, wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(1)
                return await self._resolve_sec_user_id(page, page.url)
            except Exception as exc:
                raise RuntimeError(f"Could not resolve the Douyin profile link: {exc}") from exc

        return ""

    async def _request_video_detail(
        self,
        context: BrowserContext,
        page: Page,
        detail_id: str,
        referer_url: str = "",
    ) -> dict[str, Any]:
        data = await self._request_douyin_get(
            context,
            page,
            self._DETAIL_API,
            {"aweme_id": detail_id},
            "detail",
            referer_override=referer_url or None,
            origin_override=None,
        )
        return data.get("aweme_detail") or {}

    async def _request_user_profile(self, context: BrowserContext, page: Page, sec_user_id: str) -> dict[str, Any]:
        data = await self._request_douyin_get(
            context,
            page,
            self._USER_PROFILE_API,
            {
                "sec_user_id": sec_user_id,
                "publish_video_strategy_type": 2,
                "personal_center_strategy": 1,
            },
            "user_profile",
            origin_override=self._DOUYIN_HOME,
        )
        return data

    async def _request_user_posts(
        self,
        context: BrowserContext,
        page: Page,
        sec_user_id: str,
        max_cursor: str,
        count: int,
    ) -> dict[str, Any]:
        data = await self._request_douyin_get(
            context,
            page,
            self._USER_POST_API,
            {
                "sec_user_id": sec_user_id,
                "count": max(1, min(int(count), 50)),
                "max_cursor": max_cursor,
                "locate_query": "false",
                "publish_video_strategy_type": 2,
            },
            "user_posts",
            origin_override=self._DOUYIN_HOME,
        )
        return data

    async def _request_douyin_get(
        self,
        context: BrowserContext,
        page: Page,
        uri: str,
        params: dict[str, Any],
        debug_label: str,
        *,
        referer_override: str | None = None,
        origin_override: str | None = None,
    ) -> dict[str, Any]:
        cookie_dict = self._cookie_dict(await context.cookies())
        cookie_str = ";".join(f"{key}={value}" for key, value in cookie_dict.items())
        local_storage = await page.evaluate("() => Object.assign({}, window.localStorage)")
        user_agent = await page.evaluate("() => navigator.userAgent")
        request_params = dict(params)
        request_params.update(
            {
                "device_platform": "webapp",
                "aid": "6383",
                "channel": "channel_pc_web",
                "version_code": "190600",
                "version_name": "19.6.0",
                "update_version_code": "170400",
                "pc_client_type": "1",
                "cookie_enabled": "true",
                "browser_language": "zh-CN",
                "browser_platform": "Win32",
                "browser_name": "Chrome",
                "browser_version": "129.0.0.0",
                "browser_online": "true",
                "engine_name": "Blink",
                "engine_version": "109.0",
                "os_name": "Windows",
                "os_version": "10",
                "cpu_core_num": "8",
                "device_memory": "8",
                "platform": "PC",
                "screen_width": "1920",
                "screen_height": "1080",
                "effective_type": "4g",
                "round_trip_time": "50",
                "webid": self._generate_web_id(),
                "msToken": str(local_storage.get("xmst") or cookie_dict.get("msToken") or ""),
            }
        )
        encoded = urlencode(request_params)
        request_params["a_bogus"] = await self._sign_detail(page, encoded, user_agent)

        headers = {
            "User-Agent": user_agent,
            "Cookie": cookie_str,
            "Host": "www.douyin.com",
            "Referer": referer_override or (page.url or self._DOUYIN_HOME),
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json;charset=UTF-8",
        }
        if origin_override is not None:
            headers["Origin"] = origin_override

        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            response = await client.get(f"https://www.douyin.com{uri}" if uri.startswith("/") else uri, params=request_params, headers=headers)
            status = response.status_code
            text = response.text.strip()
        self._debug(
            f"api.{debug_label}",
            uri=uri,
            status=status,
            response_preview=text[:160] or "<empty>",
        )
        if status != 200:
            raise RuntimeError(f"Douyin {debug_label} request failed with HTTP {status}.")
        if not text or text == "blocked":
            raise RuntimeError(
                f"Douyin blocked the current {debug_label} request. "
                f"HTTP {status}, page URL: {page.url}, response preview: {text[:120] or '<empty>'}"
            )
        try:
            data = json.loads(text)
        except Exception as exc:
            raise RuntimeError(f"Douyin returned a non-JSON response for the {debug_label} request.") from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"Douyin returned an unexpected response shape for {debug_label}.")
        return data

    async def _download_media_async(
        self,
        page: Page,
        video: Video,
        output_dir: Path,
        progress_hook: Callable[[dict], None] | None,
    ) -> Path:
        media_url = str(video.media_url or "").strip()
        if not media_url:
            raise RuntimeError("This Douyin video does not include a direct media URL.")

        output_path = self._build_output_path(output_dir, video, media_url)
        user_agent = await page.evaluate("() => navigator.userAgent")
        headers = {
            "User-Agent": user_agent,
            "Referer": page.url or self._DOUYIN_HOME,
            "Accept": "*/*",
        }
        response = await page.context.request.get(media_url, headers=headers, fail_on_status_code=True)
        content_type = str(response.headers.get("content-type") or "").lower()
        response_url = getattr(response, "url", media_url)
        self._debug(
            "download.media.response",
            detail_id=video.id,
            media_url=media_url,
            response_url=response_url,
            status=getattr(response, "status", ""),
            content_type=content_type,
        )
        if "text/html" in content_type:
            raise RuntimeError(
                f"The Douyin media URL returned HTML instead of video content. "
                f"See {self._debug_log_path.name} for details."
            )
        try:
            content = await response.body()
            self._debug(
                "download.media.body",
                detail_id=video.id,
                bytes=len(content),
            )
        except Exception as exc:
            self._debug(
                "download.media.body_error",
                detail_id=video.id,
                error=exc,
            )
            raise RuntimeError(
                f"Could not read the Douyin media response body. "
                f"See {self._debug_log_path.name} for details."
            ) from exc
        total = int(response.headers.get("content-length") or len(content) or 0)
        if not content:
            raise RuntimeError(
                f"Douyin returned an empty media body for this video. "
                f"See {self._debug_log_path.name} for details."
            )
        if progress_hook is not None:
            progress_hook({"status": "downloading", "downloaded_bytes": 0, "total_bytes": total})
        try:
            output_path.write_bytes(content)
        except Exception as exc:
            self._debug(
                "download.media.write_error",
                detail_id=video.id,
                path=str(output_path),
                error=exc,
            )
            raise RuntimeError(
                f"Could not write the prepared media file. "
                f"See {self._debug_log_path.name} for details."
            ) from exc
        file_size = output_path.stat().st_size if output_path.exists() else 0
        self._debug(
            "download.media.saved",
            detail_id=video.id,
            path=str(output_path),
            bytes=file_size,
        )
        if progress_hook is not None:
            progress_hook(
                {
                    "status": "downloading",
                    "downloaded_bytes": len(content),
                    "total_bytes": total,
                }
            )

        if progress_hook is not None:
            progress_hook({"status": "finished", "filename": str(output_path)})
        return output_path

    async def _download_gallery_async(
        self,
        page: Page,
        video: Video,
        output_dir: Path,
        progress_hook: Callable[[dict], None] | None,
    ) -> Path:
        gallery_dir = output_dir / self._safe_title(video)
        gallery_dir.mkdir(parents=True, exist_ok=True)
        user_agent = await page.evaluate("() => navigator.userAgent")
        headers = {
            "User-Agent": user_agent,
            "Referer": page.url or self._DOUYIN_HOME,
            "Accept": "*/*",
        }
        total = len(video.image_urls)
        for index, image_url in enumerate(video.image_urls, start=1):
            suffix = Path(httpx.URL(image_url).path).suffix.lower() or ".jpg"
            image_path = gallery_dir / f"{index:02d}{suffix}"
            response = await page.context.request.get(image_url, headers=headers, fail_on_status_code=True)
            body = await response.body()
            if not body:
                raise RuntimeError(
                    f"Douyin returned an empty image body for gallery item {index}. "
                    f"See {self._debug_log_path.name} for details."
                )
            image_path.write_bytes(body)
            self._debug(
                "download.gallery.item",
                detail_id=video.id,
                index=index,
                response_url=getattr(response, "url", image_url),
                status=getattr(response, "status", ""),
                content_type=str(response.headers.get("content-type") or ""),
                bytes=len(body),
            )
            if progress_hook is not None:
                progress_hook(
                    {
                        "status": "downloading",
                        "downloaded_bytes": index,
                        "total_bytes": total,
                    }
                )
        if progress_hook is not None:
            progress_hook({"status": "finished", "filename": str(gallery_dir)})
        return gallery_dir

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

    async def _move_window_offscreen(self, page: Page) -> None:
        if self._window_hidden:
            return
        await self._set_window_bounds(
            page,
            {
                "windowState": "normal",
                "left": -2400,
                "top": 80,
                "width": 1920,
                "height": 1080,
            },
        )
        self._window_hidden = True

    async def _restore_window(self, page: Page) -> None:
        if not self._window_hidden:
            return
        await self._set_window_bounds(
            page,
            {
                "windowState": "normal",
                "left": 120,
                "top": 80,
                "width": 1920,
                "height": 1080,
            },
        )
        self._window_hidden = False

    async def _set_window_bounds(self, page: Page, bounds: dict[str, Any]) -> None:
        try:
            session = await page.context.new_cdp_session(page)
            info = await session.send("Browser.getWindowForTarget")
            window_id = info.get("windowId")
            if window_id is None:
                return
            await session.send("Browser.setWindowBounds", {"windowId": window_id, "bounds": bounds})
        except Exception:
            pass

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

    @staticmethod
    def _safe_title(video: Video) -> str:
        title = (video.title or video.id or "video").strip()
        cleaned_chars: list[str] = []
        for ch in title:
            if ord(ch) < 32:
                cleaned_chars.append(" ")
                continue
            if ch in '<>:"/\\|?*':
                cleaned_chars.append(" ")
                continue
            cleaned_chars.append(ch)
        safe_title = "".join(cleaned_chars)
        safe_title = re.sub(r"\s+", " ", safe_title).strip().rstrip(".")
        if len(safe_title) > 120:
            safe_title = safe_title[:120].rstrip()
        return safe_title or video.id or "video"

    def _build_output_path(self, output_dir: Path, video: Video, media_url: str = "") -> Path:
        ext = ".mp4"
        suffix = Path(httpx.URL(media_url).path).suffix.lower() if media_url else ""
        if suffix and len(suffix) <= 5:
            ext = suffix
        return output_dir / f"{self._safe_title(video)}{ext}"

    def _video_from_aweme_detail(self, data: dict[str, Any], original_url: str) -> Video:
        statistics = data.get("statistics") or {}
        author = data.get("author") or {}
        music = data.get("music") or {}
        video_item = data.get("video") or {}
        view_count = self._resolve_view_count(data, statistics)

        upload_time = None
        create_time = data.get("create_time")
        if isinstance(create_time, (int, float)):
            upload_time = datetime.fromtimestamp(create_time)

        thumbnail_url = self._pick_last_url(
            (video_item.get("raw_cover") or {}).get("url_list"),
            (video_item.get("origin_cover") or {}).get("url_list"),
            (author.get("avatar_thumb") or {}).get("url_list"),
        )
        media_url = self._resolve_media_url(data, video_item)
        image_urls = tuple(self._extract_image_urls(data))
        if not thumbnail_url and image_urls:
            thumbnail_url = image_urls[0]

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
            image_urls=image_urls,
            description=data.get("desc") or None,
            duration=self._duration_to_seconds(data.get("duration")),
            thumbnail_url=thumbnail_url,
            author=author.get("nickname") or None,
            uploader=author.get("uid") or author.get("sec_uid") or None,
            view_count=view_count,
            like_count=self._to_int(statistics.get("digg_count")),
            comment_count=self._to_int(statistics.get("comment_count")),
            share_count=self._to_int(statistics.get("share_count")),
            bookmark_count=self._to_int(statistics.get("collect_count")),
            upload_time=upload_time,
            tags=tuple(tags),
            music_title=music.get("title") or None,
        )

    def _profile_from_user_info(self, data: dict[str, Any], original_url: str, sec_user_id: str) -> Profile:
        user = data.get("user") or data.get("user_info") or data.get("userInfo") or data
        stats = data.get("user_info_stats") or data.get("stats") or data.get("user_stats") or {}
        if isinstance(user, dict):
            fallback_stats = user.get("stats") or user.get("author_stats") or {}
            if isinstance(fallback_stats, dict) and not stats:
                stats = fallback_stats
        if not isinstance(user, dict):
            user = {}
        if not isinstance(stats, dict):
            stats = {}

        username = str(
            user.get("unique_id")
            or user.get("short_id")
            or user.get("nickname")
            or ""
        ).strip()
        display_name = str(user.get("nickname") or username or "Douyin Profile").strip()
        avatar_url = self._pick_last_url(
            (user.get("avatar_larger") or {}).get("url_list"),
            (user.get("avatar_medium") or {}).get("url_list"),
            (user.get("avatar_thumb") or {}).get("url_list"),
        ) or (user.get("avatar") if isinstance(user.get("avatar"), str) else None)
        return Profile(
            username=username or sec_user_id,
            display_name=display_name or sec_user_id,
            avatar_url=avatar_url,
            follower_count=self._to_int(
                stats.get("follower_count") or stats.get("fans") or user.get("follower_count") or user.get("total_favorited")
            ),
            following_count=self._to_int(
                stats.get("following_count") or stats.get("follow_count") or stats.get("follows") or user.get("following_count")
            ),
            like_count=self._to_int(
                stats.get("total_favorited") or stats.get("favoriting_count") or stats.get("interaction") or user.get("total_favorited")
            ),
            video_count=self._to_int(
                stats.get("aweme_count") or stats.get("video_count") or user.get("aweme_count")
            ),
        )

    @staticmethod
    def _pick_last_url(*lists: Any) -> str | None:
        for item in lists:
            if isinstance(item, list) and item:
                for value in reversed(item):
                    if isinstance(value, str) and value.strip():
                        return value.strip()
        return None

    def _resolve_media_url(self, data: dict[str, Any], video_item: dict[str, Any]) -> str | None:
        direct = self._pick_last_url(
            (video_item.get("play_addr_h264") or {}).get("url_list"),
            (video_item.get("play_addr_256") or {}).get("url_list"),
            (video_item.get("play_addr") or {}).get("url_list"),
            (video_item.get("play_addr_lowbr") or {}).get("url_list"),
            (video_item.get("download_addr") or {}).get("url_list"),
            (data.get("download_addr") or {}).get("url_list"),
        )
        if direct:
            return direct

        bit_rates = video_item.get("bit_rate") or video_item.get("bitRate") or []
        if isinstance(bit_rates, list):
            for item in bit_rates:
                if not isinstance(item, dict):
                    continue
                candidate = self._pick_last_url(
                    (item.get("play_addr_h264") or {}).get("url_list"),
                    (item.get("play_addr_265") or {}).get("url_list"),
                    (item.get("play_addr_264") or {}).get("url_list"),
                    (item.get("play_addr") or {}).get("url_list"),
                    (item.get("play_addr_lowbr") or {}).get("url_list"),
                    (item.get("download_addr") or {}).get("url_list"),
                )
                if candidate:
                    return candidate
        return None

    def _extract_image_urls(self, data: dict[str, Any]) -> list[str]:
        images: list[str] = []
        for image in data.get("images") or []:
            if not isinstance(image, dict):
                continue
            image_url = self._pick_last_url(
                image.get("url_list"),
                (image.get("display_image") or {}).get("url_list"),
                (image.get("origin_url") or {}).get("url_list"),
                image.get("download_url_list"),
            )
            if image_url:
                images.append(image_url)
        return images

    @staticmethod
    def _to_int(value: Any) -> int | None:
        if value in (None, ""):
            return None
        try:
            return int(float(value))
        except Exception:
            text = str(value).strip().lower().replace(",", "")
            if not text:
                return None
            multiplier = 1
            if text.endswith(("k",)):
                multiplier = 1_000
                text = text[:-1]
            elif text.endswith(("m",)):
                multiplier = 1_000_000
                text = text[:-1]
            elif text.endswith(("b",)):
                multiplier = 1_000_000_000
                text = text[:-1]
            elif text.endswith(("w", "万")):
                multiplier = 10_000
                text = text[:-1]
            elif text.endswith("亿"):
                multiplier = 100_000_000
                text = text[:-1]
            try:
                return int(float(text) * multiplier)
            except Exception:
                return None

    def _resolve_view_count(self, data: dict[str, Any], statistics: dict[str, Any]) -> int | None:
        like_count = self._to_int(statistics.get("digg_count"))
        comment_count = self._to_int(statistics.get("comment_count"))
        share_count = self._to_int(statistics.get("share_count"))
        bookmark_count = self._to_int(statistics.get("collect_count"))

        for source in (
            statistics,
            data.get("statistics_v2") or {},
            data.get("statisticsV2") or {},
            data.get("aweme_statistics") or {},
            data,
        ):
            if not isinstance(source, dict):
                continue
            for key in (
                "play_count",
                "play_cnt",
                "view_count",
                "vv_count",
                "display_play_count",
                "display_view_count",
            ):
                value = self._to_int(source.get(key))
                if value is None:
                    continue
                if value == 0 and any((like_count, comment_count, share_count, bookmark_count)):
                    continue
                return value
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

    def _debug(self, event: str, **fields: Any) -> None:
        try:
            parts = [f"{key}={self._debug_value(value)}" for key, value in fields.items()]
            line = f"{datetime.now().isoformat(timespec='seconds')} [{event}] " + " ".join(parts)
            with self._debug_log_path.open("a", encoding="utf-8", errors="ignore") as handle:
                handle.write(line + "\n")
        except Exception:
            pass

    @staticmethod
    def _debug_value(value: Any) -> str:
        text = str(value)
        text = text.replace("\r", " ").replace("\n", " ").strip()
        if len(text) > 220:
            text = text[:217] + "..."
        return text
