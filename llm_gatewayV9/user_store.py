"""Per-user gateway credentials: identities, tokens and provider keys.

The gateway used to be a single-operator service: one `.env`, one key pool,
one rate budget. A second person could either share that pool (and silently
spend the operator's quota) or could not use the gateway at all.

This store makes a user first-class. Each user has:

  * a generated bearer token whose HASH is the only thing stored - the token is
    shown once, at creation or rotation;
  * named provider keys (one pool per provider, so the existing `_fan_out`
    rotation works per user);
  * the keys encrypted at rest with Fernet, under a local master key that is
    never returned by any API.

Identity is resolved on every request by `gateway_auth`, from
`X-Aria-User-Token`. The operator's gateway token remains the admin
credential; a user token can call inference routes but never the users API.

Deliberately NOT stored here: the plaintext user token, plaintext provider
keys after the write, or a key's full value in any response. A four-character
suffix is kept as a non-secret hint so the UI can tell two keys apart.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from pathlib import Path

import atomic_json

STATE = Path(__file__).resolve().parent / "state"
DEFAULT_PATH = STATE / "users.json"
DEFAULT_MASTER = STATE / "user_keys.key"
PROVIDERS = ("gemini", "nvidia", "groq", "cerebras", "openrouter",
             "github", "kilo")
TOKEN_PREFIX = "aru_"
_MASTER_ENV = "GATEWAY_USER_KEYS_SECRET"


class UserStoreError(RuntimeError):
    """A user/key operation that cannot be completed as asked."""


def _now() -> float:
    return time.time()


def _token_hash(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def mask_secret(value: str) -> str:
    """A non-secret hint: enough to identify a key, never enough to use it."""
    s = str(value or "")
    if len(s) <= 8:
        return "•" * 8
    return f"{s[:4]}…{s[-4:]}"


def _token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(30)


def _user_id() -> str:
    return "u-" + secrets.token_hex(5)


def _key_id() -> str:
    return "k-" + secrets.token_hex(4)


class UserStore:
    def __init__(self, path: str | Path | None = None,
                 master_path: str | Path | None = None):
        self.path = Path(path) if path else DEFAULT_PATH
        self.master_path = Path(master_path) if master_path else DEFAULT_MASTER
        self._lock = threading.RLock()
        self._fernet = None

    # ── persistence ───────────────────────────────────────────────────────
    def _load(self) -> dict:
        data = atomic_json.load_json(self.path, {})
        if not isinstance(data, dict) or not isinstance(data.get("users"), list):
            return {"version": 1, "users": []}
        return data

    def _save(self, data: dict) -> None:
        atomic_json.save_json(self.path, data)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def _cipher(self):
        if self._fernet is not None:
            return self._fernet
        from cryptography.fernet import Fernet
        raw = (os.getenv(_MASTER_ENV) or "").strip()
        if raw:
            key = raw.encode("utf-8")
        else:
            try:
                key = self.master_path.read_bytes().strip()
            except OSError:
                key = b""
            if not key:
                key = Fernet.generate_key()
                self.master_path.parent.mkdir(parents=True, exist_ok=True)
                self.master_path.write_bytes(key)
                try:
                    self.master_path.chmod(0o600)
                except OSError:
                    pass
        try:
            self._fernet = Fernet(key)
        except Exception as e:
            raise UserStoreError(
                f"invalid master key ({_MASTER_ENV} or {self.master_path.name}): "
                f"{type(e).__name__}") from e
        return self._fernet

    def _encrypt(self, plain: str) -> str:
        return self._cipher().encrypt(plain.encode("utf-8")).decode("ascii")

    def _decrypt(self, token: str) -> str:
        from cryptography.fernet import InvalidToken
        try:
            return self._cipher().decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError) as e:
            raise UserStoreError(
                "a stored provider key cannot be decrypted with the current "
                "master key; re-add the key (rotation or a lost master key "
                "invalidates old ciphertext)") from e

    # ── lookup ────────────────────────────────────────────────────────────
    def _find(self, data: dict, user_id: str) -> dict | None:
        return next((u for u in data["users"]
                     if isinstance(u, dict) and u.get("id") == user_id), None)

    def _find_token(self, data: dict, token: str) -> dict | None:
        digest = _token_hash(token)
        for u in data.get("users") or []:
            stored = str(u.get("token_hash") or "")
            if stored and hmac.compare_digest(stored, digest):
                return u
        return None

    def user_id_for_token(self, token: str) -> str | None:
        token = str(token or "").strip()
        if not token.startswith(TOKEN_PREFIX):
            return None
        with self._lock:
            user = self._find_token(self._load(), token)
            if not user or user.get("disabled"):
                return None
            return str(user.get("id") or "") or None

    def get(self, user_id: str) -> dict | None:
        with self._lock:
            return self._find(self._load(), user_id)

    # ── public views ──────────────────────────────────────────────────────
    def public(self, user: dict) -> dict:
        keys = []
        for provider, rows in (user.get("keys") or {}).items():
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                keys.append({
                    "id": row.get("id"),
                    "provider": provider,
                    "label": row.get("label") or "",
                    "masked": row.get("hint") or "••••••••",
                    "created_at": row.get("created_at"),
                })
        return {
            "id": user.get("id"),
            "name": user.get("name") or "",
            "created_at": user.get("created_at"),
            "updated_at": user.get("updated_at"),
            "disabled": bool(user.get("disabled")),
            "token_hint": user.get("token_hint") or "",
            "keys": keys,
        }

    def list_public(self) -> list[dict]:
        with self._lock:
            return [self.public(u) for u in self._load()["users"]]

    # ── users ─────────────────────────────────────────────────────────────
    def create(self, name: str) -> tuple[dict, str]:
        name = " ".join(str(name or "").split())[:80]
        if not name:
            raise UserStoreError("a user needs a name")
        token = _token()
        now = _now()
        user = {
            "id": _user_id(), "name": name,
            "token_hash": _token_hash(token),
            "token_hint": "…" + token[-4:],
            "created_at": now, "updated_at": now,
            "disabled": False, "keys": {},
        }
        with self._lock:
            data = self._load()
            data["users"].append(user)
            self._save(data)
        return self.public(user), token

    def rotate_token(self, user_id: str) -> tuple[dict, str]:
        token = _token()
        with self._lock:
            data = self._load()
            user = self._find(data, user_id)
            if user is None:
                raise UserStoreError("unknown user")
            user["token_hash"] = _token_hash(token)
            user["token_hint"] = "…" + token[-4:]
            user["updated_at"] = _now()
            self._save(data)
            return self.public(user), token

    def update(self, user_id: str, *, name: str | None = None,
               disabled: bool | None = None) -> dict:
        with self._lock:
            data = self._load()
            user = self._find(data, user_id)
            if user is None:
                raise UserStoreError("unknown user")
            if name is not None:
                cleaned = " ".join(str(name).split())[:80]
                if not cleaned:
                    raise UserStoreError("a user needs a name")
                user["name"] = cleaned
            if disabled is not None:
                user["disabled"] = bool(disabled)
            user["updated_at"] = _now()
            self._save(data)
            return self.public(user)

    def delete(self, user_id: str) -> bool:
        with self._lock:
            data = self._load()
            before = len(data["users"])
            data["users"] = [u for u in data["users"]
                             if not (isinstance(u, dict)
                                     and u.get("id") == user_id)]
            if len(data["users"]) == before:
                return False
            self._save(data)
            return True

    # ── keys ──────────────────────────────────────────────────────────────
    def add_key(self, user_id: str, provider: str, key: str,
                label: str = "") -> dict:
        provider = str(provider or "").strip().lower()
        if provider not in PROVIDERS:
            raise UserStoreError(
                f"unknown provider {provider!r}; choose one of "
                f"{', '.join(PROVIDERS)}")
        plain = str(key or "").strip()
        if "\n" in plain or "\r" in plain or len(plain) < 8:
            raise UserStoreError("that does not look like an API key")
        with self._lock:
            data = self._load()
            user = self._find(data, user_id)
            if user is None:
                raise UserStoreError("unknown user")
            rows = user.setdefault("keys", {}).setdefault(provider, [])
            # A duplicate is almost always a paste of the same key under a
            # second label; compare decrypted values so it is caught.
            for row in rows:
                if row.get("secret") and hmac.compare_digest(
                        self._decrypt(row["secret"]), plain):
                    raise UserStoreError(
                        "this key is already saved for this user")
            rows.append({
                "id": _key_id(), "label": " ".join(str(label or "").split())[:60],
                "secret": self._encrypt(plain), "hint": mask_secret(plain),
                "created_at": _now(),
            })
            user["updated_at"] = _now()
            self._save(data)
            return self.public(user)

    def remove_key(self, user_id: str, key_id: str) -> dict:
        with self._lock:
            data = self._load()
            user = self._find(data, user_id)
            if user is None:
                raise UserStoreError("unknown user")
            found = False
            for provider, rows in list((user.get("keys") or {}).items()):
                keep = [r for r in rows if r.get("id") != key_id]
                if len(keep) != len(rows):
                    found = True
                if keep:
                    user["keys"][provider] = keep
                else:
                    user["keys"].pop(provider, None)
            if not found:
                raise UserStoreError("unknown key")
            user["updated_at"] = _now()
            self._save(data)
            return self.public(user)

    def provider_keys(self, user_id: str) -> dict[str, list[str]]:
        """Decrypted pools, canonical provider name → keys in saved order."""
        with self._lock:
            user = self._find(self._load(), user_id)
            if user is None or user.get("disabled"):
                return {}
            out: dict[str, list[str]] = {}
            for provider, rows in (user.get("keys") or {}).items():
                values = [self._decrypt(r["secret"]) for r in rows or []
                          if isinstance(r, dict) and r.get("secret")]
                if values:
                    out[str(provider)] = values
            return out

    def signature(self, user_id: str) -> str:
        """A cache key that changes when any key is added, removed or rotated."""
        keys = self.provider_keys(user_id)
        h = hashlib.sha256()
        for provider in sorted(keys):
            h.update(provider.encode())
            for k in keys[provider]:
                h.update(b"\x00")
                h.update(hashlib.sha256(k.encode()).digest())
        return h.hexdigest()
