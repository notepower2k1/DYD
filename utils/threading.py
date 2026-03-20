from __future__ import annotations

import threading
from functools import wraps
from typing import Any, Callable, TypeVar, cast


F = TypeVar("F", bound=Callable[..., Any])


def run_in_thread(fn: F) -> F:
    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> None:
        thread = threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True)
        thread.start()

    return cast(F, wrapper)

