from __future__ import annotations

from typing import Optional


def format_duration(seconds: Optional[int]) -> str:
    if not seconds:
        return ""
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:d}:{s:02d}"


def format_count(n: Optional[int]) -> str:
    if n is None:
        return ""
    n = int(n)
    def _pretty(v: float) -> str:
        text = f"{v:.1f}"
        if text.endswith(".0"):
            text = text[:-2]
        return text

    if n >= 1_000_000:
        return f"{_pretty(n / 1_000_000)}M"
    if n >= 1_000:
        return f"{_pretty(n / 1_000)}K"
    return str(n)

