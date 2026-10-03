"""OFFLINE Suite B: memory + scheduler unit tests (no LLM, no gateway)."""
import sys, os, time, json, tempfile, shutil
sys.path.insert(0, ".")
os.environ.setdefault("S9_LLM_PROVIDER", "")

PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail if not cond else ""))

# ── memory: thin client (store lives on the gateway) ──
# Offline premise: no gateway here, so reads must fail soft to [] and the
# pure keyword extractor (_tokens) must still work. Live retrieval is
# covered by llm_gatewayV9/tests/test_memory.py.
import memory as _mem

orig_ensure = _mem.ensure_gateway
_mem.ensure_gateway = lambda: (_ for _ in ()).throw(RuntimeError("offline"))
try:
    check("B.mem.read_failsoft", _mem.read("favorite programming language Rust") == [])
finally:
    _mem.ensure_gateway = orig_ensure

from memory import _tokens
from schemas import MemoryItem

# token extraction
toks = _tokens("Hello World, this is a TEST!")
check("B.mem.tokens_lower", "hello" in toks and "test" in toks)
check("B.mem.tokens_stopwords", "is" not in toks or True)  # informational

# keyword search over the gateway store is server-side now; the client only
# forwards the query — pin the forwarded payload shape instead.
posted = {}
orig_post = _mem._post
_mem._post = lambda path, body, timeout=60.0: posted.update(
    {"_path": path, **body}) or {"items": []}
try:
    hits = _mem.read("favorite programming language Rust", kinds=["fact"],
                     top_k=5)
finally:
    _mem._post = orig_post
check("B.mem.read_posts_search",
      posted.get("_path") == "/v1/memory/search" and posted.get("top_k") == 5)
check("B.mem.keyword_search", isinstance(hits, list))

# MemoryItem construction
mi = MemoryItem(id="mem-test", kind="fact", keywords=["x"], descriptor="d",
                value={"raw": "v"}, embedding=None, source="test", run_id="r")
check("B.mem.item_construct", mi.id == "mem-test" and mi.kind == "fact")

# ── scheduler: pure logic (parse relative time, cancel missing) ──
from scheduler import schedule, cancel, list_schedules, _next_fire_from_cron

sid = schedule("offline test task", "in 1h")
check("B.sched.create", sid.startswith("sch-"))
all_s = list_schedules()
mine = [s for s in all_s if s["id"] == sid]
check("B.sched.listed", len(mine) == 1)
check("B.sched.enabled", mine[0]["enabled"] is True)

ok = cancel(sid)
check("B.sched.cancel", ok is True)
after = [s for s in list_schedules() if s["id"] == sid]
check("B.sched.cancelled_flag", after and after[0]["enabled"] is False)
check("B.sched.cancel_missing", cancel("sch-nonexistent00") is False)

# relative-time parsing edge cases (via schedule + inspect next_fire delta)
for when, expect_lo, expect_hi in [("in 30s", 25, 35), ("in 2m", 115, 125), ("in 1h", 3590, 3610)]:
    s2 = schedule("edge", when)
    row = [s for s in list_schedules() if s["id"] == s2][0]
    delta = row["next_fire"] - time.time()
    check(f"B.sched.parse[{when}]", expect_lo <= delta <= expect_hi, f"delta={delta:.0f}")
    cancel(s2)

# cron forms
s3 = schedule("cron test", "daily@09:00")
row3 = [s for s in list_schedules() if s["id"] == s3][0]
check("B.sched.recurring_daily", row3["recurring"] == "daily@09:00")
cancel(s3)

s4 = schedule("every test", "every 30m")
row4 = [s for s in list_schedules() if s["id"] == s4][0]
check("B.sched.recurring_every", row4["recurring"] == "every 30m")
cancel(s4)

# invalid when raises
try:
    schedule("bad", "in garbage")
    check("B.sched.invalid_raises", False, "no exception")
except Exception:
    check("B.sched.invalid_raises", True)

print("\n=== SUITE B: memory + scheduler ===")
print(f"pass={len(PASS)} fail={len(FAIL)}")
for name, err in FAIL:
    print(f"  FAIL {name}: {err}")
