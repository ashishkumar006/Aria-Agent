"""OFFLINE Suite G: frontend asset/static contract validation.
No browser, network, gateway, or LLM calls.
"""
import pathlib, re
root = pathlib.Path(__file__).parent
PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail if not cond else ""))

html = (root / "web" / "index.html").read_text(encoding="utf-8")
js = (root / "web" / "app.js").read_text(encoding="utf-8")
css = (root / "web" / "style.css").read_text(encoding="utf-8")

# referenced local assets exist
for asset in re.findall(r'(?:href|src)=["\']([^"\']+)["\']', html):
    if not asset.startswith(("http:", "https:", "data:", "#")):
        check(f"G.asset[{asset}]", (root / "web" / asset).exists())

# required DOM hooks are present in HTML and referenced by JS
ids = set(re.findall(r'id=["\']([^"\']+)', html))
for ident in ("messages", "composer", "input", "send", "stop", "mic", "tts", "audioFile", "sidePanel", "costPanel", "schedPanel", "memPanel", "newChat"):
    check(f"G.dom[{ident}]", ident in ids)
    check(f"G.jsref[{ident}]", f'$("{ident}")' in js or f"getElementById(\"{ident}\")" in js)

# important behavior contracts
for token in ("fetch(\"/api/chat\"", "/api/health", "/api/tts", "renderMarkdown", "typewriter", "toast", "scroll-bottom", "prefers-reduced-motion"):
    check(f"G.feature[{token}]", token in (js + css))

# no unsafe direct document HTML insertion of server answer except escaped renderer
check("G.xss.escape_html", "function escapeHtml" in js)
check("G.xss.render_markdown", "escapeHtml(src)" in js)
check("G.xss.user_text_content", "b.textContent = text" in js)

# CSS structural sanity
check("G.css.root", ":root" in css)
check("G.css.mobile", "@media (max-width: 600px)" in css)
check("G.css.reduced_motion", "prefers-reduced-motion" in css)
check("G.css.toast", ".toast-host" in css and ".toast.show" in css)
check("G.css.avatar", ".avatar" in css)

# balanced basic delimiters (cheap static guard)
check("G.js.braces", js.count("{") == js.count("}"), f"{js.count('{')} != {js.count('}')}")
check("G.css.braces", css.count("{") == css.count("}"), f"{css.count('{')} != {css.count('}')}")

print("\n=== SUITE G: frontend static validation ===")
print(f"pass={len(PASS)} fail={len(FAIL)}")
for name, err in FAIL:
    print(f"  FAIL {name}: {err}")
