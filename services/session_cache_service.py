from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict


class SessionCacheService:
    def __init__(self, cache_file: Path) -> None:
        self.cache_file = cache_file
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> Dict[str, Any]:
        if not self.cache_file.exists():
            return {}
        try:
            raw = self.cache_file.read_text(encoding="utf-8")
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def save(self, payload: Dict[str, Any]) -> None:
        self.cache_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
