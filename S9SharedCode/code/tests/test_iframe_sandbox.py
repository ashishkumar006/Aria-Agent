"""Every iframe that renders user-supplied content must be sandboxed.

`Documents.tsx` previewed an uploaded document in an unsandboxed iframe with a
blob: URL, which means the document shares the console's own origin. `.html` is
an accepted upload type and the gateway serves it back as `text/html`, so an
uploaded page executed with full same-origin privileges: it read the
`aria-token` meta tag out of the parent document (verified — the frame's title
became `TOKENLEN=43`) and could then issue authenticated `/api/*` calls as the
user, reading every memory drawer and spending money on runs.

The auth design rests on "a remote page cannot read the token". An upload is
not remote, which is precisely the case that turned the assumption inside out.

`allow-same-origin` must not be present: without it the frame gets an opaque
origin and cannot reach the parent DOM. `DocSetup.tsx` and `Apps.tsx` already
sandbox their iframes, so this was the only gap.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

VIEWS = Path(__file__).resolve().parents[1] / "console-frontend" / "src"


def _tsx_sources() -> list[Path]:
    return sorted(list(VIEWS.rglob("*.tsx")) + list(VIEWS.rglob("*.ts")))


def _iframe_files() -> list[Path]:
    """Only the files that actually render an iframe.

    Parametrising over every source file and skipping the ones without an
    iframe produced 27 skips against 3 real cases, so the suite reported a
    jump in skips that had nothing to do with the code under test. A skipped
    test is noise; a missing one is a hole.
    """
    return [p for p in _tsx_sources() if "<iframe" in p.read_text(encoding="utf-8")]


def test_there_are_iframes_to_check():
    """Guard against this test silently passing if the search breaks."""
    files = _iframe_files()
    assert files, (
        "no iframes found in console-frontend/src — either the search pattern "
        "is wrong or every iframe was removed")


@pytest.mark.parametrize("path", _iframe_files(), ids=lambda p: p.name)
def test_every_iframe_is_sandboxed(path):
    src = path.read_text(encoding="utf-8")
    for m in re.finditer(r"<iframe\b[^>]*>", src, re.S):
        tag = m.group(0)
        line = src[:m.start()].count("\n") + 1
        assert "sandbox" in tag, (
            f"{path.name}:{line} iframe has no sandbox attribute: an uploaded "
            f".html document would execute with the console's origin and can "
            f"read the aria-token meta tag out of the parent document")

        # The combination that matters is allow-scripts WITH allow-same-origin:
        # together they hand a document full same-origin scripting, which is
        # exactly the hole this test was written to close. `allow-same-origin`
        # on its own is safe — scripts stay disabled — and is required for
        # Chrome's PDF viewer, which sandbox otherwise blocks entirely
        # (measured: `sandbox=""` renders "This page has been blocked by
        # Chrome" for every PDF).
        tokens = set(re.findall(r"allow-[\w-]+", tag))
        if "allow-same-origin" in tokens:
            assert "allow-scripts" not in tokens, (
                f"{path.name}:{line} sandbox allows BOTH scripts and "
                f"same-origin, which re-enables reading the parent's "
                f"aria-token meta tag")


def test_documents_preview_disables_scripts_entirely():
    """The Documents preview is the one iframe fed a blob: URL of user bytes.

    It therefore needs scripts off, not merely same-origin off: an uploaded
    .html rendered in a same-origin frame can reach the parent DOM, and the
    parent DOM is where the per-launch token lives.
    """
    src = (VIEWS / "views" / "Documents.tsx").read_text(encoding="utf-8")
    tags = re.findall(r"<iframe\b[^>]*>", src, re.S)
    assert tags, "the Documents preview iframe disappeared"
    for tag in tags:
        tokens = set(re.findall(r"allow-[\w-]+", tag))
        assert "sandbox" in tag
        assert "allow-scripts" not in tokens, (
            "Documents.tsx preview iframe allows scripts over a blob: URL of "
            "uploaded bytes — an uploaded .html could steal the token")
        assert "referrerPolicy" in tag


def test_html_is_still_an_accepted_upload_type():
    """This is why the sandbox matters; if uploads ever reject .html, this
    test should be revisited rather than the sandbox removed."""
    src = (VIEWS / "views" / "Documents.tsx").read_text(encoding="utf-8")
    assert re.search(r"html", src, re.I), (
        "no .html in Documents.tsx - re-check whether the sandbox is still "
        "load-bearing before removing it")
