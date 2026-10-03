"""Phase 5: memory service + vector index logic (no live LLM needed for math;
uses gateway :8109 only for one optional embed probe)."""
from __future__ import annotations
import sys, os, tempfile, shutil
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent / "S9SharedCode" / "code"
sys.path.insert(0, str(ROOT))

import numpy as np
from vector_index import VectorIndex, _l2_normalize
import memory as MEM
from schemas import MemoryItem

fails = []
def check(name, cond, detail=""):
    print(f"  {'ok' if cond else 'FAIL'}: {name}" + (f"  {detail}" if not cond else ""))
    if not cond: fails.append(name)

# ── vector_index math ──
v = VectorIndex(Path(tempfile.mkdtemp()))
v.add("a", [1.0, 0.0, 0.0])
v.add("b", [0.0, 1.0, 0.0])
v.add("c", [0.0, 0.0, 1.0])
res = v.search([1.0, 0.0, 0.0], k=3)
check("VectorIndex search ranks nearest first", res and res[0][0] == "a", str(res))
check("VectorIndex dim", v.dim == 3)
check("VectorIndex size", v.size == 3)
# dim mismatch
try:
    v.add("d", [1.0, 0.0]); check("VectorIndex dim mismatch raises", False)
except ValueError:
    check("VectorIndex dim mismatch raises", True)
# normalize
z = _l2_normalize(np.array([0.0, 0.0, 0.0]))
check("l2 normalize zero unchanged", np.allclose(z, [0,0,0]))
u = _l2_normalize(np.array([3.0, 4.0, 0.0]))
check("l2 normalize unit norm", abs(float(np.linalg.norm(u)) - 1.0) < 1e-6)
# persist/reload
v.persist()
v2 = VectorIndex(v.store_dir)
check("VectorIndex reload size", v2.size == 3)
v.clear()
check("VectorIndex clear", v.size == 0)

# ── memory thin client (store + ranking live on the gateway) ──
# Retrieval ranking moved to llm_gatewayV9/memory/service.py (covered by
# llm_gatewayV9/tests/test_memory.py). Here we pin the client contract:
# reads fail soft without a gateway, writes forward the right payloads.
orig_ensure = MEM.ensure_gateway
MEM.ensure_gateway = lambda: (_ for _ in ()).throw(RuntimeError("offline"))
try:
    check("memory.read fail-soft without gateway", MEM.read("sky color") == [])
finally:
    MEM.ensure_gateway = orig_ensure

posted = {}
orig_post = MEM._post
MEM._post = lambda path, body, timeout=60.0: posted.update(
    {"_path": path, **body}) or {"item": {
        "id": "mem:check", "kind": "fact", "descriptor": body.get("descriptor"),
        "value": {}, "source": "s", "run_id": "r"}}
try:
    it = MEM.add_fact("the sky is blue", source="s", run_id="r")
finally:
    MEM._post = orig_post
check("memory.add_fact posts remember/fact",
      posted.get("_path") == "/v1/memory/remember"
      and posted.get("kind") == "fact" and it.descriptor == "the sky is blue")

# ── client fail-soft on dead transport ──
orig_post2 = MEM._post
MEM._post = lambda *a, **k: (_ for _ in ()).throw(Exception("dead"))
r = MEM.read("x")
MEM._post = orig_post2
check("memory.read graceful on failure", r == [])

print(f"\nPHASE5: {len(fails)} failures: {fails}")
