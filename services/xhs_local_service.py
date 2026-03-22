from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
import time
from datetime import datetime
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any, Callable
from urllib.parse import quote, urlparse, parse_qs

import httpx
from playwright.async_api import BrowserContext, Page, async_playwright

from models.profile import Profile
from models.video import Video
from services.xhs_sign import b64_encode, encode_utf8, get_trace_id, mrc


class XhsLocalService:
    # Default to Rednote (international). Xiaohongshu is fallback only.
    _HOME = "https://www.rednote.com/"
    # Use explore to surface the login UI on Rednote.
    _LOGIN_HOME = "https://www.rednote.com/explore"
    _API_HOST = "https://edith.rednote.com"
    _SEARCH_API = "/api/sns/web/v1/search/notes"
    _FEED_API = "/api/sns/web/v1/feed"
    _USER_POST_API = "/api/sns/web/v1/user_posted"
    _SELF_API = "/api/sns/web/v1/user/selfinfo"
    _USER_ME_API = "/api/sns/web/v2/user/me"

    def __init__(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        self._user_data_dir = project_root / "browser_data" / "xhs_app_user"
        self._user_data_dir.mkdir(parents=True, exist_ok=True)
        self._stealth_script_path = project_root / "assets" / "douyin" / "stealth.min.js"
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: Thread | None = None
        self._loop_ready = Event()
        self._operation_lock = Lock()
        self._playwright: Any | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._window_hidden = False
        self._login_in_progress = False
        self._debug_enabled = False
        self._log_path = project_root / "xhs_debug.log"
        self._search_listener_attached = False

    @staticmethod
    def is_xhs_url(url: str) -> bool:
        lowered = (url or "").lower()
        return (
            "rednote.com/" in lowered
            or "xhslink.com/" in lowered
            or "xiaohongshu.com/" in lowered
        )

    @staticmethod
    def _normalize_xhs_url(url: str) -> str:
        text = (url or "").strip()
        if not text:
            return ""
        parsed = urlparse(text)
        host = (parsed.netloc or "").lower()
        if host.endswith("rednote.com"):
            return parsed._replace(scheme="https", netloc="www.rednote.com").geturl()
        if host in {"rednote.com", "www.rednote.com"}:
            return parsed._replace(scheme="https", netloc="www.rednote.com").geturl()
        if host.endswith("xiaohongshu.com"):
            return parsed._replace(scheme="https", netloc="www.xiaohongshu.com").geturl()
        if host in {"xiaohongshu.com", "www.xiaohongshu.com"}:
            return parsed._replace(scheme="https", netloc="www.xiaohongshu.com").geturl()
        return text

    @staticmethod
    def is_xhs_user_id(value: str) -> bool:
        text = (value or "").strip().lower()
        return len(text) == 24 and all(ch in "0123456789abcdef" for ch in text)

    def has_login_session(self, strict: bool = True) -> bool:
        if strict:
            # Prefer a real validity check to avoid false positives.
            if not self._user_data_dir.exists():
                return False
            try:
                if not any(self._user_data_dir.iterdir()):
                    return False
            except Exception:
                return False
            try:
                return bool(self._run_coro(self._has_valid_login_session_async()))
            except Exception:
                return False
        # Lightweight hint for UI that does not launch a browser.
        if not self._user_data_dir.exists():
            return False
        try:
            entries = list(self._user_data_dir.iterdir())
        except Exception:
            return False
        if not entries:
            return False
        # Look for common Chromium profile artifacts as a soft signal.
        for entry in entries:
            name = entry.name.lower()
            if name in {"cookies", "local state", "preferences", "login data"}:
                return True
        return True

    def login(self, timeout_seconds: int = 240) -> bool:
        with self._operation_lock:
            self._login_in_progress = True
            try:
                return bool(self._run_coro(self._login_async(timeout_seconds)))
            finally:
                self._login_in_progress = False

    def enable_debug(self, enabled: bool = True) -> None:
        self._debug_enabled = bool(enabled)

    def _debug(self, message: str, **data: Any) -> None:
        if not self._debug_enabled:
            return
        try:
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            payload = {"message": message} | data
            line = f"[{stamp}] {json.dumps(payload, ensure_ascii=False)}\n"
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_path.open("a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass

    def fetch_video(self, url: str) -> Video:
        with self._operation_lock:
            result = self._run_coro(self._fetch_video_async(url))
        if not isinstance(result, Video):
            raise RuntimeError("Rednote fetch did not return a valid video object.")
        return result

    def fetch_profile_videos_paged(self, url: str, start: int, count: int) -> tuple[list[Video], bool, Profile | None]:
        with self._operation_lock:
            result = self._run_coro(self._fetch_profile_videos_paged_async(url, start, count))
        if not isinstance(result, tuple) or len(result) != 3:
            raise RuntimeError("Rednote profile fetch did not return a valid result.")
        return result

    def search_by_keyword(
        self,
        keyword: str,
        offset: int = 0,
        count: int = 20,
        options: dict[str, Any] | None = None,
    ) -> tuple[list[Video], bool, int, str]:
        with self._operation_lock:
            result = self._run_coro(self._search_by_keyword_async(keyword, offset, count, options or {}))
        if not isinstance(result, tuple) or len(result) != 4:
            raise RuntimeError("Rednote search did not return a valid result.")
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
            raise RuntimeError("Rednote download did not return a valid path.")
        return result

    def get_stream_candidates(self, url: str) -> list[str]:
        with self._operation_lock:
            return list(self._run_coro(self._get_stream_candidates_async(url)))

    async def _get_stream_candidates_async(self, url: str) -> list[str]:
        context, page = await self._ensure_browser_session(keep_visible=self._debug_enabled)
        if self._debug_enabled:
            await self._restore_window(page)
        else:
            await self._move_window_offscreen(page)
        await self._ensure_home_ready(page)
        note_id, xsec_token, xsec_source = self._parse_note_url(url or "")
        if not note_id:
            return []
        try:
            note_detail = await self._request_note_detail(page, note_id, xsec_source, xsec_token)
        except Exception:
            note_detail = None
        if not isinstance(note_detail, dict):
            return []
        candidates = self._collect_video_urls(note_detail)
        return candidates

    def show_browser(self) -> None:
        with self._operation_lock:
            self._run_coro(self._show_browser_async())

    def hide_browser(self) -> None:
        with self._operation_lock:
            self._run_coro(self._hide_browser_async())

    def close(self) -> None:
        with self._operation_lock:
            self._close_locked()

    def clear_session(self) -> None:
        with self._operation_lock:
            self._clear_session_locked()

    def _clear_session_locked(self) -> None:
        # Close any active browser context first.
        self._close_locked()
        try:
            if self._user_data_dir.exists():
                for item in self._user_data_dir.iterdir():
                    if item.is_dir():
                        for sub in item.rglob("*"):
                            try:
                                if sub.is_file():
                                    sub.unlink()
                            except Exception:
                                pass
                        try:
                            item.rmdir()
                        except Exception:
                            pass
                    else:
                        try:
                            item.unlink()
                        except Exception:
                            pass
        except Exception:
            pass

    def _run_coro(self, coro: Any) -> Any:
        self._ensure_loop()
        if self._loop is None:
            raise RuntimeError("Could not initialize the Rednote background loop.")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()

    @staticmethod
    def _build_search_page_url(keyword: str) -> str:
        term = quote((keyword or "").strip())
        if not term:
            return "https://www.rednote.com/search_result"
        return f"https://www.rednote.com/search_result?keyword={term}"

    def _attach_search_interceptor(self, page: Page) -> None:
        if self._search_listener_attached:
            return

        def _pick_headers(headers: dict[str, str]) -> dict[str, str]:
            picked: dict[str, str] = {}
            for key in ("content-type", "referer", "origin", "user-agent"):
                value = headers.get(key)
                if value:
                    picked[key] = value
            return picked

        async def _on_request(request) -> None:  # type: ignore[no-untyped-def]
            try:
                url = request.url
                if "/api/sns/web/v1/search/notes" not in url:
                    return
                payload = None
                try:
                    payload = request.post_data_json
                except Exception:
                    payload = None
                if payload is None:
                    try:
                        payload = request.post_data
                    except Exception:
                        payload = None
                self._debug(
                    "search.intercept",
                    url=url,
                    method=request.method,
                    headers=_pick_headers(request.headers or {}),
                    payload=payload,
                )
            except Exception:
                pass

        try:
            page.on("request", _on_request)
            self._search_listener_attached = True
        except Exception:
            pass

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

        self._loop_thread = Thread(target=_runner, daemon=True, name="xhs-playwright-loop")
        self._loop_thread.start()
        self._loop_ready.wait(timeout=5)

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

    async def _launch_persistent_context(self) -> BrowserContext:
        launch_kwargs = {
            "user_data_dir": str(self._user_data_dir),
            "headless": False,
            "viewport": {"width": 1280, "height": 800},
            "accept_downloads": True,
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
                return await browser_type.launch_persistent_context(**launch_kwargs, **extra)
            except Exception as exc:
                last_exc = exc
                continue
        raise RuntimeError(f"Could not launch a compatible browser for Rednote: {last_exc}")

    async def _ensure_home_ready(self, page: Page) -> None:
        try:
            current_url = page.url or ""
        except Exception:
            current_url = ""
        if current_url.startswith(self._HOME) or current_url.startswith(self._LOGIN_HOME):
            return
        await page.goto(self._LOGIN_HOME, wait_until="domcontentloaded", timeout=60000)
        await self._ensure_mnsv2_ready(page)

    async def _ensure_mnsv2_ready(self, page: Page) -> None:
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                ready = await page.evaluate("() => typeof window.mnsv2 === 'function'")
            except Exception:
                ready = False
            if ready:
                return
            await asyncio.sleep(0.3)
        try:
            await page.reload(wait_until="domcontentloaded", timeout=30000)
        except Exception:
            return

    async def _login_async(self, timeout_seconds: int) -> bool:
        context, page = await self._ensure_browser_session(keep_visible=True)
        await self._restore_window(page)
        try:
            await page.bring_to_front()
        except Exception:
            pass
        # Keep login UX on Rednote, but all data APIs stay on Rednote domain.
        try:
            await page.goto(self._LOGIN_HOME, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass
        initial_web_session = await self._get_web_session_cookie(context)
        deadline = time.time() + max(30, timeout_seconds)
        while time.time() < deadline:
            # Strict verification to avoid false positives before QR login completes.
            if await self._is_logged_in(context, page, strict=True):
                await self._move_window_offscreen(page)
                return True
            current_web_session = await self._get_web_session_cookie(context)
            if current_web_session and current_web_session != initial_web_session:
                if await self._is_logged_in(context, page, strict=True):
                    await self._move_window_offscreen(page)
                    return True
            await asyncio.sleep(2)
        return False

    async def _show_browser_async(self) -> None:
        _context, page = await self._ensure_browser_session(keep_visible=True)
        await self._ensure_home_ready(page)

    async def _hide_browser_async(self) -> None:
        _context, page = await self._ensure_browser_session(keep_visible=False)
        await self._move_window_offscreen(page)

    async def _has_valid_login_session_async(self) -> bool:
        context, page = await self._ensure_browser_session(keep_visible=False)
        await self._move_window_offscreen(page)
        await self._ensure_home_ready(page)
        return await self._is_logged_in(context, page, strict=True)

    async def _is_logged_in(self, context: BrowserContext, page: Page, strict: bool = True) -> bool:
        cookies = await context.cookies()
        cookie_dict = self._cookie_dict(cookies)
        has_session_cookie = bool(
            cookie_dict.get("web_session")
            or cookie_dict.get("web_session_id")
            or cookie_dict.get("webid")
            or cookie_dict.get("webId")
            or cookie_dict.get("gid")
        )
        has_a1_cookie = bool(cookie_dict.get("a1"))
        if not has_a1_cookie:
            return False
        try:
            current_url = (page.url or "").lower()
        except Exception:
            current_url = ""
        on_login_page = ("login" in current_url) or ("/web/login" in current_url)
        has_b1_storage = await self._has_b1_storage(page)

        # Quick/passive check used during interactive login polling.
        if not strict:
            return (has_session_cookie or await self._check_login_ui(page)) and not on_login_page

        if on_login_page:
            return False
        # Rednote flow: require session cookie to avoid false positives.
        if not has_session_cookie:
            return False
        if not strict:
            return await self._check_login_ui(page)
        try:
            payload = await self._signed_get(page, self._USER_ME_API, {}, allow_navigation=True)
        except Exception:
            payload = None
        if isinstance(payload, dict):
            if payload.get("user_id") or payload.get("userId"):
                return True
            user = payload.get("user") if isinstance(payload.get("user"), dict) else None
            if isinstance(user, dict) and (user.get("user_id") or user.get("userId") or user.get("nickname")):
                return True
        try:
            payload = await self._signed_get(page, self._SELF_API, {}, allow_navigation=True)
        except Exception:
            payload = None
        if isinstance(payload, dict):
            if payload.get("user_id") or payload.get("userId"):
                return True
            user = payload.get("user") if isinstance(payload.get("user"), dict) else None
            if isinstance(user, dict) and (user.get("user_id") or user.get("userId") or user.get("nickname")):
                return True
        if has_b1_storage and await self._check_login_ui(page):
            return True
        return False

    @staticmethod
    async def _check_login_ui(page: Page) -> bool:
        try:
            # Match the "Me" profile entry, aligned with MediaCrawler.
            selector = "xpath=//a[contains(@href, '/user/profile/')]"
            return bool(await page.is_visible(selector, timeout=800))
        except Exception:
            return False

    async def _get_web_session_cookie(self, context: BrowserContext) -> str:
        try:
            cookies = await context.cookies()
        except Exception:
            return ""
        cookie_dict = self._cookie_dict(cookies)
        return str(cookie_dict.get("web_session") or cookie_dict.get("web_session_id") or "")

    async def _has_login_cookie(self, context: BrowserContext) -> bool:
        try:
            cookies = await context.cookies()
        except Exception:
            return False
        return self._has_login_cookie_dict(self._cookie_dict(cookies))

    @staticmethod
    def _has_login_cookie_dict(cookie_dict: dict[str, str]) -> bool:
        return bool(cookie_dict.get("a1") or cookie_dict.get("web_session") or cookie_dict.get("web_session_id"))

    @staticmethod
    async def _has_b1_storage(page: Page) -> bool:
        try:
            value = await page.evaluate("() => window.localStorage && window.localStorage.getItem('b1')")
        except Exception:
            return False
        return bool(value)

    async def _fetch_video_async(self, original_url: str) -> Video:
        keep_visible = self._debug_enabled or ("rednote.com" in (self._HOME or "").lower())
        context, page = await self._ensure_browser_session(keep_visible=keep_visible)
        if keep_visible:
            await self._restore_window(page)
        else:
            await self._move_window_offscreen(page)
        await self._ensure_home_ready(page)

        if not await self._is_logged_in(context, page):
            raise RuntimeError("Rednote login is required. Please login from Settings first.")

        note_id, xsec_token, xsec_source, resolved_url = await self._resolve_note_info(page, original_url)
        if not note_id:
            raise RuntimeError("Could not resolve a valid Rednote note ID from this URL.")

        note_detail = await self._request_note_detail(page, note_id, xsec_source, xsec_token)
        if not note_detail:
            note_detail = await self._request_note_detail_from_html(page, note_id, xsec_token, xsec_source)
        if not note_detail:
            await self._open_note_detail_page(page, note_id, xsec_token, xsec_source, resolved_url or original_url)
            note_detail = await self._extract_note_from_state(page, note_id)
        if not note_detail:
            note_detail = await self._request_note_detail(page, note_id, xsec_source, xsec_token)
        if not note_detail:
            raise RuntimeError("Rednote returned an empty note detail.")

        return self._video_from_note(note_detail, resolved_url or original_url, xsec_token, xsec_source)
    async def _fetch_profile_videos_paged_async(
        self,
        original_url: str,
        start: int,
        count: int,
    ) -> tuple[list[Video], bool, Profile | None]:
        context, page = await self._ensure_browser_session(keep_visible=self._debug_enabled)
        if self._debug_enabled:
            await self._restore_window(page)
        else:
            await self._move_window_offscreen(page)
        await self._ensure_home_ready(page)

        captured_responses: list[dict[str, Any]] = []
        captured_with_ids: list[dict[str, Any]] = []
        captured_urls: list[dict[str, Any]] = []

        async def _capture_user_posted(response: Any) -> None:
            try:
                url = response.url or ""
            except Exception:
                url = ""
            try:
                status = response.status
            except Exception:
                status = None
            if any(token in url for token in ("/api/", "/sns/web/")):
                if len(captured_urls) < 50:
                    captured_urls.append({"url": url, "status": status})
            try:
                payload = await response.json()
            except Exception:
                return
            if not isinstance(payload, dict):
                return
            if "/api/sns/web/v1/user_posted" in url:
                captured_responses.append(payload)
                self._debug("profile.user_posted_capture", keys=list(payload.keys())[:8])
            # Collect any response payloads that contain note_id values.
            note_ids = self._extract_note_ids_from_payload(payload)
            if note_ids:
                captured_with_ids.append({"url": url, "payload": payload})
                self._debug("profile.capture_with_ids", url=url, count=len(note_ids), sample=note_ids[:3])

        def _on_response(response: Any) -> None:
            try:
                asyncio.create_task(_capture_user_posted(response))
            except Exception:
                pass

        page.on("response", _on_response)

        self._debug("profile.start", url=original_url, start=start, count=count)
        if not await self._is_logged_in(context, page):
            self._debug("profile.login_missing")
            raise RuntimeError("Rednote login is required. Please login from Settings first.")

        user_id, xsec_token, xsec_source = await self._resolve_creator_info(page, original_url)
        if not user_id:
            self._debug("profile.user_id_missing")
            raise RuntimeError("Could not resolve a valid Rednote profile ID for this URL.")
        self._debug("profile.user_id", user_id=user_id, xsec_source=xsec_source, xsec_token=bool(xsec_token))
        await self._open_profile_page(page, user_id, xsec_token, xsec_source, original_url)

        offset = max(0, int(start) - 1)
        target_count = max(1, int(count))
        collected: list[Video] = []
        skipped = 0
        cursor = ""
        has_more = True
        profile = await self._profile_from_creator_page(page, user_id, xsec_token, xsec_source)

        while len(collected) < target_count and has_more:
            try:
                payload = await self._request_user_posts(page, user_id, cursor, target_count, xsec_token, xsec_source)
                self._debug("profile.user_posted", cursor=cursor, has_notes=isinstance(payload.get("notes"), list), has_more=payload.get("has_more"))
            except Exception as exc:
                self._debug("profile.user_posted_error", cursor=cursor, error=str(exc))
                payload = {}
            notes = payload.get("notes") if isinstance(payload, dict) else None
            notes_missing_ids = False
            if isinstance(notes, list) and notes:
                sample = notes[0]
                try:
                    self._debug("profile.note_sample", sample=sample)
                except Exception:
                    pass

            if isinstance(notes, list) and notes:
                notes_missing_ids = True
                for item in notes:
                    if not isinstance(item, dict):
                        continue
                    candidate = (
                        item.get("note_id")
                        or item.get("noteId")
                        or item.get("id")
                        or (item.get("note") or {}).get("note_id")
                        or (item.get("note") or {}).get("noteId")
                        or (item.get("note") or {}).get("id")
                    )
                    if candidate:
                        notes_missing_ids = False
                        break
                if notes_missing_ids:
                    self._debug("profile.note_ids_missing", count=len(notes))
                try:
                    preview = []
                    for idx, item in enumerate(notes[:5]):
                        if not isinstance(item, dict):
                            continue
                        candidate = (
                            item.get("note_id")
                            or item.get("noteId")
                            or item.get("id")
                            or (item.get("note") or {}).get("note_id")
                            or (item.get("note") or {}).get("noteId")
                        )
                        preview.append({"i": idx, "note_id": candidate, "keys": list(item.keys())[:8]})
                    self._debug("profile.note_id_preview", preview=preview)
                except Exception:
                    pass
            # Some profile URLs carry xsec_source=pc_note; user_posted is more stable with pc_feed.
            if (not isinstance(notes, list) or not notes) and not cursor and (xsec_source and xsec_source != "pc_feed"):
                try:
                    payload = await self._request_user_posts(page, user_id, cursor, target_count, xsec_token, "pc_feed")
                    notes = payload.get("notes") if isinstance(payload, dict) else None
                except Exception:
                    notes = None
            if (not isinstance(notes, list) or not notes) and not cursor and xsec_token:
                try:
                    payload = await self._request_user_posts(page, user_id, cursor, target_count, "", "pc_feed")
                    notes = payload.get("notes") if isinstance(payload, dict) else None
                except Exception:
                    notes = None

            if (not isinstance(notes, list) or not notes or notes_missing_ids) and not cursor:
                notes = []
                if captured_responses:
                    notes = self._extract_notes_from_captured(captured_responses[-1])
                if not notes and captured_with_ids:
                    notes = self._extract_notes_from_captured(captured_with_ids[-1].get("payload") or {})
                if not notes:
                    notes = await self._extract_profile_notes_from_page(page)
                if not notes:
                    notes = await self._extract_profile_notes_from_dom(
                        context,
                        page,
                        target_count,
                        xsec_token,
                        xsec_source,
                    )
                self._debug("profile.dom_fallback", notes=len(notes))
                payload = {"notes": notes, "has_more": False, "cursor": ""}

            notes = payload.get("notes") or []
            if not isinstance(notes, list):
                notes = []
            has_more = bool(payload.get("has_more"))
            cursor = str(payload.get("cursor") or "")

            if skipped + len(notes) <= offset:
                skipped += len(notes)
                if not has_more:
                    break
                continue

            start_index = max(0, offset - skipped)
            for note_item in notes[start_index:]:
                if not isinstance(note_item, dict):
                    continue
                note = note_item.get("note_card") or note_item.get("note") or note_item.get("noteCard") or note_item
                if not isinstance(note, dict):
                    continue

                note_id = str(
                    note.get("note_id")
                    or note.get("noteId")
                    or note.get("id")
                    or note_item.get("note_id")
                    or note_item.get("noteId")
                    or note_item.get("id")
                    or ""
                ).strip()
                item_xsec_token = str(
                    note_item.get("xsec_token")
                    or note_item.get("xsecToken")
                    or note.get("xsec_token")
                    or note.get("xsecToken")
                    or xsec_token
                    or ""
                )
                item_xsec_source = str(
                    note_item.get("xsec_source")
                    or note_item.get("xsecSource")
                    or note.get("xsec_source")
                    or note.get("xsecSource")
                    or xsec_source
                    or "pc_feed"
                )
                if note_id and not note.get("note_id"):
                    note = dict(note)
                    note["note_id"] = note_id
                if not note_id:
                    self._debug("profile.note_id_missing", note=note)
                    continue

                if note_id and not note.get("video") and not note.get("image_list"):
                    try:
                        detailed = await self._request_note_detail(page, note_id, item_xsec_source, item_xsec_token)
                        if isinstance(detailed, dict) and detailed:
                            note = detailed
                    except Exception:
                        pass

                collected.append(
                    self._video_from_note(
                        note,
                        str(note.get("_original_url") or self._build_note_url(note_id, item_xsec_token, item_xsec_source)),
                        item_xsec_token,
                        item_xsec_source,
                    )
                )
                if len(collected) >= target_count:
                    break
            skipped += len(notes)
            if not has_more:
                break

        if profile is None and collected:
            profile = self._profile_from_note(collected[0])

        try:
            page.off("response", _on_response)
        except Exception:
            pass
        if captured_urls:
            try:
                self._debug("profile.captured_urls", urls=captured_urls[:20])
            except Exception:
                pass
        return collected, has_more, profile

    async def _search_by_keyword_async(
        self,
        keyword: str,
        offset: int,
        count: int,
        options: dict[str, Any],
    ) -> tuple[list[Video], bool, int, str]:
        context, page = await self._ensure_browser_session(keep_visible=self._debug_enabled)
        if self._debug_enabled:
            await self._restore_window(page)
        else:
            await self._move_window_offscreen(page)
        await self._ensure_home_ready(page)

        if not await self._is_logged_in(context, page):
            raise RuntimeError("Rednote login is required. Please login from Settings first.")

        term = (keyword or "").strip()
        if not term:
            return [], False, 0, ""

        page_index = max(1, int(offset or 1))
        page_size = max(1, min(int(count or 20), 30))
        if self._debug_enabled:
            self._attach_search_interceptor(page)
        sort_label = str(options.get("sort_label") or "")
        sort_value = "general"
        sort_tag = "general"
        if sort_label == "Newest":
            sort_tag = "time_descending"
        elif sort_label == "Most Liked":
            sort_tag = "popularity_descending"
        elif sort_label == "Most Commented":
            sort_tag = "comment_descending"
        elif sort_label == "Most Collected":
            sort_tag = "collect_descending"
        note_label = str(options.get("note_type_label") or "")
        note_tag = "不限"
        if note_label == "Video":
            note_tag = "视频笔记"
        elif note_label == "Image":
            note_tag = "普通笔记"
        time_label = str(options.get("time_label") or "")
        time_tag = "不限"
        if time_label == "Past 24 hours":
            time_tag = "一天内"
        elif time_label == "Past week":
            time_tag = "一周内"
        elif time_label == "Past 6 months":
            time_tag = "半年内"

        search_id = str(options.get("search_id") or "").strip()
        if not search_id:
            search_id = self._get_search_id().lower()
        else:
            search_id = search_id.lower()
        filters_active = (
            sort_tag != "general"
            or note_tag != "不限"
            or time_tag != "不限"
        )
        if filters_active and "@" not in search_id:
            search_id = f"{search_id}@{self._get_search_id().lower()}"

        filters = None
        if filters_active:
            filters = [
                {"tags": [sort_tag], "type": "sort_type"},
                {"tags": [note_tag], "type": "filter_note_type"},
                {"tags": [time_tag], "type": "filter_note_time"},
                {"tags": ["不限"], "type": "filter_note_range"},
                {"tags": ["不限"], "type": "filter_pos_distance"},
            ]

        payload = {
            "keyword": term,
            "page": page_index,
            "page_size": page_size,
            "search_id": search_id,
            "sort": sort_value,
            "note_type": 0,
            "ext_flags": [],
            "geo": "",
            "image_formats": ["jpg", "webp", "avif"],
        }
        if filters is not None:
            payload["filters"] = filters
        if self._debug_enabled:
            try:
                self._debug("search.request", payload=payload, options=options)
                search_url = self._build_search_page_url(term)
                self._debug("search.navigate", url=search_url)
                await page.goto(search_url, wait_until="domcontentloaded", timeout=15000)
            except Exception:
                pass
        try:
            response = await self._signed_post(page, self._SEARCH_API, payload)
        except Exception as exc:
            if self._debug_enabled:
                try:
                    self._debug("search.error", error=str(exc or ""), payload=payload)
                except Exception:
                    pass
            raise
        items = response.get("items") or []
        has_more = bool(response.get("has_more", False))
        try:
            self._debug("search.items", count=len(items) if isinstance(items, list) else 0)
            self._debug("search.response", has_more=has_more, item_count=len(items) if isinstance(items, list) else 0)
        except Exception:
            pass

        videos: list[Video] = []
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("model_type") in {"rec_query", "hot_query"}:
                    continue
                note = (
                    item.get("note_card")
                    or item.get("note")
                    or item.get("noteCard")
                    or item.get("data")
                    or item
                )
                note_id = str(note.get("note_id") or note.get("id") or item.get("id") or "").strip()
                xsec_token = str(item.get("xsec_token") or note.get("xsec_token") or "")
                xsec_source = str(item.get("xsec_source") or note.get("xsec_source") or "pc_search")
                if "note_id" not in note and note_id:
                    note = dict(note)
                    note["note_id"] = note_id
                if not note or not note_id:
                    continue
                if self._debug_enabled:
                    try:
                        self._debug("search.note_keys", keys=list(note.keys())[:12])
                    except Exception:
                        pass
                if not note.get("video") and not note.get("image_list") and not note.get("cover"):
                    try:
                        note = await self._request_note_detail(page, note_id, xsec_source, xsec_token)
                    except Exception:
                        pass
                video_obj = self._video_from_note(
                    note,
                    self._build_note_url(note_id, xsec_token, xsec_source),
                    xsec_token,
                    xsec_source,
                )
                if (video_obj.thumbnail_url is None or (video_obj.author is None and video_obj.uploader is None)) and note_id:
                    try:
                        detailed = await self._request_note_detail(page, note_id, xsec_source, xsec_token)
                    except Exception:
                        detailed = None
                    if isinstance(detailed, dict) and detailed:
                        video_obj = self._video_from_note(
                            detailed,
                            self._build_note_url(note_id, xsec_token, xsec_source),
                            xsec_token,
                            xsec_source,
                        )
                videos.append(video_obj)

        # Server-side filters already reflect the chosen sorting.

        next_offset = page_index + 1 if has_more else page_index
        return videos, has_more, next_offset, search_id

    async def _download_video_async(
        self,
        video: Video,
        output_dir: Path,
        progress_hook: Callable[[dict], None] | None,
    ) -> Path:
        context, page = await self._ensure_browser_session(keep_visible=self._debug_enabled)
        if self._debug_enabled:
            await self._restore_window(page)
        else:
            await self._move_window_offscreen(page)
        await self._ensure_home_ready(page)

        if not await self._is_logged_in(context, page):
            raise RuntimeError("Rednote login is required before download.")

        refreshed_video = video
        # Always try to re-evaluate the best media URL from note detail to prefer CDN stream links.
        if video.url:
            note_id, xsec_token, xsec_source = self._parse_note_url(video.url)
            if note_id:
                try:
                    note_detail = await self._request_note_detail(page, note_id, xsec_source, xsec_token)
                except Exception:
                    note_detail = None
                if isinstance(note_detail, dict):
                    candidates = self._collect_video_urls(note_detail)
                    if candidates:
                        self._debug("download.candidates", count=len(candidates), sample=candidates[:5])
                        refreshed_video = self._video_from_note(note_detail, video.url, xsec_token, xsec_source)
                        refreshed_video.media_url = candidates[0]
                        self._debug("download.selected", url=refreshed_video.media_url)
        if not refreshed_video.media_url and not refreshed_video.image_urls:
            refreshed_video = await self._fetch_video_async(video.url)
        refreshed_video.is_downloaded = video.is_downloaded
        refreshed_video.downloaded_path = video.downloaded_path

        output_dir.mkdir(parents=True, exist_ok=True)
        if refreshed_video.image_urls and not refreshed_video.media_url:
            return await self._download_gallery_async(page, refreshed_video, output_dir, progress_hook)
        if not refreshed_video.media_url:
            raise RuntimeError("Could not resolve a playable Rednote media URL for this post.")
        try:
            return await self._download_media_async(page, refreshed_video, output_dir, progress_hook)
        except Exception as exc:
            self._debug("download.primary_failed", error=str(exc), url=refreshed_video.media_url)
            # Try alternative URLs from note detail if available.
            note_id, xsec_token, xsec_source = self._parse_note_url(video.url or "")
            if note_id:
                try:
                    note_detail = await self._request_note_detail(page, note_id, xsec_source, xsec_token)
                except Exception:
                    note_detail = None
                if isinstance(note_detail, dict):
                    candidates = self._collect_video_urls(note_detail)
                    candidates = [u for u in candidates if u != refreshed_video.media_url]
                    for candidate in candidates:
                        try:
                            refreshed_video.media_url = candidate
                            self._debug("download.try_fallback", url=candidate)
                            return await self._download_media_async(page, refreshed_video, output_dir, progress_hook)
                        except Exception as inner:
                            self._debug("download.fallback_failed", error=str(inner), url=candidate)
                            continue
            raise

    async def _download_media_async(
        self,
        page: Page,
        video: Video,
        output_dir: Path,
        progress_hook: Callable[[dict], None] | None,
    ) -> Path:
        media_url = (video.media_url or "").strip()
        if not media_url:
            raise RuntimeError("Missing media URL for Rednote download.")
        self._debug("download.start", url=media_url)

        output_path = self._build_output_path(output_dir, video, media_url)
        user_agent = await page.evaluate("() => navigator.userAgent")
        headers = {
            "User-Agent": user_agent,
            "Referer": self._HOME,
            "Accept": "*/*",
        }
        response = await page.context.request.get(
            media_url,
            headers=headers,
            fail_on_status_code=True,
            timeout=60000,
        )
        content_type = str(response.headers.get("content-type") or "").lower()
        if "text/html" in content_type:
            raise RuntimeError("The Rednote media URL returned HTML instead of video content.")

        content = await response.body()
        total = int(response.headers.get("content-length") or len(content) or 0)
        if not content:
            raise RuntimeError("Rednote returned an empty media body for this video.")
        if progress_hook is not None:
            progress_hook({"status": "downloading", "downloaded_bytes": 0, "total_bytes": total})
        output_path.write_bytes(content)
        if progress_hook is not None:
            progress_hook({"status": "downloading", "downloaded_bytes": len(content), "total_bytes": total})
            progress_hook({"status": "finished", "filename": str(output_path)})
        self._debug("download.finished", url=media_url, bytes=len(content))
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
            "Referer": self._HOME,
            "Accept": "*/*",
        }
        total = len(video.image_urls)
        for index, image_url in enumerate(video.image_urls, start=1):
            suffix = Path(httpx.URL(image_url).path).suffix.lower() or ".jpg"
            image_path = gallery_dir / f"{index:02d}{suffix}"
            response = await page.context.request.get(image_url, headers=headers, fail_on_status_code=True)
            body = await response.body()
            if not body:
                raise RuntimeError("Rednote returned an empty image body for gallery item.")
            image_path.write_bytes(body)
            if progress_hook is not None:
                progress_hook({"status": "downloading", "downloaded_bytes": index, "total_bytes": total})
        if progress_hook is not None:
            progress_hook({"status": "finished", "filename": str(gallery_dir)})
        return gallery_dir

    async def _resolve_note_info(self, page: Page, url: str) -> tuple[str, str, str, str]:
        normalized_url = self._normalize_xhs_url(url)
        note_id, xsec_token, xsec_source = self._parse_note_url(normalized_url)
        if note_id:
            return note_id, xsec_token, xsec_source, normalized_url

        target = (normalized_url or "").strip()
        if not target:
            return "", "", "", ""
        await page.goto(target, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(0.4)
        resolved = self._normalize_xhs_url(page.url)
        note_id, xsec_token, xsec_source = self._parse_note_url(resolved)
        return note_id, xsec_token, xsec_source, resolved

    async def _open_note_detail_page(
        self,
        page: Page,
        note_id: str,
        xsec_token: str,
        xsec_source: str,
        hint_url: str,
    ) -> None:
        target_url = self._normalize_xhs_url(hint_url or "")
        if "/discovery/item/" in target_url:
            final_url = target_url
        else:
            final_url = self._build_note_url(note_id, xsec_token, xsec_source)
        if not final_url:
            return
        try:
            await page.goto(final_url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(0.4)
        except Exception:
            pass

    async def _open_profile_page(
        self,
        page: Page,
        user_id: str,
        xsec_token: str,
        xsec_source: str,
        hint_url: str,
    ) -> None:
        target_url = self._normalize_xhs_url(hint_url or "")
        if "/user/profile/" in target_url:
            final_url = target_url
        else:
            final_url = f"{self._HOME}user/profile/{user_id}"
            if xsec_token and xsec_source:
                final_url = f"{final_url}?xsec_token={quote(xsec_token)}&xsec_source={quote(xsec_source)}"
        if not final_url:
            return
        try:
            await page.goto(final_url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(0.5)
            try:
                await page.wait_for_selector(".note-item", timeout=15000)
            except Exception:
                pass
            await self._warm_profile_page(page)
        except Exception:
            pass

    async def _warm_profile_page(self, page: Page) -> None:
        try:
            for _ in range(3):
                await page.evaluate("() => window.scrollBy(0, window.innerHeight * 0.9)")
                await asyncio.sleep(0.4)
            await page.evaluate("() => window.scrollTo(0, 0)")
        except Exception:
            pass

    async def _resolve_creator_info(self, page: Page, value: str) -> tuple[str, str, str]:
        text = self._normalize_xhs_url(value)
        if self.is_xhs_user_id(text):
            return text, "", ""
        user_id, xsec_token, xsec_source = self._parse_creator_url(text)
        if user_id:
            return user_id, xsec_token, xsec_source
        await page.goto(text, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(0.4)
        return self._parse_creator_url(self._normalize_xhs_url(page.url))

    async def _extract_note_from_state(self, page: Page, note_id: str) -> dict[str, Any] | None:
        try:
            state = await page.evaluate("() => window.__INITIAL_STATE__ || null")
        except Exception:
            return None
        if not isinstance(state, dict):
            return None
        return self._find_note_in_state(state, note_id)

    def _find_note_in_state(self, state: dict[str, Any], note_id: str) -> dict[str, Any] | None:
        def _try_maps(container: dict[str, Any]) -> dict[str, Any] | None:
            for key in ("noteDetailMap", "noteDetailV2", "noteDetail", "noteMap"):
                note_map = container.get(key)
                if not isinstance(note_map, dict):
                    continue
                entry = note_map.get(note_id) or note_map.get(str(note_id))
                if isinstance(entry, dict):
                    return entry.get("note") or entry.get("noteCard") or entry
            return None

        note_section = state.get("note")
        if isinstance(note_section, dict):
            found = _try_maps(note_section)
            if found:
                return found
        found = _try_maps(state)
        if found:
            return found

        for value in state.values():
            if isinstance(value, dict):
                found = _try_maps(value)
                if found:
                    return found
        return None

    async def _extract_profile_notes_from_page(self, page: Page) -> list[dict[str, Any]]:
        try:
            state = await page.evaluate(
                "() => window.__INITIAL_STATE__ || window.__NEXT_DATA__ || window.__NUXT__ || null"
            )
        except Exception:
            state = None
        if isinstance(state, dict):
            notes = self._find_notes_in_state(state)
            if notes:
                return notes
        try:
            html = await page.content()
        except Exception:
            html = ""
        if html:
            next_state = self._extract_next_data_from_html(html)
            if isinstance(next_state, dict):
                notes = self._find_notes_in_state(next_state)
                if notes:
                    return notes
            return self._extract_notes_from_html(html)
        return []

    @staticmethod
    def _extract_next_data_from_html(html: str) -> dict[str, Any] | None:
        marker = 'id="__NEXT_DATA__"'
        idx = html.find(marker)
        if idx < 0:
            return None
        script_start = html.find(">", idx)
        script_end = html.find("</script>", script_start + 1)
        if script_start < 0 or script_end < 0:
            return None
        raw = html[script_start + 1 : script_end].strip()
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    @staticmethod
    def _extract_notes_from_html(html: str) -> list[dict[str, Any]]:
        notes: list[dict[str, Any]] = []
        for match in re.finditer(r'"note_id"\\s*:\\s*"([^"]+)"', html):
            note_id = match.group(1)
            if note_id:
                notes.append({"note_id": note_id})
        if notes:
            return notes
        for match in re.finditer(r'"id"\\s*:\\s*"([0-9a-f]{16,})"', html):
            note_id = match.group(1)
            if note_id:
                notes.append({"note_id": note_id})
        return notes

    async def _extract_profile_notes_from_dom(
        self,
        context: BrowserContext,
        page: Page,
        limit: int,
        xsec_token: str = "",
        xsec_source: str = "",
    ) -> list[dict[str, Any]]:
        try:
            items = None
            for frame in page.frames:
                try:
                    items = await frame.evaluate(
                        """
                        () => {
                          const elements = Array.from(document.querySelectorAll('.feeds-container .note-item, .note-item'));
                          return elements.map(el => {
                            const titleEl = el.querySelector('.title span');
                            const authorEl = el.querySelector('.card-bottom-wrapper .name span.name, .author-wrapper .username');
                            const linkEl = el.querySelector('a.cover.mask, a.cover.mask.ld, a[href*="/explore/"], a[href*="/discovery/item/"], a[href*="/note/"]');
                            const link = linkEl ? linkEl.href : '';
                            return {
                              title: titleEl ? titleEl.textContent?.trim() || '' : '',
                              author: authorEl ? authorEl.textContent?.trim() || '' : '',
                              link
                            };
                          }).filter(item => item.link);
                        }
                        """
                    )
                except Exception:
                    items = None
                if isinstance(items, list) and items:
                    break
        except Exception:
            return []
        if not isinstance(items, list):
            items = []

        notes: list[dict[str, Any]] = []
        count = max(1, int(limit))
        for item in items[:count]:
            if not isinstance(item, dict):
                continue
            link = str(item.get("link") or "").strip()
            if not link:
                continue
            try:
                detail = await self._fetch_note_detail_via_dom(context, link, xsec_token, xsec_source)
            except Exception:
                detail = None
            if isinstance(detail, dict) and detail:
                notes.append(detail)
        if notes:
            return notes

        note_ids = await self._extract_note_ids_from_dom(page, count)
        if note_ids:
            self._debug("profile.dom_note_ids", count=len(note_ids), sample=note_ids[:5])
        for note_id in note_ids[:count]:
            detail = await self._request_note_detail_from_html(page, note_id, xsec_token, xsec_source)
            if not detail:
                await self._open_note_detail_page(page, note_id, xsec_token, xsec_source or "pc_feed", "")
                detail = await self._extract_note_from_state(page, note_id)
            if detail:
                notes.append(detail)
        return notes

    async def _extract_note_ids_from_dom(self, page: Page, limit: int) -> list[str]:
        results: list[str] = []
        for frame in page.frames:
            try:
                ids = await frame.evaluate(
                    """
                    () => {
                      const ids = new Set();
                      const urlRe = /(explore|discovery\\/item|note)\\/([^/?#]+)/i;
                      document.querySelectorAll('a[href]').forEach(a => {
                        const href = a.href || '';
                        const m = href.match(urlRe);
                        if (m && m[2]) ids.add(m[2]);
                      });
                      const attrRe = /[0-9a-f]{16,}/i;
                      const elements = Array.from(document.querySelectorAll('[data-id],[data-note-id],[data-noteid],[data-noteId],[data-note],[data-note-id]'));
                      elements.forEach(el => {
                        for (const [key, value] of Object.entries(el.dataset || {})) {
                          if (typeof value === 'string') {
                            const m = value.match(attrRe);
                            if (m) ids.add(m[0]);
                          }
                        }
                        for (const attr of el.getAttributeNames()) {
                          if (!/note|id/i.test(attr)) continue;
                          const val = el.getAttribute(attr) || '';
                          const m = val.match(attrRe);
                          if (m) ids.add(m[0]);
                        }
                      });
                      return Array.from(ids);
                    }
                    """
                )
            except Exception:
                ids = None
            if isinstance(ids, list) and ids:
                results.extend([str(x) for x in ids if isinstance(x, str)])
        # de-dup while preserving order
        seen: set[str] = set()
        deduped: list[str] = []
        for value in results:
            if value in seen:
                continue
            seen.add(value)
            deduped.append(value)
            if len(deduped) >= max(1, int(limit) * 2):
                break
        return deduped

    async def _fetch_note_detail_via_dom(
        self,
        context: BrowserContext,
        url: str,
        xsec_token: str = "",
        xsec_source: str = "",
    ) -> dict[str, Any] | None:
        note_page: Page | None = None
        try:
            note_page = await context.new_page()
            target_url = url
            if xsec_token and "xsec_token=" not in target_url:
                joiner = "&" if "?" in target_url else "?"
                source = xsec_source or "pc_note"
                target_url = f"{target_url}{joiner}xsec_token={quote(xsec_token)}&xsec_source={quote(source)}"
            await note_page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
            try:
                await note_page.wait_for_selector(".note-container, #noteContainer", timeout=15000)
            except Exception:
                pass
            data = None
            for frame in note_page.frames:
                try:
                    data = await frame.evaluate(
                        """
                        () => {
                          const article = document.querySelector('.note-container') || document.querySelector('#noteContainer');
                          if (!article) return null;
                          const title =
                            article.querySelector('#detail-title')?.textContent?.trim() ||
                            article.querySelector('.title')?.textContent?.trim() ||
                            '';
                          const content =
                            article.querySelector('.note-scroller .note-content .note-text span')?.textContent?.trim() ||
                            article.querySelector('#detail-desc .note-text')?.textContent?.trim() ||
                            '';
                          const author =
                            article.querySelector('.author-container .info .username')?.textContent?.trim() ||
                            article.querySelector('.author-wrapper .username')?.textContent?.trim() ||
                            '';
                          const likes = document.querySelector('.interact-container .like-wrapper .count')?.textContent?.trim() ||
                                        document.querySelector('.engage-bar-style .like-wrapper .count')?.textContent?.trim() || '';
                          const comments = document.querySelector('.interact-container .chat-wrapper .count')?.textContent?.trim() ||
                                           document.querySelector('.engage-bar-style .chat-wrapper .count')?.textContent?.trim() || '';
                          const collects = document.querySelector('.interact-container .collect-wrapper .count')?.textContent?.trim() ||
                                           document.querySelector('.engage-bar-style .collect-wrapper .count')?.textContent?.trim() || '';
                          const images = Array.from(document.querySelectorAll('.media-container img, .note-slider-img')).map(img => img.getAttribute('src') || '').filter(Boolean);
                          const videos = Array.from(document.querySelectorAll('.media-container video')).map(v => v.getAttribute('src') || '').filter(Boolean);
                          return { title, content, author, likes, comments, collects, images, videos, url: window.location.href };
                        }
                        """
                    )
                except Exception:
                    data = None
                if isinstance(data, dict):
                    break
            if not isinstance(data, dict):
                return None
            note_id, xsec_token, xsec_source = self._parse_note_url(self._normalize_xhs_url(str(data.get("url") or url)))
            return {
                "note_id": note_id,
                "title": data.get("title") or "",
                "desc": data.get("content") or "",
                "user": {"nickname": data.get("author") or ""},
                "interact_info": {
                    "liked_count": self._parse_count_text(data.get("likes")),
                    "comment_count": self._parse_count_text(data.get("comments")),
                    "collected_count": self._parse_count_text(data.get("collects")),
                },
                "image_list": [{"url": u, "url_default": u} for u in (data.get("images") or [])],
                "video": {"media": {"stream": {"h264": [{"url": u} for u in (data.get("videos") or [])]}}},
                "xsec_token": xsec_token,
                "xsec_source": xsec_source,
                "_original_url": data.get("url") or url,
            }
        finally:
            if note_page is not None:
                try:
                    await note_page.close()
                except Exception:
                    pass

    @staticmethod
    def _parse_count_text(value: Any) -> int | None:
        if value in (None, ""):
            return None
        text = str(value).strip()
        if not text:
            return None
        multiplier = 1
        if "万" in text:
            multiplier = 10000
            text = text.replace("万", "").strip()
        try:
            return int(float(text) * multiplier)
        except Exception:
            return None

    def _find_notes_in_state(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        notes: list[dict[str, Any]] = []

        def _maybe_add(item: Any) -> None:
            if not isinstance(item, dict):
                return
            note = item.get("note_card") or item.get("note") or item
            if isinstance(note, dict) and (note.get("note_id") or note.get("id")):
                notes.append(note)

        queue: list[Any] = [state]
        seen: set[int] = set()
        while queue and len(notes) < 200:
            current = queue.pop(0)
            if id(current) in seen:
                continue
            seen.add(id(current))
            if isinstance(current, dict):
                for key, value in current.items():
                    if key in {"notes", "note_list", "noteList"} and isinstance(value, list):
                        for item in value:
                            _maybe_add(item)
                    if isinstance(value, (dict, list)):
                        queue.append(value)
            elif isinstance(current, list):
                for item in current:
                    if isinstance(item, (dict, list)):
                        queue.append(item)
        return notes

    @staticmethod
    def _extract_notes_from_captured(payload: dict[str, Any]) -> list[dict[str, Any]]:
        if not isinstance(payload, dict):
            return []
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        if not isinstance(data, dict):
            return []
        for key in ("notes", "items", "list"):
            notes = data.get(key)
            if isinstance(notes, list):
                cleaned: list[dict[str, Any]] = []
                for item in notes:
                    if not isinstance(item, dict):
                        continue
                    note = item.get("note_card") or item.get("note") or item.get("noteCard") or item
                    if isinstance(note, dict):
                        cleaned.append(note if note is item else {**item, **note})
                if cleaned:
                    return cleaned
        return []

    def _extract_note_ids_from_payload(self, payload: Any) -> list[str]:
        ids: list[str] = []

        def _visit(value: Any) -> None:
            if isinstance(value, dict):
                for key, val in value.items():
                    if key in {"note_id", "noteId", "id"} and isinstance(val, str) and val:
                        ids.append(val)
                    if isinstance(val, (dict, list)):
                        _visit(val)
            elif isinstance(value, list):
                for item in value:
                    _visit(item)

        _visit(payload)
        # de-dup
        seen: set[str] = set()
        result: list[str] = []
        for value in ids:
            if value in seen:
                continue
            seen.add(value)
            result.append(value)
        return result

    async def _profile_from_creator_page(
        self,
        page: Page,
        user_id: str,
        xsec_token: str,
        xsec_source: str,
    ) -> Profile | None:
        profile = await self._request_profile_from_html(page, user_id, xsec_token, xsec_source)
        if profile is not None:
            return profile

        url = f"{self._HOME}user/profile/{user_id}"
        if xsec_token and xsec_source:
            url = f"{url}?xsec_token={xsec_token}&xsec_source={xsec_source}"
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(0.3)
            state = await page.evaluate("() => window.__INITIAL_STATE__ || null")
        except Exception:
            return None
        if not isinstance(state, dict):
            return None
        user_data = None
        user_section = state.get("user")
        if isinstance(user_section, dict):
            user_data = user_section.get("userPageData") or user_section.get("userInfo") or user_section
        if not isinstance(user_data, dict):
            return None
        return self._profile_from_user_info(user_data, user_id)

    async def _request_note_detail(self, page: Page, note_id: str, xsec_source: str, xsec_token: str) -> dict[str, Any]:
        payload = {
            "source_note_id": note_id,
            "image_formats": ["jpg", "webp", "avif"],
            "extra": {"need_body_topic": 1},
            "xsec_source": xsec_source or "pc_search",
            "xsec_token": xsec_token or "",
        }
        response = await self._signed_post(page, self._FEED_API, payload)
        items = response.get("items") or []
        if isinstance(items, list) and items:
            card = items[0].get("note_card") if isinstance(items[0], dict) else None
            if isinstance(card, dict):
                return card
        note = response.get("note") or response.get("note_card")
        if isinstance(note, dict):
            return note
        return {}

    async def _request_note_detail_from_html(
        self,
        page: Page,
        note_id: str,
        xsec_token: str,
        xsec_source: str,
    ) -> dict[str, Any]:
        url = f"{self._HOME}explore/{note_id}"
        if xsec_token:
            url = f"{url}?xsec_token={quote(xsec_token)}&xsec_source={quote(xsec_source or 'pc_search')}"
        html = await self._fetch_html(page, url)
        if not html:
            return {}
        state = self._extract_state_from_html(html)
        if not isinstance(state, dict):
            return {}
        note = self._find_note_in_state(state, note_id)
        return note or {}

    async def _request_user_posts(
        self,
        page: Page,
        user_id: str,
        cursor: str,
        page_size: int,
        xsec_token: str,
        xsec_source: str,
    ) -> dict[str, Any]:
        params = {
            "num": max(1, min(int(page_size), 30)),
            "cursor": cursor,
            "user_id": user_id,
            "xsec_token": xsec_token or "",
            "xsec_source": xsec_source or "pc_feed",
        }
        return await self._signed_get(page, self._USER_POST_API, params)

    async def _signed_get(
        self,
        page: Page,
        uri: str,
        params: dict[str, Any],
        allow_navigation: bool = True,
    ) -> dict[str, Any]:
        headers = await self._pre_headers(page, uri, params=params, allow_navigation=allow_navigation)
        last_exc: Exception | None = None
        for host in self._api_hosts():
            try:
                response = await page.context.request.get(
                    f"{host}{uri}",
                    headers=headers,
                    params=params,
                )
                return await self._parse_response(response)
            except Exception as exc:
                last_exc = exc
                continue
        if last_exc is not None:
            raise last_exc
        return {}

    async def _signed_post(
        self,
        page: Page,
        uri: str,
        payload: dict[str, Any],
        allow_navigation: bool = True,
    ) -> dict[str, Any]:
        headers = await self._pre_headers(page, uri, payload=payload, allow_navigation=allow_navigation)
        json_str = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        last_exc: Exception | None = None
        for host in self._api_hosts():
            try:
                response = await page.context.request.post(
                    f"{host}{uri}",
                    headers=headers,
                    data=json_str,
                )
                return await self._parse_response(response)
            except Exception as exc:
                last_exc = exc
                continue
        if last_exc is not None:
            raise last_exc
        return {}

    def _api_hosts(self) -> tuple[str, ...]:
        home = (self._HOME or "").lower()
        if "rednote.com" in home:
            return (
                "https://webapi.rednote.com",
                "https://edith.rednote.com",
                "https://edith.xiaohongshu.com",
            )
        return (
            "https://webapi.rednote.com",
            "https://edith.rednote.com",
            "https://edith.xiaohongshu.com",
        )

    async def _parse_response(self, response: Any) -> dict[str, Any]:
        try:
            data = await response.json()
        except Exception as exc:
            text_preview = ""
            try:
                text_preview = (await response.text())[:300]
            except Exception:
                pass
            raise RuntimeError(f"XHS response is not valid JSON: {exc}. Preview: {text_preview}") from exc

        if isinstance(data, dict) and data.get("success") is True:
            return data.get("data") or {}

        if isinstance(data, dict):
            code = data.get("code")
            msg = data.get("msg") or data.get("message") or ""
            if data.get("success") is False or code not in (None, 0):
                if str(code) == "-100" or "登录已过期" in str(msg):
                    raise RuntimeError("Rednote login expired. Please login again from Settings.")
                raise RuntimeError(f"XHS API request failed. code={code}, msg={msg}")
            if "data" in data and isinstance(data.get("data"), dict):
                return data["data"]
            return data

        return {}

    async def _pre_headers(
        self,
        page: Page,
        uri: str,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        allow_navigation: bool = True,
    ) -> dict[str, str]:
        await self._ensure_sign_context(page, allow_navigation=allow_navigation)
        cookies = await page.context.cookies()
        cookie_dict = self._cookie_dict(cookies)
        user_agent = await page.evaluate("() => navigator.userAgent")
        if params is not None:
            data = params
            method = "GET"
        elif payload is not None:
            data = payload
            method = "POST"
        else:
            raise ValueError("params or payload is required")
        signs = await self._sign_with_playwright(page, uri, data, cookie_dict.get("a1", ""), method)
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json;charset=UTF-8",
            "Origin": self._HOME.rstrip("/"),
            "Referer": self._HOME,
            "User-Agent": user_agent,
            "X-S": signs["x-s"],
            "X-T": signs["x-t"],
            "x-S-Common": signs["x-s-common"],
            "X-B3-Traceid": signs["x-b3-traceid"],
        }
        cookie_header = self._cookie_header(cookies)
        if cookie_header:
            headers["Cookie"] = cookie_header
        return headers

    async def _request_profile_from_html(
        self,
        page: Page,
        user_id: str,
        xsec_token: str,
        xsec_source: str,
    ) -> Profile | None:
        url = f"{self._HOME}user/profile/{user_id}"
        if xsec_token and xsec_source:
            url = f"{url}?xsec_token={quote(xsec_token)}&xsec_source={quote(xsec_source)}"
        html = await self._fetch_html(page, url)
        if not html:
            return None
        state = self._extract_state_from_html(html)
        if not isinstance(state, dict):
            return None
        user_section = state.get("user")
        user_data = None
        if isinstance(user_section, dict):
            user_data = user_section.get("userPageData") or user_section.get("userInfo") or user_section
        if not isinstance(user_data, dict):
            return None
        return self._profile_from_user_info(user_data, user_id)

    async def _fetch_html(self, page: Page, url: str) -> str:
        url = self._normalize_xhs_url(url)
        user_agent = await page.evaluate("() => navigator.userAgent")
        cookies = await page.context.cookies()
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "User-Agent": user_agent,
            "Referer": self._HOME,
        }
        cookie_header = self._cookie_header(cookies)
        if cookie_header:
            headers["Cookie"] = cookie_header
        try:
            response = await page.context.request.get(url, headers=headers)
        except Exception:
            response = None
        try:
            if response is not None:
                text = await response.text()
                if text:
                    return text
        except Exception:
            pass
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(0.3)
            return await page.content()
        except Exception:
            return ""

    @staticmethod
    def _extract_state_from_html(html: str) -> dict[str, Any] | None:
        if "window.__INITIAL_STATE__" not in html:
            return None

        marker = "window.__INITIAL_STATE__"
        idx = html.find(marker)
        if idx < 0:
            return None
        eq_idx = html.find("=", idx)
        if eq_idx < 0:
            return None
        brace_start = html.find("{", eq_idx)
        if brace_start < 0:
            return None

        depth = 0
        in_string = False
        escaped = False
        end_idx = -1
        for i in range(brace_start, len(html)):
            ch = html[i]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue

            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end_idx = i
                    break

        if end_idx < 0:
            return None

        raw = html[brace_start : end_idx + 1]
        raw = raw.replace(":undefined", ":null").replace("undefined", "null")
        try:
            return json.loads(raw)
        except Exception:
            return None

    async def _ensure_sign_context(self, page: Page, allow_navigation: bool = True) -> None:
        try:
            current_url = page.url or ""
        except Exception:
            current_url = ""
        if not current_url.startswith(self._HOME):
            if not allow_navigation:
                return
            try:
                await page.goto(self._HOME, wait_until="domcontentloaded", timeout=60000)
            except Exception:
                return
        await self._ensure_mnsv2_ready(page)

    async def _sign_with_playwright(
        self,
        page: Page,
        uri: str,
        data: dict[str, Any],
        a1: str,
        method: str,
    ) -> dict[str, str]:
        sign_str = self._build_sign_string(uri, data, method)
        md5_str = self._md5_hex(sign_str)
        x3_value = await self._call_mnsv2(page, sign_str, md5_str)
        data_type = "object" if isinstance(data, (dict, list)) else "string"
        x_s = self._build_xs_payload(x3_value, data_type)
        x_t = str(int(time.time() * 1000))
        b1 = await self._get_b1_from_localstorage(page)
        return {
            "x-s": x_s,
            "x-t": x_t,
            "x-s-common": self._build_xs_common(a1, b1, x_s, x_t),
            "x-b3-traceid": get_trace_id(),
        }

    async def _get_b1_from_localstorage(self, page: Page) -> str:
        try:
            local_storage = await page.evaluate("() => window.localStorage")
            return local_storage.get("b1", "")
        except Exception:
            return ""

    async def _call_mnsv2(self, page: Page, sign_str: str, md5_str: str) -> str:
        sign_str_escaped = sign_str.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
        md5_str_escaped = md5_str.replace("\\", "\\\\").replace("'", "\\'")
        try:
            result = await page.evaluate(f"window.mnsv2('{sign_str_escaped}', '{md5_str_escaped}')")
            if result:
                return str(result)
        except Exception:
            result = ""
        try:
            await self._ensure_mnsv2_ready(page)
            retry = await page.evaluate(f"window.mnsv2('{sign_str_escaped}', '{md5_str_escaped}')")
            return str(retry or "")
        except Exception:
            return ""

    @staticmethod
    def _build_sign_string(uri: str, data: dict[str, Any], method: str) -> str:
        if method.upper() == "POST":
            return uri + json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        if not data:
            return uri
        params = []
        for key in data.keys():
            value = data[key]
            if isinstance(value, list):
                value_str = ",".join(str(v) for v in value)
            elif value is not None:
                value_str = str(value)
            else:
                value_str = ""
            value_str = quote(value_str, safe="")
            params.append(f"{key}={value_str}")
        return f"{uri}?{'&'.join(params)}"

    @staticmethod
    def _md5_hex(value: str) -> str:
        return hashlib.md5(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _build_xs_payload(x3_value: str, data_type: str = "object") -> str:
        payload = {
            "x0": "4.2.1",
            "x1": "xhs-pc-web",
            "x2": "Mac OS",
            "x3": x3_value,
            "x4": data_type,
        }
        return "XYS_" + b64_encode(encode_utf8(json.dumps(payload, separators=(",", ":"))))

    @staticmethod
    def _build_xs_common(a1: str, b1: str, x_s: str, x_t: str) -> str:
        payload = {
            "s0": 3,
            "s1": "",
            "x0": "1",
            "x1": "4.2.2",
            "x2": "Mac OS",
            "x3": "xhs-pc-web",
            "x4": "4.74.0",
            "x5": a1,
            "x6": x_t,
            "x7": x_s,
            "x8": b1,
            "x9": mrc(x_t + x_s + b1),
            "x10": 154,
            "x11": "normal",
        }
        return b64_encode(encode_utf8(json.dumps(payload, separators=(",", ":"))))

    @staticmethod
    def _cookie_header(cookies: list[dict[str, Any]]) -> str:
        parts = []
        for cookie in cookies:
            name = str(cookie.get("name") or "").strip()
            value = str(cookie.get("value") or "")
            if name:
                parts.append(f"{name}={value}")
        return "; ".join(parts)

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
    def _get_search_id() -> str:
        base = int(time.time() * 1000) << 64
        rand = int(random.uniform(0, 2147483646))
        return XhsLocalService._base36_encode(base + rand)

    @staticmethod
    def _base36_encode(number: int, alphabet: str = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ") -> str:
        if number == 0:
            return alphabet[0]
        base36 = ""
        num = number
        while num:
            num, i = divmod(num, len(alphabet))
            base36 = alphabet[i] + base36
        return base36

    @staticmethod
    def _parse_note_url(url: str) -> tuple[str, str, str]:
        normalized_url = XhsLocalService._normalize_xhs_url(url)
        if not normalized_url:
            return "", "", ""
        parsed = urlparse(normalized_url)
        qs = parse_qs(parsed.query)
        xsec_token = (qs.get("xsec_token") or [""])[0]
        xsec_source = (qs.get("xsec_source") or [""])[0]
        path = parsed.path or ""
        match = re.search(r"/(explore|discovery/item|note)/([^/?]+)", path)
        if match:
            return match.group(2), xsec_token, xsec_source
        return "", xsec_token, xsec_source

    @staticmethod
    def _parse_creator_url(url: str) -> tuple[str, str, str]:
        normalized_url = XhsLocalService._normalize_xhs_url(url)
        if not normalized_url:
            return "", "", ""
        parsed = urlparse(normalized_url)
        qs = parse_qs(parsed.query)
        xsec_token = (qs.get("xsec_token") or [""])[0]
        xsec_source = (qs.get("xsec_source") or [""])[0]
        match = re.search(r"/user/profile/([^/?]+)", parsed.path or "")
        if match:
            return match.group(1), xsec_token, xsec_source
        return "", xsec_token, xsec_source

    @staticmethod
    def _build_note_url(note_id: str, xsec_token: str = "", xsec_source: str = "") -> str:
        if not note_id:
            return ""
        base = f"{XhsLocalService._HOME}explore/{note_id}"
        if xsec_token:
            return f"{base}?xsec_token={quote(xsec_token)}&xsec_source={quote(xsec_source or 'pc_search')}"
        return base

    def _video_from_note(self, note: dict[str, Any], original_url: str, xsec_token: str, xsec_source: str) -> Video:
        note_id = str(note.get("note_id") or note.get("id") or "").strip()
        title = str(
            note.get("title")
            or note.get("display_title")
            or note.get("displayTitle")
            or note.get("desc")
            or note_id
            or "Rednote Note"
        ).strip()
        user = note.get("user") or {}
        interact = note.get("interact_info") or note.get("interaction") or {}
        upload_time = None
        ts = note.get("time") or note.get("last_update_time") or note.get("lastUpdateTime")
        if isinstance(ts, (int, float)):
            try:
                value = float(ts)
                # Rednote sometimes returns milliseconds.
                if value > 10_000_000_000:
                    value = value / 1000.0
                upload_time = datetime.fromtimestamp(value)
            except Exception:
                upload_time = None

        image_urls = self._extract_image_urls(note)
        media_url = self._extract_video_url(note)
        thumbnail_url = self._extract_thumbnail(note, image_urls)
        url = original_url or self._build_note_url(note_id, xsec_token, xsec_source)

        return Video(
            id=note_id or self._extract_digits(url),
            title=title or "Rednote Note",
            url=url,
            platform="xhs",
            media_url=media_url,
            image_urls=tuple(image_urls),
            description=note.get("desc") or None,
            duration=self._to_int(note.get("duration")),
            thumbnail_url=thumbnail_url,
            author=user.get("nickname") or user.get("name") or None,
            uploader=user.get("user_id") or user.get("id") or None,
            view_count=None,
            like_count=self._to_int(interact.get("liked_count") or note.get("liked_count")),
            comment_count=self._to_int(interact.get("comment_count") or note.get("comment_count")),
            share_count=self._to_int(interact.get("share_count") or note.get("share_count")),
            bookmark_count=self._to_int(interact.get("collected_count") or note.get("collected_count")),
            upload_time=upload_time,
            tags=tuple(self._extract_tags(note)),
        )

    def _profile_from_user_info(self, user_info: dict[str, Any], user_id: str) -> Profile | None:
        if not isinstance(user_info, dict):
            return None
        user = user_info.get("user") or user_info
        if not isinstance(user, dict):
            return None
        stats = user_info.get("interact_info") or user_info.get("stats") or {}
        return Profile(
            username=str(user.get("user_id") or user.get("id") or user_id),
            display_name=user.get("nickname") or user.get("name") or None,
            avatar_url=user.get("avatar") or user.get("avatar_url") or None,
            follower_count=self._to_int(stats.get("follower_count") or user.get("follower_count")),
            following_count=self._to_int(stats.get("following_count") or user.get("following_count")),
            like_count=self._to_int(stats.get("liked_count") or user.get("liked_count")),
            video_count=self._to_int(stats.get("note_count") or user.get("note_count")),
        )

    def _profile_from_note(self, video: Video) -> Profile | None:
        if not video.author and not video.uploader:
            return None
        return Profile(
            username=str(video.uploader or video.author or ""),
            display_name=video.author or None,
            avatar_url=None,
        )

    @staticmethod
    def _extract_tags(note: dict[str, Any]) -> list[str]:
        tags = []
        for tag in note.get("tag_list") or note.get("tags") or []:
            if isinstance(tag, dict):
                name = str(tag.get("name") or tag.get("tag_name") or "").strip()
            else:
                name = str(tag).strip()
            if name:
                tags.append(name)
        return tags

    @staticmethod
    def _extract_image_urls(note: dict[str, Any]) -> list[str]:
        images: list[str] = []
        for img in note.get("image_list") or note.get("images") or []:
            if isinstance(img, str):
                if img.strip():
                    images.append(img.strip())
                continue
            if not isinstance(img, dict):
                continue
            for key in ("url_default", "url", "url_pre"):
                value = img.get(key)
                if isinstance(value, str) and value.strip():
                    images.append(value.strip())
                    break
            if images:
                continue
            url_list = img.get("url_list") or []
            if isinstance(url_list, list):
                for value in url_list:
                    if isinstance(value, str) and value.strip():
                        images.append(value.strip())
                        break
                    if isinstance(value, dict):
                        candidate = value.get("url") or value.get("url_default") or value.get("url_pre")
                        if isinstance(candidate, str) and candidate.strip():
                            images.append(candidate.strip())
                            break
        if not images:
            cover = note.get("cover") or {}
            if isinstance(cover, str):
                if cover.strip():
                    images.append(cover.strip())
            elif isinstance(cover, dict):
                for key in ("url_default", "url", "url_pre"):
                    value = cover.get(key)
                    if isinstance(value, str) and value.strip():
                        images.append(value.strip())
                        break
                if not images:
                    url_list = cover.get("url_list") or []
                    if isinstance(url_list, list):
                        for value in url_list:
                            if isinstance(value, str) and value.strip():
                                images.append(value.strip())
                                break
        return images

    @staticmethod
    def _extract_thumbnail(note: dict[str, Any], image_urls: list[str]) -> str | None:
        cover = note.get("cover") or {}
        if isinstance(cover, str):
            if cover.strip():
                return cover.strip()
        if isinstance(cover, dict):
            for key in ("url_default", "url", "url_pre"):
                value = cover.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            url_list = cover.get("url_list") or []
            if isinstance(url_list, list):
                for value in url_list:
                    if isinstance(value, str) and value.strip():
                        return value.strip()
                    if isinstance(value, dict):
                        candidate = value.get("url") or value.get("url_default") or value.get("url_pre")
                        if isinstance(candidate, str) and candidate.strip():
                            return candidate.strip()
        if isinstance(cover, list):
            for item in cover:
                if isinstance(item, str) and item.strip():
                    return item.strip()
        if image_urls:
            return image_urls[0]
        return None

    def _extract_video_url(self, note: dict[str, Any]) -> str | None:
        urls = self._collect_video_urls(note)
        return urls[0] if urls else None

    def _collect_video_urls(self, note: dict[str, Any]) -> list[str]:
        urls: list[str] = []
        video = note.get("video") or {}
        if isinstance(video, dict):
            consumer = video.get("consumer") or {}
            if isinstance(consumer, dict):
                origin_key = (
                    consumer.get("originVideoKey")
                    or consumer.get("origin_video_key")
                    or consumer.get("origin_videoKey")
                )
                if isinstance(origin_key, str) and origin_key.strip():
                    urls.append(f"https://sns-video-bd.xhscdn.com/{origin_key.strip()}")
            # Some Rednote payloads provide direct stream URLs under consumer.playable or consumer.stream.
            for key in ("playable", "stream", "streams", "playUrl", "play_url"):
                value = consumer.get(key) if isinstance(consumer, dict) else None
                if isinstance(value, str) and value.startswith("http"):
                    urls.append(value)
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, str) and item.startswith("http"):
                            urls.append(item)
            media = video.get("media") or {}
            if isinstance(media, dict):
                stream = media.get("stream") or {}
                if isinstance(stream, dict):
                    for key in ("h264", "h265"):
                        variants = stream.get(key) or []
                        if isinstance(variants, list):
                            for variant in variants:
                                if not isinstance(variant, dict):
                                    continue
                                for candidate in ("master_url", "backup_url", "url"):
                                    url = variant.get(candidate)
                                    if isinstance(url, str) and url.startswith("http"):
                                        urls.append(url)
        # Fallback: crawl any url fields in the video blob.
        found = self._find_first_media_url(video)
        if isinstance(found, str) and found.startswith("http"):
            urls.append(found)
        # de-dup while preserving order
        deduped: list[str] = []
        seen: set[str] = set()
        for url in urls:
            if url in seen:
                continue
            seen.add(url)
            deduped.append(url)
        # Prefer Rednote CDN stream URLs when available for faster downloads.
        stream_first = [u for u in deduped if "rednotecdn.com/stream" in u]
        others = [u for u in deduped if u not in stream_first]
        # Heuristic: prefer lower stream variant numbers (often cleaner).
        def _stream_rank(url: str) -> int:
            # Handle /stream/{a}/{b}/{variant}/... where variant is the 4th segment.
            match = re.search(r"/stream/\d+/\d+/(\d+)/", url)
            if match:
                try:
                    return int(match.group(1))
                except Exception:
                    return 10_000
            return 10_000
        # Prefer lower variant numbers; push 259 to the end explicitly.
        def _stream_sort_key(url: str) -> tuple[int, int]:
            variant = _stream_rank(url)
            return (1 if variant == 259 else 0, variant)
        stream_first.sort(key=_stream_sort_key)
        return stream_first + others

    @staticmethod
    def _find_first_media_url(data: Any) -> str | None:
        if isinstance(data, dict):
            for key, value in data.items():
                if key in {"master_url", "backup_url", "url"} and isinstance(value, str) and value.startswith("http"):
                    return value
                found = XhsLocalService._find_first_media_url(value)
                if found:
                    return found
        if isinstance(data, list):
            for item in data:
                found = XhsLocalService._find_first_media_url(item)
                if found:
                    return found
        return None

    @staticmethod
    def _to_int(value: Any) -> int | None:
        if value in (None, ""):
            return None
        if isinstance(value, (int, float)):
            return int(value)
        text = str(value).strip()
        if not text:
            return None
        text = text.replace(",", "").replace("+", "")
        try:
            return int(float(text))
        except Exception:
            pass
        # Handle common Chinese units like "万" (10k) and "亿" (100M).
        match = re.match(r"^\s*([0-9.]+)\s*([万亿])\s*$", text)
        if match:
            try:
                number = float(match.group(1))
            except Exception:
                return None
            unit = match.group(2)
            if unit == "万":
                return int(number * 10_000)
            if unit == "亿":
                return int(number * 100_000_000)
        return None

    @staticmethod
    def _extract_digits(text: str) -> str:
        return "".join(ch for ch in str(text) if ch.isdigit())

    @staticmethod
    def _safe_title(video: Video) -> str:
        title = (video.title or video.id or "note").strip()
        cleaned = []
        for ch in title:
            if ord(ch) < 32:
                cleaned.append(" ")
                continue
            if ch in '<>:"/\\|?*':
                cleaned.append(" ")
                continue
            cleaned.append(ch)
        safe_title = re.sub(r"\s+", " ", "".join(cleaned)).strip().rstrip(".")
        if len(safe_title) > 120:
            safe_title = safe_title[:120].rstrip()
        return safe_title or video.id or "note"

    @staticmethod
    def _build_output_path(output_dir: Path, video: Video, media_url: str = "") -> Path:
        ext = ".mp4"
        suffix = Path(httpx.URL(media_url).path).suffix.lower() if media_url else ""
        if suffix and len(suffix) <= 5:
            ext = suffix
        return output_dir / f"{XhsLocalService._safe_title(video)}{ext}"

    async def _move_window_offscreen(self, page: Page) -> None:
        if self._login_in_progress:
            return
        if self._window_hidden:
            return
        moved = await self._set_window_bounds(
            page,
            {
                "windowState": "normal",
                "left": -2400,
                "top": 80,
                "width": 1600,
                "height": 900,
            },
        )
        if moved:
            self._window_hidden = True

    async def _restore_window(self, page: Page) -> None:
        restored = await self._set_window_bounds(
            page,
            {
                "windowState": "normal",
                "left": 120,
                "top": 80,
                "width": 1600,
                "height": 900,
            },
        )
        if restored:
            self._window_hidden = False
        try:
            await page.bring_to_front()
        except Exception:
            pass

    @staticmethod
    async def _set_window_bounds(page: Page, bounds: dict[str, Any]) -> bool:
        try:
            session = await page.context.new_cdp_session(page)
            info = await session.send("Browser.getWindowForTarget")
            window_id = info.get("windowId")
            if window_id is None:
                return False
            await session.send("Browser.setWindowBounds", {"windowId": window_id, "bounds": bounds})
            return True
        except Exception:
            return False
