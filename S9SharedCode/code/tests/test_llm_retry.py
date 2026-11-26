"""Which failed model calls are worth retrying.

A long agent run dies on its weakest link: one provider 502 used to throw away
every tool call before it. These pin the line between "the server had a bad
moment" (retry) and "the request was wrong" (do not).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import mcp_runner  # noqa: E402


@pytest.mark.parametrize("text", [
    'gateway HTTP 502: {"detail":"kilo failed: "}',
    "gateway HTTP 503: upstream unavailable",
    "gateway HTTP 429: rate limit exceeded",
    "gateway HTTP 500",
    "timed out after 600s",
    "connection reset by peer",
    "[Errno 11001] getaddrinfo failed",
    "nodename nor servname provided",
])
def test_availability_failures_are_retried(text):
    assert mcp_runner._is_transient_llm_error(RuntimeError(text)) is True


@pytest.mark.parametrize("text", [
    "gateway HTTP 400: messages must not be empty",
    "gateway HTTP 401: bad token",
    "gateway HTTP 403: model not entitled",
    "gateway HTTP 404: no such model",
    "gateway HTTP 413: payload too large",
    "context budget exceeded (900000 chars)",
])
def test_bad_requests_are_not_retried(text):
    assert mcp_runner._is_transient_llm_error(RuntimeError(text)) is False


def test_an_empty_error_is_not_retried():
    assert mcp_runner._is_transient_llm_error(RuntimeError("")) is False


def test_the_retry_budget_is_bounded():
    assert mcp_runner._LLM_MAX_ATTEMPTS >= 2
    assert mcp_runner._LLM_RETRY_BASE_S > 0


@pytest.mark.parametrize("text", [
    "gateway HTTP 429: rate limit exceeded",
    "kilo HTTP 429: {\"error\":\"Rate limit exceeded\",\"error_type\":\"rate_limit_exceeded\"}",
    "too many requests, slow down",
])
def test_a_rate_limit_is_recognised_separately(text):
    e = RuntimeError(text)
    assert mcp_runner._is_rate_limit_error(e) is True
    assert mcp_runner._is_transient_llm_error(e) is True


@pytest.mark.parametrize("text,expected", [
    ("gateway HTTP 429: retry_after 45", 45),
    ("rate limit exceeded; retry in 30s", 30),
    ("please try again in 12 seconds", 12),
    ('{"error":{"retry_after":90}}', 90),
    ("rate limit exceeded", 0),
])
def test_the_provider_s_wait_is_honoured(text, expected):
    assert mcp_runner._retry_after_seconds(RuntimeError(text)) == expected


def test_a_provider_wrapper_failure_is_transient():
    """`kilo failed: 'name'` is a provider-side fault with the cause masked.

    Live: a 429 body the client could not format surfaced as a KeyError, and a
    DNS failure as getaddrinfo. Neither is the request's fault, so both are
    worth another attempt.
    """
    for text in ("gateway HTTP 502: {\"detail\":\"kilo failed: 'name'\"}",
                 "gateway HTTP 502: kilo failed: [Errno 11001] getaddrinfo failed"):
        assert mcp_runner._is_transient_llm_error(RuntimeError(text)) is True


def test_an_exhausted_quota_is_not_retried():
    """A quota that will not clear in seconds is an answer, not a bad moment."""
    e = RuntimeError("quota exceeded for this model")
    assert mcp_runner._is_rate_limit_error(e) is False


def test_a_plain_outage_is_not_treated_as_a_rate_limit():
    e = RuntimeError("gateway HTTP 502: bad gateway")
    assert mcp_runner._is_rate_limit_error(e) is False
    assert mcp_runner._is_transient_llm_error(e) is True
    # A rate limit waits longer than a plain outage.
    assert mcp_runner._LLM_RATE_LIMIT_BASE_S > mcp_runner._LLM_RETRY_BASE_S
    # Both are bounded, and both wait long enough to cross a real blip: the
    # whole point is that a 40-call run survives one.
    assert mcp_runner._LLM_MAX_ATTEMPTS >= 3
    assert mcp_runner._LLM_RETRY_BASE_S * (2 ** (mcp_runner._LLM_MAX_ATTEMPTS - 2)) >= 8


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))