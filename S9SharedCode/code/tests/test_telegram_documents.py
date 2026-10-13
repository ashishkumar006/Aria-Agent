"""Telegram documents, both directions.

The adaptor used to drop `document`/`photo`/... on the floor via
extra="ignore", so a file arrived as an empty message that the agent refused,
and `send` took a `media` argument it never read. Both halves are pinned here
against the real wire shapes, with no network: `_api` is stubbed.
"""
import sys
import pathlib

import httpx
import pytest

AGENT = pathlib.Path(r"C:\Users\AISHWARYA\Downloads\project3\S9SharedCode\code")
GW = pathlib.Path(r"C:\Users\AISHWARYA\Downloads\project3\llm_gatewayV9")
# Agent path LAST so it ends up FIRST on sys.path. Both trees contain
# same-named modules (`artifacts.py` exists in each, and the gateway's imports
# `schemas`), so whichever is inserted last silently shadows the other.
for p in (str(GW), str(AGENT)):
    if p in sys.path:
        sys.path.remove(p)
    sys.path.insert(0, p)

from adaptors.telegram.adapter import TelegramAdaptor, _decode_media  # noqa: E402
from adaptors.telegram.schemas import TelegramUpdate  # noqa: E402


def _update(**msg):
    base = {"message_id": 7, "date": 1700000000,
            "from": {"id": 42, "username": "a"},
            "chat": {"id": 99, "type": "private"}}
    base.update(msg)
    return {"update_id": 1, "message": base}


def _adaptor(monkeypatch):
    a = TelegramAdaptor()
    monkeypatch.setattr(a, "_need", lambda *_: "TOKEN")
    return a


def test_a_document_is_not_dropped(monkeypatch):
    """The original bug: extra="ignore" discarded the document, so the
    agent saw an empty message and told the user it could not read it."""
    msg = _update(document={"file_id": "F1", "file_name": "notes.md",
                            "mime_type": "text/markdown", "file_size": 120})
    out = TelegramAdaptor().normalize(msg)
    assert out.attachments, "the document was dropped again"
    a = out.attachments[0]
    assert a.file_id == "F1"
    assert a.name == "notes.md"
    assert a.mime == "text/markdown"


def test_a_photo_becomes_an_image_attachment(monkeypatch):
    msg = _update(photo=[{"file_id": "small", "width": 90},
                         {"file_id": "big", "width": 1280, "file_size": 999}])
    out = TelegramAdaptor().normalize(msg)
    assert out.attachments and out.attachments[0].kind == "image"
    assert out.attachments[0].file_id == "big", (
        "took the smallest photo; the last size is the full-resolution one")


def test_voice_is_audio_not_a_document(monkeypatch):
    """kind is what the document pipeline branches on; a voice note filed as
    a document would be handed to the PDF/Word text extractor."""
    out = TelegramAdaptor().normalize(
        _update(voice={"file_id": "V", "duration": 4}))
    assert out.attachments[0].kind == "audio"


def test_a_plain_message_still_has_no_attachments(monkeypatch):
    out = TelegramAdaptor().normalize(_update(text="hello"))
    assert out.attachments == []
    assert out.text == "hello"


def test_send_uses_sendDocument_when_media_is_present(monkeypatch):
    a = _adaptor(monkeypatch)
    seen = {}

    def fake_multipart(method, *, fields, filename, content,
                       field_name="document", timeout=60.0):
        seen.update(method=method, fields=fields, filename=filename,
                    content=content)
        return {"ok": True, "result": {"message_id": 5}}

    monkeypatch.setattr(a, "_api_multipart", fake_multipart)
    monkeypatch.setattr(
        a, "_api",
        lambda *args, **kw: pytest.fail(
            "fell back to sendMessage although a file was attached"))

    a.send(to="99", text="here it is",
           media=[{"filename": "report.pdf", "data": b"%PDF-1.4"}])

    assert seen["method"] == "sendDocument"
    assert seen["filename"] == "report.pdf"
    assert seen["content"] == b"%PDF-1.4"
    assert seen["fields"]["chat_id"] == "99"


def test_send_falls_back_to_text_when_media_is_unusable(monkeypatch):
    """An undecodable attachment must still deliver the words. Sending
    nothing because a file was malformed is the worse failure."""
    a = _adaptor(monkeypatch)
    sent = {}

    def fake_api(method, payload, timeout=20.0):
        sent.update(method=method, payload=payload)
        return {"ok": True, "result": {"message_id": 6}}

    monkeypatch.setattr(a, "_api", fake_api)
    monkeypatch.setattr(
        a, "_api_multipart",
        lambda *a_, **kw: pytest.fail("uploaded an empty file"))

    a.send(to="99", text="the words", media=[{"data_b64": "not base64!!"}])
    assert sent["method"] == "sendMessage"
    assert sent["payload"]["text"] == "the words"


def test_base64_media_is_decoded():
    import base64
    raw = b"hello"
    assert _decode_media({"data_b64": base64.b64encode(raw).decode()}) == raw
    assert _decode_media({"data": b"bytes"}) == b"bytes"
    assert _decode_media({"data_b64": "!!!"}) is None
    assert _decode_media({}) is None


def test_malformed_update_still_yields_an_envelope(monkeypatch):
    """Never crash the poller on junk."""
    upd = TelegramUpdate.model_validate({"update_id": 3, "message": {}})
    assert upd.effective() is not None
    out = TelegramAdaptor().normalize({"update_id": 3, "message": {}})
    assert out.attachments == []


# ── outbound: the reply path actually attaching the file ─────────────────

def test_notify_forwards_media_to_the_gateway(monkeypatch):
    """`send_channel_message` modelled `media` nowhere. The gateway accepted
    the key and the Telegram adaptor implemented sendDocument for it, but
    nothing ever sent it - so a caller passing a file had it silently
    dropped and the user received a caption with no attachment."""
    import notify

    sent = {}

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"ok": True}

    class _Client:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, **kw):
            sent.update(url=url, body=json)
            return _Resp()

    monkeypatch.setattr(notify, "resolve_target", lambda ch: "99")
    monkeypatch.setattr(notify, "_gw_headers", lambda: {})
    monkeypatch.setattr(httpx, "Client", _Client)

    media = [{"filename": "deck.pdf", "data_b64": "JVBERi0="}]
    notify.send_channel_message("telegram", "here it is", to="99", media=media)

    assert sent["body"].get("media") == media, (
        "the attachment never reached the gateway")
    assert sent["url"].endswith("/v1/channels/telegram/send")


def test_a_non_telegram_channel_is_not_promised_an_attachment():
    """Only Telegram implements media here. A reply that says "Attached:"
    on a channel that will not carry one is worse than no promise."""
    import channel_chat

    produced = [{"artifact": "art:0123456789abcdef", "filename": "deck.pdf"}]
    assert channel_chat._channel_media("telegram", produced) is not None
    assert channel_chat._channel_media("slack", produced) == []
    assert channel_chat._channel_media("telegram", []) == []