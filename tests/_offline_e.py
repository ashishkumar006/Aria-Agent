"""OFFLINE Suite E: sandbox and browser extraction (no network/gateway/LLM)."""
import sys, os, tempfile, pathlib, asyncio
sys.path.insert(0, ".")

PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail if not cond else ""))

from sandbox import run_python

# Successful stdout
r = run_python("print(2 + 2)")
check("E.sandbox.stdout", r["exit_code"] == 0 and r["stdout"].strip() == "4")
check("E.sandbox.not_timeout", r["timed_out"] is False)

# stderr + nonzero exit
r = run_python("import sys; print('oops', file=sys.stderr); sys.exit(3)")
check("E.sandbox.exit_code", r["exit_code"] == 3)
check("E.sandbox.stderr", "oops" in r["stderr"])

# syntax error
r = run_python("print('broken'")
check("E.sandbox.syntax_error", r["exit_code"] != 0 and r["timed_out"] is False)

# timeout with short explicit timeout
r = run_python("while True: pass", timeout_s=1)
check("E.sandbox.timeout", r["timed_out"] is True, str(r))
check("E.sandbox.timeout_exit", r["exit_code"] == -1)

# bounded execution
r = run_python("print([i*i for i in range(5)])")
check("E.sandbox.bounded", r["stdout"].strip() == "[0, 1, 4, 9, 16]")

# output cap / large output should return, not hang
r = run_python("print('x' * 100000)")
check("E.sandbox.large_output_returns", r["exit_code"] == 0)

# ── browser extraction against local HTML, no network ──
from browser.skill import _extract, _is_useful_extract, detect_gateway_block
html = """<html><head><title>Offline Test</title></head><body>
<nav>Navigation noise</nav><main><h1>Important Article</h1>
<p>This is the meaningful content of the offline test page. It has enough words to be useful.</p>
<p>Second paragraph with additional details about the subject.</p></main><footer>Footer</footer></body></html>"""
text = _extract(html) or ""
check("E.browser.extract_nonempty", len(text) > 20, repr(text[:100]))
check("E.browser.extract_content", "meaningful content" in text.lower())
check("E.browser.useful", _is_useful_extract(text, "summarise article") is True)
check("E.browser.block_clean", detect_gateway_block(html) in (None, ""))
block_html = "<html><title>Just a moment...</title><body>Checking your browser before accessing</body></html>"
check("E.browser.block_detect", bool(detect_gateway_block(block_html)))

# local browser skill fetch helper: no network, call pure extraction only
print("\n=== SUITE E: sandbox + browser pure logic ===")
print(f"pass={len(PASS)} fail={len(FAIL)}")
for name, err in FAIL:
    print(f"  FAIL {name}: {err}")
