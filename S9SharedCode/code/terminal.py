"""Integrated terminal: a real PTY session behind a WebSocket.

What this is
------------
VS Code's terminal, and what the user asked for: a shell at the bottom of the
Code view that can run anything. The shell has to run *somewhere*; it runs in
the agent process, which is what keeps the feature working in the browser dev
surface as well as in the packaged Electron app. (node-pty in Electron would
only ever work in the packaged app.)

Why a PTY and not a pipe
------------------------
A pipe is not a terminal. With `subprocess.PIPE`, `isatty()` is false, the
shell runs non-interactively and never prints a prompt, and tools silently drop
colour and line editing (`ls --color=auto`, `git`, REPLs). That is a "run
command" box, not a terminal. A PTY is the whole point, so this drives one.

Platform split
--------------
Windows has no PTY in the stdlib. ConPTY exists but is only reachable through
it from the console API, which CPython does not expose; `pywinpty` wraps it.
POSIX has `pty`/`os.openpty`, so the shell is driven directly there. Both paths
present the same small surface the WebSocket handler uses: `read`, `write`,
`resize`, `is_alive`, `close`.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

IS_WINDOWS = sys.platform.startswith("win")


def default_shell() -> list[str]:
    """The argv for the user's shell.

    Windows: prefer PowerShell 7 (`pwsh`) over Windows PowerShell, matching the
    default a developer already expects, and fall back to `%COMSPEC%` so a
    stripped machine still gets *a* shell. POSIX: `$SHELL`, then `/bin/bash`.
    """
    if IS_WINDOWS:
        for exe in ("pwsh.exe", "powershell.exe"):
            found = shutil.which(exe)
            if found:
                return [found, "-NoLogo"]
        return [os.environ.get("COMSPEC", "cmd.exe")]
    return [os.environ.get("SHELL") or "/bin/bash"]


class PtySession:
    """One interactive shell, exposed as a byte pipe with a window size."""

    def __init__(self, argv: list[str], cwd: str | None = None,
                 env: dict | None = None, cols: int = 80, rows: int = 24):
        self.cols = int(cols)
        self.rows = int(rows)
        self._closed = False
        env = dict(env if env is not None else os.environ)
        env.setdefault("TERM", "xterm-256color")
        if IS_WINDOWS:
            self._spawn_windows(argv, cwd, env)
        else:
            self._spawn_posix(argv, cwd, env)

    # ── construction ────────────────────────────────────────────────────
    def _spawn_windows(self, argv, cwd, env):
        import winpty
        self._proc = winpty.PtyProcess.spawn(
            argv, cwd=cwd or None, env=env,
            dimensions=(self.rows, self.cols))

    def _spawn_posix(self, argv, cwd, env):
        import pty
        master, slave = pty.openpty()
        self._proc = subprocess.Popen(
            argv, stdin=slave, stdout=slave, stderr=slave,
            cwd=cwd or None, env=env, close_fds=True,
            preexec_fn=os.setsid)
        os.close(slave)
        self._fd = master

    # ── io ──────────────────────────────────────────────────────────────
    def read(self, n: int = 4096) -> bytes:
        """Blocking read. Returns b"" at end of stream."""
        if self._closed:
            return b""
        if IS_WINDOWS:
            try:
                data = self._proc.read(n)
            except (EOFError, OSError):
                return b""
            if isinstance(data, str):
                return data.encode("utf-8", "replace")
            return data or b""
        try:
            return os.read(self._fd, n)
        except OSError:
            # EIO on the master fd is the POSIX "the child is gone" signal.
            return b""

    def write(self, data: bytes) -> None:
        if self._closed or not data:
            return
        if IS_WINDOWS:
            # pywinpty 3.x is asymmetric: `read()` returns bytes, but
            # `write()` forwards to a str-only backend and raises
            # `TypeError: 'bytes' object is not an instance of 'str'` on
            # bytes. Decode here; terminal input is UTF-8 (a stray invalid
            # byte from a paste degrades to U+FFFD rather than raising).
            self._proc.write(data.decode("utf-8", "replace"))
        else:
            os.write(self._fd, data)

    def resize(self, cols: int, rows: int) -> None:
        self.cols, self.rows = int(cols), int(rows)
        if self._closed:
            return
        try:
            if IS_WINDOWS:
                self._proc.setwinsize(self.rows, self.cols)
            else:
                import fcntl
                import struct
                import termios
                fcntl.ioctl(self._fd, termios.TIOCSWINSZ,
                            struct.pack("HHHH", self.rows, self.cols, 0, 0))
        except Exception:
            # A resize racing process death is not worth a traceback.
            pass

    def is_alive(self) -> bool:
        if self._closed:
            return False
        try:
            if IS_WINDOWS:
                return bool(self._proc.isalive())
            return self._proc.poll() is None
        except Exception:
            return False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if IS_WINDOWS:
                self._proc.terminate(force=True)
            else:
                self._proc.terminate()
                os.close(self._fd)
        except Exception:
            pass
