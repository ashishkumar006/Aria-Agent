"""Probe every free Kilo model: which ones actually answer?

Context length is a bad proxy for availability - `inkling-small:free` has the
largest context in the catalogue and returns 502. This asks each one the same
small question and reports what came back, so the default is chosen from
measurement rather than from a sorted list.
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kilo_models as KM  # noqa: E402

BASE = "https://api.kilo.ai/api/gateway"
PROMPT = "What is 2+2? Answer with just the number."


def raw_call(model, key=None, timeout=120):
    body = {"model": model, "messages": [{"role": "user", "content": PROMPT}],
            "stream": False, "max_tokens": 200}
    if key:
        body["headers"] = None
    req = urllib.request.Request(
        f"{BASE}/chat/completions", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {key}"} if key else {})})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode()), time.time() - t0


def main():
    import os
    key = os.getenv("KILO_API_KEY") or ""
    with urllib.request.urlopen(f"{BASE}/models", timeout=30) as r:
        data = json.loads(r.read().decode())["data"]
    free = [d["id"] for d in data
            if d.get("isFree") or (d.get("pricing", {}).get("prompt") == "0"
                                   and d.get("pricing", {}).get("completion") == "0")]
    print(f"probing {len(free)} free models\n" + "=" * 66)
    good, bad = [], []
    for mid in free:
        try:
            d, el = raw_call(mid, key)
            msg = (d.get("choices") or [{}])[0].get("message") or {}
            text = (msg.get("content") or msg.get("reasoning") or "").strip()
            out = d.get("usage", {}).get("completion_tokens", 0)
            ok = "4" in text[:40] and out > 0
            (good if ok else bad).append(mid)
            print(f"{'OK  ' if ok else 'WEAK'} {el:5.1f}s out={out:<5} "
                  f"{mid:<52} [{text[:34]!r}]")
        except Exception as e:
            bad.append(mid)
            print(f"{'DOWN'}        {mid:<52} {str(e)[:40]}")
    print("=" * 66)
    print(f"\nanswered correctly: {len(good)}/{len(free)}")
    for m in good:
        print(f"  + {m}")
    print(f"unusable: {len(bad)}")
    for m in bad:
        print(f"  - {m}")
    return good


if __name__ == "__main__":
    main()
