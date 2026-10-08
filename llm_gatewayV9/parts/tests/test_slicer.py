"""Slicability probe: argv, environment, failure modes, and real subprocesses."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from parts.mesh import Mesh
from parts.slicer import (
    FAILED,
    OK,
    TIMEOUT,
    UNAVAILABLE,
    _run,
    build_argv,
    check_mesh,
    child_env,
    find_prusa_slicer,
    slicer_callable,
)
from parts.tests import primitives as prim

#: Present in this environment, so it satisfies the "is it a real file" check
#: without depending on PrusaSlicer being installed.
FAKE_EXE = sys.executable


def cube_mesh() -> Mesh:
    return Mesh.create(*prim.cube())


class StubRunner:
    """Stands in for the subprocess call. Records argv and can fake outcomes."""

    def __init__(self, *, returncode=0, stderr=b"", write_gcode=True, gcode=b"G28\n"):
        self.calls = []
        self.returncode = returncode
        self.stderr = stderr
        self.write_gcode = write_gcode
        self.gcode = gcode

    def __call__(self, argv, *, timeout, cwd):
        # Sampled here, inside the call: the workdir is deleted before
        # `check_mesh` returns, so afterwards there is nothing left to look at.
        self.calls.append({
            "argv": argv, "timeout": timeout, "cwd": cwd,
            "input_existed": Path(argv[-1]).is_file(),
            "input_bytes": Path(argv[-1]).stat().st_size if Path(argv[-1]).is_file() else 0,
        })
        if self.write_gcode:
            out = Path(argv[argv.index("--output") + 1])
            out.write_bytes(self.gcode)
        return subprocess.CompletedProcess(argv, self.returncode, b"", self.stderr)


# -- discovery -------------------------------------------------------------

def test_a_missing_slicer_is_reported_not_raised(monkeypatch):
    monkeypatch.delenv("PRUSA_SLICER_PATH", raising=False)
    monkeypatch.setattr("parts.slicer.shutil.which", lambda name: None)
    assert find_prusa_slicer() is None


def test_the_env_var_is_honoured(monkeypatch, tmp_path):
    monkeypatch.setenv("PRUSA_SLICER_PATH", str(tmp_path / "ps.exe"))
    (tmp_path / "ps.exe").write_bytes(b"MZ")
    assert find_prusa_slicer() == str(tmp_path / "ps.exe")


def test_a_named_slicer_that_does_not_exist_is_not_used(monkeypatch):
    assert find_prusa_slicer("/definitely/not/here/prusa-slicer") is None


def test_no_slicer_means_unavailable_and_nothing_is_spawned(monkeypatch):
    monkeypatch.delenv("PRUSA_SLICER_PATH", raising=False)
    monkeypatch.setattr("parts.slicer.shutil.which", lambda name: None)
    spawned = []
    result = check_mesh(cube_mesh(), runner=lambda *a, **k: spawned.append(a))
    assert result.status == UNAVAILABLE
    assert result.sliced is False
    assert "PRUSA_SLICER_PATH" in result.detail
    assert spawned == [], "the subprocess must not be reached"


# -- command line ----------------------------------------------------------

def test_the_argv_is_exactly_what_prusaslicer_needs(tmp_path):
    argv = build_argv("ps", tmp_path / "in.stl", tmp_path / "out.gcode")
    assert argv == ["ps", "--export", "gcode", "--output",
                    str(tmp_path / "out.gcode"), str(tmp_path / "in.stl")]


def test_ensure_valid_mesh_is_opt_in_and_lands_before_export():
    argv = build_argv("ps", Path("a.stl"), Path("b.gcode"), ensure_valid=True)
    assert argv[:2] == ["ps", "--ensure-valid-mesh"]
    assert argv[2:4] == ["--export", "gcode"]


def test_the_timeout_is_passed_through():
    runner = StubRunner()
    check_mesh(cube_mesh(), executable=FAKE_EXE, timeout=7.5, runner=runner)
    assert runner.calls[0]["timeout"] == 7.5


def test_the_mesh_is_written_to_a_private_directory_not_the_users_file():
    from parts.export import to_binary_stl

    runner = StubRunner()
    result = check_mesh(cube_mesh(), executable=FAKE_EXE, runner=runner)
    call = runner.calls[0]
    assert call["input_existed"] is True
    assert call["input_bytes"] == len(to_binary_stl(cube_mesh()))
    assert Path(call["argv"][-1]).parent == Path(call["cwd"])
    assert result.status == OK


def test_the_temporary_directory_is_removed_afterwards():
    runner = StubRunner()
    check_mesh(cube_mesh(), executable=FAKE_EXE, runner=runner)
    assert not Path(runner.calls[0]["cwd"]).exists()


def test_the_temporary_directory_is_removed_even_on_failure():
    runner = StubRunner(returncode=1, stderr=b"boom")
    result = check_mesh(cube_mesh(), executable=FAKE_EXE, runner=runner)
    assert result.status == FAILED
    assert not Path(runner.calls[0]["cwd"]).exists()


# -- environment -----------------------------------------------------------

def test_the_child_does_not_inherit_the_gateways_secrets():
    """The slicer has no use for provider keys, so it does not get them."""
    os.environ["GATEWAY_V9_TEST_FAKE_SECRET"] = "sk-do-not-leak"
    try:
        env = child_env()
        assert "GATEWAY_V9_TEST_FAKE_SECRET" not in env
        assert "PATH" in env
        assert "SystemRoot" in env or "SYSTEMROOT" in env, (
            "Windows needs SystemRoot to load DLLs"
        )
    finally:
        del os.environ["GATEWAY_V9_TEST_FAKE_SECRET"]


# -- outcomes --------------------------------------------------------------

def test_a_clean_slice_is_reported_with_the_gcode_size():
    runner = StubRunner(gcode=b"; generated by prusaslicer\n" * 40)
    result = check_mesh(cube_mesh(), executable=FAKE_EXE, runner=runner)
    assert result.status == OK
    assert result.sliced is True
    assert result.gcode_bytes == 40 * len(b"; generated by prusaslicer\n")
    assert result.returncode == 0
    assert "bytes of G-code" in result.detail
    assert result.to_dict()["argv"][0] == FAKE_EXE


def test_a_non_zero_exit_is_reported_with_the_tail_of_stderr():
    runner = StubRunner(returncode=2, stderr=b"x" * 500 + b"ERROR: broken mesh")
    result = check_mesh(cube_mesh(), executable=FAKE_EXE, runner=runner)
    assert result.status == FAILED
    assert result.returncode == 2
    assert "exited 2" in result.detail
    assert result.detail.endswith("ERROR: broken mesh")
    assert len(result.detail) < 260, "stderr must be truncated, not pasted whole"


def test_exit_zero_with_no_gcode_is_not_reported_as_success():
    """The failure this catches is a slicer that parsed nothing and said nothing."""
    runner = StubRunner(write_gcode=False)
    result = check_mesh(cube_mesh(), executable=FAKE_EXE, runner=runner)
    assert result.status == FAILED
    assert "wrote no G-code" in result.detail
    assert result.gcode_bytes == 0


def test_an_empty_gcode_file_is_not_reported_as_success():
    runner = StubRunner(gcode=b"")
    result = check_mesh(cube_mesh(), executable=FAKE_EXE, runner=runner)
    assert result.status == FAILED
    assert "wrote no G-code" in result.detail


def test_a_hang_is_killed_and_reported():
    def hang(argv, *, timeout, cwd):
        raise subprocess.TimeoutExpired(argv, timeout)

    result = check_mesh(cube_mesh(), executable=FAKE_EXE, timeout=0.25, runner=hang)
    assert result.status == TIMEOUT
    assert result.sliced is False
    assert "did not finish" in result.detail
    assert "killed" in result.detail


def test_the_callable_adapter_matches_the_seam_in_analyse_part():
    runner = StubRunner()
    slicer = slicer_callable(executable=FAKE_EXE, runner=runner)
    body = slicer(cube_mesh())
    assert body["status"] == OK
    assert set(body) == {"status", "detail", "executable", "argv", "duration_ms",
                         "returncode", "gcode_bytes"}


# -- the real subprocess layer --------------------------------------------

def test_the_real_subprocess_layer_reports_a_non_zero_exit():
    """Not a stub: an actual process, actually failing."""
    completed = _run([sys.executable, "-c", "import sys; sys.exit(3)"], timeout=30, cwd=".")
    assert completed.returncode == 3


def test_the_real_subprocess_layer_actually_raises_on_timeout():
    """subprocess.run kills and reaps on timeout; prove it rather than assume."""
    with pytest.raises(subprocess.TimeoutExpired):
        _run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.5, cwd=".")


def test_the_real_subprocess_layer_runs_with_no_shell_and_no_stdin(tmp_path):
    marker = tmp_path / "ran.txt"
    _run([sys.executable, "-c",
          "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('ok')", str(marker)],
         timeout=30, cwd=str(tmp_path))
    assert marker.read_text() == "ok"