from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from typing import Protocol


class RateLimiter(Protocol):
    def allow(self, bucket: str, key: str, *, limit: int, window_seconds: int) -> bool: ...


class InMemoryRateLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._entries: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, bucket: str, key: str, *, limit: int, window_seconds: int) -> bool:
        now = self._clock()
        cutoff = now - window_seconds
        identity = (bucket, key)
        with self._lock:
            timestamps = self._entries[identity]
            while timestamps and timestamps[0] <= cutoff:
                timestamps.popleft()
            if len(timestamps) >= limit:
                return False
            timestamps.append(now)
            return True
