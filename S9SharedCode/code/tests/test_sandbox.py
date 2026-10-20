"""The executor behind run_command / run_checks.

Proves the boundary that makes an arbitrary PowerShell command acceptable:
environment scrub, wall-clock kill of the process TREE, bounded output, and
the tool-level refusal to start outside the workspace. Deliberately not a
claim of filesystem isolation - sandbox.py says so itself.
"""
from __future__ import annotations

import json
import sys
import time

import pytest

import mcp_server as mcp
import sandbox


def _python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


@pytest.fixture(autouse=True)
def _isolated_audit(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp, "_EXEC_AUDIT", tmp_path / "audit.jsonl")


def test_clean_env_drops_credentials(monkeypatch):
    monkeypatch.setenv("SOME_PASSWORD", "hunter2")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-nope")
    monkeypatch.setenv("ARIA_INTERNAL_FLAG", "1")
    env = sandbox.clean_env()
    assert "SOME_PASSWORD" not in env
    assert "OPENAI_API_KEY" not in env
    assert "ARIA_INTERNAL_FLAG" not in env
    assert env.get("PYTHONIOENCODING") == "utf-8"


def test_run_returns_output_and_exit_code():
    res = sandbox.run(_python("print('hello'); raise SystemExit(3)"),
                      cwd=".", timeout_s=30)
    assert res.exit_code == 3
    assert res.stdout.strip() == "hello"
    assert res.ok is False
    assert res.backend in ("jobobject", "process")


def test_timeout_kills_the_process_tree():
    t0 = time.time()
    res = sandbox.run(_python("import time; time.sleep(60)"),
                      cwd=".", timeout_s=1)
    elapsed = time.time() - t0
    assert res.timed_out is True
    assert res.exit_code is not None
    assert elapsed < 20, f"the timeout did not terminate the child ({elapsed:.1f}s)"


def test_output_is_capped_but_the_pipe_keeps_draining():
    res = sandbox.run(_python("print('x' * 200000)"),
                      cwd=".", timeout_s=30, max_output=4096)
    assert len(res.stdout) == 4096
    assert res.truncated is True
    assert res.ok is True


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell tool is Windows-only")
def test_run_command_runs_in_the_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp, "SANDBOX", tmp_path)
    out = mcp.run_command("Write-Output ('cwd=' + (Get-Location).Path)")
    assert out["ok"] is True, out
    assert str(tmp_path).lower() in out["stdout"].lower()


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell tool is Windows-only")
def test_run_command_refuses_a_credential_environment_read(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp, "SANDBOX", tmp_path)
    monkeypatch.setenv("SOME_PASSWORD", "hunter2")
    out = mcp.run_command("Write-Output ('v=' + $env:SOME_PASSWORD)")
    assert out["ok"] is False
    assert out["refused"] is True
    assert "hunter2" not in str(out)


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell tool is Windows-only")
def test_run_command_reports_a_failing_exit_code(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp, "SANDBOX", tmp_path)
    out = mcp.run_command("cmd /c exit 7")
    assert out["exit_code"] == 7, out
    assert out["ok"] is False


def test_run_command_refuses_a_cwd_outside_the_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp, "SANDBOX", tmp_path)
    out = mcp.run_command("echo hi", cwd="..")
    assert out["ok"] is False
    assert "escapes the sandbox" in out["error"]


def test_command_guard_refuses_machine_destruction(tmp_path):
    assert "format" in mcp._refuse_command("format C: /q", tmp_path)
    assert "processes" in mcp._refuse_command("Stop-Process -Name python", tmp_path)
    assert "uncommitted" in mcp._refuse_command("git reset --hard HEAD", tmp_path)
    assert "drive root" in mcp._refuse_command("rm -rf /", tmp_path)
    assert "force-push" in mcp._refuse_command("git push --force origin main", tmp_path)


def test_command_guard_refuses_credential_files_and_profile(tmp_path):
    assert "credential" in mcp._refuse_command("Get-Content .env", tmp_path)
    assert "credential" in mcp._refuse_command("Get-Content C:\\Users\\me\\.ssh\\id_rsa", tmp_path)
    assert "profile" in mcp._refuse_command("cd $env:USERPROFILE", tmp_path)


def test_command_guard_refuses_parent_traversal(tmp_path):
    assert "traverse" in mcp._refuse_command("Get-Content ..\\..\\secret.txt", tmp_path)
    assert "traverse" in mcp._refuse_command("cd ..", tmp_path)


def test_command_guard_allows_workspace_relative_work(tmp_path):
    assert mcp._refuse_command("npm run build", tmp_path) == ""
    assert mcp._refuse_command("Remove-Item -Recurse -Force .\\dist", tmp_path) == ""
    assert mcp._refuse_command(f'Write-Output "{tmp_path}"', tmp_path) == ""


def test_command_guard_refuses_absolute_paths_outside_the_workspace(tmp_path):
    reason = mcp._refuse_command("Get-Content C:\\Windows\\win.ini", tmp_path)
    assert "absolute paths outside the workspace" in reason


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell tool is Windows-only")
def test_run_command_audits_allowed_and_refused_attempts(tmp_path, monkeypatch):
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setattr(mcp, "SANDBOX", tmp_path)
    monkeypatch.setattr(mcp, "_EXEC_AUDIT", audit)
    mcp.run_command("Write-Output ok")
    mcp.run_command("git reset --hard HEAD")
    lines = [json.loads(x) for x in audit.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2
    assert lines[0]["ok"] is True and "refused" not in lines[0]
    assert lines[1]["refused"] and lines[1]["ok"] is False
