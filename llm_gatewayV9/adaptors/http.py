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
         files: dict | None = None,
         timeout: float = DEFAULT_TIMEOUT) -> httpx.Response:
    """POST a request.

    `files` switches the body to multipart/form-data, which Telegram's upload
    methods (sendDocument, sendPhoto, ...) require: they reject a JSON body
    outright, so `json=` cannot be coerced into working.
    """
    h = {"User-Agent": USER_AGENT}
    if headers:
        h.update(headers)
    with client(timeout) as c:
        if files is not None:
            # httpx sets the multipart boundary itself; a hand-written
            # Content-Type here would omit it and the server would not be
            # able to find the boundary to parse against.
            return c.post(url, data=data, files=files, headers=h)
        return c.post(url, json=json, data=data, headers=h)


def get(url: str, *, params: dict | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT) -> httpx.Response:
    h = {"User-Agent": USER_AGENT}
    if headers:
        h.update(headers)
    with client(timeout) as c:
        return c.get(url, params=params, headers=h)
