"""The Telegram long-poll listener.

The bug this closes: nothing in the repo ever called `setWebhook`, and Telegram
cannot reach 127.0.0.1 anyway — so no inbound message ever arrived, no sender
was ever paired, and every "talking to the agent from Telegram" path was
unreachable on a local install. These tests pin the listener's guarantees.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import telegram_poller as T


class _FakeTelegram:
    """A configured adaptor stand-in that serves canned updates."""

    def __init__(self, updates=None, webhook_url="", api_error=""):
        self.updates = list(updates or [])
        self.webhook_url = webhook_url
        self.api_error = api_error
        self.normalized: list[dict] = []

    def is_configured(self):
        return True

    def _api(self, method, payload, **kwargs):
        if method == "getWebhookInfo":
            # Always answerable: it is precisely how a getUpdates 409 is
            # attributed to a webhook rather than to transient contention.
            return {"ok": True, "result": {"url": self.webhook_url}}
        if method == "getUpdates":
            if self.api_error:
                raise RuntimeError(self.api_error)
            return {"ok": True, "result": self.updates}
        raise AssertionError(f"unexpected API call {method}")

    def normalize(self, payload):
        """Real envelope, so the real `_record_inbound` runs and the test can
        observe what it does to the pairing store."""
        from adaptors.envelope import ChannelMessage, TrustLevel
        m = ((payload or {}).get("message") or {})
        self.normalized.append(payload)
        return ChannelMessage(
            channel="telegram", raw=payload,
            sender_id=str((m.get("from") or {}).get("id") or ""),
            chat_id=str((m.get("chat") or {}).get("id") or ""),
            text=m.get("text"), msg_id=str(m.get("message_id") or ""),
            ts=float(m.get("date") or 0), trust_level=TrustLevel.untrusted)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "_OFFSET_PATH", tmp_path / "off.json")
    T.stop()
    yield
    T.stop()
    T._THREAD = None
    T._STOOD_DOWN = ""
    T._LAST_ERROR = ""


def _update(uid, text="hello"):
    return {"update_id": uid,
            "message": {"message_id": 1, "date": 1700000000,
                        "text": text,
                        "from": {"id": 555, "first_name": "A"},
                        "chat": {"id": 555}}}


def test_a_real_attachment_model_is_stored_not_crashed(monkeypatch, tmp_path):
    """The poller read attachments as dicts (`att.get(...)`), but the envelope
    holds pydantic `Attachment` models. The download thread died with
    AttributeError before storing anything, so no Telegram file ever reached
    the Documents store - which is why the agent kept answering as if nothing
    had been sent."""
    from adaptors.envelope import Attachment

    ingested = []

    class _Inst:
        def download(self, file_id, timeout=60.0):
            assert file_id == "FILEID1"
            return b"%PDF-1.4 data", str(tmp_path / "x.pdf")

    import memory_api
    monkeypatch.setattr(
        memory_api, "ingest_document",
        lambda name, data: ingested.append((name, data)) or {"id": "doc-x"})
    msg = type("M", (), {"msg_id": 7, "attachments": [
        Attachment(kind="file", name="contract.pdf", mime="application/pdf",
                   size=13, file_id="FILEID1")]})()

    T._absorb_files(_Inst(), msg)

    assert ingested == [("contract.pdf", b"%PDF-1.4 data")]


def test_a_stored_attachment_is_indexed_not_left_pending(monkeypatch, tmp_path):
    """`ingest_document` only writes the record. Outside an upload request
    nothing started the indexer, so a Telegram file sat in `pending` forever:
    never READY, so it could not be enabled and search never saw a chunk."""
    from adaptors.envelope import Attachment

    indexed = []

    class _Inst:
        def download(self, file_id, timeout=60.0):
            return b"%PDF-1.4 data", str(tmp_path / "x.pdf")

    import memory_api
    monkeypatch.setattr(memory_api, "ingest_document",
                        lambda name, data: {"id": "doc-y"})
    monkeypatch.setattr(memory_api, "index_document",
                        lambda doc_id, app: indexed.append((doc_id, app)))
    msg = type("M", (), {"msg_id": 8, "attachments": [
        Attachment(kind="file", name="a.pdf", mime="application/pdf",
                   size=13, file_id="FILEID2")]})()
    app = object()

    T._absorb_files(_Inst(), msg, app)

    assert indexed == [("doc-y", app)]


def test_forward_sends_attachment_metadata_not_a_crash(monkeypatch, tmp_path):
    """`_forward_to_agent` did `a.get(...)` on `Attachment` objects; the
    AttributeError aborted the forward before any HTTP call, so the ledger
    showed two agent-forward errors exactly when two files were sent."""
    import httpx

    import channels_api
    from adaptors.envelope import Attachment, ChannelMessage, TrustLevel

    seen = {}

    class _Resp:
        status_code = 200
        text = "{}"

    def _post(url, headers=None, json=None, timeout=None):
        seen["url"] = url
        seen["json"] = json
        return _Resp()

    tok = tmp_path / "agent.token"
    tok.write_text("test-token", encoding="utf-8")
    monkeypatch.setattr(channels_api, "_agent_token_path", lambda: tok)
    monkeypatch.setattr(httpx, "post", _post)
    msg = ChannelMessage(
        channel="telegram", sender_id="555", chat_id="555", text="",
        attachments=[Attachment(kind="file", name="contract.pdf",
                                mime="application/pdf", size=13,
                                file_id="FILEID1")],
        trust_level=TrustLevel.paired)

    out = channels_api._forward_to_agent("telegram", msg, "555")

    assert out["status"] == 200, out
    assert seen["json"]["attachments"] == [
        {"kind": "file", "name": "contract.pdf", "mime": "application/pdf",
         "size": 13, "file_id": "FILEID1"}]


def test_updates_are_delivered_to_the_webhook_path(monkeypatch):
    """The poller must not invent a second delivery path — an inbound message
    is worth exactly what the webhook does with it."""
    delivered = []
    import channels_api
    monkeypatch.setattr(channels_api, "_record_inbound",
                        lambda name, msg: delivered.append((name, msg)))

    inst = _FakeTelegram([_update(10), _update(11)])
    report = T.poll_once(inst)

    assert report["delivered"] == 2
    assert [name for name, _ in delivered] == ["telegram", "telegram"]
    assert [m.chat_id for _, m in delivered] == ["555", "555"]


def test_the_offset_advances_past_what_was_delivered(monkeypatch):
    monkeypatch.setattr("channels_api._record_inbound",
                        lambda *a: {"ok": True})
    inst = _FakeTelegram([_update(41), _update(42)])
    T._set_offset(41)
    report = T.poll_once(inst)

    assert report["offset"] == 43, "the next poll must resume after the last update"
    assert T._offset() == 43, "the offset must survive a restart"


def test_a_failed_delivery_is_retried_not_dropped(monkeypatch):
    """Acknowledge only what was delivered: the agent being briefly down is
    not a reason to lose what the user said."""
    import channels_api

    calls = []

    def _boom(name, msg):
        calls.append(msg)
        if len(calls) == 1:
            raise RuntimeError("agent down")

    monkeypatch.setattr(channels_api, "_record_inbound", _boom)
    inst = _FakeTelegram([_update(7)])
    T._set_offset(0)

    report = T.poll_once(inst)

    assert report["failed"] == 1
    assert report["delivered"] == 0
    assert report["offset"] == 0, "an undelivered update was acknowledged anyway"
    # A later cycle retries it.
    report2 = T.poll_once(inst)
    assert report2["delivered"] == 1
    assert report2["offset"] == 8
    assert len(calls) == 2


def test_a_registered_webhook_stops_the_listener():
    """Two consumers of one queue is a bug: while a webhook is set, Telegram
    answers getUpdates with 409 and the queue is not ours to read."""
    inst = _FakeTelegram([_update(1)], webhook_url="https://x/hook",
                         api_error="409 conflict")
    report = T.poll_once(inst)

    assert report["stopped"] is True
    assert "webhook" in report["error"]


def test_a_conflict_without_a_webhook_is_retried_not_fatal():
    """A 409 with no webhook is contention with another getUpdates consumer —
    often the tail of a request this process abandoned. Standing down forever
    would silently kill inbound chat over a momentary clash."""
    inst = _FakeTelegram([_update(1)], webhook_url="",
                         api_error="409 Conflict: terminated by other "
                                   "getUpdates request")
    report = T.poll_once(inst)

    assert report["stopped"] is False, (
        "the listener gave up permanently over a transient conflict")
    assert report["conflict"] is True
    assert "retry" in report["error"]


def test_the_listener_does_not_start_when_a_webhook_is_registered(monkeypatch):
    import telegram_poller as T2

    inst = _FakeTelegram(webhook_url="https://example.com/hook")
    monkeypatch.setattr(T2, "_webhook_registered", lambda i: True)
    monkeypatch.setattr("adaptors.registry.get", lambda name: inst)

    assert T2.start() is not None
    thread = T2._THREAD
    thread.join(timeout=5)

    assert not thread.is_alive(), "the listener ran despite a registered webhook"
    assert "webhook" in (T2._STOOD_DOWN or "")


def test_nothing_pairing_gets_paired_by_the_poller(monkeypatch, tmp_path):
    """Trust-on-first-contact is what makes the pairing store usable at all:
    the first inbound sender becomes owner. The poller feeds that path, so a
    fresh install's first message pairs itself."""
    import channels_api
    store = tmp_path / "pairing.json"
    store.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("adaptors.trust.STORE_PATH", store)
    monkeypatch.setattr(channels_api, "_forward_to_agent",
                        lambda *a: {"status": 200, "body": "ok"})

    inst = _FakeTelegram([_update(99, "/start")])
    T.poll_once(inst)

    data = json.loads(store.read_text(encoding="utf-8"))
    assert data.get("telegram", {}).get("555") == "owner", (
        "the first sender to talk to the bot was not paired as owner — this "
        "is the state the user is actually in: configured bot, empty store")


def test_a_transport_error_does_not_raise():
    """One bad cycle is a logged failure, never a dead thread."""
    inst = _FakeTelegram(api_error="telegram transport error: ConnectError")
    report = T.poll_once(inst)

    assert report["delivered"] == 0
    assert "transport" in report["error"]
    assert T._LAST_ERROR


def test_status_reports_the_listener(monkeypatch):
    monkeypatch.setattr(T, "_offset", lambda: 12)
    st = T.status()
    assert st["enabled"] is True
    assert st["offset"] == 12
    assert st["running"] is False
    assert "paired_senders" not in st


def test_the_agent_token_path_resolves_inside_the_repo():
    """`_agent_token_path` climbed one directory too far, so it pointed above
    the repo at a file that does not exist, and every inbound message was
    skipped as "no agent token" — received, paired, and delivered to nobody,
    behind a 200 response and no error anywhere. The path must land where the
    agent writes: <repo>/S9SharedCode/code/state/agent.token."""
    from channels_api import _agent_token_path

    path = _agent_token_path()
    repo = Path(__file__).resolve().parents[2]  # the repo root

    assert repo in path.parents, (
        f"the gateway looks for the agent token at {path}, which is outside "
        f"the repo ({repo}); inbound messages are silently dropped")
    assert path.parts[-4:] == ("S9SharedCode", "code", "state", "agent.token")


def test_a_forward_that_cannot_authenticate_is_reported(monkeypatch, tmp_path):
    """The skip is returned to the caller and written to the ledger, so the
    failure is visible rather than a cheerful 200."""
    import channels_api

    monkeypatch.setattr(channels_api, "_agent_token_path",
                        lambda: tmp_path / "missing.token")
    rows = []
    monkeypatch.setattr(channels_api.db, "log_call", lambda **kw: rows.append(kw))

    out = channels_api._forward_to_agent(
        "telegram",
        type("M", (), {"chat_id": "1", "text": "hi"})(), "owner")

    assert out["skipped"] == "no agent token"
    assert rows and any(r.get("status") == "error" for r in rows), (
        "a silently dropped message is how this bug hid in the first place")


def test_the_hook_route_does_not_run_the_forward_on_the_event_loop(
        monkeypatch):
    """The forward into the agent is a BLOCKING call, and the agent calls
    straight back into this same server to ask whether the sender is paired.
    Run on the event loop, that is a self-deadlock: the loop waits on the
    agent, the agent waits on the loop, the trust lookup times out, and every
    message is refused as untrusted while the webhook returns a cheerful 200.

    Measured by which thread does the work, because a caller inside the same
    loop cannot time a stall in it — it is simply not scheduled until the stall
    is over, so a wall-clock assertion here passes whether or not the bug is
    present. The loop runs on this test's thread; a blocking forward must not.
    """
    import asyncio
    import threading

    import httpx
    from fastapi import FastAPI

    import channels_api
    from adaptors.envelope import ChannelMessage, TrustLevel

    class _Inst:
        def normalize(self, payload):
            return ChannelMessage(channel="telegram", raw=payload,
                                  sender_id="1", chat_id="1", text="hi",
                                  trust_level=TrustLevel.owner)

        def verify_webhook(self, headers, body):
            return True

    ran_on: dict = {}

    def _record(name, msg):
        ran_on["thread"] = threading.get_ident()
        return {"ok": True, "forwarded": True}

    monkeypatch.setattr(channels_api, "_adaptor_or_404", lambda name: _Inst())
    monkeypatch.setattr(channels_api, "_uses_custom_verify", lambda inst: False)
    monkeypatch.setattr(channels_api, "_record_inbound", _record)

    app = FastAPI()
    app.include_router(channels_api.router)
    transport = httpx.ASGITransport(app=app)
    loop_thread = threading.get_ident()  # asyncio.run runs the loop here

    async def _post():
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://t") as c:
            r = await c.post("/v1/hooks/telegram",
                             json={"message": {"text": "hi"}})
            assert r.status_code == 200, r.text

    asyncio.run(_post())

    assert ran_on.get("thread") not in (None, loop_thread), (
        "the agent forward ran on the event loop thread; while it blocks, the "
        "agent cannot call back for its trust lookup, so every inbound "
        "message is refused as untrusted")
