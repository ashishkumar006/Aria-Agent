"""Remove LLM provider keys from the process environment after start-up.

The gateway builds provider objects at boot; after that nothing needs the raw
provider keys in `os.environ`. Leaving them there is exactly the glc attack:
any in-process code (a channel adapter, a compromised dependency, an
agent-generated import) can read `os.environ["GEMINI_API_KEY"]` with one line.

This module captures the provider-key variables into memory for the
dashboard's presence/count view, registers their values with the redactor, and
deletes them from `os.environ`. Per-user pools already receive keys explicitly
from the encrypted user store, so they are unaffected.

Residual: in-process code can still reach the provider objects themselves
(`app.state.providers[name].api_key`) and any keys still present for channel or
integration services. Process isolation is the durable fix; this removes the
one-line environment read the security exercise demonstrates.
"""
from __future__ import annotations

import os
import re
import threading

PROVIDER_KEY_VARS = (
    "GEMINI_API_KEY", "GEMINI_API_KEYS",
    "NVIDIA_API_KEY", "NVIDIA_API_KEYS",
    "GROQ_API_KEY", "GROQ_API_KEYS",
    "CEREBRAS_API_KEY", "CEREBRAS_API_KEYS",
    "OPEN_ROUTER_API_KEY", "OPEN_ROUTER_API_KEYS",
    "GITHUB_ACCESS_TOKEN", "GITHUB_ACCESS_TOKENS",
    "KILO_API_KEY", "KILO_API_KEYS",
)

_lock = threading.Lock()
_values: dict[str, str] = {}
_scrubbed: set[str] = set()


def capture_and_scrub(names=PROVIDER_KEY_VARS) -> int:
    """Move provider secrets from the environment into this in-memory vault.

    Returns how many variables were scrubbed. Never logs a value.
    """
    count = 0
    with _lock:
        for name in names:
            raw = os.environ.get(name)
            if not raw:
                continue
            _values[name] = raw
            _scrubbed.add(name)
            os.environ.pop(name, None)
            count += 1
    try:
        import redact
        redact.register_many(_values.values())
        for raw in _values.values():
            for part in re.split(r"[,\s]+", raw):
                part = part.strip().strip('"').strip("'")
                if part:
                    redact.register_secret(part)
    except Exception:
        pass
    return count


def remember(pairs: dict[str, str]) -> None:
    """Track keys written at runtime (they still require a restart to build
    new provider objects, but the dashboard should show them)."""
    with _lock:
        for name, value in (pairs or {}).items():
            if str(value or "").strip():
                _values[str(name)] = str(value).strip()
    try:
        import redact
        redact.register_many(_values.values())
        for raw in _values.values():
            for part in re.split(r"[,\s]+", raw):
                part = part.strip().strip('"').strip("'")
                if part:
                    redact.register_secret(part)
    except Exception:
        pass


def forget(names) -> None:
    """Drop captured values, so a key the operator removes really disappears.

    `capture_and_scrub` moves a key out of the environment and into `_values`,
    after which the environment is no longer the whole story: an operator who
    shrinks a pool from two keys to one clears the PLURAL variable from `.env`
    and `os.environ`, but the vault still answers `value(plural)` and
    `present(plural)`. The removed sibling then keeps appearing in
    `/v1/config/keys` pool counts, and any reader that still consults the vault
    would keep using a credential that was deliberately withdrawn.
    """
    with _lock:
        for name in names or ():
            _values.pop(str(name), None)
            _scrubbed.discard(str(name))


def present(name: str) -> bool:
    # Environment wins when it is set (a runtime write or a test fixture
    # overriding the value); the vault is the fallback for scrubbed vars.
    if os.environ.get(name):
        return True
    with _lock:
        return bool(_values.get(name))


def value(name: str) -> str:
    env = os.environ.get(name)
    if env:
        return env
    with _lock:
        return _values.get(name, "")


def values_for(single: str, plural: str) -> list[str]:
    """All keys for a provider, vault-first; preserves env parsing semantics."""
    raw: list[str] = []
    one = value(single).strip()
    if one:
        raw.append(one)
    many = value(plural)
    if many:
        for part in re.split(r"[,\s]+", many):
            part = part.strip().strip('"').strip("'")
            if part:
                raw.append(part)
    seen: set[str] = set()
    out: list[str] = []
    for key in raw:
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def scrub_again(names) -> None:
    """Remove runtime-written provider keys from the environment too."""
    with _lock:
        for name in names or ():
            raw = os.environ.pop(name, None)
            if raw:
                _values[str(name)] = raw
    try:
        import redact
        redact.register_many(_values.values())
        for raw in _values.values():
            for part in re.split(r"[,\s]+", raw):
                part = part.strip().strip('"').strip("'")
                if part:
                    redact.register_secret(part)
    except Exception:
        pass


def restore() -> None:
    """Put captured values back on shutdown.

    Production exits immediately, so this changes nothing there. Test clients
    run several lifespans in one process; without restoring, the first
    startup would leave every later provider-building test with no keys.
    """
    with _lock:
        for name, value in _values.items():
            os.environ.setdefault(name, value)


def scrubbed_names() -> tuple[str, ...]:
    with _lock:
        return tuple(sorted(_scrubbed))
