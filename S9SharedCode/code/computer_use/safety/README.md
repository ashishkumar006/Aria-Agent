# Computer-use Safety (`computer_use/safety/`)

The gates and permission pre-flight that wrap the destructive/sensitive surface so
the agent can never silently do something irreversible. Apply to **both** the L0
gated-shell path and any layered desktop action.

| File | Responsibility |
|---|---|
| `gates.py` | `SafetyGates` — the gate chain (below); shared instance via `shared_gates`. |
| `permissions.py` | Pre-flight: `check_permissions()` (AX ok? screenshot ok? daemon up? elevated?), `grant_permissions()`, `user_friendly_error()`. |

## Gate chain (in order)
1. **ENABLED** — computer-use is OFF unless `COMPUTER_USE_ENABLED=true`.
2. **MODE** — dry-run (default) describes only; live actually executes.
3. **ALLOW/DENY** — command substrings / paths pinned to lists.
4. **APPROVAL** — sensitive patterns return a pending token; the web UI resolves it (approve/reject) before execution.
5. **AUDIT** — every request + outcome appended to `state/computer_use.log`.

## Notes
- Most Windows apps need no special setup; apps that elevate (installers, system
  settings) require the agent to run elevated too — `permissions.py` reports this.
- The approval store is **in-process** (`tool.py`) so the Computer page and the
  chat/researcher see the same pending approvals.