"""Studio routing: the sidebar must be absent, and the flag must fall back.

The claim under test is the one the request was actually about — "a new tab I
can open within Aria itself, isolated from the sidebar, only focusing on the
coding". That is a structural property of the route, and it is the kind of thing
a re-skinned tab can appear to satisfy while rendering the Rail anyway. So it is
asserted against the source of the module, not against a screenshot.
"""
from __future__ import annotations

import sys
from pathlib import Path

CODE = Path(__file__).resolve().parents[1]
SRC = CODE / "console-frontend" / "src"
sys.path.insert(0, str(CODE))


def read(rel: str) -> str:
    return (SRC / rel).read_text(encoding="utf-8")


def test_studio_is_registered_as_its_own_section():
    app = read("App.tsx")
    assert "const Studio = lazy(() => import('./views/studio/Studio'));" in app
    assert "{ id: '/studio', render: () => <Studio /> }," in app, (
        "/studio must be a registered section, or the route renders the "
        "fallback view and looks like a broken page"
    )


def test_studio_renders_no_rail_at_all():
    """THE isolation claim.

    `/authoring/focus` hides the Rail by re-implementing the layout; this route
    must never import it. An import that exists but is hidden by a flag is one
    refactor away from being visible again.

    Checked against import statements only — the file's own prose says the word
    "Rail" while explaining why it is absent, and a test that greps for a word
    in comments is a test that will be "fixed" by rewording the comment.
    """
    files = ["views/studio/Studio.tsx", "views/studio/Explorer.tsx",
             "views/studio/AgentPanel.tsx"]
    imports = [ln.strip()
               for f in files
               for ln in read(f).splitlines()
               if ln.strip().startswith(("import ", "export {"))]
    for line in imports:
        assert "Rail" not in line, line
        assert "components/ui" not in line, line


def test_the_studio_chunk_is_never_preloaded():
    """Studio pulls the same 4 MB editor chunk as /code, so warming it on idle
    would tax every page load for a route most sessions never open."""
    prefetch = read("lib/prefetch.ts")
    assert "'/studio': () => import('../views/studio/Studio')," in prefetch
    line = [l for l in prefetch.splitlines() if "NEVER_PRELOADED = " in l]
    assert line and "/studio" in line[0], line


def test_the_flag_defaults_off_and_is_one_switch():
    import flags as f
    d = f.DEFINITIONS.get("ui.code_ide")
    assert d, "the studio needs a named master switch"
    assert d["type"] == "bool" and d["default"] is False, d
    assert "sessions/" in d["description"] or "code-studio" in d["description"], (
        "a kill switch must state what its off position actually leaves behind")


def test_off_means_the_old_page_not_an_empty_shell():
    studio = read("views/studio/Studio.tsx")
    assert "const CodePage = lazy(() => import('../Code'));" in studio
    assert "if (!on)" in studio, (
        "with the flag off, /studio must render the Code page it replaced"
    )


def test_the_code_redirect_only_fires_while_that_section_is_visible():
    """KeepAlive leaves Code mounted after you navigate away. An unconditional
    redirect effect would yank the user to /studio from whichever page they had
    actually gone to, so the guard has to be on the active section."""
    code = read("views/Code.tsx")
    i = code.index("studioOn")
    window = code[i - 1200: i + 1200]
    assert "sectionActive && studioOn" in window, window[window.index("studioOn") - 200:][:400]
    assert "navigate('/studio'" in window


def test_a_studio_session_id_cannot_be_injected_into_a_path():
    """The id becomes a filename under state/code-studio/roots/. The client
    mints it, so the shape is validated on both ends — a client that could send
    `../../` would be writing outside the directory it owns."""
    import studio_workspace as sw
    assert sw.SID_RE.match("st-a1b2c3d4")
    for bad in ("", " st", "st/../x", "st\\x", "x" * 90, ".hidden", "a/b"):
        assert not sw.SID_RE.match(bad), bad
    js = read("lib/studioSession.ts")
    assert "SID_RE" in js or "^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$" in js, (
        "the client's id shape must match the server's, or a minted id is "
        "silently refused and the workspace looks empty"
    )


def test_a_query_suffix_is_only_built_on_a_url_that_already_has_a_query():
    """`sidQs(sid, true)` emits `&sid=`, which is only valid behind a `?`.

    Getting this wrong is not a 400 with a message: the browser sends the whole
    `roots&sid=st-x` as the PATH, FastAPI matches nothing, and the user reads
    "405 Method Not Allowed" in a status bar. Caught by clicking "+ folder" in
    the running studio, which is the only way it shows up.
    """
    import re
    api = read("api.ts")
    bad = []
    for m in re.finditer(r"`(/api/[^`]*)\$\{sidQs\(([^)]*)\)\}", api):
        url, args = m.group(1), m.group(2)
        wants_amp = "true" in args
        has_query = "?" in url
        if wants_amp != has_query:
            bad.append((url, args))
    assert not bad, f"sid suffix disagrees with the URL's query state: {bad}"
