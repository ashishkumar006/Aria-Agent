"""A failed gateway call must say WHY it failed.

The Chat view used to show only `HTTPStatusError: Server error '502 Bad
Gateway' for url ...`. The gateway had already diagnosed the fault - a provider
400 ("must have non-empty content or tool calls") - but `raise_for_status()`
discarded the body, so the cause was invisible to the user and to the agent.
`_raise_for_status` keeps the gateway's message in the exception.
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from client import _raise_for_status  # noqa: E402


def _response(status: int, *, json=None, text: str | None = None) -> httpx.Response:
    req = httpx.Request("POST", "http://127.0.0.1:8109/v1/chat")
    if json is not None:
        return httpx.Response(status, json=json, request=req)
    return httpx.Response(status, text=text or "", request=req)


def test_the_gateways_detail_reaches_the_exception_message():
    r = _response(502, json={"detail": (
        "kilo failed: kilo HTTP 400: invalid message provided at index 1: "
        "must have non-empty content or tool calls")})
    with pytest.raises(httpx.HTTPStatusError) as err:
        _raise_for_status(r)
    message = str(err.value)
    assert "502 Bad Gateway" in message
    assert "kilo HTTP 400" in message
    assert "non-empty content" in message


def test_an_error_key_and_a_plain_text_body_are_also_surfaced():
    r = _response(503, json={"error": "all providers cooling down"})
    with pytest.raises(httpx.HTTPStatusError) as err:
        _raise_for_status(r)
    assert "all providers cooling down" in str(err.value)

    r = _response(500, text="<html>boom</html>")
    with pytest.raises(httpx.HTTPStatusError) as err:
        _raise_for_status(r)
    assert "boom" in str(err.value)


def test_a_success_is_untouched():
    assert _raise_for_status(_response(200, json={"text": "ok"})) is None


def test_a_streaming_error_body_is_read_before_it_is_reported():
    req = httpx.Request("POST", "http://127.0.0.1:8109/v1/chat")
    r = httpx.Response(
        502, stream=httpx.ByteStream(b'{"detail": "provider said no"}'),
        request=req)
    with pytest.raises(httpx.HTTPStatusError) as err:
        _raise_for_status(r)
    assert "provider said no" in str(err.value)

