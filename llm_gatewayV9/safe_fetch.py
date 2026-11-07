"""SSRF-safe fetching for user-supplied URLs.

`/v1/chat` and `/v1/vision` accept image URLs and fetch them with the
gateway's network position. Before this module, `httpx` followed redirects to
anywhere: loopback, private ranges, link-local and the cloud metadata
addresses. That is the classic SSRF in the security checklist.

Rules enforced here:
  * http/https only, no credentials in the URL, ports 80/443
  * resolve the host and reject the request if ANY address is loopback,
    private, link-local, multicast, reserved or unspecified (IPv4 and IPv6,
    including IPv4-mapped IPv6)
  * follow redirects manually, validating every hop
  * cap the body size and the elapsed time
  * optionally require an image content type

DNS rebinding between the check and the connection remains possible without a
pinned-IP transport; the pre-resolution check still blocks the metadata and
private-address classes that this route is exposed to.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urljoin, urlsplit

import httpx

ALLOWED_PORTS = (80, 443)
BLOCKED_HOSTS = {
    "localhost", "metadata", "metadata.google.internal", "instance-data",
    "metadata.azure.internal",
}
USER_AGENT = "AriaGateway/1.0 (+image resolver)"


class UnsafeURLError(ValueError):
    """The URL is not safe to fetch from the gateway's network position."""


def _public_ip(raw: str) -> bool:
    ip = ipaddress.ip_address(raw)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return not (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def validate_url(url: str, *, resolve: bool = True) -> str:
    """Return the URL unchanged or raise UnsafeURLError.

    Resolution uses every address the host returns; one private answer is
    enough to refuse, because the client may pick any of them.
    """
    text = str(url or "").strip()
    if not text:
        raise UnsafeURLError("empty URL")
    parts = urlsplit(text)
    if parts.scheme.lower() not in ("http", "https"):
        raise UnsafeURLError("only http(s) URLs are allowed")
    if parts.username or parts.password:
        raise UnsafeURLError("credentials in URLs are not allowed")
    host = (parts.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise UnsafeURLError("URL has no host")
    if host in BLOCKED_HOSTS or host.endswith(".localhost"):
        raise UnsafeURLError("local and metadata hosts are not allowed")
    # Decimal/octal/hex IP spellings resolve on some stacks (2130706433 is
    # 127.0.0.1). They are never legitimate hostnames.
    if re.fullmatch(r"(?:0x[0-9a-f]+|[0-9]+)", host or ""):
        raise UnsafeURLError("numeric host encodings are not allowed")
    try:
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    except ValueError as e:
        raise UnsafeURLError("invalid port") from e
    if port not in ALLOWED_PORTS:
        raise UnsafeURLError("only ports 80 and 443 are allowed")
    if not resolve:
        return text
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        ip = None
    if ip is not None:
        if not _public_ip(str(ip)):
            raise UnsafeURLError("private or local addresses are not allowed")
        return text
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as e:
        raise UnsafeURLError(f"host could not be resolved: {e}") from e
    addresses = {str(info[4][0]) for info in infos if info[4]}
    if not addresses:
        raise UnsafeURLError("host resolved to no addresses")
    for addr in addresses:
        try:
            public = _public_ip(addr)
        except ValueError as e:
            raise UnsafeURLError("host resolved to an invalid address") from e
        if not public:
            raise UnsafeURLError(
                "host resolves to a private or local address")
    return text


async def fetch_bytes(url: str, *, max_bytes: int = 8 * 1024 * 1024,
                      timeout: float = 15.0, max_redirects: int = 5,
                      image_only: bool = False,
                      transport: httpx.AsyncBaseTransport | None = None
                      ) -> tuple[bytes, str, str]:
    """Fetch a validated URL with redirect and size limits.

    Returns (body, content_type, final_url). Every redirect target is
    validated again before it is requested.
    """
    current = validate_url(url)
    headers = {"User-Agent": USER_AGENT,
               "Accept": "image/*,*/*;q=0.8" if image_only else "*/*"}
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False,
                                 headers=headers, transport=transport) as client:
        for _hop in range(max_redirects + 1):
            r = await client.get(current)
            if r.status_code in (301, 302, 303, 307, 308):
                loc = r.headers.get("location")
                if not loc:
                    raise UnsafeURLError("redirect without a Location")
                current = validate_url(urljoin(current, loc))
                continue
            if r.status_code >= 400:
                raise UnsafeURLError(f"upstream HTTP {r.status_code}")
            ctype = (r.headers.get("content-type") or "").split(";")[0].strip()
            if image_only and ctype and not ctype.startswith("image/"):
                raise UnsafeURLError(f"not an image ({ctype})")
            declared = r.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise UnsafeURLError("response is too large")
            body = bytearray()
            async for chunk in r.aiter_bytes():
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise UnsafeURLError("response is too large")
            return bytes(body), ctype or "application/octet-stream", current
    raise UnsafeURLError("too many redirects")
