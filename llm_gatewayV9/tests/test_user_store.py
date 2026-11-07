"""Multi-user credentials: storage, isolation, auth precedence, API surface."""
import asyncio
import json
from pathlib import Path

import pytest

import gateway_auth
import user_store
from user_store import UserStore, UserStoreError


@pytest.fixture
def store(tmp_path):
    return UserStore(tmp_path / "users.json", tmp_path / "master.key")


def test_a_token_is_returned_once_and_only_its_hash_is_stored(store):
    user, token = store.create("Ada")
    assert token.startswith("aru_")
    assert user["token_hint"].endswith(token[-4:])
    raw = (store.path).read_text(encoding="utf-8")
    assert token not in raw
    assert user["id"] in raw
    assert store.user_id_for_token(token) == user["id"]
    assert store.user_id_for_token(token + "x") is None


def test_rotation_invalidates_the_old_token(store):
    user, old = store.create("Ada")
    _, new = store.rotate_token(user["id"])
    assert store.user_id_for_token(old) is None
    assert store.user_id_for_token(new) == user["id"]


def test_keys_are_encrypted_and_masked(store):
    user, _ = store.create("Ada")
    public = store.add_key(user["id"], "gemini", "sk-super-secret-key-1234",
                           "work")
    assert public["keys"][0]["masked"].startswith("sk-s")
    assert "sk-super-secret-key-1234" not in json.dumps(public)
    raw = store.path.read_text(encoding="utf-8")
    assert "sk-super-secret-key-1234" not in raw
    assert store.provider_keys(user["id"]) == {
        "gemini": ["sk-super-secret-key-1234"]}


def test_a_duplicate_key_is_rejected(store):
    user, _ = store.create("Ada")
    store.add_key(user["id"], "gemini", "sk-super-secret-key-1234")
    with pytest.raises(UserStoreError, match="already saved"):
        store.add_key(user["id"], "gemini", "sk-super-secret-key-1234",
                      "again")


def test_removing_a_key_and_disabling_a_user(store):
    user, token = store.create("Ada")
    pub = store.add_key(user["id"], "gemini", "sk-super-secret-key-1234")
    key_id = pub["keys"][0]["id"]
    pub = store.remove_key(user["id"], key_id)
    assert pub["keys"] == []
    store.update(user["id"], disabled=True)
    assert store.user_id_for_token(token) is None
    assert store.provider_keys(user["id"]) == {}


def test_the_signature_changes_when_a_key_changes(store):
    user, _ = store.create("Ada")
    empty = store.signature(user["id"])
    store.add_key(user["id"], "gemini", "sk-one-key-12345678")
    one = store.signature(user["id"])
    store.add_key(user["id"], "gemini", "sk-two-key-12345678")
    two = store.signature(user["id"])
    assert len({empty, one, two}) == 3


def test_a_lost_master_key_is_a_clear_error(store, tmp_path):
    user, _ = store.create("Ada")
    store.add_key(user["id"], "gemini", "sk-super-secret-key-1234")
    store2 = UserStore(store.path, tmp_path / "other-master.key")
    with pytest.raises(UserStoreError, match="cannot be decrypted"):
        store2.provider_keys(user["id"])


def test_auth_precedence(store, monkeypatch):
    user, token = store.create("Ada")
    monkeypatch.setattr(gateway_auth, "_token", "operator-token")
    # operator only -> shared pool
    ctx = gateway_auth.authenticate("operator-token", None, store)
    assert ctx.admin and ctx.user_id is None
    # user only -> user pool, no admin
    ctx = gateway_auth.authenticate(None, token, store)
    assert ctx.admin is False and ctx.user_id == user["id"]
    # both -> user pool, admin allowed
    ctx = gateway_auth.authenticate("operator-token", token, store)
    assert ctx.admin and ctx.user_id == user["id"]
    # bad user token with a valid operator token degrades to the operator
    ctx = gateway_auth.authenticate("operator-token", "aru_nope", store)
    assert ctx.admin and ctx.user_id is None
    # nothing valid -> unauthenticated
    assert gateway_auth.authenticate(None, None, store) is None


def test_two_users_get_separate_pools(store):
    from user_pools import UserPoolManager
    a, _ = store.create("Ada")
    b, _ = store.create("Bob")
    store.add_key(a["id"], "gemini", "sk-user-a-key-123456")
    store.add_key(b["id"], "gemini", "sk-user-b-key-123456")
    manager = UserPoolManager(["gemini35lite", "gemini", "kilo"],
                              ["cerebras", "groq", "nvidia", "github", "kilo"])
    pa = manager.get(store, a["id"])
    pb = manager.get(store, b["id"])
    assert pa and pb and pa is not pb
    assert pa.providers["gemini"].api_key == "sk-user-a-key-123456"
    assert pb.providers["gemini"].api_key == "sk-user-b-key-123456"
    # Separate Router state is the point: one user's RPM cannot throttle
    # the other.
    assert pa.router.state is not pb.router.state
    assert manager.get(store, a["id"]) is pa
    store.add_key(a["id"], "gemini", "sk-user-a-second-123456")
    pa2 = manager.get(store, a["id"])
    assert pa2 is not pa
    assert "gemini-2" in pa2.providers
    # A user with no keys gets None, never the operator's pool.
    c, _ = store.create("Cid")
    assert manager.get(store, c["id"]) is None


def test_the_users_api_is_operator_only(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    import main as M

    local = UserStore(tmp_path / "users.json", tmp_path / "master.key")
    monkeypatch.setattr(M, "UserStore", lambda: local)

    async def drive():
        async with M.app.router.lifespan_context(M.app):
            c = TestClient(M.app)
            created = c.post("/v1/users", json={"name": "Ada"})
            assert created.status_code == 200, created.text
            body = created.json()
            token = body["token"]
            uid = body["user"]["id"]
            assert token.startswith("aru_")
            listing = c.get("/v1/users")
            assert listing.status_code == 200, listing.text
            assert [u["id"] for u in listing.json()["users"]] == [uid]

            # A user token alone can read itself but cannot administer.
            user_headers = {"X-Aria-User-Token": token}
            me = c.get("/v1/users/me", headers=user_headers)
            assert me.status_code == 200, me.text
            assert me.json()["user"]["id"] == uid
            # The admin dependency itself refuses a user-only context. (A
            # TestClient request still carries the operator header injected by
            # conftest, which is exactly the agent's both-headers case.)
            import users_api
            from types import SimpleNamespace
            from fastapi import HTTPException
            fake = SimpleNamespace(state=SimpleNamespace(
                auth=gateway_auth.AuthContext(admin=False, user_id=uid,
                                               via="user")))
            with pytest.raises(HTTPException) as denied:
                users_api._admin(fake)
            assert denied.value.status_code == 403

            added = c.post(f"/v1/users/{uid}/keys", json={
                "provider": "gemini", "key": "sk-user-key-123456",
                "label": "work"})
            assert added.status_code == 200, added.text
            assert "sk-user-key-123456" not in added.text
            assert added.json()["user"]["keys"][0]["masked"].startswith("sk-u")

            # No keys yet -> chat refuses rather than spending the operator's.
            empty = c.post("/v1/users", json={"name": "No Keys"})
            empty_token = empty.json()["token"]
            chat = c.post("/v1/chat", json={"prompt": "hi", "max_tokens": 5},
                          headers={"X-Aria-User-Token": empty_token})
            assert chat.status_code == 503, chat.text
            assert "no provider keys" in chat.text

            # A user token can call chat with its own pool; we stop before the
            # provider network by asserting the pool is reachable through /me.
            me2 = c.get("/v1/users/me", headers=user_headers)
            assert me2.json()["user"]["keys"][0]["provider"] == "gemini"

    asyncio.run(drive())
