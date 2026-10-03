"""Deprecated shim — canonical implementation lives in computer_use.core.daemon.

Kept for backwards-compat (`from computer_use.daemon import ...`).
New code should import from `computer_use.core.daemon`.
"""
from __future__ import annotations

from .core.daemon import (  # noqa: F401
    DaemonError,
    PreconditionError,
    PermissionsError,
    ensure_daemon,
    stop_daemon,
    call,
    capabilities,
    _binary,
    _is_running,
    _pipe_responsive,
)
