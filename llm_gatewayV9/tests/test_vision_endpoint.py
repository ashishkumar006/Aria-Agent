"""V9 `/v1/vision` smoke: a tiny red/blue PNG with a JSON schema.

Was a module-level script - the request ran at *import* time, so any failure
raised during collection and took the entire suite down with it rather than
failing one test. It also sent no `X-Gateway-Token`, so once the gateway
required auth it got a 401 and broke collection for all 500+ tests.

Now a real test that skips when the live gateway or a vision provider is not
available, instead of aborting the run.
"""
import base64
import json
import struct
import sys
import zlib
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

GW = "http://localhost:8109"

SCHEMA = {
    "type": "object",
    "properties": {"left": {"type": "string"}, "right": {"type": "string"}},
    "required": ["left", "right"],
    "additionalProperties": False,
}


def _token() -> str:
    """The gateway token, or "" when the gateway publishes none."""
    env = ""
    p = ROOT / "state" / "gateway.token"
    if p.exists():
        env = p.read_text(encoding="utf-8").strip()
    return env


def make_png() -> bytes:
    W = H = 32

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0)
    raw = bytearray()
    for _ in range(H):
        raw.append(0)
        for x in range(W):
            raw.extend(b"\xff\x20\x20" if x < W // 2 else b"\x20\x40\xff")
    return (sig + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b""))


BODY = {
    "image": f"data:image/png;base64,{base64.b64encode(make_png()).decode()}",
    "prompt": "Name the color on the left half and the color on the right half.",
    "schema": SCHEMA,
    "schema_name": "Halves",
    "agent": "v9_vision_endpoint_smoke",
}


def test_vision_returns_schema_validated_output():
    """Needs a live gateway and a working vision provider, so it skips rather
    than failing when either is absent."""
    headers = {"X-Gateway-Token": _token()} if _token() else {}
    try:
        with httpx.Client(timeout=60) as c:
            r = c.post(f"{GW}/v1/vision", json=BODY, headers=headers)
    except Exception as e:                        # noqa: BLE001
        pytest.skip(f"gateway not reachable: {e}")
    if r.status_code in (401, 403):
        pytest.skip("gateway auth token unavailable")
    if r.status_code == 503:
        pytest.skip(f"no vision provider available: {r.text[:120]}")
    r.raise_for_status()
    out = r.json()
    assert out.get("parsed"), out
    assert out["parsed"]["left"].lower().startswith("red"), out["parsed"]
    assert out["parsed"]["right"].lower().startswith("blue"), out["parsed"]
    print("provider:", out.get("provider"), "model:", out.get("model"),
          "latency_ms:", out.get("latency_ms"))
    print("text:", out.get("text"))
    print("parsed:", json.dumps(out["parsed"]))