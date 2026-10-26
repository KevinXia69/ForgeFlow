"""Bounded, process-local admission control for a single hosted API worker.

The reverse proxy must replace forwarding headers and Uvicorn must trust only
that proxy. This module deliberately never parses X-Forwarded-For itself.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable


class FixedWindowLimiter:
    """Return a Retry-After delay, or zero when a request is admitted.

    A full table rejects new identities until existing windows expire. It
    never evicts active limits, so rotating identities cannot grow memory or
    reset an existing client's budget. Multiple API workers need a shared
    limiter before this can be used as a deployment-wide budget.
    """

    def __init__(self, *, max_keys: int = 4096, window_seconds: int = 60,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if max_keys < 1 or window_seconds < 1:
            raise ValueError("limiter capacity and window must be positive")
        self.max_keys = max_keys
        self.window_seconds = window_seconds
        self.clock = clock
        self._windows: dict[str, tuple[float, int]] = {}
        self._lock = threading.Lock()

    def retry_after(self, key: str, *, limit: int) -> int:
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._lock:
            now = self.clock()
            current = self._windows.get(key)
            if current is not None and now - current[0] < self.window_seconds:
                started, count = current
                if count >= limit:
                    return max(1, math.ceil(started + self.window_seconds - now))
                self._windows[key] = (started, count + 1)
                return 0
            if current is None and len(self._windows) >= self.max_keys:
                self._windows = {
                    item: window for item, window in self._windows.items()
                    if now - window[0] < self.window_seconds
                }
                if len(self._windows) >= self.max_keys:
                    expires = min(started for started, _ in self._windows.values())
                    return max(1, math.ceil(expires + self.window_seconds - now))
            self._windows[key] = (now, 1)
            return 0
