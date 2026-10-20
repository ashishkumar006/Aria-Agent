"""Resource and process-tree boundary for commands the agent runs.

The agent runs three kinds of code: the project's own checks (`run_checks`),
a PowerShell command (`run_command`), and Python emitted by the Coder skill
(`run_python`, used by sandbox_executor). All are arbitrary code, so none may
run unbounded. This module gives them one executor with:

  * a hard wall-clock timeout, after which the whole process TREE is killed
    (PowerShell starts native commands as children; killing only the shell
    leaves `pytest`/`npm` running);
  * a Windows Job Object with kill-on-close, per-process and job memory
    limits, and an active-process cap, so a fork bomb or a runaway test
    cannot take the machine down;
  * on POSIX, a new session/process group, killed with SIGKILL;
  * a bounded output buffer that keeps draining the pipes (a command that
    prints a gigabyte must not deadlock or OOM this server);
  * an environment scrub that removes credential-shaped variables before the
    child starts (see `clean_env`).

What this module does NOT do: it is not a filesystem or network jail. The
mcp-level command guard refuses common destructive and credential-read shapes,
but it is a heuristic, not a kernel boundary; network access remains possible.
That boundary belongs to the approval gate (run_command is gated like a write)
and, for hard isolation, to a container/AppContainer backend. Pretending a Job
Object provides that would be worse than saying so.
"""
from __future__ import annotations

import ctypes
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT_S = 120.0
MAX_TIMEOUT_S = 900.0
DEFAULT_MAX_OUTPUT = 64_000

#: The sandbox_executor contract predates `run`; keep its defaults so existing
#: callers and prompts do not silently change behaviour.
PYTHON_TIMEOUT_S = 30
PYTHON_STDOUT_CAP = 1_000_000
PYTHON_STDERR_CAP = 1_000_000
PYTHON_ENV_WHITELIST = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE")

#: Same rule the check subprocess used before this module existed: any
#: credential-shaped name, plus this app's own ARIA_* configuration.
_SECRET_ENV_RE = re.compile(
    r"(TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|API_?KEY|PRIVATE_?KEY|"
    r"^AUTH|_AUTH$|AUTHORIZATION|COOKIE)", re.IGNORECASE)

KILL_ON_CLOSE = 0x00002000
LIMIT_ACTIVE_PROCESS = 0x00000008
LIMIT_PROCESS_MEMORY = 0x00000100
LIMIT_JOB_MEMORY = 0x00000200


def clean_env(extra: dict | None = None) -> dict:
    """The operator's environment with credentials and ARIA_* config removed."""
    env = {k: v for k, v in os.environ.items()
           if not _SECRET_ENV_RE.search(k) and not k.startswith("ARIA_")}
    env["PYTHONIOENCODING"] = "utf-8"
    if extra:
        for k, v in extra.items():
            if not _SECRET_ENV_RE.search(k) and not str(k).startswith("ARIA_"):
                env[str(k)] = str(v)
    return env


@dataclass
class ExecResult:
    ok: bool
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False
    truncated: bool = False
    backend: str = "process"
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
            "timed_out": self.timed_out,
            "truncated": self.truncated,
            "backend": self.backend,
            "error": self.error,
        }


if os.name == "nt":
    from ctypes import wintypes as _wt

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _BASIC_LIMIT(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class _EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BASIC_LIMIT),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    _kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    _kernel32.SetInformationJobObject.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    _kernel32.SetInformationJobObject.restype = _wt.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _kernel32.AssignProcessToJobObject.restype = _wt.BOOL
    _kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    _kernel32.TerminateJobObject.restype = _wt.BOOL
    _kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    _kernel32.CloseHandle.restype = _wt.BOOL

    def _new_job(timeout_s: float) -> int | None:
        job = _kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _EXTENDED_LIMIT()
        info.BasicLimitInformation.LimitFlags = (
            KILL_ON_CLOSE | LIMIT_ACTIVE_PROCESS | LIMIT_PROCESS_MEMORY
            | LIMIT_JOB_MEMORY)
        info.BasicLimitInformation.ActiveProcessLimit = int(
            os.environ.get("ARIA_SANDBOX_PROCESSES", "24") or 24)
        mb = int(os.environ.get("ARIA_SANDBOX_MEMORY_MB", "1024") or 1024)
        info.ProcessMemoryLimit = mb * 1024 * 1024
        info.JobMemoryLimit = mb * 2 * 1024 * 1024
        if not _kernel32.SetInformationJobObject(
                job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            _kernel32.CloseHandle(job)
            return None
        return job

    def _assign(job: int | None, proc: subprocess.Popen) -> bool:
        if not job:
            return False
        try:
            return bool(_kernel32.AssignProcessToJobObject(
                job, ctypes.c_void_p(int(proc._handle))))
        except Exception:
            return False

    def _terminate(job: int | None, proc: subprocess.Popen) -> None:
        if job:
            try:
                _kernel32.TerminateJobObject(job, 1)
            except Exception:
                pass
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
        # Belt and braces: the shell is gone but a grandchild may have escaped
        # the job before it was assigned.
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:
            pass

    def _close(job: int | None) -> None:
        if job:
            try:
                _kernel32.CloseHandle(job)
            except Exception:
                pass


def _spawn(argv: list[str], cwd: str, env: dict):
    if os.name == "nt":
        flags = 0
        flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return subprocess.Popen(
            argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=flags)
    return subprocess.Popen(
        argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True)


def _drain(pipe, sink: bytearray, cap: int, state: dict) -> None:
    try:
        while True:
            chunk = pipe.read(8192)
            if not chunk:
                break
            state["total"] += len(chunk)
            room = cap - len(sink)
            if room > 0:
                sink.extend(chunk[:room])
    except Exception:
        pass
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def run(argv: list[str], cwd: str | Path, timeout_s: float = DEFAULT_TIMEOUT_S,
        env: dict | None = None, max_output: int = DEFAULT_MAX_OUTPUT) -> ExecResult:
    """Run `argv` in `cwd` under the resource boundary and return the result."""
    t0 = time.monotonic()
    cwd = str(cwd)
    env = env if env is not None else clean_env()
    to = max(1.0, min(float(timeout_s or DEFAULT_TIMEOUT_S), MAX_TIMEOUT_S))
    max_output = max(1024, int(max_output or DEFAULT_MAX_OUTPUT))

    try:
        proc = _spawn(list(argv), cwd, env)
    except FileNotFoundError as e:
        return ExecResult(False, None, "", "", 0, error=str(e))
    except Exception as e:
        return ExecResult(False, None, "", "", 0,
                          error=f"{type(e).__name__}: {e}")

    job = _new_job(to) if os.name == "nt" else None
    if os.name == "nt" and not _assign(job, proc):
        _close(job)
        job = None

    out, err = bytearray(), bytearray()
    state = {"total_out": 0, "total_err": 0}
    readers = [
        threading.Thread(target=_drain, args=(proc.stdout, out, max_output,
                                             {"total": 0}), daemon=True),
        threading.Thread(target=_drain, args=(proc.stderr, err, max_output,
                                             {"total": 0}), daemon=True),
    ]
    for t in readers:
        t.start()

    timed_out = False
    try:
        proc.wait(timeout=to)
    except subprocess.TimeoutExpired:
        timed_out = True
        if os.name == "nt":
            _terminate(job, proc)
        else:
            try:
                os.killpg(proc.pid, 9)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        try:
            proc.wait(timeout=10)
        except Exception:
            pass

    # Kill any descendant the shell left behind before closing the pipe: on
    # Windows closing the job does that; on POSIX the session kill did.
    if os.name == "nt":
        _close(job)
    else:
        try:
            os.killpg(proc.pid, 9)
        except Exception:
            pass

    deadline = time.monotonic() + 2.0
    for t in readers:
        t.join(timeout=max(0.05, deadline - time.monotonic()))

    out_s = bytes(out).decode("utf-8", errors="replace")
    err_s = bytes(err).decode("utf-8", errors="replace")
    truncated = len(out) >= max_output or len(err) >= max_output
    rc = proc.returncode
    duration = int((time.monotonic() - t0) * 1000)
    ok = (not timed_out) and rc == 0
    error = ""
    if timed_out:
        error = f"timed out after {to:g}s and was terminated"
    elif rc not in (0, None):
        error = ""
    return ExecResult(ok, rc, out_s, err_s, duration, timed_out=timed_out,
                      truncated=truncated,
                      backend="jobobject" if job else "process", error=error)


def _cap_text(text: str, cap: int) -> tuple[str, bool]:
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= cap:
        return text, False
    head = raw[: max(0, cap - 200)].decode("utf-8", errors="replace")
    return (head + f"\n...[truncated; {len(raw) - cap + 200} more bytes]...",
            True)


def run_python(
    code: str,
    *,
    timeout_s: int = PYTHON_TIMEOUT_S,
    stdout_cap: int = PYTHON_STDOUT_CAP,
    stderr_cap: int = PYTHON_STDERR_CAP,
    env_whitelist: tuple[str, ...] = PYTHON_ENV_WHITELIST,
    extra_env: dict[str, str] | None = None,
) -> dict:
    """Execute Python in a temporary directory under the shared boundary.

    This is the original sandbox_executor contract, now running on `run()`:
    the child gets a fresh cwd, a whitelisted environment, a wall-clock
    timeout, and the Job Object / process-group kill. The return shape is
    unchanged: exit code, bounded stdout/stderr, files written, timeout flag.
    The temporary directory is deleted before returning; `files_written`
    carries names and sizes only.
    """
    scrubbed = {k: os.environ[k] for k in env_whitelist if k in os.environ}
    scrubbed["PYTHONIOENCODING"] = "utf-8"
    if extra_env:
        scrubbed.update({str(k): str(v) for k, v in extra_env.items()})

    with tempfile.TemporaryDirectory(prefix="s8sandbox-") as cwd:
        workdir = Path(cwd)
        (workdir / "main.py").write_text(code, encoding="utf-8")
        to = max(1.0, float(timeout_s or PYTHON_TIMEOUT_S))
        res = run([sys.executable, str(workdir / "main.py")], cwd=workdir,
                  timeout_s=to, env=scrubbed,
                  max_output=max(stdout_cap, stderr_cap))

        stdout, so_trunc = _cap_text(res.stdout, stdout_cap)
        stderr = res.stderr
        if res.timed_out:
            stderr += f"\n[sandbox] killed after {timeout_s}s wall-clock"
        stderr, se_trunc = _cap_text(stderr, stderr_cap)

        files = []
        for p in sorted(workdir.iterdir()):
            if p.name == "main.py":
                continue
            try:
                files.append({"name": p.name, "size_bytes": p.stat().st_size})
            except OSError:
                continue

        return {
            "exit_code": -1 if res.timed_out else (
                res.exit_code if res.exit_code is not None else -1),
            "stdout": stdout,
            "stdout_truncated": so_trunc,
            "stderr": stderr,
            "stderr_truncated": se_trunc,
            "files_written": files,
            "timed_out": res.timed_out,
            "timeout_s": timeout_s,
            "cwd": str(workdir),
        }
