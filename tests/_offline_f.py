"""OFFLINE Suite F: prompt/config consistency (no network/gateway/LLM)."""
import sys, os, pathlib, re, yaml
sys.path.insert(0, ".")
root = pathlib.Path(__file__).parent
PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail if not cond else ""))

# All configured prompt files exist and are non-empty
cfg = yaml.safe_load((root / "agent_config.yaml").read_text(encoding="utf-8"))
for name, entry in cfg.items():
    if not isinstance(entry, dict) or "prompt" not in entry:
        continue
    p = root / entry["prompt"]
    check(f"F.prompt_exists[{name}]", p.exists(), str(p))
    if p.exists():
        check(f"F.prompt_nonempty[{name}]", len(p.read_text(encoding="utf-8").strip()) > 20)

# Required core skills present
required = {"planner", "formatter", "coder", "sandbox_executor", "browser", "researcher", "action", "critic", "summariser", "distiller"}
check("F.config.required_skills", required.issubset(cfg), str(required - set(cfg)))

# tool lists are catalog-backed
from skills import _TOOL_CATALOG
for name, entry in cfg.items():
    if isinstance(entry, dict):
        for tool in entry.get("tools_allowed", []) or []:
            check(f"F.tool_registered[{name}:{tool}]", tool in _TOOL_CATALOG)

# planner contract markers
planner = (root / "prompts" / "planner.md").read_text(encoding="utf-8").lower()
for phrase in ("final node", "inputs", "coder", "sandbox_executor", "formatter", "critic"):
    check(f"F.planner_contract[{phrase}]", phrase in planner)

# formatter contract markers
formatter = (root / "prompts" / "formatter.md").read_text(encoding="utf-8").lower()
for phrase in ("last node", "show", "merge all", "inputs"):
    check(f"F.formatter_contract[{phrase}]", phrase in formatter)

# coder safety/output markers
coder = (root / "prompts" / "coder.md").read_text(encoding="utf-8").lower()
for phrase in ("print", "unbounded", "30s"):
    check(f"F.coder_contract[{phrase}]", phrase in coder)

# no stale forced-provider language in active routing files
for rel in ("gateway.py", "browser/client.py"):
    text = (root / rel).read_text(encoding="utf-8").lower()
    check(f"F.no_forced_kilo[{rel}]", "forced routing defaults" not in text and "route to kilo unless" not in text)

# browser UA policy marker
browser = (root / "browser" / "skill.py").read_text(encoding="utf-8")
check("F.browser.descriptive_ua", "AriaAgent/1.0" in browser)

# frontend required DOM IDs exist
html = (root / "web" / "index.html").read_text(encoding="utf-8")
js = (root / "web" / "app.js").read_text(encoding="utf-8")
for ident in ("messages", "composer", "input", "send", "stop", "sidePanel", "costPanel", "schedPanel", "memPanel"):
    check(f"F.frontend.id[{ident}]", f'id="{ident}"' in html)
for token in ("renderMarkdown", "typewriter", "toast", "scroll-bottom", "prefers-reduced-motion"):
    check(f"F.frontend.feature[{token}]", token in (js + (root / "web" / "style.css").read_text(encoding="utf-8")))

print("\n=== SUITE F: prompt/config consistency ===")
print(f"pass={len(PASS)} fail={len(FAIL)}")
for name, err in FAIL:
    print(f"  FAIL {name}: {err}")
