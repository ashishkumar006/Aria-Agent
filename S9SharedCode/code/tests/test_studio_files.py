"""Slice 8 — file operations, project search with replace, and the git verbs.

`studio_files.py` is a planner: it decides whether something may happen and
what it would do. These tests are the reason it is not an executor — a
function that renames files cannot be tested for "did it refuse the rename
that would have eaten a repository" without a repository, and the important
cases are the refusals.

The route half then checks the three promises that only exist at the boundary:

- a delete, a move and a replace all leave a checkpoint that puts the bytes
  back;
- a project-wide replace applies ONLY what the preview showed, and if a file
  moved in between the batch is refused rather than half-done;
- git verbs that move HEAD answer with the diff and a 428 until the caller
  says `confirm`, and `push`/`reset`/`clean` are refused at the plan, not at
  the subprocess.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))

import agent_server as A          # noqa: E402
import studio_files as F          # noqa: E402
import studio_review as _review   # noqa: E402
import studio_workspace as sw     # noqa: E402
from agent_server import app      # noqa: E402

SID = "s-files"


class Display:
    """The test's own vocabulary: `<root>/inner`, absolute both ways.

    Mirrors production `_code_agent_abs`: a leading segment that names one of
    the session's folders selects that folder, and anything else resolves
    against the BASE (the common parent), which is precisely the case that is
    outside every root and must therefore be refused.
    """

    def __init__(self, roots, base):
        self.roots = [Path(r) for r in roots]
        self.base = Path(base)

    def resolve(self, client):
        rel = str(client or "").strip().replace("\\", "/").lstrip("/")
        if not rel:
            return None
        head, sep, rest = rel.partition("/")
        for r in self.roots:
            if r.name == head and sep:
                return r / rest
        return self.base / rel

    def show(self, abs_path):
        p = Path(abs_path)
        for r in self.roots:
            try:
                inner = p.relative_to(r).as_posix()
            except ValueError:
                continue
            return f"{r.name}/{inner}" if inner != "." else r.name
        return str(p)


@pytest.fixture(autouse=True)
def _unbind():
    sw.bind("")
    yield
    sw.bind("")


@pytest.fixture()
def trees(tmp_path):
    a = tmp_path / "proj"
    b = tmp_path / "other"
    (a / "src").mkdir(parents=True)
    (b / "lib").mkdir(parents=True)
    (a / "src" / "app.py").write_text(
        "def greet(name):\n    return 'hi ' + name\n\nOLD = 1\n",
        encoding="utf-8")
    (a / "src" / "util.py").write_text("OLD = 2\n", encoding="utf-8")
    (b / "lib" / "x.ts").write_text("export const OLD = 3;\n", encoding="utf-8")
    (a / "node_modules").mkdir()
    (a / "node_modules" / "hidden.py").write_text("OLD = 999\n", encoding="utf-8")
    (a / "blob.png").write_bytes(b"\x89PNG\x00\x01OLD")
    outside = tmp_path / "outside.py"
    outside.write_text("OLD = outside\n", encoding="utf-8")
    for root in (a, b):
        state, why = sw.add_root(SID, str(root))
        assert state is not None, why
    display = Display([a, b], tmp_path)
    yield {"a": a, "b": b, "roots": [a, b], "display": display,
           "outside": outside, "tmp": tmp_path}
    sw.clear(SID)


# ── the regex that must not run ───────────────────────────────────────
def test_a_nested_quantifier_is_refused_with_a_reason():
    why = F.dangerous_pattern(r"(a+)+b")
    assert why and "quantifier" in why


def test_an_invalid_expression_is_refused_not_raised():
    assert "not a valid expression" in F.dangerous_pattern(r"(unclosed")


def test_an_empty_and_an_overlong_pattern_are_refused():
    assert F.dangerous_pattern("   ")
    assert "characters" in F.dangerous_pattern("a" * (F.MAX_PATTERN_CHARS + 1))


def test_ordinary_expressions_are_allowed():
    for ok in (r"def\s+\w+", r"OLD|NEW", r"^\s*import .*", r"(a|b)+"):
        assert F.dangerous_pattern(ok) == "", ok


def test_a_group_at_the_end_of_a_pattern_is_not_a_nested_quantifier():
    """`nxt in "*+"` is True for the empty string, so the naive check flags
    every pattern that ends in a capture group — which is most of them. This
    is the mutation test for that off-by-one."""
    assert F.dangerous_pattern(r"OLD\s*=\s*(\d+)") == ""
    assert F.dangerous_pattern(r"(?:foo|bar)$") == ""


# ── plan_op ───────────────────────────────────────────────────────────
def test_a_rename_stays_in_its_folder(trees):
    d, roots = trees["display"], trees["roots"]
    plan = F.plan_op(roots, d, "rename", "proj/src/app.py", "proj/src/main.py")
    assert plan["ok"] and plan["kind"] == "rename"
    assert Path(plan["abs_to"]).name == "main.py"


def test_a_rename_cannot_also_move_the_file(trees):
    """Renaming into a different folder is a move wearing a label, and the
    two have different undo stories."""
    plan = F.plan_op(trees["roots"], trees["display"], "rename",
                    "proj/src/app.py", "proj/main.py")
    assert not plan["ok"] and "move" in plan["reason"]


def test_a_move_that_would_overwrite_is_refused(trees):
    plan = F.plan_op(trees["roots"], trees["display"], "move",
                     "proj/src/app.py", "proj/src/util.py")
    assert not plan["ok"] and "already" in plan["reason"]


def test_a_folder_cannot_be_moved_into_itself(trees):
    plan = F.plan_op(trees["roots"], trees["display"], "move",
                     "proj/src", "proj/src/inner")
    assert not plan["ok"] and "itself" in plan["reason"]


def test_no_operation_reaches_outside_the_session(trees):
    d = trees["display"]
    for op, frm in (("delete", "outside.py"), ("rename", "outside.py")):
        plan = F.plan_op(trees["roots"], d, op, frm,
                         "proj/x.py" if op == "rename" else "")
        assert not plan["ok"], (op, plan)


def test_the_session_folder_itself_is_not_a_file_operation(trees):
    plan = F.plan_op(trees["roots"], trees["display"], "delete", "proj")
    assert not plan["ok"] and "remove it from" in plan["reason"]


def test_repository_internals_are_not_a_file_operation(trees):
    (trees["a"] / ".git").mkdir(exist_ok=True)
    (trees["a"] / ".git" / "config").write_text("x\n", encoding="utf-8")
    plan = F.plan_op(trees["roots"], trees["display"], "delete", "proj/.git/config")
    assert not plan["ok"] and "internals" in plan["reason"]


def test_a_symlink_out_of_the_root_is_not_inside_it(trees):
    link = trees["a"] / "escape.py"
    try:
        os.symlink(trees["outside"], link)
    except (OSError, NotImplementedError):
        pytest.skip("this user cannot create symlinks")
    plan = F.plan_op(trees["roots"], trees["display"], "delete", "proj/escape.py")
    assert not plan["ok"], "a link followed the planner out of the session"


def test_mkdir_refuses_an_existing_path(trees):
    plan = F.plan_op(trees["roots"], trees["display"], "mkdir", "proj/src")
    assert not plan["ok"] and "exists" in plan["reason"]


def test_an_unknown_operation_is_refused(trees):
    assert not F.plan_op(trees["roots"], trees["display"],
                         "chown", "proj/src/app.py")["ok"]


# ── search ────────────────────────────────────────────────────────────
def test_search_covers_every_root_and_skips_the_vendor_tree(trees):
    got = F.search(trees["roots"], trees["display"], r"OLD\s*=\s*(\d+)",
                   regex=True)
    paths = {m["path"] for m in got["matches"]}
    assert "proj/src/util.py" in paths and "other/lib/x.ts" in paths
    assert not any("node_modules" in p for p in paths), paths
    assert not any(p.endswith("blob.png") for p in paths), "a png is not text"


def test_a_literal_search_finds_what_a_regex_would_not(trees):
    got = F.search(trees["roots"], trees["display"], "return 'hi '",
                   regex=False)
    assert [m["path"] for m in got["matches"]] == ["proj/src/app.py"]


def test_case_insensitive_search_counts_the_same_file_once_per_hit(trees):
    got = F.search(trees["roots"], trees["display"], "old", regex=False,
                   case_sensitive=False)
    assert got["count"] >= 3, got["matches"]
    assert all(m["line"] >= 1 and m["col"] >= 1 for m in got["matches"])


def test_a_glob_narrows_to_matching_files(trees):
    got = F.search(trees["roots"], trees["display"], "OLD", regex=False,
                   glob="*.ts")
    assert {m["path"] for m in got["matches"]} == {"other/lib/x.ts"}


def test_a_truncated_search_says_so(trees):
    got = F.search(trees["roots"], trees["display"], "OLD", regex=False,
                   max_matches=1)
    assert got["count"] == 1 and got["truncated"] is True


def test_an_unsafe_pattern_never_reaches_the_walk(trees):
    got = F.search(trees["roots"], trees["display"], "(a+)+")
    assert got["count"] == 0 and "quantifier" in got["error"]


# ── replace plans ─────────────────────────────────────────────────────
# `after` is BYTES: the plan must hand the writer exactly what to put on disk,
# and a string that gets re-encoded at the boundary is how a CRLF file becomes
# LF and a stale check refuses a file nobody touched.
def test_a_replace_plan_touches_only_the_files_that_were_shown(trees):
    hits = [{"path": "proj/src/util.py", "line": 1, "col": 1, "text": "OLD = 2"}]
    plan = F.plan_replace(trees["roots"], trees["display"], hits, "OLD", "NEW",
                          regex=False)
    assert plan["ok"] and plan["count"] == 1
    assert [f["path"] for f in plan["files"]] == ["proj/src/util.py"]
    # The bytes that were written are the file's own newline with OLD → NEW:
    # a project-wide replace must not reformat the line endings of every file
    # it touched.
    assert plan["files"][0]["after"] == b"NEW = 2" + (
        b"\r\n" if plan["files"][0]["newline"] == "\r\n" else b"\n")
    assert "app.py" not in str(plan["files"])


def test_a_case_insensitive_literal_replace_returns_a_count_not_a_string(trees):
    # The case-insensitive branch once returned the bare substituted string,
    # so plan_replace's `new, n = ...` unpack raised and the preview route 500ed
    # for every case-insensitive literal replace.
    hits = [{"path": "proj/src/util.py", "line": 1, "col": 1, "text": "OLD = 2"}]
    plan = F.plan_replace(trees["roots"], trees["display"], hits, "old", "NEW",
                          regex=False, case_sensitive=False)
    assert plan["ok"] and plan["count"] == 1
    assert plan["files"][0]["after"].startswith(b"NEW = 2")


def test_a_regex_replace_keeps_its_back_reference(trees):
    hits = [{"path": "proj/src/app.py", "line": 4, "col": 1, "text": "OLD = 1"}]
    plan = F.plan_replace(trees["roots"], trees["display"], hits,
                          r"OLD\s*=\s*(\d+)", r"NEW = \1", regex=True)
    text = plan["files"][0]["after"].decode("utf-8")
    assert "NEW = 1" in text and "OLD" not in text, text


def test_an_unpaired_backslash_is_escaped_not_crashed(trees):
    hits = [{"path": "proj/src/util.py", "line": 1, "col": 1, "text": "OLD = 2"}]
    plan = F.plan_replace(trees["roots"], trees["display"], hits,
                          r"OLD", r"\lNEW", regex=True)
    assert plan["ok"], plan
    assert "\\l" in plan["files"][0]["after"].decode("utf-8")


def test_a_batch_over_the_limit_is_refused_before_anything_is_read(trees):
    hits = [{"path": f"proj/src/f{i}.py", "line": 1} for i in range(
        F.MAX_REPLACE_FILES + 5)]
    plan = F.plan_replace(trees["roots"], trees["display"], hits, "OLD", "NEW",
                          regex=False)
    assert not plan["ok"] and "limit" in plan["error"]


def test_a_path_that_left_the_session_is_refused_not_skipped_silently(trees):
    hits = [{"path": "proj/src/util.py", "line": 1},
            {"path": "proj/gone.py", "line": 1}]
    plan = F.plan_replace(trees["roots"], trees["display"], hits, "OLD", "NEW",
                          regex=False)
    assert plan["refused"] == ["proj/gone.py"], plan
    assert plan["note"]


# ── git argv ──────────────────────────────────────────────────────────
def test_read_actions_need_no_confirmation():
    for action in ("status", "diff", "diff_staged", "log", "branch", "branches"):
        got = F.git_argv(action)
        assert got["ok"] and not got["needs_confirm"], (action, got)


def test_every_work_changing_verb_asks_first():
    # `stash` does not move HEAD, but it removes the work from the tree until
    # it is popped, which is the same thing a person needs to agree to.
    for action, args in (("commit", {"message": "x", "paths": ["a.py"]}),
                         ("branch_switch", {"name": "topic"}),
                         ("stash_pop", {}), ("restore", {"paths": ["a.py"]}),
                         ("stash", {})):
        got = F.git_argv(action, args)
        assert got["ok"], (action, got)
        assert got["needs_confirm"] is True, got
    for action, args in (("stage", {"paths": ["a.py"]}),
                         ("unstage", {"paths": ["a.py"]}),
                         ("branch_create", {"name": "topic"})):
        got = F.git_argv(action, args)
        assert got["ok"] and got["needs_confirm"] is False, got


@pytest.mark.parametrize("action", ["push", "force_push", "reset", "clean",
                                    "rebase", "merge", "amend", "init",
                                    "checkout"])
def test_the_refused_verbs_are_refused_by_the_plan(action):
    got = F.git_argv(action, {"paths": ["a.py"], "message": "x", "name": "b"})
    assert not got["ok"] and got["refused"] is True and got["error"]


def test_a_commit_without_a_message_or_without_files_is_refused():
    assert not F.git_argv("commit", {"paths": ["a.py"]})["ok"]
    assert not F.git_argv("commit", {"message": "msg"})["ok"]


def test_a_dash_named_file_cannot_become_a_git_option():
    got = F.git_argv("stage", {"paths": ["--force", "ok.py"]})
    assert "--force" not in got["argv"], got["argv"]
    assert "ok.py" in got["argv"] and got["argv"][-2] == "--", got["argv"]


def test_a_parent_traversal_in_a_git_path_is_dropped():
    got = F.git_argv("restore", {"paths": ["../../etc/passwd", "a.py"]})
    assert got["argv"] == ["restore", "--", "a.py"], got["argv"]


@pytest.mark.parametrize("bad", ["", "has space", "~tilde", "-leading",
                                 "..dots", "trail/", "a\\b", "a" * 200])
def test_branch_names_that_git_would_reject_are_rejected_here(bad):
    assert not F.git_argv("branch_create", {"name": bad})["ok"], bad


def test_a_sane_branch_name_is_accepted():
    got = F.git_argv("branch_create", {"name": "feature/studio-8"})
    assert got["ok"] and got["argv"] == ["branch", "feature/studio-8"]


# ── workspaces (git worktrees) ────────────────────────────────────────
def test_worktrees_are_listed_read_only():
    got = F.git_argv("worktrees")
    assert got["ok"] and got["argv"][:2] == ["worktree", "list"]
    assert got["needs_confirm"] is False


def test_a_workspace_is_a_worktree_on_an_opencode_branch():
    got = F.git_argv("worktree_add", {"name": "feature-x",
                                      "path": r"C:\repos\app-feature-x"})
    assert got["ok"], got
    assert got["argv"] == ["worktree", "add", "-b", "opencode/feature-x",
                           r"C:\repos\app-feature-x"]
    # A new workspace must not move the branch you are on.
    assert got["moves_head"] is False


def test_a_workspace_name_is_namespaced_once():
    got = F.git_argv("worktree_add", {"name": "opencode/feature-x",
                                      "path": r"C:\repos\app-feature-x"})
    assert got["argv"][3] == "opencode/feature-x", got["argv"]


def test_removing_a_workspace_asks_first():
    got = F.git_argv("worktree_remove", {"path": r"C:\repos\app-feature-x"})
    assert got["ok"] and got["needs_confirm"] is True, got
    assert got["argv"] == ["worktree", "remove", r"C:\repos\app-feature-x"]


def test_a_workspace_add_without_a_path_is_refused():
    assert not F.git_argv("worktree_add", {"name": "x"})["ok"]
    assert not F.git_argv("worktree_remove", {})["ok"]


def test_status_porcelain_keeps_the_states_a_panel_cannot_act_on():
    rows = F.status_paths("## main...origin/main\n"
                          "M  a.py\n M b.py\n?? c.py\nUU d.py\n D e.py\n"
                          "R  old.py -> new.py\n")
    by = {r["path"]: r for r in rows}
    assert "a.py" in by and by["a.py"]["staged"] is True
    assert by["b.py"]["staged"] is False
    assert by["c.py"]["untracked"] is True
    assert by["d.py"]["unmerged"] is True
    assert by["e.py"]["deleted"] is True
    assert by["new.py"]["path"] == "new.py", "a rename is shown by its new name"


# ── the routes ────────────────────────────────────────────────────────
@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _session(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("OLD = 1\n", encoding="utf-8")
    (root / "src" / "gone.py").write_text("bye\n", encoding="utf-8")
    # add_root APPENDS to whatever SID already holds, and the session id is
    # shared across tests in a run — without this clear the "session" is the
    # union of every previous test's temp folder, half of which no longer
    # exists. Tests that assert on one project must start from one project.
    sw.clear(SID)
    state, why = sw.add_root(SID, str(root))
    assert state is not None, why
    _review.clear(SID)
    return root


def test_the_files_route_deletes_and_the_checkpoint_puts_the_bytes_back(client,
                                                                        tmp_path):
    root = _session(tmp_path, None)
    sw.bind(SID)
    got = client.post("/api/studio/files", json={
        "sid": SID, "ops": [{"op": "delete", "from": "proj/src/gone.py"},
                            {"op": "rename", "from": "proj/src/app.py",
                             "to": "proj/src/kept.py"}]}).json()
    assert got["applied"] == 2, got
    assert not (root / "src" / "gone.py").exists()
    assert (root / "src" / "kept.py").read_text(encoding="utf-8") == "OLD = 1\n"
    assert got["checkpoint"], "an undo must exist before a destructive verb runs"
    restored = client.post("/api/studio/checkpoint/restore",
                           json={"sid": SID, "id": got["checkpoint"]}).json()
    assert restored.get("ok"), restored
    assert (root / "src" / "gone.py").read_text(encoding="utf-8") == "bye\n"
    assert (root / "src" / "app.py").read_text(encoding="utf-8") == "OLD = 1\n"


def test_the_files_route_reports_a_refusal_per_operation(client, tmp_path):
    root = _session(tmp_path, None)
    sw.bind(SID)
    got = client.post("/api/studio/files", json={
        "sid": SID, "ops": [{"op": "delete", "from": "proj/nope.py"},
                            {"op": "delete", "from": "../escape.py"}]}).json()
    assert got["applied"] == 0 and got["refused_count"] == 2, got
    assert (root / "src" / "app.py").exists()
    assert not got["checkpoint"], "nothing happened, so there is nothing to undo"


def test_the_replace_route_previews_then_applies_exactly_that(client, tmp_path):
    root = _session(tmp_path, None)
    sw.bind(SID)
    hits = client.get(f"/api/studio/search?sid={SID}&q=OLD&regex=false").json()
    assert hits["count"] >= 1, hits
    dry = client.post("/api/studio/replace", json={
        "sid": SID, "hits": hits["matches"], "pattern": "OLD",
        "replacement": "NEW", "regex": False}).json()
    assert dry["dry_run"] is True and dry["token"], dry
    assert not dry.get("applied"), "a preview must not write"
    applied = client.post("/api/studio/replace", json={
        "sid": SID, "hits": hits["matches"], "pattern": "OLD",
        "replacement": "NEW", "regex": False, "apply": True,
        "token": dry["token"]}).json()
    assert applied["ok"] and applied["applied"] >= 1, applied
    assert "NEW" in (root / "src" / "app.py").read_text(encoding="utf-8")
    assert applied["checkpoint"], "a project-wide replace is undoable as one act"


def test_a_preview_is_single_use(client, tmp_path):
    _session(tmp_path, None)
    sw.bind(SID)
    hits = client.get(f"/api/studio/search?sid={SID}&q=OLD&regex=false").json()
    dry = client.post("/api/studio/replace", json={
        "sid": SID, "hits": hits["matches"], "pattern": "OLD",
        "replacement": "NEW", "regex": False}).json()
    first = client.post("/api/studio/replace", json={
        "sid": SID, "hits": hits["matches"], "pattern": "OLD",
        "replacement": "NEW", "regex": False, "apply": True,
        "token": dry["token"]})
    assert first.status_code == 200, first.text
    again = client.post("/api/studio/replace", json={
        "sid": SID, "hits": hits["matches"], "pattern": "OLD",
        "replacement": "NEW", "regex": False, "apply": True,
        "token": dry["token"]})
    assert again.status_code == 409, again.text


def test_a_file_that_moved_after_the_preview_is_skipped_and_named(client, tmp_path):
    """A race is not a reason to lose the work that DID match, and it is not a
    reason to write the one that didn't. The verified file lands; the raced
    file is skipped and named; the person sees both halves."""
    root = _session(tmp_path, None)
    sw.bind(SID)
    (root / "src" / "util.py").write_text("OLD = 3\n", encoding="utf-8")
    hits = client.get(f"/api/studio/search?sid={SID}&q=OLD&regex=false").json()
    assert len(hits["matches"]) == 2, hits
    dry = client.post("/api/studio/replace", json={
        "sid": SID, "hits": hits["matches"], "pattern": "OLD",
        "replacement": "NEW", "regex": False}).json()
    (root / "src" / "app.py").write_text("OLD = edited by someone else\n",
                                         encoding="utf-8")
    applied = client.post("/api/studio/replace", json={
        "sid": SID, "hits": hits["matches"], "pattern": "OLD",
        "replacement": "NEW", "regex": False, "apply": True,
        "token": dry["token"]}).json()
    assert applied["stale"] and "app.py" in applied["stale"][0], applied
    assert applied["applied"] == 1, applied
    assert "NEW = 3" in (root / "src" / "util.py").read_text(encoding="utf-8")
    assert "edited by someone else" in (root / "src" / "app.py").read_text(
        encoding="utf-8"), "the raced file must not be written from a stale plan"
    assert applied["note"], "a partial result has to be said out loud"


def test_the_git_route_asks_before_moving_head(client, tmp_path):
    import subprocess
    root = _session(tmp_path, None)
    git = __import__("shutil").which("git")
    if not git:
        pytest.skip("git is not installed")
    for args in (["init", "-q"], ["config", "user.email", "a@b.c"],
                 ["config", "user.name", "t"], ["add", "-A"],
                 ["commit", "-q", "-m", "seed"]):
        subprocess.run([git, "-C", str(root)] + args, capture_output=True)
    (root / "src" / "app.py").write_text("NEW = 1\n", encoding="utf-8")
    sw.bind(SID)
    stage = client.post("/api/studio/git", json={
        "sid": SID, "action": "stage", "args": {"paths": ["proj/src/app.py"]}})
    assert stage.status_code == 200, stage.text
    unconfirmed = client.post("/api/studio/git", json={
        "sid": SID, "action": "commit",
        "args": {"paths": ["proj/src/app.py"], "message": "update"}})
    assert unconfirmed.status_code == 428, unconfirmed.text
    body = unconfirmed.json()
    assert body["confirm_required"] is True
    assert "NEW = 1" in json.dumps(body["preview"]), (
        "the pre-commit diff has to be the thing the person approves")
    done = client.post("/api/studio/git", json={
        "sid": SID, "action": "commit", "confirm": True,
        "args": {"paths": ["proj/src/app.py"], "message": "update"}})
    assert done.status_code == 200 and done.json()["ok"], done.text
    log = subprocess.run([git, "-C", str(root), "log", "--oneline"],
                         capture_output=True, text=True).stdout
    assert "update" in log


def test_a_push_is_refused_before_the_subprocess(client, tmp_path):
    _session(tmp_path, None)
    sw.bind(SID)
    got = client.post("/api/studio/git", json={
        "sid": SID, "action": "push", "args": {"paths": []}})
    assert got.status_code == 403, got.text
    assert "remote" in got.json()["error"]


def test_the_journal_records_what_a_person_started(client, tmp_path):
    import subprocess
    root = _session(tmp_path, None)
    git = __import__("shutil").which("git")
    if not git:
        pytest.skip("git is not installed")
    for args in (["init", "-q"], ["config", "user.email", "a@b.c"],
                 ["config", "user.name", "t"]):
        subprocess.run([git, "-C", str(root)] + args, capture_output=True)
    sw.bind(SID)
    client.post("/api/studio/files", json={
        "sid": SID, "ops": [{"op": "delete", "from": "proj/src/gone.py"}]})
    client.post("/api/studio/git", json={"sid": SID, "action": "status"})
    rows = client.get(f"/api/studio/journal?sid={SID}").json()["entries"]
    kinds = [r["kind"] for r in rows]
    assert "files" in kinds and "git" in kinds, rows
    assert rows[0]["at"] >= rows[-1]["at"], "newest first"
    git_row = next(r for r in rows if r["kind"] == "git")
    assert git_row["argv"] == ["status", "--porcelain=v1", "--branch"], git_row

