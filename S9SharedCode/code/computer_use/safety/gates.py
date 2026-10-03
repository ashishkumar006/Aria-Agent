"""safety/gates.py — safety gates for computer-use (charter §13).

These gates wrap the *destructive / sensitive* surface so the agent can
never silently do something irreversible. They apply to BOTH the
gated-shell L0 path and any desktop action the layered engine wants to gate.

Gates (in order):
  1. ENABLED  — computer-use is OFF unless COMPUTER_USE_ENABLED=true.
  2. MODE     — dry-run (default) describes only; live actually executes.
  3. ALLOW/DENY — command substrings / paths can be pinned to lists.
  4. APPROVAL — sensitive patterns return a pending token; the web UI
                resolves it (approve/reject) before execution.
  5. AUDIT    — every request + outcome appended to state/computer_use.log.
"""
from __future__ import annotations

import os
import re as _re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
AUDIT_PATH = ROOT / "state" / "computer_use.log"
AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)

# Patterns that ALWAYS require human approval, even in live mode.
DEFAULT_APPROVAL_PATTERNS = (
    "rm ", "del ", "format", "mkfs", "shutdown", "reboot", "restart",
    "sudo", "runas", "reg ", "sc delete", "taskkill", "net user",
    "curl ", "wget ", "powershell -enc", "Invoke-WebRequest",
    "kill_app", "rmdir", "rd ", "format ", "diskpart",
)
# Paths that are NEVER allowed to be touched (defence in depth).
DEFAULT_DENY_PATHS = (
    "C:\\Windows", "C:\\Program Files", "/etc", "/System",
    os.path.expanduser("~/.ssh"), os.path.expanduser("~/.aws"),
)


@dataclass
class Approval:
    id: str
    action: str
    params: dict
    created: float = field(default_factory=time.time)
    status: str = "pending"  # pending | approved | rejected


class SafetyGates:
    def __init__(self):
        self.enabled = os.environ.get("COMPUTER_USE_ENABLED", "false").lower() == "true"
        self.mode = os.environ.get("COMPUTER_USE_MODE", "dry-run").lower()
        self.allow_cmds = [c for c in os.environ.get("COMPUTER_USE_ALLOW", "").split(";") if c]
        self.deny_cmds = [c for c in os.environ.get("COMPUTER_USE_DENY", "").split(";") if c]
        self.approval_patterns = DEFAULT_APPROVAL_PATTERNS
        self.deny_paths = DEFAULT_DENY_PATHS
        self._approvals: dict[str, Approval] = {}
        self._lock = threading.Lock()
        self._seq = 0

    # Cap: the audit log is append-only by design (compliance trail), but
    # an uncapped file makes export_audit read megabytes per /api/audit
    # call. Trim to the newest lines on each write.
    _AUDIT_MAX_LINES = 20000

    # ── audit ────────────────────────────────────────────
    def _audit(self, action: str, params: dict, outcome: str, ref: str = "") -> None:
        tag = f" [{ref}]" if ref else ""
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {action}{tag} {outcome} {params}\n"
        try:
            with open(AUDIT_PATH, "a", encoding="utf-8") as f:
                f.write(line)
            if AUDIT_PATH.stat().st_size > 4 * 1024 * 1024:
                _lines = AUDIT_PATH.read_text(encoding="utf-8-sig").splitlines()
                AUDIT_PATH.write_text("\n".join(_lines[-self._AUDIT_MAX_LINES:]) + "\n",
                                      encoding="utf-8")
        except OSError:
            pass

    # Secret shapes redacted on export (values only, structure kept).
    _REDACT_KEYS = frozenset({
        "token", "api_key", "apikey", "api-key", "secret", "password",
        "passwd", "pwd", "client_secret", "access_token", "refresh_token",
        "authorization", "auth", "private_key",
    })
    _REDACT_RES = (
        r"sk-[A-Za-z0-9_\-]{8,}",
        r"sk-or-[A-Za-z0-9_\-]+",          # OpenRouter
        r"AIza[0-9A-Za-z_\-]{10,}",        # Google
        r"xox[bpaser]-[A-Za-z0-9\-]+",     # Slack (bot/user/app/refresh)
        r"gh[op]_[A-Za-z0-9_]+",           # GitHub
        r"gsk_[A-Za-z0-9_\-]+",            # Groq
        r"nvapi-[A-Za-z0-9_\-]+",          # NVIDIA
        r"csk-[A-Za-z0-9_\-]+",            # Cerebras
        r"secret_[A-Za-z0-9_\-]+",
    )

    @staticmethod
    def redact(text: str) -> str:
        """Strip secret values from arbitrary text (fail-closed).

        Covers `key=value` / `key: value` / dict-style pairs, `--flag value`
        CLI shapes, `Bearer <token>`, and bare well-known secret shapes.
        """
        import re as _re
        keys = ("token|api_key|apikey|api-key|secret|password|passwd|pwd|"
                "client_secret|access_token|refresh_token|authorization|auth|"
                "private_key")
        red = str(text or "")
        # 1) key=value / key: value / 'key': 'value' (optional quotes).
        red = _re.sub(
            r"(?i)\b(" + keys + r")\b(['\"]?\s*[:=]\s*['\"]?)"
            r"([^\s,'\"]+)",
            lambda m: m.group(0)[:m.start(3) - m.start(0)] + "***REDACTED***",
            red)
        # 2) CLI flags: --password hunter2 / -token abc.
        red = _re.sub(
            r"(?i)(--?(" + keys + r")\s+)([^\s]+)",
            lambda m: m.group(1) + "***REDACTED***",
            red)
        # 3) Bearer tokens.
        red = _re.sub(r"(?i)\bearer\s+[A-Za-z0-9_\-\.~+/=]+",
                      "Bearer ***REDACTED***", red)
        # 4) bare well-known secret shapes.
        for pat in SafetyGates._REDACT_RES:
            red = _re.sub(pat, "***REDACTED***", red)
        return red

    @staticmethod
    def export_audit(redact: bool = True) -> list[str]:
        """Return audit lines for /api/audit and compliance tooling.

        Reads the module-global AUDIT_PATH at call time (tests repoint it).
        With redact=True, secret values are replaced by ***REDACTED***.
        Never raises — returns [] when the log is missing/unreadable.
        """
        try:
            text = AUDIT_PATH.read_text(encoding="utf-8-sig")
        except OSError:
            return []
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if not redact:
            return lines
        return [SafetyGates.redact(ln) for ln in lines]

    # ── gate checks ──────────────────────────────────────
    def needs_approval(self, action: str, params: dict) -> bool:
        import re as _re
        blob = f"{action} {params}".lower()
        for p in self.approval_patterns:
            pat = p.strip().lower()
            if not pat:
                continue
            # Token boundaries are "not a word char AND not a hyphen" on both
            # sides: \b alone treats '-' as a boundary, so "format" would hit
            # "format-table" (a benign PowerShell cmdlet) and "restart" would
            # hit "restart-service". Hyphenated compounds are single tokens.
            rx = r"(?<![\w-])" + _re.escape(pat) + r"(?![\w-])"
            if _re.search(rx, blob):
                return True
            # Fallback: multi-word patterns with internal spaces/punctuation
            # (e.g. "powershell -enc", "sc delete") match as literal phrases.
            if (" " in pat or "-" in pat.strip("-")) and pat in blob:
                return True
        return False

    def path_blocked(self, path: str) -> bool:
        # Case-insensitive on Windows so c:\windows can't bypass C:\Windows.
        p = os.path.abspath(os.path.expanduser(path))
        p_norm = os.path.normcase(p)
        for d in self.deny_paths:
            try:
                d_abs = os.path.normcase(os.path.abspath(os.path.expanduser(d)))
                if p_norm == d_abs or p_norm.startswith(d_abs.rstrip(os.sep) + os.sep):
                    return True
            except Exception:
                continue
        return False

    def cmd_blocked(self, cmd: str) -> str | None:
        c = cmd.lower()
        for d in self.deny_cmds:
            if d and d.lower() in c:
                return f"matches denylist pattern '{d}'"
        for d in DEFAULT_DENY_PATHS:
            if d.lower() in c:
                return f"references protected path '{d}'"
        if self.allow_cmds and not any(a.lower() in c for a in self.allow_cmds):
            return "not on the command allow-list"
        return None

    # ── approval store ────────────────────────────────────
    def create_approval(self, action: str, params: dict) -> str:
        with self._lock:
            self._seq += 1
            aid = f"cu-{int(time.time())}-{self._seq}"
            self._approvals[aid] = Approval(id=aid, action=action, params=params)
        # Tag the line with the approval id so resolve() outcomes and the
        # export can be correlated back to this exact request.
        self._audit(action, params, "pending-approval", ref=aid)
        return aid

    def list_approvals(self) -> list[dict]:
        with self._lock:
            return [{"id": a.id, "action": a.action, "params": a.params,
                     "status": a.status, "created": a.created}
                    for a in self._approvals.values() if a.status == "pending"]

    def resolve(self, approval_id: str, approve: bool) -> dict | None:
        with self._lock:
            a = self._approvals.get(approval_id)
            if not a:
                return None
            # Idempotent: resolving twice returns current state, doesn't flip.
            if a.status != "pending":
                return {"action": a.action, "params": a.params, "approve": a.status == "approved",
                        "status": a.status}
            a.status = "approved" if approve else "rejected"
        # Audit both outcomes (previously only rejections were logged),
        # tagged with the approval id for correlation.
        self._audit(a.action, a.params, a.status, ref=approval_id)
        # Prune old non-pending entries to bound memory (keep last 200).
        try:
            with self._lock:
                if len(self._approvals) > 300:
                    old = sorted(self._approvals.items(), key=lambda kv: kv[1].created)
                    for k, v in old[:100]:
                        if v.status != "pending":
                            del self._approvals[k]
        except Exception:
            pass
        return {"action": a.action, "params": a.params, "approve": approve,
                "status": a.status}


# ── shared singleton ─────────────────────────────────────
_shared_gates: SafetyGates | None = None


def shared_gates() -> SafetyGates:
    global _shared_gates
    if _shared_gates is None:
        _shared_gates = SafetyGates()
    return _shared_gates


def reset_shared_gates() -> None:
    """Drop the cached singleton so the next call re-reads env vars."""
    global _shared_gates
    _shared_gates = None
