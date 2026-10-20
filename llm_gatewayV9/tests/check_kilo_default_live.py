"""Prove the default model is derived from the live roster, in-process.

Kilo is not wired on this box (no KILO_API_KEY), so the default is null and
there is nothing to check. This sets the key for THIS process only - .env is
never touched - builds the pool, runs discovery, and asserts the model the
provider ends up with actually exists upstream.
"""
import asyncio
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ["KILO_API_KEY"] = "sk-placeholder-not-used-for-a-real-call"
os.environ.pop("KILO_MODEL", None)

import providers as P          # noqa: E402
from cache import GeminiCache  # noqa: E402
import kilo_models as KM       # noqa: E402

ws = P.build_providers(GeminiCache())
assert "kilo" in ws, "kilo should be wired with a key present"
guess = ws["kilo"].model
print(f"build_providers() guess : {guess}")

added = asyncio.run(KM.prime_routes(ws))
chosen = ws["kilo"].model
print(f"after live discovery    : {chosen}  (+{added} routes)")

with urllib.request.urlopen("https://api.kilo.ai/api/gateway/models",
                            timeout=30) as r:
    upstream = {m["id"] for m in json.loads(r.read().decode())["data"]}

assert guess in upstream, f"startup guess {guess!r} is not a real id"
assert chosen in upstream, f"chosen default {chosen!r} is not a real id"
assert not chosen.endswith(":free") or chosen in upstream

roster = KM._LAST_GOOD["models"]
free = [m for m in roster if m["is_free"]]
print(f"roster: {len(roster)} models, {len(free)} free")
print(f"chosen is free          : {any(m['id'] == chosen and m['is_free'] for m in roster)}")
print(f"chosen supports tools   : {next((m['supports_tools'] for m in roster if m['id'] == chosen), None)}")

print("\nVERIFIED: the default is a real, current upstream id "
      "(the old tencent/hy3:free is not served any more).")
