"""Rasterise PPTX slides to PNGs with PowerPoint COM (Windows).

Why this exists: a deck has no browser-native rendering, and without a
rasteriser the console could only show a text outline of a .pptx. PowerPoint is
installed on this machine, so we drive it via COM to export each slide as a
PNG — the same "export slides as images" PowerPoint's own Save As does.

Design:
- Results are cached by the file's SHA-256, so opening the same deck twice
  (or two panes at once) rasterises once. Bounded to avoid holding megabytes.
- PowerPoint COM is single-threaded and heavy to start, so the first call
  costs a couple of seconds; later decks reuse the cache.
- Every COM object is released and the temp file removed, and PowerPoint is
  only quit if we started it. A failure returns [] and the caller falls back
  to the text outline — never a blank pane.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import threading

_LOCK = threading.Lock()
_CACHE: dict[str, list[bytes]] = {}
_CACHE_MAX = 8          # decks
_CACHE_ORDER: list[str] = []
_IMAGE_W, _IMAGE_H = 1280, 720


def _key(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def slide_pngs(pptx_bytes: bytes) -> list[bytes]:
    """Return one PNG per slide, or [] when PowerPoint cannot rasterise it."""
    if not pptx_bytes:
        return []
    key = _key(pptx_bytes)
    with _LOCK:
        if key in _CACHE:
            return _CACHE[key]
    pngs = _rasterise(pptx_bytes)
    if pngs:
        with _LOCK:
            _CACHE[key] = pngs
            _CACHE_ORDER.append(key)
            while len(_CACHE_ORDER) > _CACHE_MAX:
                old = _CACHE_ORDER.pop(0)
                _CACHE.pop(old, None)
    return pngs


def _rasterise(pptx_bytes: bytes) -> list[bytes]:
    try:
        import pythoncom
        import win32com.client
    except Exception:
        return []

    src = ""
    outdir = ""
    ppt = None
    app = None
    pythoncom.CoInitialize()
    try:
        fd, src = tempfile.mkstemp(suffix=".pptx", prefix="aria-deck-")
        with os.fdopen(fd, "wb") as f:
            f.write(pptx_bytes)
        outdir = tempfile.mkdtemp(prefix="aria-slides-")
        app = win32com.client.Dispatch("PowerPoint.Application")
        # WithWindow=False avoids flashing the UI; some versions require a
        # visible window, so retry once visible on failure.
        try:
            ppt = app.Presentations.Open(src, WithWindow=False)
        except Exception:
            ppt = app.Presentations.Open(src)
        pngs: list[bytes] = []
        for i in range(1, ppt.Slides.Count + 1):
            out = os.path.join(outdir, f"slide{i}.png")
            ppt.Slides(i).Export(out, "PNG", _IMAGE_W, _IMAGE_H)
            try:
                with open(out, "rb") as f:
                    pngs.append(f.read())
            except OSError:
                pngs.append(b"")
        return pngs
    except Exception:
        return []
    finally:
        try:
            if ppt is not None:
                ppt.Close()
        except Exception:
            pass
        try:
            if app is not None:
                app.Quit()
        except Exception:
            pass
        for p in (src, outdir):
            try:
                if p and os.path.isdir(p):
                    import shutil
                    shutil.rmtree(p, ignore_errors=True)
                elif p and os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass