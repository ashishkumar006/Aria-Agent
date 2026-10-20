"""Kilo must not be blackbailed for an hour by one model's quota.

Kilo fronts ~400 model ids behind ONE endpoint. A 429 that mentions a daily
quota on a single id says nothing about the other 399, so applying the generic
RPD backoff took the entire free tool-calling tier offline for 3600s - observed
live, and it left every code-agent run with nowhere to go.
"""
import sys
import pathlib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from main import _backoff_for            # noqa: E402


class _Err(Exception):
    def __init__(self, status, msg=""):
        super().__init__(msg)
        self.status = status


LONG = 600   # anything at or above this is an outage, not a backoff


def test_kilo_daily_quota_does_not_take_the_catalogue_offline():
    secs, _reason = _backoff_for(_Err(429, "RPD quota burned"), True, "kilo")
    assert secs < 60, f"kilo blackballed for {secs}s by one model's quota"
    assert secs < LONG


@pytest.mark.parametrize("status,msg", [
    (429, "per day quota exceeded"),
    (429, "daily limit reached"),
    (429, "queue full"),
    (500, "upstream boom"),
    (503, "unavailable"),
])
def test_kilo_backoff_stays_short(status, msg):
    secs, _ = _backoff_for(_Err(status, msg), True, "kilo")
    assert secs < 60, f"kilo backoff {secs}s is too long for {status}/{msg!r}"


def test_kilo_billing_and_auth_are_not_retried_into_a_wall():
    """A 402 or a bad key cannot be fixed by waiting, and hammering it wastes
    the round trip. Both are a clean no-backoff."""
    for status in (402, 401, 403):
        secs, _ = _backoff_for(_Err(status, "nope"), True, "kilo")
        assert secs == 0, f"kilo {status} should not schedule a retry backoff"


def test_other_providers_keep_their_backoffs():
    """The exemption is scoped to Kilo. Removing the long backoff everywhere
    would be a much worse outage than the one being fixed."""
    secs, _ = _backoff_for(_Err(402, "payment required"), True, "gemini35lite")
    assert secs > 0, "gemini 402 lost its billing backoff"
    secs, _ = _backoff_for(_Err(429, "queue full"), False, "gemini35lite")
    assert secs >= 15


def test_kilo_is_the_only_exemption():
    """Explicit, so adding a second exempt provider is a deliberate act."""
    for other in ("gemini", "gemini35lite", "ollama"):
        secs, _ = _backoff_for(_Err(402, "payment required"), False, other)
        assert secs > 0, f"{other} unexpectedly got the kilo exemption"


def test_default_provider_argument_is_backwards_compatible():
    """Existing callers pass no provider; they must still get the old ladder."""
    secs, _ = _backoff_for(_Err(402, "payment required"), False)
    assert secs == 300
