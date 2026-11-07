"""Per-session code workspaces (Studio slice 1).

The defect being removed is that the code workspace lives in two module globals.
`POST /api/code/workspace` rewrites them, so:

  - two console tabs re-root each other — opening a folder in one changes what
    the other's editor saves into;
  - doing it while an agent is running re-roots that run's NEXT tool call into a
    different project than the one the user asked it to change.

Both are silent: nothing errors, the file just lands somewhere else.

The two rules these tests exist to keep honest are the ones that are easy to
get wrong while making the feature work. A session whose declared folder has
been deleted or moved must end up with NO workspace — not the process default,
which is a different project and would then be read and written without
anyone's consent. And a path is confined once, by `agent_server._code_abs` for
the human's routes and by `mcp_server._safe` for the agent's tools: this module
decides WHICH folders are allowed and never whether a path may be touched.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))

import agent_server as A        # noqa: E402
import studio_workspace as sw   # noqa: E402


@pytest.fixture(autouse=True)
def _unbind():
    """The bound session is a ContextVar; leave no session for the next test.

    Without this, a test that binds `sid-a` and a later test that expects the
    legacy globals would both pass in isolation and fail in the file — which is
    precisely the shape of bug this slice is about.
    """
    sw.bind("")
    yield
    sw.bind("")


@pytest.fixture()
def two_projects(tmp_path):
    a = tmp_path / "proj-a"
    a.mkdir()
    (a / "main.py").write_text("print('a')\n", encoding="utf-8")
    b = tmp_path / "proj-b"
    b.mkdir()
    (b / "app.ts").write_text("export const x = 1\n", encoding="utf-8")
    return a, b


# ── the store ───────────────────────────────────────────────────────────────

def test_a_session_holds_its_own_roots_and_a_stranger_holds_none(two_projects):
    a, b = two_projects
    assert sw.add_root("s1", str(a))[0] is not None
    sw.add_root("s1", str(b))
    assert sw.add_root("s2", str(a))[0] is not None
    assert len(sw.load("s1")["roots"]) == 2
    assert len(sw.load("s2")["roots"]) == 1, "sessions must not share a root list"


def test_roots_survive_a_reload_of_the_module(two_projects, monkeypatch):
    """The store is a file for a reason: the studio reboots the agent, the user's
    folders must not have to be re-picked."""
    a, _b = two_projects
    sw.add_root("s-persist", str(a))
    import importlib
    again = importlib.reload(sw)
    assert again.live_roots("s-persist") == (a.resolve(),)


def test_adding_the_same_folder_twice_is_reported_not_duplicated(two_projects):
    a, _b = two_projects
    sw.add_root("s-dup", str(a))
    state, why = sw.add_root("s-dup", str(a))
    assert "already a root" in why
    assert len(state["roots"]) == 1


def test_the_root_cap_is_enforced(tmp_path):
    folders = []
    for i in range(sw.MAX_ROOTS + 3):
        d = tmp_path / f"r{i}"
        d.mkdir()
        folders.append(d)
    for d in folders[:sw.MAX_ROOTS]:
        state, why = sw.add_root("s-cap", str(d))
        assert state is not None, why
    state, why = sw.add_root("s-cap", str(folders[-1]))
    assert f"at most {sw.MAX_ROOTS}" in why, why
    assert len(state["roots"]) == sw.MAX_ROOTS


def test_a_file_and_a_missing_path_are_refused_with_a_reason(tmp_path):
    f = tmp_path / "not-a-folder.py"
    f.write_text("x\n", encoding="utf-8")
    _state, why = sw.add_root("s-bad", str(f))
    assert "not a folder" in why, why
    _state, why = sw.add_root("s-bad", str(tmp_path / "ghost"))
    assert "does not exist" in why, why


@pytest.mark.parametrize("bad", ["../evil", "has space", "", "x" * 200,
                                 "a/b", "a\\b", ".hidden"])
def test_a_session_id_that_could_escape_its_own_file_is_rejected(bad):
    assert not sw.valid_sid(bad), bad
    assert sw.load(bad) is None
    # …and binding one is a no-op rather than an error: the honest reading of a
    # malformed `?sid=` is "no session".
    assert sw.bind(bad) == ""


# ── the rule that a workspace is not a fallback ─────────────────────────────

def test_the_state_dir_and_its_ancestors_can_never_be_roots():
    """The shipped playground LIVES inside the state dir and is fine; the state
    dir itself, or anything holding it, would publish sessions/, threads/ and
    agent.token to the editor. Ancestry, not location.

    Written after the first version of this rule came out backwards — it
    rejected descendants (refusing the app's own default workspace) and allowed
    ancestors. `state/sessions` is the case that makes the direction matter.
    """
    state = sw.state_root().resolve()
    assert sw.forbidden_root(state), "the state dir must not be a root"
    assert sw.forbidden_root(state.parent), "an ancestor of it must not either"
    sessions = state / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    assert sw.forbidden_root(sessions), (
        "a subtree of the state dir holds every other conversation's "
        "transcripts and must not be declarable")
    leaf = state / "code-studio" / "playgrounds" / "s-x"
    leaf.mkdir(parents=True, exist_ok=True)
    assert sw.forbidden_root(leaf.resolve()) == "", (
        "a folder inside the state dir is the default workspace and must stay "
        "declarable")
    default = state / "code_workspace"
    default.mkdir(parents=True, exist_ok=True)
    assert sw.forbidden_root(default.resolve()) == "", default


def test_a_drive_or_volume_root_is_refused(tmp_path):
    p = Path(os.path.abspath(os.sep))
    assert sw.forbidden_root(p), p


def test_a_session_whose_folder_vanished_gets_no_workspace_not_the_default(
        two_projects, tmp_path):
    """Fail closed. Falling back to `_CODE_*` here would read and write the
    shipped playground with nobody having asked for it — a different project,
    reached because a folder was renamed on disk."""
    a, _b = two_projects
    sw.add_root("s-gone", str(a))
    assert sw.live_roots("s-gone")
    import shutil
    shutil.rmtree(a)
    assert sw.live_roots("s-gone") == ()
    sw.bind("s-gone")
    base, allowed = A._code_workspace()
    assert allowed == (), "an unreadable workspace must be EMPTY, not inherited"
    assert A._code_abs("anything.py") is None


# ── confinement, through agent_server's single resolver ────────────────────

def test_sid_scopes_which_folders_the_human_routes_can_reach(two_projects):
    a, b = two_projects
    sw.add_root("s-one", str(a))
    sw.bind("s-one")
    assert A._code_abs("proj-a/main.py") == (a / "main.py").resolve()
    assert A._code_abs("proj-b/app.ts") is None, (
        "a second project must be unreachable until it is declared")
    sw.add_root("s-one", str(b))
    assert A._code_abs("proj-b/app.ts") == (b / "app.ts").resolve()
    assert A._code_abs("proj-a/main.py") == (a / "main.py").resolve()


def test_no_sid_means_the_legacy_globals_unchanged():
    """The back-compat contract: every existing caller, the CLI and the whole
    Code page must behave exactly as before when no session is bound."""
    sw.bind("")
    assert A._code_workspace() == (A._CODE_BASE, A._CODE_ALLOWED)


def test_traversal_and_absolute_paths_stay_refused_under_a_session(two_projects):
    a, _b = two_projects
    sw.add_root("s-trav", str(a))
    sw.bind("s-trav")
    for bad in ("../etc/passwd", "proj-a/../../Windows",
                str(a / "main.py"), "C:\\Windows\\system32",
                "proj-a\\main.py", "proj-a/\x00main.py"):
        assert A._code_abs(bad) is None, bad


def test_the_agent_sandbox_and_the_extra_roots_split_by_name(two_projects):
    """`ARIA_SANDBOX` is one folder (where a bare relative path lands);
    `ARIA_ROOTS` is the rest of the allow-list. Getting this wrong confines the
    human to two projects and the agent to the first."""
    a, b = two_projects
    sw.add_root("s-env", str(a))
    sw.add_root("s-env", str(b))
    sw.bind("s-env")
    assert A._code_agent_root().resolve() == a.resolve()
    assert A._code_extra_roots() == [str(b.resolve())]


def test_client_and_tool_paths_round_trip_for_a_second_root(two_projects):
    a, b = two_projects
    sw.add_root("s-rt", str(a))
    sw.add_root("s-rt", str(b))
    sw.bind("s-rt")
    assert A._code_agent_rel("proj-b/src/app.ts") == "src/app.ts"
    assert A._code_agent_rel("proj-a/main.py") == "main.py"
    assert A._code_client_path("proj-b/app.ts") == "proj-b/app.ts"
    # A tool path that names nothing is relative to the sandbox, and the UI has
    # to see it under the root it will actually resolve against.
    assert A._code_client_path("main.py") == "proj-a/main.py"


# ── the MCP child's own confinement ─────────────────────────────────────────

def test_the_child_accepts_a_declared_root_and_rejects_everything_else(
        two_projects, monkeypatch):
    import mcp_server as M
    a, b = two_projects
    sibling = a.parent / "proj-c"
    sibling.mkdir()
    (sibling / "secret.txt").write_text("not yours\n", encoding="utf-8")
    monkeypatch.setattr(M, "SANDBOX", a)
    monkeypatch.setenv("ARIA_ROOTS", str(b.resolve()))
    assert M._safe("main.py") == (a / "main.py").resolve()
    # The second project is reachable by name, which is how the explorer labels
    # a file it is holding from there.
    assert M._safe("proj-b/app.ts") == (b / "app.ts").resolve()
    with pytest.raises(ValueError):
        M._safe("../../outside.txt")
    # An undeclared sibling of the two roots is not reachable.
    with pytest.raises(ValueError):
        M._safe("../proj-c/secret.txt")
    # A path that does not exist yet but lands INSIDE a declared root stays
    # legal — that is how the agent creates a file in a new subfolder. This
    # asserted the opposite until the test was corrected: containment, not
    # existence, is the rule.
    assert M._safe("newdir/newfile.py") == (a / "newdir/newfile.py").resolve()
    # And the same escape written as an ABSOLUTE path, which is the shape the
    # first multi-root rewrite silently accepted by stripping the leading
    # separator before joining.
    with pytest.raises(ValueError):
        M._safe(str(sibling / "secret.txt"))
    # A rooted relative form must not be rewritten into a sandbox-relative one
    # either (`/../..` is not `<sandbox>/../..`).
    with pytest.raises(ValueError):
        M._safe("/../outside.txt")


def test_the_child_unchanged_when_no_second_root_is_declared(
        two_projects, monkeypatch):
    """Every non-studio caller today sets no ARIA_ROOTS, and the re-anchoring
    branch must be inert for them — including the trap where a workspace holds a
    directory named exactly like itself, which must still resolve inside itself.
    """
    import mcp_server as M
    a, _b = two_projects
    nested = a / a.name
    nested.mkdir()
    (nested / "inner.py").write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(M, "SANDBOX", a)
    monkeypatch.delenv("ARIA_ROOTS", raising=False)
    assert M._safe(f"{a.name}/inner.py") == (nested / "inner.py").resolve()
    assert len(M._roots()) == 1


def test_a_root_that_no_longer_exists_is_not_an_allow_list_entry(tmp_path,
                                                                 monkeypatch):
    """`ARIA_ROOTS` naming a deleted folder must not become a wildcard: the
    containment test runs against directories that exist now."""
    import mcp_server as M
    real = tmp_path / "real"
    real.mkdir()
    ghost = tmp_path / "ghost"
    monkeypatch.setattr(M, "SANDBOX", real)
    monkeypatch.setenv("ARIA_ROOTS", str(ghost))
    assert M._roots() == (real.resolve(),)


# ── the routes ──────────────────────────────────────────────────────────────

@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    return TestClient(A.app)


def test_the_roots_routes_validate_list_and_remove(client, two_projects):
    a, b = two_projects
    r = client.post("/api/studio/roots?sid=s-http", json={"path": str(a)})
    assert r.status_code == 200, r.text
    assert r.json()["roots"] == [{"path": str(a.resolve()), "name": "proj-a"}]
    r = client.post("/api/studio/roots?sid=s-http", json={"path": "Z:\\nope"})
    assert r.status_code == 400
    assert "does not exist" in r.json()["error"]
    r = client.post("/api/studio/roots?sid=s-http", json={"path": str(b)})
    assert r.json()["count"] == 2
    assert client.get("/api/studio/roots?sid=s-http").json()["scoped"] is True
    r = client.post("/api/studio/roots/remove?sid=s-http", json={"path": str(a)})
    assert [x["name"] for x in r.json()["roots"]] == ["proj-b"]
    r = client.post("/api/studio/workspace/reset?sid=s-http")
    body = r.json()
    assert body["roots"] == [], body
    assert body["scoped"] is True, (
        "a reset session is still a session — it must not revert to the "
        "process default workspace")
    assert body["is_default"] is False, body
    # And with no roots, nothing at all is reachable: the empty allow-list has
    # to fail closed, not inherit.
    sw.bind("s-http")
    assert A._code_workspace()[1] == ()
    assert A._code_abs("anything.py") is None


def test_opening_a_folder_for_a_session_does_not_re_root_the_process(
        client, two_projects):
    """THE headline bug. `/api/code/workspace` used to rewrite two globals; with
    a session bound it must not, or a second tab's editor starts saving into
    this session's project."""
    a, _b = two_projects
    before_base, before_allowed = A._CODE_BASE, A._CODE_ALLOWED
    r = client.post("/api/code/workspace?sid=s-share", json={"path": str(a)})
    assert r.status_code == 200, r.text
    assert (A._CODE_BASE, A._CODE_ALLOWED) == (before_base, before_allowed), (
        "a session-scoped open must not touch the process workspace")
    assert r.json()["scoped"] is True
    assert [x["name"] for x in r.json()["roots"]] == ["proj-a"]
    # …and the legacy caller with no sid still gets the old global behaviour.
    r2 = client.post("/api/code/workspace", json={"path": str(a)})
    assert r2.status_code == 200
    assert A._CODE_ALLOWED[0] == a.resolve(), (
        "no sid must still mean the legacy global workspace, or /code breaks")
    A._CODE_BASE, A._CODE_ALLOWED = before_base, before_allowed


def test_the_two_sessions_do_not_see_each_others_folders(client, two_projects):
    a, b = two_projects
    client.post("/api/studio/roots?sid=left", json={"path": str(a)})
    client.post("/api/studio/roots?sid=right", json={"path": str(b)})
    assert [x["name"] for x in
           client.get("/api/studio/roots?sid=left").json()["roots"]] == ["proj-a"]
    assert [x["name"] for x in
           client.get("/api/studio/roots?sid=right").json()["roots"]] == ["proj-b"]


def test_a_tree_listing_for_a_session_shows_only_that_sessions_roots(
        client, two_projects):
    """The explorer's first level is the session's allow-list, not the base
    directory's contents — which is the difference between showing the user
    their two projects and showing them `sessions/` and `threads/`.
    """
    a, b = two_projects
    client.post("/api/studio/roots?sid=list-a", json={"path": str(a)})
    client.post("/api/studio/roots?sid=list-b", json={"path": str(b)})
    top_a = [e["name"] for e in
             client.get("/api/code/tree?sid=list-a").json().get("entries") or []]
    assert top_a == ["proj-a"], top_a
    top_b = [e["name"] for e in
             client.get("/api/code/tree?sid=list-b").json().get("entries") or []]
    assert top_b == ["proj-b"], top_b
    # Descending works, and the other session's folder is not merely hidden —
    # it is unreachable.
    inside = [e["name"] for e in
              client.get("/api/code/tree?path=proj-a&sid=list-a").json()
              .get("entries") or []]
    assert inside == ["main.py"], inside
    escape = client.get("/api/code/tree?path=proj-b&sid=list-a").json()
    assert "error" in escape, escape
    assert not any(n.startswith("proj-b") for n in top_a)


def test_two_roots_appear_side_by_side_in_one_session(client, two_projects):
    a, b = two_projects
    client.post("/api/studio/roots?sid=both", json={"path": str(a)})
    client.post("/api/studio/roots?sid=both", json={"path": str(b)})
    top = client.get("/api/code/tree?sid=both").json().get("entries") or []
    assert sorted(e["name"] for e in top) == ["proj-a", "proj-b"], top
    assert [e["name"] for e in
            client.get("/api/code/tree?path=proj-b&sid=both").json()["entries"]
            ] == ["app.ts"]


def test_a_playground_belongs_to_one_session(client):
    r1 = client.post("/api/studio/playground?sid=pg-1")
    r2 = client.post("/api/studio/playground?sid=pg-2")
    assert r1.status_code == 200, r1.text
    p1 = Path(r1.json()["created"])
    p2 = Path(r2.json()["created"])
    assert p1.exists() and p1.is_dir()
    assert p1 != p2, "two sessions sharing one scratch folder is the bug"
    assert r1.json()["roots"][0]["path"] == str(p1.resolve())
    # A playground with no session is refused rather than silently global.
    assert client.post("/api/studio/playground").status_code == 400


def test_the_middleware_binds_the_sid_for_a_route_that_never_asks(client,
                                                                  two_projects,
                                                                  monkeypatch):
    """The binding is middleware because a route that forgets to ask for its
    session is indistinguishable from a route with no session — and the symptom
    is a file written into the wrong project, not an error.

    `/api/code/tree` takes no sid parameter and calls nothing to bind; it must
    still resolve the session's workspace. Observed by recording what the deep
    resolver saw while the request was in flight.
    """
    _a, b = two_projects
    client.post("/api/studio/roots?sid=mw", json={"path": str(b)})
    seen: dict = {}
    real = A._code_listing

    def spy(rel):
        seen["sid"] = sw.bound_sid()
        seen["allowed"] = [str(p) for p in A._code_workspace()[1]]
        return real(rel)

    monkeypatch.setattr(A, "_code_listing", spy)
    r = client.get("/api/code/tree?path=&sid=mw")
    assert r.status_code == 200
    assert seen["sid"] == "mw", seen
    assert seen["allowed"] == [str(b.resolve())], seen


def test_an_agent_run_naming_its_session_in_the_body_works_in_that_session(
        two_projects, client, monkeypatch):
    """The one case the query-only middleware could not see, and the reason it
    is the worst kind of bug.

    The studio's run posts `sid` inside its JSON payload — the payload already
    carries the question, the buffer and the open tabs, so that is where an id
    naturally goes. `_studio_session_scope` binds `?sid=` and nothing else, so
    the run bound NO session and read the PROCESS workspace: the agent's
    `edit_file` reported success against the shipped default folder while the
    file the user opened stayed exactly as it was. The transcript said ✓ and
    the disk said otherwise, which is the definition of a lie.

    Observed live 2026-11-06 while verifying slice 4 in the browser.
    """
    import mcp_runner

    a, _b = two_projects
    sw.add_root("s-run", str(a))
    seen: dict = {}

    async def fake_run(**kwargs):
        seen["sid"] = sw.bound_sid()
        seen["root"] = str(A._code_agent_root())
        return {"text": "edited", "tool_calls": [], "provider": "test",
                "input_tokens": 0, "output_tokens": 0}

    monkeypatch.setattr(mcp_runner, "run_with_tools", fake_run)
    with client.stream("POST", "/api/code/agent", json={
            "question": "edit main.py", "path": "", "text": "",
            "max_hops": 3, "sid": "s-run"}) as r:
        assert r.status_code == 200
        "".join(r.iter_text())
    assert seen.get("sid") == "s-run", (
        "the run bound no session, so it worked in the process default "
        f"workspace instead: {seen}")
    assert seen.get("root") == str(a.resolve()), seen


# ── one vocabulary, two halves ───────────────────────────────────────────────
# A client path that NAMES a root is a root selector. The server's resolver and
# the MCP child's `_safe` must apply that rule identically, or the file the
# review snapshot is taken on is not the file the tool wrote — which is not an
# error, it is a Revert that restores the wrong thing. Observed live 2026-11-06:
# `s4demo/app.py` resolved to `studio-demo/app.py` server-side while the child
# wrote the real file, the snapshot came back `existed: false`, and the pending
# review was silently dropped because the wrong file matched its own empty
# snapshot.

def test_a_path_naming_a_second_root_resolves_there_even_when_the_first_has_that_dir(
        two_projects):
    a, b = two_projects
    # A directory inside proj-a with the SAME NAME as proj-b. Existence is not
    # meaning, and the old rule let it win.
    trap = a / "proj-b"
    trap.mkdir()
    (trap / "app.ts").write_text("export const impostor = 1\n", encoding="utf-8")
    (b / "app.ts").write_text("export const real = 1\n", encoding="utf-8")
    sw.add_root("s-vocab", str(a))
    sw.add_root("s-vocab", str(b))
    sw.bind("s-vocab")
    got = A._code_agent_abs("proj-b/app.ts")
    assert got == (b / "app.ts").resolve(), (
        f"resolved to {got}: a name collision decided which project the path "
        "meant, so the snapshot and the write are for different files")
    sw.bind("")


def test_the_child_applies_the_same_selector_rule_as_the_server(
        two_projects, monkeypatch):
    import mcp_server as M
    a, b = two_projects
    trap = a / "proj-b"
    trap.mkdir()
    (trap / "app.ts").write_text("impostor\n", encoding="utf-8")
    monkeypatch.setattr(M, "SANDBOX", a)
    monkeypatch.setenv("ARIA_ROOTS", str(b.resolve()))
    got = M._safe("proj-b/app.ts")
    assert got == (b / "app.ts").resolve(), (
        f"the child chose {got}; the server chose the other project and the "
        "two halves of the product disagree about one path")


def test_a_review_snapshot_for_a_second_root_file_reverts_that_file(
        two_projects, client):
    """The whole point of the vocabulary rule, checked through the review path
    that was silently broken."""
    a, b = two_projects
    target = b / "app.ts"
    original = "export const x = 1\n"
    target.write_bytes(original.encode("utf-8"))
    sw.add_root("s-rev", str(a))
    sw.add_root("s-rev", str(b))
    sw.bind("s-rev")
    import difflib
    import studio_review as R
    path = A._code_client_path("proj-b/app.ts")
    assert path == "proj-b/app.ts", path

    tok = A._code_review_take(path, "edit_file", "s", "", turn="t-rev")
    changed = "export const x = 2\nexport const y = 3\n"
    target.write_bytes(changed.encode("utf-8"))
    d = "\n".join(difflib.unified_diff(
        original.splitlines(), changed.splitlines(),
        fromfile="a/proj-b/app.ts", tofile="b/proj-b/app.ts", lineterm="", n=3))
    A._code_review_take(path, "edit_file", "s", d, turn="t-rev")

    entry = R.get("s-rev", path)
    assert entry is not None, (
        "the pending review vanished: the snapshot was compared against a "
        "different file than the one the tool wrote")
    assert entry["baseline"] == original.encode("utf-8")
    cp = R.checkpoint_detail("s-rev", entry["checkpoint"])
    assert cp["files"][0]["abs"] == str(target.resolve()).lower() or \
        cp["files"][0]["abs"].replace("\\", "/").lower() == \
        str(target.resolve()).replace("\\", "/").lower(), cp["files"]

    hs = R.hunks(entry["diff"])
    got = R.apply_hunk("s-rev", path, A._code_review_abs, hunk_id=hs[0]["id"],
                       action="revert")
    assert got["ok"], got
    assert target.read_bytes().decode("utf-8") == original
    sw.bind("")
