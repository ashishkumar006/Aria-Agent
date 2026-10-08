"""Slicability probe: PrusaSlicer CLI as an isolated subprocess.

Deliberately a subprocess and not a library. The only realistic slicer is a
CLI, shelling out to it is how anyone actually checks slicability, and keeping
it out of process means a slicer crash, hang or licence prompt cannot take the
gateway with it. It runs with a timeout, a scrubbed environment, its own
temporary directory and no shell.

Synchronous on purpose. The rest of the pipeline is blocking numpy work, so
the route should call `analyse_part` off the event loop anyway; a sync call
keeps the seam in `analyse_part(slicer=...)` a plain callable and keeps this
module testable without an event loop.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .export import to_binary_stl
from .mesh import Mesh

OK = "ok"
UNAVAILABLE = "unavailable"
FAILED = "failed"
TIMEOUT = "timeout"

DEFAULT_TIMEOUT = 120.0

#: Checked in order. `prusa-slicer` is the Linux package name; the Windows and
#: macOS builds install as `PrusaSlicer.exe` / `prusa-slicer` respectively.
CANDIDATES = ("prusa-slicer", "prusa-slicer-console", "PrusaSlicer", "PrusaSlicer.exe")

#: Passed to the child instead of `os.environ`. The gateway's environment holds
#: provider API keys; a mesh slicer has no use for them, so it does not get
#: them. Windows needs SystemRoot to load DLLs and APPDATA to find settings.
_ENV_ALLOWLIST = (
    "PATH", "SystemRoot", "SYSTEMROOT", "WINDIR", "COMSPEC",
    "TEMP", "TMP", "TMPDIR",
    "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
    "LANG", "LC_ALL", "DISPLAY", "XDG_CONFIG_HOME",
)


@dataclass(frozen=True)
class SlicerResult:
    status: str
    detail: str
    executable: str | None = None
    argv: list[str] | None = None
    duration_ms: int | None = None
    returncode: int | None = None
    gcode_bytes: int | None = None

    @property
    def sliced(self) -> bool:
        return self.status == OK

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "detail": self.detail,
            "executable": self.executable,
            "argv": self.argv,
            "duration_ms": self.duration_ms,
            "returncode": self.returncode,
            "gcode_bytes": self.gcode_bytes,
        }


def child_env() -> dict:
    """A minimal environment for the slicer."""
    return {k: v for k, v in os.environ.items() if k in _ENV_ALLOWLIST}


def find_prusa_slicer(explicit: str | None = None) -> str | None:
    """Resolve the slicer path: explicit override, env var, then PATH."""
    if explicit:
        return explicit if _usable(explicit) else None
    from_env = os.getenv("PRUSA_SLICER_PATH")
    if from_env and _usable(from_env):
        return from_env
    for name in CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    return None


def _usable(path: str) -> bool:
    if os.path.isabs(path) or os.sep in path:
        return Path(path).is_file()
    return shutil.which(path) is not None


def build_argv(
    executable: str,
    input_path: Path,
    output_path: Path,
    *,
    ensure_valid: bool = False,
) -> list[str]:
    """The exact command line.

    `--export gcode` is the slicability claim: PrusaSlicer only produces output
    after it has read the mesh, repaired what it can, built the slices and
    written toolpaths, so a clean exit with a non-empty G-code means the mesh
    survived the whole pipeline. `--ensure-valid-mesh` makes the slicer do its
    own repair pass first, which distinguishes "PrusaSlicer can fix this" from
    "this was already fine"; it is opt-in because it hides the original defect.
    """
    argv = [executable, "--export", "gcode", "--output", str(output_path), str(input_path)]
    if ensure_valid:
        argv.insert(1, "--ensure-valid-mesh")
    return argv


def check_mesh(
    mesh: Mesh,
    *,
    executable: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    ensure_valid: bool = False,
    runner=None,
) -> SlicerResult:
    """Slice `mesh` and report whether a printer could have printed it.

    The mesh is written to a private temporary directory as binary STL rather
    than sliced from wherever the upload landed: the user's file is never
    opened for writing, and the directory is removed whatever happens.
    """
    exe = find_prusa_slicer(executable)
    if exe is None:
        return SlicerResult(
            status=UNAVAILABLE,
            detail=(
                "PrusaSlicer was not found on PATH. Set PRUSA_SLICER_PATH or pass "
                "executable= to enable the slicability check."
            ),
        )

    workdir = Path(tempfile.mkdtemp(prefix="aria-slicer-"))
    try:
        stl = workdir / "part.stl"
        gcode = workdir / "part.gcode"
        stl.write_bytes(to_binary_stl(mesh))
        argv = build_argv(exe, stl, gcode, ensure_valid=ensure_valid)
        call = runner or _run
        started = time.monotonic()
        try:
            completed = call(argv, timeout=timeout, cwd=str(workdir))
        except subprocess.TimeoutExpired:
            return SlicerResult(
                status=TIMEOUT,
                detail=f"PrusaSlicer did not finish within {timeout:g}s and was killed",
                executable=exe, argv=argv,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        duration_ms = int((time.monotonic() - started) * 1000)

        if completed.returncode != 0:
            tail = _tail(completed.stderr)
            return SlicerResult(
                status=FAILED,
                detail=f"PrusaSlicer exited {completed.returncode}: {tail}",
                executable=exe, argv=argv, duration_ms=duration_ms,
                returncode=completed.returncode,
            )
        if not gcode.exists() or gcode.stat().st_size == 0:
            return SlicerResult(
                status=FAILED,
                detail=(
                    "PrusaSlicer exited 0 but wrote no G-code, so it did not "
                    f"actually slice. {_tail(completed.stderr)}"
                ).strip(),
                executable=exe, argv=argv, duration_ms=duration_ms,
                returncode=completed.returncode, gcode_bytes=0,
            )
        size = gcode.stat().st_size
        return SlicerResult(
            status=OK,
            detail=f"sliced to {size} bytes of G-code in {duration_ms}ms",
            executable=exe, argv=argv, duration_ms=duration_ms,
            returncode=0, gcode_bytes=size,
        )
    finally:
        # shutil.rmtree on a directory a subprocess may still be writing to
        # raises on Windows; ignore_errors keeps cleanup best-effort and, since
        # the child has already exited or been killed, nothing is left running.
        shutil.rmtree(workdir, ignore_errors=True)


def _run(argv: list[str], *, timeout: float, cwd: str):
    """The real subprocess call. Replaced in tests by a stub."""
    return subprocess.run(  # noqa: S603 - fixed argv, shell=False, no user input
        argv,
        capture_output=True,
        timeout=timeout,
        cwd=cwd,
        env=child_env(),
        stdin=subprocess.DEVNULL,
        shell=False,
        check=False,
    )


def _tail(raw, limit: int = 200) -> str:
    text = (raw or b"").decode("utf-8", "replace").strip()
    return text[-limit:] if len(text) > limit else text


def slicer_callable(**kwargs):
    """Adapt `check_mesh` to the `slicer=` seam in `analyse_part`."""

    def _run(mesh: Mesh) -> dict:
        return check_mesh(mesh, **kwargs).to_dict()

    return _run


__all__ = [
    "OK", "UNAVAILABLE", "FAILED", "TIMEOUT",
    "SlicerResult",
    "build_argv",
    "check_mesh",
    "child_env",
    "find_prusa_slicer",
    "slicer_callable",
]