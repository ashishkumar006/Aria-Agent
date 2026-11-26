"""Tests for the Gmail + Slack inbound pollers (channel_chat over email/Slack).

No network: the provider reads and `channels_api._record_inbound` are mocked.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import gmail_poller  # noqa: E402
import slack_poller  # noqa: E402


class TestGmailPoller:
    def test_forwards_allowed_sender_skips_self_and_others(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GMAIL_INBOUND_FROM", "alex@example.com")
        monkeypatch.setattr(gmail_poller, "_WM_PATH", tmp_path / "wm.json")
        gmail_poller._STOP.clear()

        import integrations.gmail as g
        monkeypatch.setattr(g, "profile",
                            lambda: {"ok": True, "email": "agent@gmail.com"})

        def fake_query(*, api_method, query="", max_results=5):
            if api_method == "list":
                return {"ok": True, "message_ids": ["m1", "m2", "m3"]}
            data = {
                "m1": {"ok": True, "id": "m1", "snippet": "hi from alex",
                       "headers": {"From": "Alex <alex@example.com>",
                                   "Subject": "Hello"}},
                # m2 is the agent's OWN reply — must never loop.
                "m2": {"ok": True, "id": "m2", "snippet": "my reply",
                       "headers": {"From": "agent@gmail.com", "Subject": "Re: Hello"}},
                # m3 is a newsletter — not on the allowlist.
                "m3": {"ok": True, "id": "m3", "snippet": "sale",
                       "headers": {"From": "news@spam.com", "Subject": "Sale"}},
            }
            return data.get(query, {"ok": False})
        monkeypatch.setattr(g, "query", fake_query)

        from adaptors import registry
        inst = registry.get("gmail")
        monkeypatch.setattr(type(inst), "is_configured", lambda self: True)

        forwarded = []
        import channels_api
        monkeypatch.setattr(channels_api, "_record_inbound",
                            lambda name, msg: forwarded.append((name, msg)) or {"ok": True})

        report = gmail_poller.poll_once()
        assert report["delivered"] == 1          # only alex's mail
        assert report["skipped"] == 1            # the newsletter
        assert len(forwarded) == 1
        name, msg = forwarded[0]
        assert name == "gmail"
        assert "alex@example.com" in (msg.chat_id or msg.sender_id)
        assert "hi from alex" in (msg.text or "")
        # Watermark records every seen id, so a restart does not reprocess.
        wm = gmail_poller._wm()
        assert {"m1", "m2", "m3"} <= wm

    def test_second_poll_is_a_no_op(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GMAIL_INBOUND_FROM", "alex@example.com")
        monkeypatch.setattr(gmail_poller, "_WM_PATH", tmp_path / "wm.json")
        import integrations.gmail as g
        monkeypatch.setattr(g, "profile", lambda: {"ok": True, "email": "agent@gmail.com"})
        monkeypatch.setattr(g, "query", lambda **k: {"ok": True, "message_ids": ["m1"]}
                            if k.get("api_method") == "list" else
                            {"ok": True, "id": "m1", "snippet": "hi",
                             "headers": {"From": "alex@example.com", "Subject": "H"}})
        from adaptors import registry
        inst = registry.get("gmail")
        monkeypatch.setattr(type(inst), "is_configured", lambda self: True)
        import channels_api
        monkeypatch.setattr(channels_api, "_record_inbound", lambda *a: {"ok": True})

        assert gmail_poller.poll_once()["delivered"] == 1
        # Already in the watermark -> not delivered again.
        assert gmail_poller.poll_once()["delivered"] == 0

    def test_idle_without_allowlist(self, monkeypatch, tmp_path):
        monkeypatch.delenv("GMAIL_INBOUND_FROM", raising=False)
        monkeypatch.setattr(gmail_poller, "_WM_PATH", tmp_path / "wm.json")
        from adaptors import registry
        inst = registry.get("gmail")
        monkeypatch.setattr(type(inst), "is_configured", lambda self: True)
        called = []
        import channels_api
        monkeypatch.setattr(channels_api, "_record_inbound", lambda *a: called.append(a))
        report = gmail_poller.poll_once()
        assert report["delivered"] == 0
        assert called == []


class TestSlackPoller:
    def test_forwards_user_message_skips_bot(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")
        monkeypatch.setattr(slack_poller, "_WM_PATH", tmp_path / "wm.json")

        def fake_api(method, **params):
            if method == "auth.test":
                return {"ok": True, "user_id": "UBOT"}
            if method == "conversations.list":
                return {"ok": True, "channels": [{"id": "C1"}]}
            if method == "conversations.history":
                return {"ok": True, "messages": [
                    {"ts": "100.0", "user": "UBOT", "text": "bot reply",
                     "bot_id": "B1"},
                    {"ts": "90.0", "user": "U1", "text": "hello from user"},
                ]}
            return {"ok": False}
        monkeypatch.setattr(slack_poller, "_api", fake_api)

        from adaptors import registry
        inst = registry.get("slack")
        forwarded = []
        import channels_api
        monkeypatch.setattr(channels_api, "_record_inbound",
                            lambda name, msg: forwarded.append((name, msg)) or {"ok": True})

        report = slack_poller.poll_once(inst)
        assert report["delivered"] == 1            # only the user's message
        name, msg = forwarded[0]
        assert name == "slack"
        assert msg.text == "hello from user"
        assert msg.thread_id is None
        # Watermark advanced to the newest ts seen (the bot's 100.0).
        assert slack_poller._wm().get("C1") == 100.0

    def test_no_token_is_idle(self, monkeypatch, tmp_path):
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        monkeypatch.setattr(slack_poller, "_WM_PATH", tmp_path / "wm.json")
        called = []
        import channels_api
        monkeypatch.setattr(channels_api, "_record_inbound", lambda *a: called.append(a))
        report = slack_poller.poll_once()
        assert report["delivered"] == 0
        assert called == []