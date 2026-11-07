"""Admin API for multi-user gateway credentials.

Only the operator (the gateway token) can create users, rotate tokens, or add
and remove keys. A user token can call `GET /v1/users/me` so an agent can
verify the credential it was given, and nothing else here.

No route ever returns a provider key or a complete user token. A new token is
returned exactly once, from create/rotate, because only its hash is stored.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request

from user_store import PROVIDERS, UserStoreError

router = APIRouter(prefix="/v1/users", tags=["users"])


def _store(request: Request):
    store = getattr(request.app.state, "user_store", None)
    if store is None:
        raise HTTPException(503, "user store is not initialised")
    return store


def _auth(request: Request):
    return getattr(request.state, "auth", None)


def _admin(request: Request):
    auth = _auth(request)
    if auth is None or not auth.admin:
        raise HTTPException(
            403, "the users API requires the operator gateway token")
    return auth


def _audit(request: Request, event: str, *, user_id: str = "",
           **detail) -> None:
    try:
        import security_audit
        security_audit.record(
            event, actor="operator", user_id=user_id,
            ip=str(getattr(request.client, "host", "") or "")[:80],
            path=str(request.url.path)[:200], **detail)
    except Exception:
        pass


def _wrap(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except UserStoreError as e:
        raise HTTPException(400, str(e)) from e


@router.get("")
async def list_users(request: Request, _=Depends(_admin)):
    store = _store(request)
    return {"users": store.list_public()}


@router.post("")
async def create_user(request: Request, body: dict, _=Depends(_admin)):
    store = _store(request)
    user, token = _wrap(store.create, (body or {}).get("name") or "")
    _audit(request, "user_created", user_id=user["id"],
           target=str(user.get("name") or ""))
    # The only time the plaintext token exists in a response.
    return {"user": user, "token": token}


@router.get("/me")
async def me(request: Request):
    auth = _auth(request)
    if auth is None:
        raise HTTPException(401, "not authenticated")
    store = _store(request)
    if auth.user_id:
        user = store.get(auth.user_id)
        if user is None:
            raise HTTPException(404, "user no longer exists")
        return {"kind": "user", "user": store.public(user)}
    return {"kind": "operator", "user": None}


@router.get("/providers")
async def providers(request: Request, _=Depends(_admin)):
    """Providers this gateway can actually reach.

    Offering nvidia to a user on an operator pool where nvidia was disabled
    would save a key that can never be routed. Canonical base names only;
    key-pool siblings (`gemini-2`) collapse to `gemini`.
    """
    import re
    wired: set[str] = set()
    for pool in (getattr(request.app.state, "providers", {}) or {},
                 getattr(request.app.state, "router_providers", {}) or {}):
        for name in pool:
            wired.add(re.sub(r"-\d+$", "", str(name)))
    return {"providers": [p for p in PROVIDERS if p in wired]}


@router.get("/{user_id}")
async def get_user(user_id: str, request: Request, _=Depends(_admin)):
    store = _store(request)
    user = store.get(user_id)
    if user is None:
        raise HTTPException(404, "unknown user")
    return {"user": store.public(user)}


@router.patch("/{user_id}")
async def update_user(user_id: str, request: Request, body: dict,
                      _=Depends(_admin)):
    store = _store(request)
    body = body or {}
    user = _wrap(store.update, user_id,
                 name=body.get("name") if "name" in body else None,
                 disabled=(bool(body["disabled"])
                           if "disabled" in body else None))
    request.app.state.user_pools.invalidate(user_id)
    _audit(request, "user_updated", user_id=user_id,
           target=json.dumps({k: body[k] for k in body
                              if k in ("name", "disabled")}))
    return {"user": user}


@router.get("/{user_id}/usage")
async def user_usage(user_id: str, request: Request, days: int = 30,
                     _=Depends(_admin)):
    store = _store(request)
    if store.get(user_id) is None:
        raise HTTPException(404, "unknown user")
    import db
    return db.user_usage(user_id, days)


@router.delete("/{user_id}")
async def delete_user(user_id: str, request: Request, _=Depends(_admin)):
    store = _store(request)
    if not store.delete(user_id):
        raise HTTPException(404, "unknown user")
    request.app.state.user_pools.invalidate(user_id)
    _audit(request, "user_deleted", user_id=user_id)
    return {"ok": True}


@router.post("/{user_id}/token")
async def rotate_token(user_id: str, request: Request, _=Depends(_admin)):
    store = _store(request)
    user, token = _wrap(store.rotate_token, user_id)
    _audit(request, "token_rotated", user_id=user_id)
    return {"user": user, "token": token}


@router.post("/{user_id}/keys")
async def add_key(user_id: str, request: Request, body: dict,
                  _=Depends(_admin)):
    store = _store(request)
    body = body or {}
    user = _wrap(store.add_key, user_id, body.get("provider") or "",
                 body.get("key") or "", body.get("label") or "")
    request.app.state.user_pools.invalidate(user_id)
    _audit(request, "key_added", user_id=user_id,
           target=str(body.get("provider") or ""))
    return {"user": user}


@router.delete("/{user_id}/keys/{key_id}")
async def remove_key(user_id: str, key_id: str, request: Request,
                     _=Depends(_admin)):
    store = _store(request)
    user = _wrap(store.remove_key, user_id, key_id)
    request.app.state.user_pools.invalidate(user_id)
    _audit(request, "key_removed", user_id=user_id, target=key_id)
    return {"user": user}
