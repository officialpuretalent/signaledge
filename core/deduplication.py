"""
SignalEdge — Signal Deduplication
==================================
Prevents duplicate alerts when the same signal persists across successive
hourly runs (e.g. an EMA crossover that stays valid for 3 consecutive bars).

Key: (pair, signal_direction, bar_time) — tied to the specific bar, not clock time.
Value: datetime the signal was first seen.

The store is module-level, so it survives multiple API requests within the
same process lifetime. A Railway redeploy clears it (acceptable: any signal
genuinely re-fires after a deploy gets through once, then dedup resumes).
"""

import threading
from datetime import datetime, timedelta

_DEFAULT_EXPIRY_HOURS = 48  # Safe upper bound — no forex bar fires twice in 48 hours


class SignalDeduplicator:
    """Thread-safe in-memory dedup store."""

    def __init__(self, expiry_hours: int = _DEFAULT_EXPIRY_HOURS) -> None:
        self._seen: dict[tuple[str, str, str], datetime] = {}
        self._lock = threading.Lock()
        self._expiry = timedelta(hours=expiry_hours)

    def is_new(self, pair: str, signal: str, bar_time: str) -> bool:
        """
        Return True and record this signal if it has NOT been seen before
        (or if its entry has expired). Return False if it is a duplicate.
        """
        key = (pair, signal, bar_time)
        now = datetime.now()
        with self._lock:
            self._evict(now)
            if key in self._seen:
                return False
            self._seen[key] = now
            return True

    def _evict(self, now: datetime) -> None:
        """Remove entries older than expiry_hours. Called under lock."""
        expired = [k for k, seen_at in self._seen.items() if now - seen_at > self._expiry]
        for k in expired:
            del self._seen[k]

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._seen)


# Module-level singleton — shared across all requests in the same process.
deduplicator = SignalDeduplicator()
