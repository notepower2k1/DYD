from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from models.video import Video


class DownloadHistoryService:
    def __init__(self, history_file: Path) -> None:
        self.history_file = history_file
        self.history_file.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> List[Dict[str, Any]]:
        if not self.history_file.exists():
            return []
        try:
            raw = self.history_file.read_text(encoding="utf-8")
            data = json.loads(raw)
            if isinstance(data, list):
                return [x for x in data if isinstance(x, dict)]
            return []
        except Exception:
            return []

    def _write(self, rows: List[Dict[str, Any]]) -> None:
        self.history_file.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    def list_records(self) -> List[Dict[str, Any]]:
        rows = self._read()
        return sorted(rows, key=lambda x: str(x.get("downloaded_at") or ""), reverse=True)

    def is_downloaded(self, video: Video) -> bool:
        rows = self._read()
        vid = (video.id or "").strip()
        url = (video.url or "").strip()
        for row in rows:
            if vid and str(row.get("video_id") or "") == vid:
                return True
            if url and str(row.get("url") or "") == url:
                return True
        return False

    def find_record(self, video: Video) -> Optional[Dict[str, Any]]:
        rows = self._read()
        vid = (video.id or "").strip()
        url = (video.url or "").strip()
        for row in rows:
            if vid and str(row.get("video_id") or "") == vid:
                return row
            if url and str(row.get("url") or "") == url:
                return row
        return None

    def apply_status(self, videos: List[Video]) -> None:
        for v in videos:
            rec = self.find_record(v)
            if rec:
                v.is_downloaded = True
                v.downloaded_path = str(rec.get("file_path") or "") or None
            else:
                v.is_downloaded = False
                v.downloaded_path = None

    def add_record(self, video: Video, file_path: Path, profile_username: str | None = None) -> None:
        rows = self._read()

        rec = {
            "video_id": video.id,
            "title": video.title,
            "url": video.url,
            "file_path": str(file_path),
            "profile_username": profile_username or "",
            "downloaded_at": datetime.now().isoformat(timespec="seconds"),
        }

        # Upsert by video_id first, then by URL.
        replaced = False
        for i, row in enumerate(rows):
            if video.id and str(row.get("video_id") or "") == video.id:
                rows[i] = rec
                replaced = True
                break
            if video.url and str(row.get("url") or "") == video.url:
                rows[i] = rec
                replaced = True
                break

        if not replaced:
            rows.append(rec)

        self._write(rows)
