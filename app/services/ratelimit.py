"""Small in-memory sliding-window rate limiter (academic demo: one process, resets on restart).

The client key is ``request.client.host``. Uvicorn replaces it with the X-Forwarded-For address ONLY when the
connection comes from a trusted proxy (--proxy-headers + FORWARDED_ALLOW_IPS), so this respects the deployment's
trusted-proxy configuration and never reads forwarded headers itself (they are trivially forged).
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, limit: int, window_seconds: int):
        self.limit, self.window = limit, window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] > self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            if len(self._hits) > 10_000:          # bound memory: drop idle clients
                for k in [k for k, v in self._hits.items() if not v]:
                    del self._hits[k]
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


def client_key(request) -> str:
    return request.client.host if request.client else "unknown"


# Submissions per client: 5 per 10 minutes. Tracking attempts: 30 per 10 minutes (slows code guessing; the
# 80-bit code makes guessing infeasible anyway).
SUBMISSIONS = RateLimiter(5, 600)
TRACKING = RateLimiter(30, 600)
