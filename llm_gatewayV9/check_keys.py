"""Report which provider credentials are present (names + SET/NOT SET only).

Key names match llm_gatewayV9/providers.py build_providers()/build_router_providers()
exactly — a mismatch here previously reported phantom keys (OPENAI_API_KEY,
GEMINI_API_KEY_2, GITHUB_TOKEN, OPENROUTER_API_KEY) while missing real ones.
Run: uv run python check_keys.py
"""
import os
from dotenv import load_dotenv

load_dotenv('.env')

# (env var, what it enables)
KEYS = [
    ('GEMINI_API_KEY', 'gemini + gemini35lite workers'),
    ('GEMINI_MODEL', 'gemini worker model override (optional)'),
    ('GEMINI_35_LITE_MODEL', 'gemini35lite worker model override (optional)'),
    ('NVIDIA_API_KEY', 'nvidia worker (+ router default)'),
    ('NVIDIA_MODEL', 'nvidia worker model override (optional)'),
    ('GROQ_API_KEY', 'groq worker (+ router default)'),
    ('GROQ_MODEL', 'groq worker model override (optional)'),
    ('CEREBRAS_API_KEY', 'cerebras worker (+ router default)'),
    ('CEREBRAS_MODEL', 'cerebras worker model override (optional)'),
    ('OPEN_ROUTER_API_KEY', 'openrouter worker (note underscore)'),
    ('OPENROUTER_MODEL', 'openrouter model override, no underscore (optional)'),
    ('GITHUB_ACCESS_TOKEN', 'github worker (not GITHUB_TOKEN)'),
    ('GITHUB_MODEL', 'github worker model override (optional)'),
    ('KILO_API_KEY', 'kilo worker'),
    ('KILO_MODEL', 'kilo model override (optional)'),
    ('KILO_BASE_URL', 'kilo gateway URL override (optional)'),
    ('OLLAMA_MODEL', 'ollama worker (also needs ENABLE_OLLAMA_LLM=true)'),
    ('ENABLE_OLLAMA_LLM', 'ollama worker switch (optional)'),
    ('OLLAMA_URL', 'ollama URL override (optional)'),
    ('ROUTER_CEREBRAS_MODEL', 'router model override (optional)'),
    ('ROUTER_GROQ_MODEL', 'router model override (optional)'),
    ('ROUTER_NVIDIA_MODEL', 'router model override (optional)'),
    ('ROUTER_GITHUB_MODEL', 'router model override (optional)'),
    ('EMBED_OLLAMA_MODEL', 'embedder model (optional)'),
    ('EMBED_ORDER', 'embedder order override (optional)'),
    ('LLM_ORDER', 'worker failover order override (optional)'),
    ('ROUTER_ORDER', 'router failover order override (optional)'),
]
for k, _why in KEYS:
    v = os.getenv(k)
    print(k + ': ' + ('SET' if v else 'NOT SET'))

# Multi-key pools: SINGULAR + comma/space-separated PLURAL per provider.
# Counts only — values never printed.
print("\n── key pools (one pool member per key) ──")
import re as _re


def _pool_count(single, plural):
    n = 0
    if (os.getenv(single) or "").strip():
        n += 1
    if os.getenv(plural):
        seen = {(os.getenv(single) or "").strip()}
        for part in _re.split(r"[,\s]+", os.getenv(plural)):
            part = part.strip().strip('"').strip("'")
            if part and part not in seen:
                seen.add(part)
                n += 1
    return n


def _pool_line(member, single, plural, desc):
    n = _pool_count(single, plural)
    extra = "" if n <= 1 else " (" + ", ".join([member] + [f"{member}-{i}" for i in range(2, n + 1)]) + ")"
    print(f"{member:16s} {n} key(s){extra} — {desc}")


_pool_line("gemini", "GEMINI_API_KEY", "GEMINI_API_KEYS", "gemini + gemini35lite workers")
for _m, _s, _p, _d in [("nvidia", "NVIDIA_API_KEY", "NVIDIA_API_KEYS", "nvidia worker"),
                       ("groq", "GROQ_API_KEY", "GROQ_API_KEYS", "groq worker"),
                       ("cerebras", "CEREBRAS_API_KEY", "CEREBRAS_API_KEYS", "cerebras worker"),
                       ("openrouter", "OPEN_ROUTER_API_KEY", "OPEN_ROUTER_API_KEYS", "openrouter worker"),
                       ("kilo", "KILO_API_KEY", "KILO_API_KEYS", "kilo worker"),
                       ("github", "GITHUB_ACCESS_TOKEN", "GITHUB_ACCESS_TOKENS", "github worker")]:
    _pool_line(_m, _s, _p, _d)

print("\n── channels (from adaptor registry) ──")
try:
    from adaptors import registry
    live = configured = 0
    for c in registry.inventory():
        mark = "LIVE " if c.get("configured") else " -   "
        if c.get("available"):
            live += 1
        if c.get("configured"):
            configured += 1
        need = ",".join(c.get("required_keys") or [])
        print(f"{mark} {c['name']:16s} needs: {need or '(none)'}" +
              ("" if c.get("available") else f"  [IMPORT FAIL: {c.get('error', '')[:80]}]"))
    print(f"\n{configured}/{live} channels live (keys present).")
except Exception as e:
    print(f"registry check failed: {type(e).__name__}: {e}")

print("\n── integrations (Tier-1, gateway-owned) ──")
for svc, ks in [("gmail", ["GMAIL_TOKEN"]), ("calendar", ["GOOGLE_CALENDAR_TOKEN"]),
                 ("github", ["GITHUB_TOKEN"]), ("notion", ["NOTION_TOKEN"]),
                 ("websearch", ["TAVILY_API_KEY"])]:
    ok = all(os.getenv(k) for k in ks)
    print(f"{'LIVE ' if ok else ' -   '} {svc:16s} needs: {','.join(ks) or '(none)'}")

print("\n── voice ──")
for k in ["KOKORO_MODEL", "KOKORO_VOICES", "STT_MODEL"]:
    print(k + ': ' + ('SET' if os.getenv(k) else 'default'))
