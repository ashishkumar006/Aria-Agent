"""Shared HTTP client factory for adaptors (and integrations).

One place for timeouts, user-agent, and redirect policy so 16 adaptors
don't each invent their own. All adaptor network I/O goes through here.

`client()` still hands out a NEW client, because several adaptors write
`with client(timeout) as c:` and closing that client at the end of the block is
what they asked for. `post()`/`get()` deliberately do NOT do that: building an
`httpx.Client` costs 230-590 ms measured on this host (fresh SSL context plus
connection pool every time), and it used to happen on every single channel
call - so a Telegram send paid half a second of local overhead before it sent
a byte. They share one pooled client instead.
"""
from __future__ import annotations

import contextlib
import threading

import httpx

USER_AGENT = "AriaGateway/1.0 (+channel-adaptor)"
DEFAULT_TIMEOUT = 20.0

_POOL: dict[float, httpx.Client] = {}
_POOL_LOCK = threading.Lock()


def client(timeout: float = DEFAULT_TIMEOUT) -> httpx.Client:
    return httpx.Client(timeout=timeout, follow_redirects=True,
                        headers={"User-Agent": USER_AGENT})


def pooled_client(timeout: float = DEFAULT_TIMEOUT) -> httpx.Client:
    """A reusable client for `timeout`. Callers must not close it.

    `httpx.Client` is documented as thread-safe, so one instance can serve the
    poller, the send endpoints and the deploy probes at once.
    """
    key = float(timeout)
    with _POOL_LOCK:
        c = _POOL.get(key)
        # `getattr`, not `c.is_closed`: a duck-typed client (a test stub, or a
        # wrapper) that simply does not implement the flag is treated as usable
        # rather than crashing the caller with an AttributeError that looks like
        # a credential failure. Real httpx clients always have it.
        if c is None or getattr(c, "is_closed", False):
            c = httpx.Client(timeout=key, follow_redirects=True,
                             headers={"User-Agent": USER_AGENT},
                             limits=httpx.Limits(max_keepalive_connections=8,
                                                 max_connections=32))
            _POOL[key] = c
        return c


@contextlib.contextmanager
def shared(timeout: float = DEFAULT_TIMEOUT):
    """`with shared() as client:` over the pooled client - exit does not close.

    For call sites that were written as `with httpx.Client(...) as client:` and
    only need the connection reuse, not the teardown.
    """
    yield pooled_client(timeout)


def reset_pools() -> None:
    """Forget every pooled client. For tests only, never for traffic.

    `tests/conftest.py` patches `httpx.Client.__init__` to inject default
    headers, and several suites patch `httpx.Client` outright to record a call.
    Those only take effect while the call site still constructs a client, so a
    pooled instance would carry the FIRST test's patched behaviour into every
    later one. Ten integration tests failed on exactly that ordering effect.
    """
    with _POOL_LOCK:
        _POOL.clear()


def post(url: str, *, json: dict | None = None, data: dict | None = None,
         headers: dict[str, str] | None = None,
         files: dict | None = None,
         timeout: float = DEFAULT_TIMEOUT) -> httpx.Response:
    """POST a request.

    `files` switches the body to multipart/form-data, which Telegram's upload
    methods (sendDocument, sendPhoto, ...) require: they reject a JSON body
    outright, so `json=` cannot be coerced into working.

    The response is read before it is returned, so the connection goes straight
    back to the pool and the caller never has to close anything.
    """
    h = {"User-Agent": USER_AGENT}
    if headers:
        h.update(headers)
    c = pooled_client(timeout)
    if files is not None:
        # httpx sets the multipart boundary itself; a hand-written
        # Content-Type here would omit it and the server would not be
        # able to find the boundary to parse against.
        r = c.post(url, data=data, files=files, headers=h)
    else:
        r = c.post(url, json=json, data=data, headers=h)
    r.read()
    return r


def get(url: str, *, params: dict | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT) -> httpx.Response:
    h = {"User-Agent": USER_AGENT}
    if headers:
        h.update(headers)
    r = pooled_client(timeout).get(url, params=params, headers=h)
    r.read()
    return r

