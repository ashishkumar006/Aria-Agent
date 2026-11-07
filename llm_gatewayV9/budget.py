"""Per-user request and token budgets for the inference surface.

The gateway's provider Router already protects upstream rate limits, but it
does not stop one credential from issuing unlimited requests or spending an
unbounded number of tokens. This is the operator-side ceiling behind
invariant 8 (hard limits on time, requests and cost).

Defaults are deliberately generous for a local install; an internet-facing
install can lower them without code changes.
"""
from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, str(default)) or default))
    except (TypeError, ValueError):
        return default


class UserBudget:
    def __init__(self):
        self.rpm = _env_int("GATEWAY_USER_RPM", 120)
        self.daily_requests = _env_int("GATEWAY_USER_DAILY_REQUESTS", 5000)
        self.daily_tokens = _env_int("GATEWAY_USER_DAILY_TOKENS", 5_000_000)
        self._lock = threading.Lock()
        self._minute: dict[str, deque] = defaultdict(deque)
        self._day: dict[str, tuple[int, int]] = {}   # user -> (day, count)
        self._token_cache: dict[str, tuple[float, int]] = {}

    def _day_key(self) -> int:
        return int(time.time() // 86400)

    def check(self, user_id: str) -> tuple[bool, int, str]:
        """Return (allowed, retry_after_seconds, reason)."""
        now = time.time()
        with self._lock:
            q = self._minute[user_id]
            cutoff = now - 60
            while q and q[0] < cutoff:
                q.popleft()
            if self.rpm and len(q) >= self.rpm:
                wait = max(1, int(60 - (now - q[0])))
                return False, wait, "per-minute request budget"
            day, count = self._day.get(user_id, (self._day_key(), 0))
            if day != self._day_key():
                day, count = self._day_key(), 0
            if self.daily_requests and count >= self.daily_requests:
                return False, 3600, "daily request budget"
            q.append(now)
            self._day[user_id] = (day, count + 1)

        if self.daily_tokens:
            cached = self._token_cache.get(user_id)
            used = 0
            if cached and now - cached[0] < 30:
                used = cached[1]
            else:
                try:
                    import db
                    totals = db.user_usage(user_id, days=1)["totals"]
                    used = int(totals.get("input_tokens", 0)) + int(
                        totals.get("output_tokens", 0))
                except Exception:
                    used = 0
                self._token_cache[user_id] = (now, used)
            if used >= self.daily_tokens:
                return False, 3600, "daily token budget"
        return True, 0, ""


budget = UserBudget()
