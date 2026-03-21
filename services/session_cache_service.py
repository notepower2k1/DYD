from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime
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
        normalized = self._normalize(payload)
        temp_file = self.cache_file.with_suffix(self.cache_file.suffix + ".tmp")
        temp_file.write_text(
            json.dumps(normalized, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_file.replace(self.cache_file)

    def _normalize(self, value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, Path):
            return str(value)
        if is_dataclass(value):
            return self._normalize(asdict(value))
        if isinstance(value, dict):
            return {str(key): self._normalize(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [self._normalize(item) for item in value]
        if hasattr(value, "to_dict") and callable(value.to_dict):
            try:
                return self._normalize(value.to_dict())
            except Exception:
                return str(value)
        return str(value)
