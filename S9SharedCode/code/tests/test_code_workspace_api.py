"""The Code workspace endpoints are read-only and path-confined.

These are the guards that keep `/api/code/*` from becoming a filesystem
disclosure hole on a server that currently has no authentication at all: the
agent runs as the user, so any path it can be talked into reading is a path
the attacker can read too. Every case below is an escape attempt or a
resource-exhaustion attempt, and each must be refused rather than served.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent_server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


client = TestClient(agent_server.app)


def _get(path: str, params: dict) -> tuple[int, dict]:
    r = client.get(path, params=params)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {}


# ── traversal ───────────────────────────────────────────────────────────────

ESCAPES = [
    "../../../../Windows/System32/drivers/etc/hosts",
    "..\\..\\..\\Windows\\win.ini",
    "/etc/passwd",
    "C:/Windows/win.ini",
    "S9SharedCode/code/../../../../etc/passwd",
    "S9SharedCode/code/../../../../../../../../boot.ini",
    "./../../secrets.md",
    "..%2f..%2f..%2fetc%2fpasswd",
]


def test_no_traversal_escapes_the_workspace():
    for esc in ESCAPES:
        code, body = _get("/api/code/file", {"path": esc})
        assert code in (400, 403), f"{esc!r} returned {code}: {body}"
        assert "text" not in body, f"{esc!r} leaked file content: {body}"


def test_traversal_is_refused_for_tree_listing_too():
    for esc in ESCAPES:
        code, body = _get("/api/code/tree", {"path": esc})
        assert code in (200, 400, 403), f"{esc!r} returned {code}"
        if code == 200:
            assert "entries" not in body or body.get("error"), (
                f"{esc!r} listed a directory outside the workspace: {body}")


def test_sibling_directory_of_the_repo_is_not_reachable():
    """The roots are fixed server-side; a client cannot ask for another one."""
    code, body = _get("/api/code/tree", {"path": "llm_gatewayV9/../llm_gatewayV8"})
    assert code in (400, 403) or "error" in body, body
    code, body = _get("/api/code/file", {"path": "S9SharedCode"})
    assert code in (400, 403), body


# ── hidden and sensitive files ───────────────────────────────────────────────


def test_env_files_are_never_served():
    for name in (".env", "S9SharedCode/code/.env"):
        code, body = _get("/api/code/file", {"path": name})
        assert code in (403, 404), f"{name} returned {code}: {body}"
        assert "text" not in body, f"{name} leaked: {body}"


def test_hidden_files_are_not_listed():
    code, body = _get("/api/code/tree", {"path": "S9SharedCode/code"})
    assert code == 200, body
    names = {e["name"] for e in body["entries"]}
    assert not any(n.startswith(".") for n in names), names
    assert ".env" not in names


def test_ignored_directories_are_absent_from_every_listing():
    code, body = _get("/api/code/tree", {"path": "S9SharedCode/code/console-frontend"})
    assert code == 200, body
    names = {e["name"] for e in body["entries"]}
    assert "node_modules" not in names, names
    assert "dist" not in names, names


# ── type and size limits ────────────────────────────────────────────────────


def test_disallowed_extensions_are_refused():
    for name in ("S9SharedCode/code/uv.lock.orig",
                 "llm_gatewayV9/gateway_v9.db"):
        code, body = _get("/api/code/file", {"path": name})
        assert code in (403, 404, 415), f"{name} returned {code}: {body}"


def test_empty_path_is_a_bad_request():
    assert _get("/api/code/file", {"path": ""})[0] == 400
    assert _get("/api/code/file", {})[0] == 400


def test_a_real_file_beside_the_roots_is_still_refused():
    """The guard must be doing the work, not the file simply not existing.

    `AGENTS.md` sits at the repo root, outside both Code roots, and is a real
    readable file. If traversal handling were broken while path resolution
    still worked, this is the case that would expose it.
    """
    assert (agent_server._CODE_ROOT / "AGENTS.md").is_file(), "fixture moved"
    code, body = _get("/api/code/file", {"path": "AGENTS.md"})
    assert code == 403, f"a file outside the roots was served: {code} {body}"
    assert "text" not in body


def test_the_roots_themselves_are_readable():
    """Both directions: the guard must not refuse legitimate paths."""
    for good in ("llm_gatewayV9/main.py",
                 "S9SharedCode/code/agent_server.py",
                 "llm_gatewayV9/docs/DOCUMENTS_PLAN.md"):
        code, body = _get("/api/code/file", {"path": good})
        assert code == 200, f"{good} was refused: {code} {body}"
        assert body["text"], good


def test_directory_is_not_served_as_a_file():
    code, body = _get("/api/code/file", {"path": "llm_gatewayV9/documents"})
    assert code == 400, body
    assert "text" not in body


# ── the happy path still works ───────────────────────────────────────────────


def test_real_source_file_is_served_with_language_and_line_count():
    code, body = _get("/api/code/file", {"path": "llm_gatewayV9/documents/chunker.py"})
    assert code == 200, body
    assert body["language"] == "python"
    assert body["lines"] > 10
    assert "def " in body["text"]
    assert body["version"]


def test_tree_listing_returns_sorted_dirs_first():
    code, body = _get("/api/code/tree", {"path": "llm_gatewayV9/documents"})
    assert code == 200, body
    entries = body["entries"]
    assert entries, body
    dirs = [i for i, e in enumerate(entries) if e["dir"]]
    files = [i for i, e in enumerate(entries) if not e["dir"]]
    assert not files or not dirs or max(dirs) < min(files), entries
    assert all(e["name"] in {f.name for f in (agent_server._CODE_ROOT / "llm_gatewayV9" / "documents").iterdir()}
               for e in entries)


def test_roots_endpoint_lists_the_two_service_trees():
    r = client.get("/api/code/roots")
    assert r.status_code == 200
    names = [x["name"] for x in r.json()["roots"]]
    assert names == ["S9SharedCode/code", "llm_gatewayV9"], names
    assert all(x["exists"] for x in r.json()["roots"])


def test_flat_index_covers_both_roots_and_is_sorted():
    """Quick open needs the whole tree, not just expanded folders."""
    r = client.get("/api/code/files")
    assert r.status_code == 200
    body = r.json()
    files = body["files"]
    assert files == sorted(files), "index must be sorted for stable ranking"
    assert body["count"] == len(files)
    assert any(f.startswith("S9SharedCode/code/") for f in files), files[:5]
    assert any(f.startswith("llm_gatewayV9/") for f in files), files[:5]
    # Nested files (below a subdirectory) must be present, not just top level.
    assert "llm_gatewayV9/documents/chunker.py" in files


def test_flat_index_excludes_ignored_and_hidden_entries():
    files = client.get("/api/code/files").json()["files"]
    assert not any("/node_modules/" in f for f in files)
    assert not any("/dist/" in f for f in files)
    assert not any(f.endswith("/.env") or f == ".env" for f in files)
    assert not any(part.startswith(".") for f in files for part in f.split("/"))
    assert not any(f.endswith(".db") or f.endswith(".lock") for f in files)


def test_flat_index_returns_paths_only():
    """The index must not become a bulk content read."""
    body = client.get("/api/code/files").json()
    assert set(body) == {"files", "count", "truncated"}


def test_there_is_no_write_or_delete_route():
    """The section is read-only by construction; prove no verb sneaks in."""
    routes = {
        (r.path, m)
        for r in agent_server.app.routes
        for m in getattr(r, "methods", set())
    }
    code_routes = {p for p, _m in routes if p.startswith("/api/code")}
    assert code_routes == {
        "/api/code/roots", "/api/code/tree", "/api/code/files",
        "/api/code/file", "/api/code/check", "/api/code/search",
    }, code_routes
    for p in code_routes:
        verbs = {m for path, m in routes if path == p}
        # `/api/code/check` takes a buffer, never a path, and never writes.
        assert verbs <= {"GET", "HEAD", "POST"}, f"{p} exposes {verbs}"


# ── draft checking (real diagnostics) ───────────────────────────────────────


def _check(text: str, language: str) -> dict:
    r = client.post("/api/code/check", json={"text": text, "language": language})
    assert r.status_code == 200, r.text
    return r.json()


def test_valid_python_reports_no_problems():
    out = _check("def f(x):\n    return x + 1\n", "python")
    assert out["mode"] == "ast"
    assert out["checked"] is True
    assert out["problems"] == []


def test_python_syntax_error_gives_a_real_line_and_message():
    out = _check("def f(x):\n    return x + 1\n\ndef g(:\n    pass\n", "python")
    assert out["checked"] is True
    assert len(out["problems"]) == 1, out
    p = out["problems"][0]
    assert p["line"] == 4, p
    assert p["severity"] == "error"
    assert p["message"]
    assert out["source"]["line"] == 4


def test_python_unclosed_bracket_is_reported_with_a_position():
    out = _check("x = [1, 2\n", "python")
    assert out["problems"], out
    assert out["problems"][0]["line"] >= 1


def test_check_ignores_comments_and_strings_when_parsing():
    """A bracket inside a comment or string must not be counted as code."""
    ok = 'x = ")"  # )\ny = 1\n'
    assert _check(ok, "python")["problems"] == []


def test_brace_check_finds_an_unclosed_delimiter():
    out = _check("function a() {\n  return 1;\n", "typescript")
    assert out["mode"] == "balance"
    assert out["checked"] is True
    assert out["problems"], out
    assert "never closed" in out["problems"][0]["message"]


def test_brace_check_finds_a_stray_closer():
    out = _check("const a = 1;\n}\n", "javascript")
    assert out["problems"], out
    assert out["problems"][0]["line"] == 2


def test_brace_check_does_not_count_delimiters_in_comments_or_strings():
    src = 'const a = "{"; // {\nconst b = 2;\n'
    assert _check(src, "typescript")["problems"] == []


def test_check_declines_honestly_for_a_language_with_no_parser():
    out = _check("anything at all", "markdown")
    assert out["checked"] is False
    assert out["problems"] == []
    assert out["note"]


def test_check_requires_text():
    r = client.post("/api/code/check", json={"language": "python"})
    assert r.status_code == 400
    r = client.post("/api/code/check", json={"text": 5, "language": "python"})
    assert r.status_code == 400


def test_check_refuses_an_oversized_buffer():
    r = client.post("/api/code/check", json={"text": "x" * (513 * 1024),
                                             "language": "python"})
    assert r.status_code == 413, r.text


def test_check_takes_no_path_and_so_cannot_be_used_to_read_one():
    """The endpoint judges a buffer. If it ever accepted a path it would become
    a second, differently-guarded way to read the filesystem."""
    body = {"language": "python", "path": "../../../../etc/passwd"}
    r = client.post("/api/code/check", json=body)
    assert r.status_code == 400, r.text
    assert "text" in r.text.lower()


# ── workspace search ────────────────────────────────────────────────────────


def test_search_finds_a_known_symbol_and_reports_its_position():
    out = client.get("/api/code/search", params={"q": "DOCUMENT_EMBED_BATCH"}).json()
    assert out["count"] > 0, out
    for m in out["matches"]:
        assert agent_server._code_abs(m["path"]) is not None, m
        assert m["line"] >= 1 and m["col"] >= 1, m
        assert m["text"], m
    # The definition site must be among them, not just this test file (which
    # quotes the constant itself).
    paths = {m["path"] for m in out["matches"]}
    assert "llm_gatewayV9/main.py" in paths, sorted(paths)


def test_search_is_case_insensitive_and_bounded():
    out = client.get("/api/code/search", params={"q": "document_embed_batch",
                                                 "limit": 3}).json()
    assert out["count"] <= 3
    one = client.get("/api/code/search", params={"q": "DOCUMENT_EMBED_BATCH"}).json()
    assert out["count"] == min(3, one["count"])


def test_search_never_reaches_outside_the_workspace():
    out = client.get("/api/code/search", params={"q": "root:"}).json()
    for m in out["matches"]:
        assert not m["path"].startswith("/")
        assert ".." not in m["path"].split("/")
        assert agent_server._code_abs(m["path"]) is not None


def test_search_ignores_ignored_trees():
    out = client.get("/api/code/search", params={"q": "node_modules"}).json()
    for m in out["matches"]:
        assert "/node_modules/" not in m["path"], m


def test_search_rejects_an_empty_or_absurd_query():
    assert client.get("/api/code/search", params={"q": ""}).json()["count"] == 0
    assert client.get("/api/code/search").json()["count"] == 0
    assert client.get("/api/code/search", params={"q": "x" * 300}).status_code == 400


def test_search_never_returns_secret_files():
    out = client.get("/api/code/search", params={"q": "OPENAI_API_KEY"}).json()
    for m in out["matches"]:
        assert ".env" not in m["path"], m


def test_symlink_escape_is_refused(tmp_path=None):
    """A symlink inside the tree must not become a way out of it."""
    import os
    import tempfile

    root = agent_server._CODE_ROOT / "S9SharedCode" / "code"
    link = root / "_code_escape_probe"
    target = tempfile.gettempdir()
    try:
        if link.exists() or link.is_symlink():
            return  # a previous run left one behind; nothing to prove
        try:
            os.symlink(target, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            return  # no symlink privilege on this host
        code, body = _get("/api/code/file", {"path": "S9SharedCode/code/_code_escape_probe/x"})
        assert code in (403, 404), f"symlink escape served: {code} {body}"
        assert "text" not in body
    finally:
        try:
            if link.is_symlink():
                os.unlink(link)
        except OSError:
            pass
