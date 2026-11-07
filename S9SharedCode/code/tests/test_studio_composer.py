"""Slice 6 — the composer: `@` mentions, `/` commands and a real cost readout.

Three things a composer promises that a transcript cannot show:

- `@app.py` means *this file*, in the vocabulary the agent's own tools accept,
  and it means it across every folder the session holds — not only the first;
- `/` commands are a list the server owns, so the popup cannot offer a verb
  that does nothing;
- the money shown is the gateway's, not tokens multiplied by a price someone
  typed into TypeScript.

The pure half lives in `studio_composer.py` and is tested directly; the route
half is tested through the ASGI app with the model call stubbed, so the
honesty rules (a mention that does not resolve is REPORTED, a diff that did not
fit is LISTED, an unreachable ledger is `unknown` and never `$0`) are checked
where they are actually enforced.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))

import agent_server as A              # noqa: E402
import studio_composer as C           # noqa: E402
import studio_runs as R               # noqa: E402
import studio_workspace as sw         # noqa: E402
from agent_server import app          # noqa: E402

SID = "s-composer"


@pytest.fixture(autouse=True)
def _unbind():
    sw.bind("")
    yield
    sw.bind("")


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    """A session holding TWO folders, so "first root only" cannot pass."""
    a = tmp_path / "alpha"
    b = tmp_path / "beta"
    (a / "src").mkdir(parents=True)
    b.mkdir()
    (a / "src" / "app.py").write_text("print('alpha')\n", encoding="utf-8")
    (a / "src" / "models.py").write_text("X = 1\n", encoding="utf-8")
    (a / "README.md").write_text("# alpha\n", encoding="utf-8")
    (b / "tool.ts").write_text("export const x = 1\n", encoding="utf-8")
    (a / "node_modules").mkdir()
    (a / "node_modules" / "junk.py").write_text("noise\n", encoding="utf-8")
    (a / ".secret").write_text("token=1\n", encoding="utf-8")
    for root in (a, b):
        state, why = sw.add_root(SID, str(root))
        assert state is not None, why
    R.forget(SID)
    yield a, b
    R.forget(SID)
    sw.clear(SID)


# ── @-mentions: the candidate list ─────────────────────────────────────
def _cands(base, roots, frag, **kw):
    return C.mentions(base, roots, frag, skip_dirs=A._CODE_SKIP_DIRS,
                      skip_files=A._CODE_SKIP_FILES,
                      visible=A._code_name_visible, **kw)


def test_a_mention_completes_inside_both_roots(workspace):
    """A session with two folders must complete in both. Listing only the
    first is the single-root assumption, and it silently makes the second
    project unmentionable."""
    sw.bind(SID)
    base, roots = A._code_workspace()
    got = _cands(base, roots, "tool")
    assert [m["path"] for m in got["matches"]] == ["beta/tool.ts"], got


def test_mention_paths_use_the_explorer_vocabulary(workspace):
    """`<root>/dir/file`, exactly as the explorer shows it and exactly what
    `_code_agent_abs` accepts back. A completion in any other form is a path
    the agent then fails to read."""
    sw.bind(SID)
    base, roots = A._code_workspace()
    got = _cands(base, roots, "app")
    assert [m["path"] for m in got["matches"]] == ["alpha/src/app.py"]


def test_mentions_offer_folders_as_well_as_files(workspace):
    sw.bind(SID)
    base, roots = A._code_workspace()
    got = _cands(base, roots, "sr")
    rows = {m["path"]: m["dir"] for m in got["matches"]}
    assert rows.get("alpha/src") is True, "a folder must be mentionable"


def test_a_directory_fragment_completes_inside_that_directory_only(workspace):
    sw.bind(SID)
    base, roots = A._code_workspace()
    got = _cands(base, roots, "alpha/src/")
    paths = {m["path"] for m in got["matches"]}
    assert paths == {"alpha/src/app.py", "alpha/src/models.py"}, paths
    assert "alpha/README.md" not in paths


def test_a_folder_is_also_completable_without_its_root_prefix(workspace):
    """`@src/` is what a person types when they are already thinking inside
    the project. Requiring the explorer's full `<root>/src/` spelling made the
    typed form return NOTHING — and an empty popup reads as a broken tool, not
    as a hint about vocabulary."""
    sw.bind(SID)
    base, roots = A._code_workspace()
    got = _cands(base, roots, "src/")
    paths = {m["path"] for m in got["matches"]}
    assert paths == {"alpha/src/app.py", "alpha/src/models.py"}, paths


def test_blocked_and_hidden_trees_are_never_suggested(workspace):
    """node_modules is a build tree and `.secret` is where keys live. Neither
    may appear in a completion, because the completion is an offer to put the
    path in front of a model."""
    sw.bind(SID)
    base, roots = A._code_workspace()
    for frag in ("junk", "node", "secret", ".secret"):
        got = _cands(base, roots, frag)
        assert not got["matches"], f"{frag!r} offered {got['matches']}"


def test_a_prefix_on_the_file_name_beats_a_substring_in_the_path(workspace):
    sw.bind(SID)
    base, roots = A._code_workspace()
    got = _cands(base, roots, "read")
    assert got["matches"][0]["path"] == "alpha/README.md", got["matches"]


def test_the_limit_and_the_walk_budget_are_both_reported(workspace):
    """Truncation is allowed; a truncation nobody can see is not."""
    sw.bind(SID)
    base, roots = A._code_workspace()
    got = _cands(base, roots, "", limit=2)
    assert len(got["matches"]) == 2
    assert got["truncated"] is True
    tiny = _cands(base, roots, "", max_entries=1)
    assert tiny["stopped"] is True and tiny["note"], "an exhausted walk lies"


# ── @-mentions: reading them out of a question ────────────────────────
def test_mentions_are_pulled_from_prose_and_deduplicated():
    got = C.extract_mentions(
        "diff @alpha/src/app.py against @alpha/src/app.py, then @beta/tool.ts.")
    assert got == ["alpha/src/app.py", "beta/tool.ts"], got


def test_an_email_is_not_a_file():
    """The naive `@\\S+` pattern turns "ping me at a@b.com" into an attachment
    request for `b.com` — and the run then reports it could not read it."""
    assert C.extract_mentions("ping me at dev@aisha.example.com") == []


def test_a_trailing_slash_is_not_part_of_the_path():
    assert C.extract_mentions("@alpha/src/") == ["alpha/src"]


# ── /commands ──────────────────────────────────────────────────────────
def test_the_command_registry_is_server_owned_and_described():
    rows = C.commands()
    names = [r["name"] for r in rows]
    assert len(names) == len(set(names)), "two commands, one name"
    for row in rows:
        assert row["desc"] and row["kind"], row
    assert {"new", "stop", "undo", "checks", "commit-msg", "roots"} <= set(names)


# ── the commit-message diff binding ───────────────────────────────────
def test_every_changed_file_survives_a_trimmed_diff():
    """The honesty rule: the budget may cut HUNKS, never the FILE LIST, and the
    cut has to be named so the UI can say what the model did not see."""
    status = "\n".join(f"M  app{i}.py" for i in range(30))
    diff = "".join(
        f"diff --git a/app{i}.py b/app{i}.py\n"
        + "".join(f"+line {j}\n" for j in range(400))
        for i in range(30))
    got = C.diff_prompt(status, diff, budget=20_000, per_file=1_500)
    assert len(got["files"]) == 30, "the file list is not a place to truncate"
    assert got["truncated"] is True and got["omitted"]
    assert "left out" in got["prompt"]
    for path in got["omitted"]:
        assert path in got["prompt"], "the model must be told the file exists"


def test_a_small_changeset_is_not_marked_truncated():
    status = "M  app.py\n"
    diff = ("diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
            "@@ -1 +1 @@\n-1\n+2\n")
    got = C.diff_prompt(status, diff)
    assert got["truncated"] is False and got["omitted"] == []
    assert got["files_with_diffs"] == ["app.py"]
    assert "left out" not in got["prompt"]


def test_a_renamed_file_is_counted_by_its_new_name():
    got = C.diff_prompt("R  old.py -> new.py\n", "")
    assert got["files"] == ["new.py"], got["files"]


def test_a_commit_reply_is_cleaned_but_not_rewritten():
    got = C.parse_commit_reply(
        'Here is a commit message:\n```text\n"fix: handle empty diffs"\n\n'
        'Body stays a sentence.\n```')
    assert got["subject"] == "fix: handle empty diffs", got
    assert got["body"] == "Body stays a sentence.", got


# ── the routes ─────────────────────────────────────────────────────────
@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def test_the_mentions_route_is_scoped_to_the_session_it_bound(client, workspace):
    got = client.get(f"/api/studio/mentions?sid={SID}&q=app")
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["sid"] == SID
    assert [m["path"] for m in body["matches"]] == ["alpha/src/app.py"]
    assert len(body["roots"]) == 2


def test_the_command_route_serves_the_registry_the_popup_renders(client):
    got = client.get("/api/studio/commands").json()["commands"]
    assert {r["name"] for r in got} == C.command_names(), (
        "the route and the module must be the same list")


# ── the ceilings ride on the run, not on a constant in TypeScript ─────
def test_the_started_frame_carries_the_ceilings_and_the_ledger_key(
        client, workspace, monkeypatch):
    """The composer's context chip says "hop 7 of 40". If 40 were a number the
    frontend invented it would drift from the server's cap the first time the
    cap changes — and the chip would then lie about a run that stopped early."""
    import mcp_runner

    seen: dict = {}

    async def fake_run(**kwargs):
        seen.update(kwargs)
        return {"text": "done", "provider": "test", "input_tokens": 1,
                "output_tokens": 1, "tool_calls": 0}

    monkeypatch.setattr(mcp_runner, "run_with_tools", fake_run)
    sw.bind(SID)
    with client.stream("POST", "/api/code/agent", json={
            "question": "hi", "path": "", "text": "", "sid": SID,
            "max_hops": 7}) as r:
        body = "".join(r.iter_text())
    assert '"type": "started"' in body or "type\":\"started" in body, body
    assert "max_hops" in body and "gateway_session" in body, body
    assert "code-run-" in body, "the frame must name the ledger it will read"
    assert seen["session_id"].startswith("code-run-"), (
        "per-run session, or two runs on one file share a cost column")
    assert seen["max_hops"] == 7


def test_two_runs_on_one_file_do_not_share_a_cost_ledger(client, workspace,
                                                         monkeypatch):
    """The old key was `code-{path}`, so the panel for run B showed run A's
    money. Each run must land in its own gateway session."""
    import mcp_runner
    sessions: list[str] = []

    async def fake_run(**kwargs):
        sessions.append(kwargs["session_id"])
        return {"text": "ok", "provider": "test", "input_tokens": 1,
                "output_tokens": 1, "tool_calls": 0}

    monkeypatch.setattr(mcp_runner, "run_with_tools", fake_run)
    sw.bind(SID)
    for _ in range(2):
        with client.stream("POST", "/api/code/agent", json={
                "question": "edit app.py", "path": "alpha/src/app.py",
                "text": "x", "sid": SID}) as r:
            "".join(r.iter_text())
    assert len(set(sessions)) == 2, sessions


def test_the_cost_route_reads_the_gateway_and_never_invents_zero(
        client, workspace, monkeypatch):
    calls: list[str] = []

    def fake(session_id: str):
        calls.append(session_id)
        return {"code": {"usd": 0.0125, "in_tok": 400, "out_tok": 90,
                         "calls": 2}}

    monkeypatch.setattr(A, "_session_cost_breakdown", fake)
    run = R.start(SID, "cost test")
    got = client.get(f"/api/studio/runs/{run['id']}/cost?sid={SID}").json()
    assert got["usd"] == pytest.approx(0.0125)
    assert got["calls"] == 2 and got["unknown"] is False
    assert calls == [f"code-run-{run['id']}"], "asked for THIS run's ledger"

    monkeypatch.setattr(A, "_session_cost_breakdown", lambda s: None)
    down = client.get(f"/api/studio/runs/{run['id']}/cost?sid={SID}").json()
    assert down["unknown"] is True and down["usd"] is None, (
        "an unreachable ledger is a gap, not a $0 row")
    # An id the transcript path cannot be built from. `..%2Fetc` is refused by
    # the router before it reaches here, so this checks the handler's own shape
    # guard — the one that stops a run id becoming a file path.
    assert client.get("/api/studio/runs/a%20b/cost").status_code == 400
    missing = client.get(f"/api/studio/runs/r-deadbeefcafe/cost?sid={SID}")
    assert missing.status_code == 404, (
        "a run this session never had is not the same as unreadable money")
    R.finish(run["id"], "finished")


# ── the commit-message route ──────────────────────────────────────────
class _FakeLLM:
    text = "feat: add a reviewer\n\nExplains why the review surface exists."
    seen: dict = {}

    def chat(self, **kwargs):
        _FakeLLM.seen = kwargs
        return {"text": type(self).text}


@pytest.fixture()
def fake_git(monkeypatch, tmp_path):
    repo = tmp_path / "alpha"
    monkeypatch.setattr(A, "_code_git_root", lambda root: repo)
    box = {"staged": "", "working": "", "status": "M  app.py\n"}

    def fake(repo_, *args):
        if "status" in args:
            return {"ok": True, "exit_code": 0, "output": box["status"],
                    "truncated": False, "stderr": ""}
        out = box["staged"] if "--staged" in args else box["working"]
        return {"ok": True, "exit_code": 0, "output": out,
                "truncated": False, "stderr": ""}

    monkeypatch.setattr(A, "_code_git", lambda repo_, *a: fake(repo_, *a))
    import gateway
    monkeypatch.setattr(gateway, "LLM", _FakeLLM)
    return box


def test_the_commit_message_is_written_from_the_staged_diff(
        client, workspace, fake_git):
    fake_git["staged"] = ("diff --git a/app.py b/app.py\n--- a/app.py\n"
                          "+++ b/app.py\n@@ -1 +1 @@\n-1\n+2\n")
    fake_git["working"] = "diff --git a/other.py b/other.py\n+x\n"
    got = client.post(f"/api/studio/commit-msg", json={"sid": SID})
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["staged"] is True
    assert body["subject"] == "feat: add a reviewer", body
    assert "app.py" in _FakeLLM.seen["prompt"]
    assert "other.py" not in _FakeLLM.seen["prompt"].split("CHANGED FILES")[1], \
        "the staged diff was asked for; the working tree must not leak in"


def test_an_empty_index_falls_back_to_the_working_tree_and_says_so(
        client, workspace, fake_git):
    fake_git["staged"] = "   \n"
    fake_git["working"] = ("diff --git a/app.py b/app.py\n"
                           "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-1\n+2\n")
    body = client.post("/api/studio/commit-msg", json={"sid": SID}).json()
    assert body["staged"] is False, "the UI has to label what it wrote from"
    assert body["subject"]


def test_no_repository_is_a_refusal_not_an_empty_message(
        client, workspace, fake_git, monkeypatch):
    monkeypatch.setattr(A, "_code_git_root", lambda root: None)
    got = client.post("/api/studio/commit-msg", json={"sid": SID})
    assert got.status_code == 409
    assert "git repository" in got.json()["error"]


def test_a_clean_tree_is_refused_rather_than_invented(client, workspace,
                                                     fake_git):
    fake_git["staged"] = fake_git["working"] = ""
    fake_git["status"] = ""
    got = client.post("/api/studio/commit-msg", json={"sid": SID})
    assert got.status_code == 409 and "no changes" in got.json()["error"]


# ── a mention in the question is context, and a broken one is loud ────
def _run_with(monkeypatch, question: str, sid: str = SID):
    import mcp_runner
    seen: dict = {}

    async def fake_run(**kwargs):
        seen["messages"] = kwargs["messages"]
        return {"text": "ok", "provider": "test", "input_tokens": 1,
                "output_tokens": 1, "tool_calls": 0}

    monkeypatch.setattr(mcp_runner, "run_with_tools", fake_run)
    monkeypatch.setattr(A, "_code_write_guard", lambda *a, **k: None)
    sw.bind(sid)
    with TestClient(app) as c:
        with c.stream("POST", "/api/code/agent", json={
                "question": question, "path": "", "text": "", "sid": sid}) as r:
            body = "".join(r.iter_text())
    return seen, body


def test_an_at_mention_puts_the_file_in_the_prompt(workspace, monkeypatch):
    seen, _ = _run_with(monkeypatch, "explain @alpha/src/models.py")
    blob = str(seen["messages"])
    assert "ATTACHED: src/models.py" in blob, (
        "the mention must arrive in the tools' own vocabulary")
    assert "X = 1" in blob, "and with its contents, not only its name"


def test_a_mention_outside_the_session_is_reported_not_dropped(
        workspace, monkeypatch):
    _seen, body = _run_with(monkeypatch, "read @../../elsewhere/thing.py")
    assert "not attached" in body, (
        "silently ignoring a mention makes the agent answer as if it read it")


def test_a_mentioned_file_counts_as_read_for_the_edit_guard(
        workspace, monkeypatch):
    """The read-before-edit guard refuses an edit to a file the model never
    opened. A mention put the file IN the prompt, so the model has seen it —
    and a guard that says otherwise sends the agent to re-read its own context
    (or, worse, makes it give up on a file the person just named)."""
    import mcp_runner
    verdict: dict = {}

    async def fake_run(**kwargs):
        approve = kwargs["approve_fn"]
        verdict["edit"] = await approve("edit_file",
                                        {"path": "src/models.py",
                                         "find": "X = 1", "replace": "X = 2"})
        return {"text": "ok", "provider": "test", "input_tokens": 1,
                "output_tokens": 1, "tool_calls": 0}

    monkeypatch.setattr(mcp_runner, "run_with_tools", fake_run)
    sw.bind(SID)
    with TestClient(app) as c:
        with c.stream("POST", "/api/code/agent", json={
                "question": "add a comment to @alpha/src/models.py",
                "path": "", "text": "", "sid": SID,
                "auto_approve": True}) as r:
            "".join(r.iter_text())
    assert verdict["edit"] is True, (
        f"the guard refused a file the mention had already supplied: "
        f"{verdict['edit']!r}")


def test_an_unmentioned_file_is_still_refused_until_it_is_read(
        workspace, monkeypatch):
    """The mutation check for the test above: if the seeding were unconditional
    the agent could edit anything in the tree unseen, and that is the rule this
    guard exists to hold."""
    import mcp_runner
    verdict: dict = {}

    async def fake_run(**kwargs):
        verdict["edit"] = await kwargs["approve_fn"](
            "edit_file", {"path": "src/never-mentioned.py",
                          "find": "a", "replace": "b"})
        return {"text": "ok", "provider": "test", "input_tokens": 1,
                "output_tokens": 1, "tool_calls": 0}

    monkeypatch.setattr(mcp_runner, "run_with_tools", fake_run)
    sw.bind(SID)
    with TestClient(app) as c:
        with c.stream("POST", "/api/code/agent", json={
                "question": "edit @alpha/src/models.py", "path": "",
                "text": "", "sid": SID, "auto_approve": True}) as r:
            "".join(r.iter_text())
    assert "read_file" in str(verdict["edit"]), verdict["edit"]

