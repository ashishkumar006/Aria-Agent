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
        # on its own is safe — scripts stay disabled — and is what an HTML blob
        # frame needs to keep its own styling. (A PDF used to need the frame at
        # all: sandboxing disables plugins, and Chrome's PDF viewer IS a plugin,
        # which is why every PDF opened that way answered "Blocked by Chrome".
        # PDFs are drawn by pdf.js now — see the test below.)
        tokens = set(re.findall(r"allow-[\w-]+", tag))
        if "allow-same-origin" in tokens:
            assert "allow-scripts" not in tokens, (
                f"{path.name}:{line} sandbox allows BOTH scripts and "
                f"same-origin, which re-enables reading the parent's "
                f"aria-token meta tag")


def test_documents_preview_disables_scripts_entirely():
    """The preview iframe is the one fed a blob: URL of user bytes.

    It therefore needs scripts off, not merely same-origin off: an uploaded
    .html rendered in a same-origin frame can reach the parent DOM, and the
    parent DOM is where the per-launch token lives.

    The blob frame has moved twice: out of `Documents.tsx` into the shared
    document window (`components/DocumentModal.tsx`), and then into
    `components/FilePreview.tsx` once the in-page file viewers (Authoring's
    Preview tab, Research's file view, the Discuss page) needed the same
    text/HTML view rather than the raster endpoint that answers 415 for them.
    One frame, one review. Asserted against the file that now owns it, and
    asserted to EXIST — a refactor that drops the blob iframe entirely must
    fail here rather than leave this test checking a file that has none.
    """
    preview = (VIEWS / "components" / "FilePreview.tsx").read_text(
        encoding="utf-8")
    tags = re.findall(r"<iframe\b[^>]*>", preview, re.S)
    assert tags, "the console's blob iframe disappeared from FilePreview.tsx"
    for tag in tags:
        tokens = set(re.findall(r"allow-[\w-]+", tag))
        assert "sandbox" in tag
        assert "allow-scripts" not in tokens, (
            "FilePreview.tsx's iframe allows scripts over a blob: URL of "
            "uploaded bytes — an uploaded .html could steal the token")
        assert "referrerPolicy" in tag

    # The document window must route through that reviewed frame, not keep a
    # private copy of it that nobody re-checks.
    modal = (VIEWS / "components" / "DocumentModal.tsx").read_text(
        encoding="utf-8")
    assert "BlobFrame" in modal, (
        "DocumentModal no longer uses the shared BlobFrame; either it grew its "
        "own iframe or the text/HTML view left the document window entirely")

    # Documents.tsx must not grow a second, unreviewed blob iframe behind it.
    docs = (VIEWS / "views" / "Documents.tsx").read_text(encoding="utf-8")
    assert "<iframe" not in docs, (
        "Documents.tsx renders its own iframe again; the sandboxed one lives in "
        "components/FilePreview.tsx")


def test_a_pdf_is_never_handed_to_a_plugin_frame():
    """The reason the document viewer stopped working in both shells.

    Chrome's PDF viewer is an internal plugin, and a sandboxed browsing context
    disables plugins — so the frame that was there to contain an uploaded `.html`
    also blocked the PDF, and the user read "Blocked by Chrome" in the browser
    and a dark rectangle in the Electron app. The fix is not to relax the
    sandbox: a blob frame with `allow-scripts` back on is the token theft this
    file exists to prevent. A PDF is drawn with pdf.js instead, which needs no
    plugin and puts the page's words in the DOM where a citation can be matched.

    Asserted structurally, because the failure was silent: a frame that renders
    nothing looks, to the code, exactly like a frame that rendered fine.
    """
    modal = (VIEWS / "components" / "DocumentModal.tsx").read_text(
        encoding="utf-8")
    assert "PdfViewer" in modal, "the PDF path no longer draws the document"

    # The guard has to be the format, not merely the render mode: a format the
    # browser can display itself is also "native", so a PDF must be ruled out
    # before the frame branch. That test now lives in one pure function
    # (`pickPanel` in lib/docsource.ts) that every viewer calls, so it is
    # asserted there once instead of per host — and a host that stops calling
    # it fails too.
    preview = (VIEWS / "components" / "FilePreview.tsx").read_text(
        encoding="utf-8")
    source = (VIEWS / "lib" / "docsource.ts").read_text(encoding="utf-8")
    pick = source[source.index("export function pickPanel"):]
    pick = pick[:pick.index("\n}") + 2]
    assert "'pdf'" in pick and "'frame'" in pick and "'raster'" in pick, (
        "pickPanel no longer distinguishes the three panels; a PDF would be "
        "sent to a sandboxed frame again and Chrome would block the plugin")
    for name in ("FilePreview.tsx", "DocumentModal.tsx"):
        body = (VIEWS / "components" / name).read_text(encoding="utf-8")
        assert "pickPanel" in body, (
            f"{name} chooses its viewer by hand again — that is how the raster "
            f"panel came to mount underneath a readable txt document")
    for tag in re.findall(r"<iframe\b[^>]*>", preview, re.S):
        assert "sandbox" in tag

    viewer = (VIEWS / "components" / "PdfViewer.tsx").read_text(encoding="utf-8")
    assert "<iframe" not in viewer, (
        "PdfViewer grew a frame; the whole point is that a PDF is drawn on canvas")
    assert "GlobalWorkerOptions.workerSrc" in viewer, (
        "pdf.js without a worker source parses on the main thread")


def test_html_is_still_an_accepted_upload_type():
    """This is why the sandbox matters; if uploads ever reject .html, this
    test should be revisited rather than the sandbox removed."""
    src = (VIEWS / "views" / "Documents.tsx").read_text(encoding="utf-8")
    assert re.search(r"html", src, re.I), (
        "no .html in Documents.tsx - re-check whether the sandbox is still "
        "load-bearing before removing it")
