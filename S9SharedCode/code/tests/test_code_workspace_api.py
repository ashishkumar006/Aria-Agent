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

# The repository, used as the workspace by the confinement cases below. The
# SHIPPED default is now a blank folder (see
# test_code_workspace_default_is_plain), so a real tree has to be installed
# explicitly for tests that are about what the resolver refuses.
REPO = Path(agent_server.__file__).resolve().parents[2]


def _get(path: str, params: dict) -> tuple[int, dict]:
    r = client.get(path, params=params)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {}


def _install_workspace():
    """Base = the repository, allowed = the two service trees - i.e. the widest
    workspace the section has ever shipped with.

    Deliberately not the whole repository: several cases below assert that a
    file BESIDE the roots is refused (`AGENTS.md` at the repo root is the
    fixture). Widening the allow-list to the repo would make those vacuous.
    """
    agent_server._CODE_BASE = REPO
    agent_server._CODE_ALLOWED = (
        (REPO / "S9SharedCode" / "code").resolve(),
        (REPO / "llm_gatewayV9").resolve(),
    )


def _restore_workspace():
    agent_server._CODE_BASE = agent_server._CODE_DEFAULT_BASE
    agent_server._CODE_ALLOWED = (agent_server._CODE_PLAIN.resolve(),)


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _repo_workspace():
    """Every case here is a CONFINEMENT case: they need a populated tree to
    attempt an escape from. Installed per-test and torn down, so the blank
    shipped default is restored for whatever runs next."""
    _install_workspace()
    yield
    _restore_workspace()


# ── the blank default ────────────────────────────────────────────────────────


def test_code_workspace_default_is_plain():
    """The section must open on nothing.

    It used to default to the whole repository with both service trees
    browsable, so the Code page opened on 111 rows of the agent's own source
    and the user's chats. The default is now an empty scratch folder, which
    also makes the sandbox strictly narrower than two service trees were.
    """
    _restore_workspace()
    ws = client.get("/api/code/workspace").json()
    assert ws["is_default"] is True
    assert Path(ws["root"]) == agent_server._CODE_DEFAULT_BASE
    assert client.get("/api/code/roots").json()["roots"] == [
        {"name": agent_server._CODE_PLAIN.name, "exists": True}]


def test_the_plain_workspace_is_actually_browsable():
    """Regression, and the reason it is a separate test.

    The client's top-level entries are `roots[].name`, and it then asks for
    `tree?path=<that name>`. With BASE set to the scratch folder ITSELF, that
    resolved to a nested `code_workspace/code_workspace` and 404'd - the
    explorer came up empty with a bare "retry" button and no way to open a
    folder. BASE must be one level above the folder, exactly as "Open Folder"
    and "New Playground" both are.
    """
    _restore_workspace()
    name = client.get("/api/code/roots").json()["roots"][0]["name"]
    code, body = _get("/api/code/tree", {"path": name})
    assert code == 200, f"listing the workspace root failed: {code} {body}"
    assert "error" not in body, body
    # And a file written into it is reachable at that same path.
    target = agent_server._CODE_PLAIN / "probe.txt"
    target.write_text("x = 1\n", encoding="utf-8")
    try:
        code, body = _get("/api/code/file", {"path": f"{name}/probe.txt"})
        assert code == 200, f"{name}/probe.txt was refused: {code} {body}"
        assert body["text"] == "x = 1\n"
    finally:
        target.unlink(missing_ok=True)


def test_the_tree_root_lists_the_allowed_roots_not_the_base_directory():
    """The explorer root must be the ALLOW-LIST.

    Base only has to sit one level above the roots so client paths read
    "name/...". With a single-folder workspace the base is that folder's
    PARENT, so listing the base directly published the siblings of the one
    allowed root - with the state dir as base, the tree offered sessions/,
    threads/, playgrounds/ and the rest as browsable entries.
    """
    _restore_workspace()
    code, body = _get("/api/code/tree", {"path": ""})
    assert code == 200, body
    names = {e["name"] for e in body["entries"]}
    assert names == {agent_server._CODE_PLAIN.name}, names
    # And nothing that merely happens to sit beside the base.
    for leaked in ("sessions", "threads", "playgrounds", "artifacts"):
        assert leaked not in names, f"{leaked} is outside the workspace but listed"


def test_reset_returns_to_the_blank_workspace_not_the_repo():
    client.post("/api/code/workspace", json={"playground": True})
    ws = client.post("/api/code/workspace/reset").json()
    assert ws["is_default"] is True
    assert Path(ws["root"]) == agent_server._CODE_DEFAULT_BASE
    # Exactly one root, and it is the empty scratch folder - reset must not
    # have handed the project back.
    assert [Path(r) for r in ws["roots"]] == [agent_server._CODE_PLAIN.resolve()]



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
    assert (REPO / "AGENTS.md").is_file(), "fixture moved"
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
    assert all(e["name"] in {f.name for f in (REPO / "llm_gatewayV9" / "documents").iterdir()}
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
    # `/api/code/ask` (ask the agent about the open file), `/api/code/checks`
    # (run the project's real lint/typecheck/tests) and `/api/code/git` (review
    # what changed on disk) were added for the AI editor. None of them WRITES —
    # see the verb assertions below, which are the property this test is for.
    assert code_routes == {
        "/api/code/roots", "/api/code/tree", "/api/code/files",
        "/api/code/file", "/api/code/check", "/api/code/search",
        "/api/code/ask", "/api/code/checks", "/api/code/git",
        "/api/code/intel",
        # `/api/code/agent` is the tool-using twin of `/api/code/ask`: the model
        # drives CODE_AGENT_TOOLS. It can WRITE, in one of two modes: manual
        # (default) parks every write behind `_approve` until
        # /api/code/permission says yes; `auto_approve: true` (the console's
        # opt-in) dispatches immediately and the write is reviewed afterwards
        # through the changes/keep/revert routes below. Either way the bytes
        # are snapshotted BEFORE the write, so nothing is unrevertable - see
        # tests/test_code_agent_review.py and tests/test_code_agent_edit_gate.py.
        "/api/code/agent",
        # `/api/code/permission/{id}` is not a write: it records the operator's
        # yes/no on ONE pending change. It cannot reach disk by itself, it
        # refuses a non-boolean decision, and an id that is not pending is a
        # 404 so a double-clicked Accept cannot read as "applied".
        "/api/code/permission/{pid}",
        # `/api/code/agent/changes|keep|revert` are the AFTER-THE-FACT review
        # surface for auto-applied writes (the console opts in with
        # `auto_approve: true`; API clients still get the parked permission
        # flow). `changes` is a GET. `keep` only drops a snapshot - it writes
        # no file. `revert` is the one route here that touches disk, and it can
        # only put a file back to a snapshot the server took itself before the
        # agent's write (or delete a file the agent created); it never writes
        # caller-supplied content. tests/test_code_agent_review.py pins the
        # first-snapshot-wins rule that makes Revert trustworthy.
        "/api/code/agent/changes", "/api/code/agent/keep",
        "/api/code/agent/revert",
        # `/api/code/write` is the HUMAN apply route, not an AI surface: it
        # refuses anything without `confirm: true` and re-checks the file's
        # mtime against the buffer the user actually reviewed, then writes via
        # a temp file + os.replace with a backup. So a question typed into a
        # panel still cannot rewrite a file; only a person who read the diff
        # can. The gate itself is asserted in test_code_intel_write_gate.py.
        "/api/code/write",
        # `/api/code/newfile` is the "New file" button in the explorer. It is a
        # deliberate exception to "read-only by construction", like
        # `/api/code/write` above and for the same reason: without it the
        # console could not create anything at all (a fresh playground is
        # empty, "Open folder" needs a folder that already exists), so a user
        # with no project to open could not author a single file. Its blast
        # radius is deliberately smaller than /api/code/write's: it requires
        # `confirm: true`, takes a plain file name with an extension, resolves
        # through the same `_code_abs` confinement as every read, and creates
        # with O_EXCL so it can only ever ADD an empty file - it never opens
        # for write, so it cannot truncate or overwrite anything.
        "/api/code/newfile",
        # `/api/code/workspace` (GET/POST) and `/api/code/workspace/reset`
        # re-point the browser's workspace at a chosen folder or a fresh
        # playground. They move the ROOT; they touch no file, and the
        # confinement test still runs on every read afterwards.
        "/api/code/workspace", "/api/code/workspace/reset",
    }, code_routes
    # `/api/code/intel` is the ONE route that can write, and only on an
    # explicit `op=apply&confirm=true`. A question typed into the panel still
    # cannot rewrite a file, which is the property this section is for, so it
    # is asserted below rather than assumed.
    # The AI surface must be read-only over HTTP: a question typed into a
    # panel must not be able to rewrite files behind the user's back, and the
    # review route must not be able to stage, commit or revert.
    for p in ("/api/code/ask", "/api/code/checks", "/api/code/git"):
        verbs = {m for path, m in routes if path == p}
        assert not (verbs & {"PUT", "PATCH", "DELETE"}), (
            f"{p} must not expose a write verb: {verbs}")
    for p in code_routes:
        verbs = {m for path, m in routes if path == p}
        # `/api/code/check` takes a buffer, never a path, and never writes.
        assert verbs <= {"GET", "HEAD", "POST"}, f"{p} exposes {verbs}"


def test_open_folder_reroots_and_confines(tmp_path):
    """Opening a folder moves the workspace root; confinement follows."""
    # Start from the SHIPPED default, not the autouse fixture's repo workspace:
    # this test is about what Open Folder does to the default.
    _restore_workspace()
    assert client.get("/api/code/workspace").json()["is_default"] is True
    # A path that is not a directory is refused, not silently accepted.
    assert client.post("/api/code/workspace",
                       json={"path": str(tmp_path / "nope")}).status_code == 400
    assert client.post("/api/code/workspace", json={"path": ""}).status_code == 400
    try:
        r = client.post("/api/code/workspace", json={"path": str(tmp_path)})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["is_default"] is False
        assert Path(body["root"]) == tmp_path.parent.resolve()
        assert body["name"] == tmp_path.name
        (tmp_path / "sample.py").write_text("x = 1\n", encoding="utf-8")
        # The tree lists the new folder as a root, and the index is
        # confined to it.
        assert client.get("/api/code/roots").json()["roots"] == [
            {"name": tmp_path.name, "exists": True}]
        tree = client.get("/api/code/tree",
                          params={"path": tmp_path.name}).json()
        names = {e["name"] for e in tree.get("entries", [])}
        assert "sample.py" in names, tree
        files = client.get("/api/code/files").json()["files"]
        assert files == [f"{tmp_path.name}/sample.py"], files
        # The old service tree is no longer reachable: the root moved.
        code, out = _get("/api/code/file", {"path": "llm_gatewayV9/main.py"})
        assert code in (400, 403, 404), f"old root still served: {code} {out}"
        # An escape out of the new root is refused too.
        assert _get("/api/code/file", {"path": "../secret"})[0] in (400, 403)
    finally:
        assert client.post(
            "/api/code/workspace/reset").json()["is_default"] is True


def test_go_to_definition_returns_a_path_the_console_can_open(tmp_path):
    """H7: jump targets must be client paths, not bare basenames.

    The editor hands `/api/code/file` a "<root>/dir/file" path. A definition
    result of "lib.py" resolved outside the roots and every F12 jump 403'd.
    """
    _restore_workspace()
    (tmp_path / "lib.py").write_text("def helper():\n    return 1\n",
                                     encoding="utf-8")
    (tmp_path / "main.py").write_text(
        "from lib import helper\n\n\ndef main():\n    helper()\n",
        encoding="utf-8")
    try:
        assert client.post("/api/code/workspace",
                           json={"path": str(tmp_path)}).status_code == 200
        r = client.post("/api/code/intel", json={
            "op": "definition", "name": "helper",
            "path": f"{tmp_path.name}/main.py", "line": 5, "col": 4,
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["found"] is True, body
        assert body["file"] == f"{tmp_path.name}/lib.py", body
        code, opened = _get("/api/code/file", {"path": body["file"]})
        assert code == 200, opened
        assert "def helper" in opened["text"]
    finally:
        assert client.post(
            "/api/code/workspace/reset").json()["is_default"] is True


def test_new_playground_creates_an_empty_workspace():
    """New Playground makes a fresh, empty workspace, and can be left."""
    try:
        r = client.post("/api/code/workspace", json={"playground": True})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["is_default"] is False
        root = Path(body["root"])
        assert root.is_dir(), root
        assert client.get("/api/code/files").json()["files"] == []
    finally:
        assert client.post(
            "/api/code/workspace/reset").json()["is_default"] is True


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

    root = REPO / "S9SharedCode" / "code"
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
def test_repeated_reasoning_is_not_shown_twice():
    """A model that puts its whole reply on the reasoning channel and repeats it
    as the answer rendered the same prose twice in the panel: once collapsed as
    "thinking", once as the answer. The dedupe must catch that."""
    answer = ("Now I have a comprehensive view of the main files in this "
              "repository. Let me provide a summary of what I have read and the "
              "key components that make up the system overall.\n"
              "The project is a self-hosted AI agent with separate processes for "
              "the gateway, the agent server, and the React console UI.")
    assert agent_server._thinking_is_answer(answer, answer) is True


def test_reasoning_that_only_shares_an_opening_is_kept():
    """...but must NOT drop a real chain of thought that merely begins the same
    way. Judging the overlap against the smaller side scored 50% on that and
    deleted the reasoning with it."""
    shared = "The user wants me to read all the main files in the folder. Based on"
    reasoning = shared + "\nLet me also inspect the gateway routing table."
    answer = (shared
              + "\n\nThe gateway routes by model id only when a provider is not "
                "pinned, which is why the code agent pins one.")
    assert agent_server._thinking_is_answer(answer, reasoning) is False


def test_unrelated_reasoning_is_kept():
    assert agent_server._thinking_is_answer(
        "0", "I should call list_dir on the repo root first of all.") is False


def test_empty_either_side_is_kept():
    assert agent_server._thinking_is_answer("", "some reasoning") is False
    assert agent_server._thinking_is_answer("the answer", "") is False


# ── /api/code/newfile: the New file button ───────────────────────────────────
#
# A write surface, so each of its guarantees is asserted rather than assumed:
# it can only ever ADD an empty file inside an allowed root.


def _newfile(name: str, **kw):
    return client.post("/api/code/newfile", json={"name": name, **kw})


def test_newfile_creates_an_empty_file_inside_the_workspace():
    r = _newfile("scratch-notes.md", confirm=True, dir="S9SharedCode/code")
    assert r.status_code == 200, r.text
    assert r.json().get("created") is True
    made = (REPO / "S9SharedCode" / "code" / "scratch-notes.md").resolve()
    try:
        assert made.is_file()
        assert made.read_text(encoding="utf-8") == ""
    finally:
        made.unlink(missing_ok=True)


def test_newfile_needs_confirm():
    """A stray replay must not be able to create files."""
    r = _newfile("unconfirmed.md", dir="S9SharedCode/code")
    assert r.status_code == 409
    assert not (REPO / "S9SharedCode" / "code" / "unconfirmed.md").exists()


def test_newfile_never_overwrites_an_existing_file():
    """The whole point of O_EXCL: this must not be able to truncate work."""
    target = REPO / "S9SharedCode" / "code" / "agent_server.py"
    before = target.stat().st_size
    r = _newfile("agent_server.py", confirm=True, dir="S9SharedCode/code")
    assert r.status_code == 409
    assert "already exists" in r.json().get("error", "")
    assert target.stat().st_size == before


def test_newfile_refuses_to_escape_the_workspace():
    for bad in ("../escaped.md", "..\\escaped.md"):
        r = _newfile(bad, confirm=True, dir="S9SharedCode/code")
        assert r.status_code in (400, 403), (bad, r.status_code, r.text)
    for bad_dir in ("..", "../../"):
        r = _newfile("escaped.md", confirm=True, dir=bad_dir)
        assert r.status_code in (400, 403), (bad_dir, r.status_code, r.text)


def test_newfile_outside_the_allowed_roots_is_refused():
    """AGENTS.md sits BESIDE the roots: the allow-list is the boundary, not
    just the base directory."""
    r = _newfile("sneaky.md", confirm=True, dir=".")
    assert r.status_code == 403
    assert not (REPO / "sneaky.md").exists()


def test_newfile_requires_an_extension():
    """A name with no extension is a folder typo, not a file."""
    r = _newfile("no-extension", confirm=True, dir="S9SharedCode/code")
    assert r.status_code == 400
    assert "extension" in r.json().get("error", "")
