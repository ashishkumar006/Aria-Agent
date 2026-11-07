"""Control-plane routes over HTTP: the token gate, the loopback gate, and the
promise that a key value never travels back the way it came.

These nine routes (`/v1/control/*`, `/v1/config/keys`) had no behavioural test
at all. They are the ones that write secrets and stop the process, and two of
the properties they depend on are invisible until they break:

* `_loopback_only` is a *second* gate on top of the token — a stolen operator
  token from another machine must still not be able to repaste keys, pair a
  stranger or arm the kill switch.
* the pool route replaces a provider's *whole* key set, and shrinking it to one
  key has to clear the plural variable from `.env`, from `os.environ` and from
  `secret_vault`. It used to clear the first two only, so a withdrawn
  credential kept showing up as a live pool sibling in `/v1/config/keys`.

Everything here is offline, and every route that persists is pointed at
`tmp_path` first: these handlers write the gateway's real `.env`, the call
ledger, the pairing store and the audit log. All key material is fake.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import channels_api as C  # noqa: E402
import db as gdb  # noqa: E402
import main as M  # noqa: E402
import redact  # noqa: E402
import secret_vault  # noqa: E402
import security_audit  # noqa: E402
import telegram_poller as tgp  # noqa: E402
from adaptors import trust  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

# Must equal tests/conftest.py's TEST_TOKEN, which the autouse fixture there
# pins into gateway_auth._token for every test in the suite.
TOKEN = "test-gateway-token"

# `request.client.host` is what `_loopback_only` trusts. Starlette's default
# in-process host string is "testclient", which is neither — so the peer
# address is set explicitly on every client below rather than left to chance.
LOOPBACK_PEER = ("127.0.0.1", 51234)
REMOTE_PEER = ("203.0.113.9", 40000)  # TEST-NET-3: documentation range, never routable

# The routes this file is about, in (method, path, body) form.
CONTROL_ROUTES = [
    ("GET", "/v1/control/presence", None),
    ("GET", "/v1/control/notify-target", None),
    ("GET", "/v1/control/trust", None),
    ("GET", "/v1/control/telegram", None),
    ("POST", "/v1/control/pair", {"channel": "telegram", "sender_id": "zz-9999"}),
    ("POST", "/v1/control/kill", None),
    ("GET", "/v1/config/keys", None),
    ("POST", "/v1/control/keys", {"keys": {"TELEGRAM_BOT_TOKEN": "zzfake-token-abcdef"}}),
    ("POST", "/v1/control/pool", {"provider": "groq", "keys": ["zzfake-pool-1"]}),
]

# The subset that is additionally loopback-gated. `/v1/control/presence` and
# `/v1/config/keys` are deliberately not here: they report counts and names.
LOOPBACK_ONLY_ROUTES = [r for r in CONTROL_ROUTES
                        if r[1] not in ("/v1/control/presence", "/v1/config/keys")]


@pytest.fixture
def local():
    """An operator client that presents as a loopback peer."""
    return TestClient(M.app, client=LOOPBACK_PEER)


@pytest.fixture
def remote():
    """An operator client holding a perfectly valid token, from elsewhere."""
    return TestClient(M.app, client=REMOTE_PEER)


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Redirect every side effect into `tmp_path`, and start each test with a
    clean, known provider-key state.

    The routes in this file are the only ones that *write* outside the request:
    `_write_env_keys` targets the gateway's real `.env`, `db.log_call` appends
    to the 36 MB production ledger, `trust.pair` rewrites `state/pairing.json`
    and the auth middleware logs a refusal into `state/security_audit.jsonl`.
    A test that "just calls the route" would therefore mutate the operator's
    machine, and a refused write would be indistinguishable from an applied one
    because nothing observable changed.

    The provider vars are cleared from BOTH stores (process env and vault).
    `main.py` loads the real `.env` at import, and any earlier test that ran
    the app lifespan left those captured values in `secret_vault._values`, so
    pool counts would otherwise depend on test order — the exact failure mode
    the root conftest was written for. Restoring is automatic: monkeypatch puts
    the original objects back, so the vault and the redactor are untouched by
    anything a test here registered.
    """
    monkeypatch.setattr(C, "_ENV_PATH", tmp_path / "keys.env")
    monkeypatch.setattr(gdb, "DB_PATH", str(tmp_path / "ledger.db"))
    gdb.init()
    monkeypatch.setattr(trust, "STORE_PATH", tmp_path / "pairing.json")

    monkeypatch.setattr(security_audit, "AUDIT_PATH", tmp_path / "audit.jsonl")
    monkeypatch.setattr(security_audit, "KEY_PATH", tmp_path / "audit.key")
    # A configured key means no key file is ever created, not even in tmp.
    monkeypatch.setenv("GATEWAY_AUDIT_KEY", "zz-fake-audit-key")
    monkeypatch.setattr(security_audit, "_key", None)
    monkeypatch.setattr(security_audit, "_last_hash", "")

    vault = dict(secret_vault._values)
    for name in secret_vault.PROVIDER_KEY_VARS:
        vault.pop(name, None)
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(secret_vault, "_values", vault)
    monkeypatch.setattr(secret_vault, "_scrubbed", set(secret_vault._scrubbed))
    monkeypatch.setattr(redact, "_secrets", set(redact._secrets))
    for name in ("TELEGRAM_BOT_TOKEN", "DISCORD_BOT_TOKEN", "ARIA_ENABLE_KILL"):
        monkeypatch.delenv(name, raising=False)
    # `/v1/control/telegram` reads the poller's thread/offset state. Pinned so
    # the answer describes the route, not whatever a neighbouring test left.
    monkeypatch.setattr(tgp, "_THREAD", None)
    monkeypatch.setattr(tgp, "_STOOD_DOWN", "")
    monkeypatch.setattr(tgp, "_LAST_ERROR", "")
    monkeypatch.setattr(tgp, "_OFFSET_PATH", tmp_path / "offset.json")


# ── 1. the token gate ────────────────────────────────────────────────────────
@pytest.mark.parametrize("method,path,body", CONTROL_ROUTES)
def test_a_caller_without_a_token_is_refused(local, method, path, body):
    """Every control route is a `/v1/*` route, so the middleware must answer
    before the handler runs — including for the loopback-gated ones, which
    would otherwise be reachable by any process on this machine with no
    credential at all."""
    local.headers.pop("X-Gateway-Token", None)
    r = local.request(method, path, json=body)
    assert r.status_code == 401, (method, path, r.text[:200])
    assert "token" in r.text.lower(), r.text[:200]
    # The rejection must not hand back the credential it just refused, nor
    # anything the caller pasted in the body it never got to run.
    assert TOKEN not in r.text
    assert "zzfake-token-abcdef" not in r.text
    assert "zz-9999" not in r.text


@pytest.mark.parametrize("method,path,body", CONTROL_ROUTES)
def test_a_caller_with_the_wrong_token_is_refused(local, method, path, body):
    local.headers["X-Gateway-Token"] = "not-the-operator-token"
    r = local.request(method, path, json=body)
    assert r.status_code == 401, (method, path, r.text[:200])
    assert TOKEN not in r.text
    assert "not-the-operator-token" not in r.text


def test_a_refused_key_write_writes_nothing(local):
    """A 401 on a POST that would have persisted secrets must leave no trace.

    Otherwise "rejected" and "applied then hidden" look identical from outside,
    which is how a half-configured gateway gets shipped.
    """
    local.headers.pop("X-Gateway-Token", None)
    r = local.post("/v1/control/keys",
                   json={"keys": {"TELEGRAM_BOT_TOKEN": "zzfake-token-abcdef"}})
    assert r.status_code == 401, r.text
    assert not Path(C._ENV_PATH).exists(), "the .env write ran despite the 401"
    assert os.environ.get("TELEGRAM_BOT_TOKEN") is None


# ── 2. the loopback gate ─────────────────────────────────────────────────────
@pytest.mark.parametrize("method,path,body", LOOPBACK_ONLY_ROUTES)
def test_a_valid_token_from_a_non_loopback_peer_is_still_refused(
        remote, method, path, body):
    """The token proves *who*; `_loopback_only` proves *where*. A credential
    copied off a laptop must not let a remote caller repaste keys, pair a
    stranger, read the notify destination, or arm the kill switch."""
    r = remote.request(method, path, json=body)
    assert r.status_code == 403, (method, path, r.text[:200])
    assert "loopback" in r.text.lower(), r.text[:200]
    assert "zzfake" not in r.text, "the refusal echoed a pasted value"


def test_a_remote_control_call_leaves_no_state(remote):
    """Refusal happens before the write, for both mutating control routes."""
    assert remote.post("/v1/control/pair",
                       json={"channel": "telegram",
                             "sender_id": "zz-9999", "role": "owner"}).status_code == 403
    assert remote.post("/v1/control/keys",
                       json={"keys": {"TELEGRAM_BOT_TOKEN": "zzfake-token-abcdef"}}).status_code == 403
    assert remote.post("/v1/control/pool",
                       json={"provider": "groq", "keys": ["zzfake-pool-1"]}).status_code == 403
    assert not Path(C._ENV_PATH).exists()
    assert not Path(trust.STORE_PATH).exists()
    assert os.environ.get("TELEGRAM_BOT_TOKEN") is None
    assert os.environ.get("GROQ_API_KEY") is None


def test_the_read_only_status_routes_stay_reachable_with_a_token(remote):
    """The gate is on the mutating half of the control plane only. Presence and
    the keys inventory answer for a remote operator too, because they disclose
    names and counts, never values — pinning the boundary keeps someone from
    "fixing" this into a blanket block that breaks the remote dashboard, and
    keeps someone from quietly adding a value to the payload."""
    assert remote.get("/v1/control/presence").status_code == 200
    assert remote.get("/v1/config/keys").status_code == 200


# ── presence ─────────────────────────────────────────────────────────────────
def test_presence_counts_the_adaptor_plane_and_nothing_else(local, monkeypatch):
    """`configured` is derived from env presence; the payload carries counts."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "zzfake-presence-probe-1234")
    from adaptors import registry
    inv = registry.inventory()
    r = local.get("/v1/control/presence")
    body = r.json()
    assert body["up"] is True and body["version"] == "v9"
    assert body["channels_total"] == len(inv)
    assert body["channels_live"] == sum(1 for c in inv if c.get("configured"))
    assert body["channels_live"] >= 1, "a configured channel went uncounted"
    assert "zzfake-presence-probe-1234" not in r.text


# ── trust / notify-target / telegram ─────────────────────────────────────────
def test_trust_classifies_a_paired_sender_and_calls_everything_else_untrusted(local):
    trust.pair("telegram", "1234567890", "owner")
    got = local.get("/v1/control/trust",
                    params={"channel": "telegram", "sender": "1234567890"}).json()
    assert got == {"channel": "telegram", "sender": "1234567890", "trust": "owner"}
    assert local.get("/v1/control/trust", params={"channel": "telegram",
                                                  "sender": "9999999999"}).json()["trust"] == "untrusted"
    # No sender at all is answered as untrusted rather than 422: the agent asks
    # about whoever typed, and an empty id must not become a crash it swallows.
    assert local.get("/v1/control/trust", params={"channel": "telegram"}).json()["trust"] == "untrusted"


def test_notify_target_hands_the_real_chat_id_to_a_loopback_caller(local):
    """The display path masks ids; a scheduled notification needs the real one.
    This is the route that exists for that, so the unmasked value has to be
    there — while staying absent from the masked route next door."""
    trust.pair("telegram", "9988776655", "owner")
    body = local.get("/v1/control/notify-target", params={"channel": "telegram"}).json()
    assert body["target"] == "9988776655"
    assert "channel" in body and "configured" in body
    tg = local.get("/v1/control/telegram").json()
    assert "9988776655" not in str(tg), "the masked view leaked the id"
    assert tg["notify_target"].endswith("55") and tg["notify_target"].startswith("998")


def test_notify_target_answers_empty_for_a_channel_nobody_paired_with(local):
    assert local.get("/v1/control/notify-target",
                     params={"channel": "discord"}).json()["target"] == ""


def test_control_telegram_reports_the_listener_and_the_paired_population(local):
    """`running`/`offset` come from the poller; the pairing population is added
    here and the notify target is masked, because this route feeds a panel."""
    trust.pair("telegram", "9988776655", "owner")
    trust.pair("telegram", "1111222233", "paired")
    body = local.get("/v1/control/telegram").json()
    assert isinstance(body["running"], bool)
    assert body["paired_senders"] == 2
    assert body["has_owner"] is True
    assert "9988776655" not in str(body), "the masked view leaked a chat id"
    assert local.get("/v1/control/telegram").status_code == 200
    trust.unpair("telegram", "9988776655")
    trust.unpair("telegram", "1111222233")
    empty = local.get("/v1/control/telegram").json()
    assert empty["paired_senders"] == 0 and empty["has_owner"] is False
    assert empty["notify_target"] == "", "a phantom destination for an empty store"


def test_pair_records_a_sender_and_refuses_a_bad_role(local):
    out = local.post("/v1/control/pair", json={"channel": "telegram",
                                               "sender_id": "5550101", "role": "paired"})
    assert out.status_code == 200, out.text
    assert out.json()["entry"] == {"channel": "telegram", "sender_id": "5550101",
                                   "role": "paired"}
    assert trust.classify("telegram", "5550101").value == "paired"
    # Only owner/paired exist. Anything else is a 400, not a silently-stored
    # role that `classify` would then read back as untrusted.
    bad = local.post("/v1/control/pair", json={"channel": "telegram",
                                               "sender_id": "5550101", "role": "admin"})
    assert bad.status_code == 400, bad.text
    assert "role" in bad.json()["detail"]


# ── kill switch ──────────────────────────────────────────────────────────────
def test_kill_is_disarmed_by_default_and_never_signals_the_process(
        local, monkeypatch):
    """Opt-in only. And the test itself must be unable to kill the test runner
    if that ordering ever changes, so `os.kill` is replaced with a recorder."""
    kills = []
    monkeypatch.setattr(os, "kill", lambda *a, **k: kills.append(a))
    r = local.post("/v1/control/kill")
    assert r.status_code == 403, r.text
    assert "ARIA_ENABLE_KILL" in r.text
    assert kills == [], "the gateway SIGTERM'd its own process during the suite"


def test_an_armed_kill_still_refuses_a_remote_peer_before_signalling(
        remote, monkeypatch):
    """The loopback check runs first. If the env flag is ever left armed, the
    control plane must still be unreachable from off-machine."""
    kills = []
    monkeypatch.setattr(os, "kill", lambda *a, **k: kills.append(a))
    monkeypatch.setenv("ARIA_ENABLE_KILL", "1")
    assert remote.post("/v1/control/kill").status_code == 403
    assert kills == []


# ── /v1/control/keys ─────────────────────────────────────────────────────────
def test_a_saved_channel_key_never_comes_back_in_the_response(local):
    pasted = "zzfake-telegram-token-abcdef"
    r = local.post("/v1/control/keys", json={"keys": {"TELEGRAM_BOT_TOKEN": pasted}})
    assert r.status_code == 200, r.text
    assert r.json()["saved"] == ["TELEGRAM_BOT_TOKEN"]
    assert pasted not in r.text, "the response echoed the pasted secret"
    # Applied live and persisted — to the (tmp) env file, never to the reply.
    assert os.environ["TELEGRAM_BOT_TOKEN"] == pasted
    assert f"TELEGRAM_BOT_TOKEN={pasted}" in Path(C._ENV_PATH).read_text(encoding="utf-8")


def test_a_provider_key_goes_to_the_vault_not_the_process_environment(local):
    """The one-line `os.environ["KILO_API_KEY"]` read is what `secret_vault`
    exists to close, and a runtime write is the path that used to re-open it.

    KILO_API_KEY is the singular variable `/v1/control/keys` accepts (the other
    providers' singulars are owned by `/v1/control/pool`); it is also in
    `secret_vault.PROVIDER_KEY_VARS`, so this is the branch that must remember
    into the vault and scrub the environment again. The route still reports the
    name back, because that is all the dashboard needs.
    """
    pasted = "zzfake-kilo-key-991122"
    r = local.post("/v1/control/keys", json={"keys": {"KILO_API_KEY": pasted}})
    assert r.status_code == 200, r.text
    assert r.json()["saved"] == ["KILO_API_KEY"]
    assert pasted not in r.text, "the response echoed the pasted secret"
    assert os.environ.get("KILO_API_KEY") is None, "a provider key is back in the env"
    assert secret_vault.value("KILO_API_KEY") == pasted
    assert secret_vault.present("KILO_API_KEY") is True
    assert f"KILO_API_KEY={pasted}" in Path(C._ENV_PATH).read_text(encoding="utf-8")


@pytest.mark.parametrize("body,frag", [
    ({"keys": {}}, "no keys supplied"),
    ({"keys": {"PATH": "zzfake-evil"}}, "unknown keys"),
    ({"keys": {"GATEWAY_V9_TOKEN": "zzfake-evil"}}, "unknown keys"),
    ({"keys": {"TELEGRAM_BOT_TOKEN": "   "}}, "empty value"),
    ({"keys": {"TELEGRAM_BOT_TOKEN": "line1\nline2"}}, "single line"),
    ({"keys": {"TELEGRAM_BOT_TOKEN": "x" * 2001}}, "too long"),
])
def test_key_writes_are_bounded_to_the_allowlist_and_to_one_line(
        local, body, frag):
    """`ALLOWED_KEYS` is the reason this endpoint cannot write arbitrary
    variables — including the gateway's own token, which would be a way to
    lock every legitimate client out. A multi-line value would also let a
    paste append extra `NAME=value` lines to `.env`."""
    r = local.post("/v1/control/keys", json=body)
    assert r.status_code == 400, (body, r.text)
    assert frag in r.json()["detail"]
    assert "zzfake-evil" not in r.text
    assert os.environ.get("GATEWAY_V9_TOKEN") == TOKEN, "the operator token was overwritten"


def test_a_rejected_key_write_persists_nothing(local, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "pre-existing-value")
    r = local.post("/v1/control/keys", json={"keys": {
        "TELEGRAM_BOT_TOKEN": "good-value", "NOT_AReal_VAR": "zzfake-evil"}})
    assert r.status_code == 400, r.text
    assert os.environ["TELEGRAM_BOT_TOKEN"] == "pre-existing-value"
    assert not Path(C._ENV_PATH).exists(), "a partial write landed in .env"


# ── /v1/config/keys ──────────────────────────────────────────────────────────
def test_config_keys_reports_names_presence_and_counts_never_values(local):
    """Every panel on the Keys page is driven by this payload. Presence and
    counts are safe; a single value in it would be a disclosure endpoint for
    the whole secret inventory."""
    alpha, beta = "zzfake-cfg-alpha-1029", "zzfake-cfg-beta-3847"
    assert local.post("/v1/control/pool", json={"provider": "groq",
                                                "keys": [alpha, beta]}).status_code == 200
    view = local.get("/v1/config/keys")
    body = view.json()
    assert alpha not in view.text
    assert beta not in view.text
    # Presence is reported by NAME only; `keys` is the ALLOWED_KEYS inventory
    # and `pools` is where the plural variable's count lives.
    assert {"name": "GROQ_API_KEYS", "set": True} in body["keys"]
    assert {"name": "DISCORD_BOT_TOKEN", "set": False} in body["keys"]
    assert all(set(k) == {"name", "set"} for k in body["keys"])
    assert body["pools"]["groq"]["count"] == 2
    assert body["pools"]["groq"]["members"] == ["groq", "groq-2"]
    # Names only, for every provider: nothing in `pools` may look like a value.
    for provider, info in body["pools"].items():
        assert set(info) == {"count", "members"}, provider
        assert all(isinstance(m, str) and m.startswith(provider)
                   for m in info["members"]), provider
    # A cleared pool is reported as empty, not as a phantom sibling.
    assert body["pools"]["gemini"]["count"] == 0
    assert body["pools"]["gemini"]["members"] == []


# ── /v1/control/pool ─────────────────────────────────────────────────────────
def test_pool_shrink_clears_the_plural_var_from_every_store(local):
    """The regression gate.

    Shrinking a pool to one key used to remove `GROQ_API_KEYS` from `.env` and
    from `os.environ` but leave it in `secret_vault`, so `_pool_members` kept
    answering `["groq", "groq-2"]` and `/v1/config/keys` reported a sibling the
    operator had just withdrawn — a revoked credential that still read as live,
    and a count the dashboard pasted over. The assertion is deliberately made
    four ways, because the old bug was exactly "one store said one thing and
    another said something else".
    """
    alpha, beta = "zzfake-pool-alpha-1111", "zzfake-pool-beta-2222"

    two = local.post("/v1/control/pool", json={"provider": "groq",
                                               "keys": [alpha, beta]})
    assert two.status_code == 200, two.text
    assert two.json()["members"] == ["groq", "groq-2"]
    assert alpha not in two.text and beta not in two.text, "pool route echoed a key"
    env_text = Path(C._ENV_PATH).read_text(encoding="utf-8")
    assert f"GROQ_API_KEY={alpha}" in env_text
    assert f"GROQ_API_KEYS={beta}" in env_text
    assert "GROQ_API_KEYS" not in os.environ, "pool keys went back into the env"
    assert secret_vault.value("GROQ_API_KEYS") == beta
    assert C._pool_key_count("groq") == 2

    one = local.post("/v1/control/pool", json={"provider": "groq", "keys": [alpha]})
    assert one.status_code == 200, one.text
    assert one.json()["members"] == ["groq"], "the withdrawn sibling is still listed"
    assert alpha not in one.text
    env_text = Path(C._ENV_PATH).read_text(encoding="utf-8")
    assert "GROQ_API_KEYS" not in env_text, "the plural var survived in .env"
    assert f"GROQ_API_KEY={alpha}" in env_text, "the surviving key was clobbered"
    assert "GROQ_API_KEYS" not in os.environ
    assert secret_vault.present("GROQ_API_KEYS") is False
    assert secret_vault.value("GROQ_API_KEYS") == ""
    assert C._pool_members("groq") == ["groq"]
    assert C._pool_key_count("groq") == 1
    # The two views must agree, since one feeds the paste and the other the
    # count the operator compares it against.
    cfg = local.get("/v1/config/keys").json()
    assert cfg["pools"]["groq"] == {"count": 1, "members": ["groq"]}


def test_pool_input_is_deduplicated_and_blank_entries_are_skipped(local):
    """A paste with a repeated key and stray blank lines must land as a
    two-member pool, not a three-member one with a phantom empty slot."""
    a, b = "zzfake-dedup-a-1111", "zzfake-dedup-b-2222"
    r = local.post("/v1/control/pool", json={"provider": "groq",
                                             "keys": [a, f" {a} ", "", "   ", b]})
    assert r.status_code == 200, r.text
    assert r.json()["members"] == ["groq", "groq-2"]
    assert C._pool_key_count("groq") == 2
    assert secret_vault.value("GROQ_API_KEYS") == b


@pytest.mark.parametrize("payload,frag", [
    ({"provider": "not-a-provider", "keys": ["zzfake-1"]}, "unknown provider"),
    ({"provider": "groq", "keys": []}, "no usable keys"),
    ({"provider": "groq", "keys": ["z" * 2001]}, "too long"),
    ({"provider": "groq", "keys": ["zzfake-a\nzzfake-b"]}, "single-line"),
    ({"provider": "groq", "keys": [f"zzfake-{i}-pad" for i in range(21)]},
     "too many keys"),
])
def test_pool_rejects_input_it_cannot_store_before_writing_anything(
        local, payload, frag):
    r = local.post("/v1/control/pool", json=payload)
    assert r.status_code == 400, (payload, r.text)
    assert frag in r.json()["detail"]
    assert "zzfake" not in r.text
    assert not Path(C._ENV_PATH).exists(), "a rejected pool write still hit .env"
    assert secret_vault.value("GROQ_API_KEYS") == ""


def test_pool_replacement_drops_a_key_that_was_removed_from_the_paste(local):
    """This REPLACES the whole set. A paste of two keys after a three-key pool
    must not keep the third: an operator revoking one credential by re-pasting
    the list expects the withdrawn key to stop being callable."""
    a, b, c = ("zzfake-repl-a-1", "zzfake-repl-b-2", "zzfake-repl-c-3")
    assert local.post("/v1/control/pool", json={"provider": "groq",
                                                "keys": [a, b, c]}).status_code == 200
    assert C._pool_key_count("groq") == 3
    assert local.post("/v1/control/pool", json={"provider": "groq",
                                                "keys": [a, b]}).status_code == 200
    assert C._pool_members("groq") == ["groq", "groq-2"]
    assert c not in secret_vault.value("GROQ_API_KEY") + secret_vault.value("GROQ_API_KEYS")


# ── GLC_KILL_ALLOW_REMOTE is the kill switch's hatch, not the plane's ───────
# It used to be read inside the shared `_loopback_only()`, so an operator who
# armed a remote emergency stop ALSO opened key writes, pool writes, pairing,
# trust and the deploy probes to any network peer holding the gateway token.
# That is a credential plus an address to rewrite the provider key set. The fix
# passes `allow_remote=True` from `/v1/control/kill` alone; these tests are the
# gate that keeps it that way.
class TestKillEscapeHatchIsScoped:
    def test_the_flag_no_longer_opens_key_writes(self, remote, monkeypatch):
        monkeypatch.setenv("GLC_KILL_ALLOW_REMOTE", "1")
        r = remote.post("/v1/control/keys",
                        json={"keys": {"TELEGRAM_BOT_TOKEN": "zzfake-remote-1"}})
        assert r.status_code == 403, (
            "the emergency-stop escape hatch armed a credential WRITE route")
        if Path(C._ENV_PATH).exists():
            assert "zzfake-remote-1" not in Path(C._ENV_PATH).read_text(
                encoding="utf-8"), "refused but still persisted"

    def test_the_flag_no_longer_opens_pool_writes(self, remote, monkeypatch):
        monkeypatch.setenv("GLC_KILL_ALLOW_REMOTE", "1")
        r = remote.post("/v1/control/pool",
                        json={"provider": "groq", "keys": ["zzfake-remote-2"]})
        assert r.status_code == 403

    def test_the_flag_no_longer_opens_pairing_or_trust(self, remote, monkeypatch):
        monkeypatch.setenv("GLC_KILL_ALLOW_REMOTE", "1")
        assert remote.post("/v1/control/pair", json={
            "channel": "telegram", "sender_id": "zz-remote"}).status_code == 403
        assert remote.get("/v1/control/trust").status_code == 403

    def test_the_kill_route_alone_honours_the_flag(self, remote, monkeypatch):
        """And the hatch still works where it was always meant to work.

        Expect 403 from the SEPARATE kill gate (`ARIA_ENABLE_KILL`), not from
        the loopback gate — the distinction is the whole point: the peer
        address was accepted, the opt-in was not.
        """
        monkeypatch.setenv("GLC_KILL_ALLOW_REMOTE", "1")
        monkeypatch.delenv("ARIA_ENABLE_KILL", raising=False)
        r = remote.post("/v1/control/kill")
        assert r.status_code == 403
        assert "loopback" not in r.text.lower(), (
            "kill was refused on ADDRESS, so the escape hatch no longer reaches "
            "a remote operator at all — that is the other kind of surprise")
        assert "kill switch disabled" in r.text.lower()

    def test_without_the_flag_kill_stays_loopback_only(self, remote, monkeypatch):
        monkeypatch.delenv("GLC_KILL_ALLOW_REMOTE", raising=False)
        assert remote.post("/v1/control/kill").status_code == 403

    def test_every_loopback_route_still_refuses_a_remote_peer(self, remote):
        """Default-deny, no flags set. Guards against a future route calling
        `_loopback_only(request, allow_remote=True)` by accident."""
        for method, path, body in LOOPBACK_ONLY_ROUTES:
            resp = (remote.post(path, json=body) if method == "POST"
                    else remote.get(path))
            assert resp.status_code == 403, f"{method} {path} -> {resp.status_code}"
