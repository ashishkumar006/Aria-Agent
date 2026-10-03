"""Shared HTTP client factory for adaptors (and integrations).

One place for timeouts, user-agent, and redirect policy so 16 adaptors
don't each invent their own. All adaptor network I/O goes through here.
"""
from __future__ import annotations

import httpx

USER_AGENT = "AriaGateway/1.0 (+channel-adaptor)"
DEFAULT_TIMEOUT = 20.0


def client(timeout: float = DEFAULT_TIMEOUT) -> httpx.Client:
    return httpx.Client(timeout=timeout, follow_redirects=True,
                        headers={"User-Agent": USER_AGENT})


def post(url: str, *, json: dict | None = None, data: dict | None = None,
         headers: dict[str, str] | None = None,
         timeout: float = DEFAULT_TIMEOUT) -> httpx.Response:
    h = {"User-Agent": USER_AGENT}
    if headers:
        h.update(headers)
    with client(timeout) as c:
        return c.post(url, json=json, data=data, headers=h)


def get(url: str, *, params: dict | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT) -> httpx.Response:
    h = {"User-Agent": USER_AGENT}
    if headers:
        h.update(headers)
    with client(timeout) as c:
        return c.get(url, params=params, headers=h)
