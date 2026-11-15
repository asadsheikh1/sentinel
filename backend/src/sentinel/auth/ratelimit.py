"""In-memory sliding-window rate limiter.

Good enough for the single-process deployment in v1 (one API container).
If the API is ever scaled horizontally, this moves to Postgres or Caddy.
"""

import time
from collections import defaultdict, deque
from collections.abc import Callable
from threading import Lock


class RateLimiter:
    def __init__(
        self, limit: int, window_seconds: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.limit = limit
        self.window = window_seconds
        self._clock = clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def hit(self, key: str) -> float | None:
        """Record an attempt. Returns None if allowed, else seconds until retry."""
        now = self._clock()
        with self._lock:
            q = self._hits[key]
            while q and q[0] <= now - self.window:
                q.popleft()
            if len(q) >= self.limit:
                return max(0.0, q[0] + self.window - now)
            q.append(now)
            if len(self._hits) > 100_000:  # crude memory bound under attack
                self._prune(now)
            return None

    def _prune(self, now: float) -> None:
        for k in [k for k, q in self._hits.items() if not q or q[-1] <= now - self.window]:
            del self._hits[k]
