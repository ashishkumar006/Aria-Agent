"""Document generation API: POST a content model, download a real file.

Routes:
    GET  /v1/docgen/formats     - what can be produced
    GET  /v1/docgen/schema      - the content model, layout options, block types
    POST /v1/docgen             - {format, spec} -> the file itself
    POST /v1/docgen/preview     - {format, spec | artifact} -> page images

Returns the binary with Content-Disposition so a browser (or curl -OJ)
saves it directly. Errors are 400 with a readable reason: a document
author should never get a 500 for a typo.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

import docgen
import docpreviewimg as _preview

router = APIRouter()

FORMATS = {
    "pdf": "Printable report — headings, paragraphs, bullets, tables",
    "pptx": "PowerPoint deck — title slide + one section per block group",
    "docx": "Word document",
    "xlsx": "Excel workbook — one sheet per table",
}


@router.get("/v1/docgen/formats")
async def docgen_formats():
    return {"formats": [{"id": k, "what": v} for k, v in FORMATS.items()]}


@router.get("/v1/docgen/schema")
async def docgen_schema():
    """The content model, so a caller (or an LLM) can build a valid spec.

    The block list comes from docgen.BLOCK_SCHEMA - the same source the
    normaliser is checked against - so it cannot fall behind the code again.
    """
    out = docgen.spec_schema()
    out["limits"] = {
        "max_blocks": docgen.MAX_BLOCKS,
        "max_table_rows": docgen.MAX_TABLE_ROWS,
        "max_table_cols": docgen.MAX_TABLE_COLS,
        "max_output_bytes": docgen.MAX_BYTES,
    }
    return out


@router.post("/v1/docgen/preview")
async def docgen_preview(request: Request):
    """Render the DOCUMENT, not a description of it, for the console to show.

    Two ways in:
      * a `spec` - a layout preview while the user is still choosing options,
        which is what makes the setup panel feel live rather than a form;
      * an `artifact` id - the delivered file's own bytes, so what the
        console previews is exactly what the download returns.

    Returns PNG page images for pdf, SVG slides for pptx, and HTML built from
    the saved .docx/.xlsx for the others.
    """
    try:
        raw = await request.body()
        body = json.loads(raw.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError) as e:
        return JSONResponse(status_code=400,
                            content={"error": f"invalid JSON body: {e}"[:200]})
    if not isinstance(body, dict):
        return JSONResponse(status_code=400,
                            content={"error": "body must be a JSON object"})
    fmt = str(body.get("format") or body.get("kind") or "").strip().lower()
    spec = body.get("spec")
    if not isinstance(spec, dict):
        # `blob_b64` must be excluded. An artifact request is
        # {artifact, format, blob_b64}: without this the derived "spec" was
        # the whole request minus three keys, so it contained blob_b64, so it
        # was non-empty, so the pptx path never rebuilt a spec from the bytes
        # and every delivered deck previewed as "0 slides".
        spec = {k: v for k, v in body.items()
                if k not in ("format", "kind", "spec", "artifact",
                             "max_pages", "blob_b64")}
    blob = None
    # The agent owns the artifact store, so previewing a DELIVERED file means
    # the bytes come with the request. Rasterising what the download button
    # returns is the whole point of the artifact path.
    raw_b64 = body.get("blob_b64")
    if isinstance(raw_b64, str) and raw_b64:
        import base64 as _b64
        try:
            blob = _b64.b64decode(raw_b64, validate=True)
        except Exception:
            return JSONResponse(status_code=400,
                                content={"error": "blob_b64 is not valid base64"})
        if len(blob) > 40 * 1024 * 1024:
            return JSONResponse(status_code=413,
                                content={"error": "artifact too large to preview"})
        if not fmt:
            fmt = {b"%PDF": "pdf", b"PK\x03\x04": ""}.get(blob[:5], "")
    art = str(body.get("artifact") or "")
    if art and blob is None:
        # Only valid when the gateway holds the artefact itself.
        if not re.fullmatch(r"art:[0-9a-fA-F]{16}", art):
            return JSONResponse(status_code=400,
                                content={"error": "malformed artifact id"})
        try:
            blob = _artifact_bytes(art)
        except FileNotFoundError:
            return JSONResponse(status_code=404,
                                content={"error": "no such artifact"})
        if not fmt:
            fmt = _artifact_format(art)
    try:
        out = _preview.render_preview(fmt, blob, spec,
                                      body.get("max_pages") or
                                      _preview.MAX_PREVIEW_PAGES)
    except _preview.ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)[:200]})
    except Exception as e:
        return JSONResponse(status_code=500, content={
            "error": f"{type(e).__name__}: {e}"[:300]})
    out["format"] = fmt
    return JSONResponse(content=out)


def _artifact_bytes(artifact_id: str) -> bytes:
    root = Path(__file__).parent / "state" / "artifacts"
    digest = artifact_id.removeprefix("art:")
    path = root / f"{digest}.bin"
    if not path.is_file():
        raise FileNotFoundError(artifact_id)
    return path.read_bytes()


def _artifact_format(artifact_id: str) -> str:
    import json as _json
    root = Path(__file__).parent / "state" / "artifacts"
    meta = root / f"{artifact_id.removeprefix('art:')}.json"
    try:
        data = _json.loads(meta.read_text(encoding="utf-8"))
    except Exception:
        return ""
    ctype = str(data.get("content_type") or "")
    return {"application/pdf": "pdf",
            "application/vnd.openxmlformats-officedocument."
            "presentationml.presentation": "pptx",
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document": "docx",
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet": "xlsx"}.get(ctype, "")


@router.post("/v1/docgen")
async def docgen_create(request: Request):
    try:
        raw = await request.body()
        body = json.loads(raw.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError) as e:
        return JSONResponse(status_code=400,
                            content={"error": f"invalid JSON body: {e}"[:200]})
    if not isinstance(body, dict):
        return JSONResponse(status_code=400,
                            content={"error": "body must be a JSON object"})
    fmt = body.get("format") or body.get("kind") or ""
    spec = body.get("spec")
    if not isinstance(spec, dict):
        spec = {k: v for k, v in body.items()
                if k not in ("format", "kind", "spec")}
    if not spec:
        return JSONResponse(status_code=400, content={
            "error": "nothing to render: send a `spec` with a title or blocks"})
    try:
        data, name, ctype = docgen.generate(str(fmt), spec)
    except docgen.DocGenError as e:
        return JSONResponse(status_code=400, content={"error": str(e)[:300]})
    except ImportError as e:
        return JSONResponse(status_code=503, content={
            "error": f"missing generator library: {e}. "
                     f"pip install reportlab python-pptx python-docx openpyxl"})
    except Exception as e:  # a generator bug must not be a 500 with no detail
        return JSONResponse(status_code=500, content={
            "error": f"{type(e).__name__}: {e}"[:300]})
    return Response(
        content=data, media_type=ctype,
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Content-Length": str(len(data)),
            "X-Doc-Format": str(fmt).lower(),
            "X-Doc-Bytes": str(len(data)),
        })
