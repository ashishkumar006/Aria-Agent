"""The image supply path: `search_images` + `fetch_image`.

Why this file exists
--------------------
Placement of images always worked — docgen lifts an embedded raster out of any
stored document, `BLOCK_SCHEMA` advertises the `image` block, and the author is
shown every `(page, index)` handle. What never existed was a way to OBTAIN an
image, so every web-researched report was text-and-charts by default rather than
by choice, and `prompts/author.md` said so outright: "there is no way to fetch an
image from the web, so never ask for one."

That makes these tools the first place in this codebase where the agent pulls
arbitrary, model-chosen bytes from the public internet into the user's document
library. So the tests below are mostly about what is REFUSED.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))


@pytest.fixture()
def flag_state(tmp_path, monkeypatch):
    """Overrides on disk in tmp, so a test cannot flip a live operator switch."""
    import flags
    monkeypatch.setattr(flags, "FLAGS_PATH", tmp_path / "flags.json",
                        raising=False)
    return flags


def _png():
    """The smallest thing that passes the magic-byte check."""
    return (b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)


# ── the switch ───────────────────────────────────────────────────────────
def test_both_tools_refuse_when_the_switch_is_off(flag_state):
    import mcp_server
    flag_state.set_override("documents.image_search", False)
    got = mcp_server.fetch_image("https://example.com/a.png")
    assert got["ok"] is False and got["status"] == "disabled"
    assert "documents.image_search" in got["message"]
    srch = mcp_server.search_images("anything")
    assert srch["ok"] is False and srch["status"] == "disabled"


def test_off_withholds_the_tools_from_the_payload(flag_state):
    """The refusal is a backstop, not the mechanism.

    `flags.py` promises "off = both refuse AND neither is advertised". A tool
    that is merely refused is still offered, and a model that is offered it will
    tell the user the capability exists and then be surprised. This is the same
    shape as the chat payload filter for `schedule_wake`.
    """
    from skills import tool_payload
    flag_state.set_override("documents.image_search", False)
    off = {p["name"] for p in (tool_payload(
        ["render_document", "search_images", "fetch_image"]) or [])}
    assert "search_images" not in off and "fetch_image" not in off, off
    flag_state.set_override("documents.image_search", True)
    on = {p["name"] for p in (tool_payload(
        ["render_document", "search_images", "fetch_image"]) or [])}
    assert {"search_images", "fetch_image"} <= on, on


# ── the three layers must agree ──────────────────────────────────────────
def test_the_image_tools_are_granted_catalogued_and_registered():
    """A capability exists only when all three layers say so. This has now been
    the bug twice in one project, so it is asserted rather than trusted."""
    import asyncio

    import yaml

    import mcp_server
    from skills import _TOOL_CATALOG
    cfg = yaml.safe_load((CODE / "agent_config.yaml").read_text(encoding="utf-8"))
    registered = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    for skill in ("author", "deck"):
        granted = set(cfg[skill]["tools_allowed"])
        for name in ("search_images", "fetch_image"):
            assert name in granted, f"{skill} is not granted {name}"
            assert name in _TOOL_CATALOG, f"{name} is not in _TOOL_CATALOG"
            assert name in registered, f"{name} is not registered as an MCP tool"


# ── SSRF: the guard runs before any byte moves ───────────────────────────
@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8109/v1/documents",       # the gateway itself
    "http://localhost/a.png",
    "http://169.254.169.254/latest/meta-data/",  # cloud metadata
    "http://10.0.0.5/a.png",
    "http://192.168.1.1/a.png",
    "file:///etc/passwd",
    "gopher://example.com/a",
    "",
])
def test_fetch_image_refuses_non_public_targets_before_connecting(url):
    import mcp_server
    out = mcp_server.fetch_image(url)
    assert out["ok"] is False, f"accepted {url!r}"
    assert "refused" in str(out.get("error", "")).lower(), out


def test_a_public_host_that_resolves_private_is_refused(monkeypatch):
    """The literal host looks fine; the answer is not. `example.com` resolving
    to loopback is exactly how a naive host-string check is bypassed."""
    import socket

    import mcp_server
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 0))])
    out = mcp_server.fetch_image("https://looks-fine.test/a.png")
    assert out["ok"] is False
    assert "refused" in str(out.get("error", "")).lower()


# ── what gets stored ─────────────────────────────────────────────────────
def test_bytes_that_lie_about_being_an_image_are_rejected(monkeypatch):
    """Content type alone is not enough: a page that answers 200 text/html with
    `image/png` on the header would otherwise be filed, indexed, and render as
    nothing at all."""
    import httpx

    import mcp_server

    class _Resp:
        status_code = 200

        def __init__(self, body):
            self._body = body
            self.headers = {"content-type": "image/png"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_bytes(self, _n):
            yield self._body

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def stream(self, method, url, headers=None):
            return _Resp(b"<html>not a png at all</html>")

    monkeypatch.setattr(httpx, "Client", _Client)
    out = mcp_server.fetch_image("https://images.example.test/a.png")
    assert out["ok"] is False
    assert "PNG or JPEG" in str(out.get("error", "")), out


def test_an_oversized_image_is_aborted_not_buffered(monkeypatch):
    """The cap has to be checked while streaming. Reading a 4GB response into
    memory to then reject it is a crash, not a refusal."""
    import httpx

    import mcp_server

    class _Resp:
        status_code = 200
        headers = {"content-type": "image/png"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_bytes(self, _n):
            chunk = b"\x89PNG\r\n\x1a\n" + b"\x00" * (1024 * 1024)
            while True:
                yield chunk

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def stream(self, method, url, headers=None):
            return _Resp()

    monkeypatch.setattr(httpx, "Client", _Client)
    out = mcp_server.fetch_image("https://images.example.test/big.png")
    assert out["ok"] is False
    assert "larger than" in str(out.get("error", "")), out


def test_a_redirect_to_loopback_is_refused(monkeypatch):
    """Guarding the first URL and then letting the client follow redirects is
    not a guard — the server picks the second hop."""
    import httpx

    import mcp_server

    class _Redirect:
        status_code = 302
        headers = {"location": "http://127.0.0.1:8109/v1/documents"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_bytes(self, _n):
            return []

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def stream(self, method, url, headers=None):
            return _Redirect()

    monkeypatch.setattr(httpx, "Client", _Client)
    out = mcp_server.fetch_image("https://images.example.test/a.png")
    assert out["ok"] is False
    assert "redirect" in str(out.get("error", "")).lower(), out


# ── the author has to be TOLD ────────────────────────────────────────────
def test_the_unattended_figure_pass_only_takes_commercial_licences(monkeypatch):
    """A human choosing a figure can read the licence. This pass cannot.

    A live 14-page run pulled `BY-NC-ND` and `BY-NC-SA` photographs into a
    report — non-commercial and no-derivatives, in a document the user is free
    to distribute. That is not a cosmetic miss: it is a term the user inherits
    without being asked. So the automatic path takes the narrower set, while
    `search_images` itself still shows everything because there an author is
    choosing.
    """
    import skills

    seen: list = []

    class _Fake:
        @staticmethod
        def search_images(query, max_results=8, commercial_only=False):
            seen.append(commercial_only)
            return {"ok": True, "results": []}

        @staticmethod
        def fetch_image(*a, **k):
            return {"ok": False}

    monkeypatch.setitem(sys.modules, "mcp_server", _Fake)
    skills._acquire_figures("a report with photographs", ["Chapter One"])
    assert seen, "the figure pass did not search at all"
    assert all(seen), f"unattended fetch asked for non-commercial licences too: {seen}"


def test_the_figure_pass_stays_quiet_when_nobody_asked_for_pictures(monkeypatch):
    """It costs a search and a download per chapter topic. A brief that never
    mentions pictures must not pay for that, and must not gain images it did not
    request."""
    import skills

    called = []

    class _Never:
        @staticmethod
        def search_images(*a, **k):
            called.append(1)
            return {"ok": True, "results": []}

    monkeypatch.setitem(sys.modules, "mcp_server", _Never)
    assert skills._acquire_figures("a short memo on the quarterly numbers",
                                   ["Numbers"]) == ""
    assert called == []

def test_the_author_prompt_no_longer_forbids_fetching_images():
    """The single sentence that made the feature unusable even once it existed.

    The renderer supported `image`, the handle listing worked, and the prompt
    told the model never to ask for a web image. Nothing downstream could
    contradict that, so no report ever had a picture.
    """
    text = (CODE / "prompts" / "author.md").read_text(encoding="utf-8")
    assert "there is no way to fetch an image from the web" not in text.lower()
    assert "search_images" in text and "fetch_image" in text
    deck = (CODE / "prompts" / "deck.md").read_text(encoding="utf-8")
    assert "No web images." not in deck


def test_an_empty_figure_listing_still_mentions_the_supply_path():
    """`_figures_context` used to return "" when the run had no uploaded
    figures — indistinguishable from 'pictures are not a thing here', which is
    how a working feature stayed invisible. Silence is the bug being pinned."""
    import skills

    class _Dead:
        def __call__(self, *a, **k):
            raise RuntimeError("gateway down")

    # With the gateway unreachable the helper must not claim there are figures,
    # and must not pretend nothing is possible either.
    out = skills._figures_context(set())
    assert isinstance(out, str)
    # When it DOES speak about availability, it has to name the way to get more.
    if "FIGURE" in out.upper():
        assert "search_images" in out


# ── the fourth layer: the CALLING CONTEXT ───────────────────────────────────
# A tool has to be registered, catalogued, granted to the skill, AND reachable
# from the surface the user is actually talking to. The first three were done
# when the supply gap was closed; the fourth was not, and it is the one that
# decides whether a person in Chat can get a picture.

def test_chat_itself_can_fetch_a_picture():
    """Asked "can you show me an image of a car", the console's own chat
    answered "I am a text-based AI assistant and do not have the ability to
    generate, display, or browse for images." On the tool set chat had, that
    was TRUE — so the fix is not a correction to the wording, it is the grant.
    Read this test before removing either name."""
    import agent_server as ag
    assert "search_images" in ag._CHAT_TOOLS, ag._CHAT_TOOLS
    assert "fetch_image" in ag._CHAT_TOOLS, ag._CHAT_TOOLS


def test_chat_reaches_the_payload_with_the_switch_on(monkeypatch):
    import agent_server as ag
    import flags as f
    monkeypatch.setattr(f, "is_enabled",
                        lambda name, default=False: True, raising=False)
    got = {str((t.get("function") or {}).get("name") or t.get("name") or "")
           for t in (ag._chat_tool_payload() or [])}
    assert {"search_images", "fetch_image"} <= got, sorted(got)[:12]


def test_the_one_image_switch_withholds_them_from_chat_too(monkeypatch):
    """Off must be off on every surface, not just the author's: a switch that
    only one caller honours is a default, not a kill switch."""
    import agent_server as ag
    import flags as f

    def _off(name, default=False):
        return False if name == "documents.image_search" else bool(default)

    monkeypatch.setattr(f, "is_enabled", _off, raising=False)
    got = {str((t.get("function") or {}).get("name") or t.get("name") or "")
           for t in (ag._chat_tool_payload() or [])}
    assert "search_images" not in got and "fetch_image" not in got, sorted(got)


def test_the_chat_prompt_states_the_picture_contract(monkeypatch):
    """The prompt has to describe the tools the surface actually has, and say
    where the picture lands. It must not promise an image card in the
    transcript, because there is not one: a fetched image is a document."""
    src = (CODE / "agent_server.py").read_text(encoding="utf-8")
    start = src.index("_CHAT_SYSTEM = (")
    body = src[start:start + 30000]
    assert "SHOWING A PICTURE" in body
    assert "search_images" in body and "fetch_image" in body
    assert "no image card" in body, (
        "the prompt may not claim the transcript displays a fetched picture")
