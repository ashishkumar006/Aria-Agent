"""Talking to the agent FROM a chat channel, not just TO one.

Until now a channel was outbound-only: the agent could push a reminder, but a
message the user typed back was verified, paired, logged — and dropped. This
makes it a chat turn.

The shape
---------
    Telegram → gateway webhook /v1/hooks/telegram
             → normalise, trust, pair            (channels_api)
             → POST /api/channel/inbound         (this gateway forwards)
             → route_inbound: ack thread first   (this module)
             → then a trust-gated chat turn
             → POST /api/chat/simple/stream (self, loopback)
                                    → memory, docs, tools, thread history
             → POST /v1/channels/{name}/send     → back to the user

The chat turn goes through the agent's own HTTP API rather than a reimplementation
of `chat_simple_stream`. That is deliberate: it reuses the whole pipeline (per-thread
history, memory recall, document grounding, the tool loop, cost recording) with no
second implementation to keep in step, and the loopback call costs nothing.

The lightweight stream is the right target, not `POST /api/chat`: that route runs
the full research DAG (planner, researcher, formatter, critic), and "what time is
it" should not cost a multi-node run.

Safety properties
-----------------
* **An acknowledgement thread owns its own replies.** A reply to a reminder is
  routed to `ack_bridge` first and never falls through to a general chat turn:
  "yes I took the pills" is an answer to the reminder, not a new question, and
  answering it twice is worse than not at all.
* **Trust is required.** Only a sender the pairing store records as `owner` or
  `paired` may drive an agent turn. Anything else is refused and logged, so the
  bot cannot be used by a stranger to spend tokens.
* **One conversation per (channel, sender)**, derived deterministically and
  stably — a restart must not hand a returning user a new, empty thread.
* **The webhook answers immediately.** The turn runs in a daemon thread, because
  a chat platform retries on a slow webhook, and a reply arriving twice is worse
  than one arriving late.
* **The reply is bounded and failures are visible.** A turn that raises is
  reported to the user rather than swallowed, because a channel that goes quiet
  is indistinguishable from one that was never wired up.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

STATE_DIR = Path(os.environ.get("S9_STATE_DIR")
                 or (Path(__file__).parent / "state"))
MAP_PATH = STATE_DIR / "channel_conversations.json"
_LOCK = threading.Lock()

# In-flight turns, so a rapid-fire reply does not start three paid runs on the
# same conversation. Bounded and self-clearing; a turn that never finishes is
# not blocked forever.
_INFLIGHT: dict[str, float] = {}
_INFLIGHT_TTL_S = 600.0

# How much of an answer goes in one channel message, and how long to wait for
# one. Chat platforms have hard message-size limits far below the console's;
# longer answers are split, never truncated (see `_chunks`).
_MAX_REPLY = 3500


def _chunks(text: str) -> list[str]:
    """Split a reply on paragraph/sentence boundaries at the channel limit.

    `answer[:_MAX_REPLY]` is what a Telegram-initiated research request used to
    get: one message containing the first 3500 characters of a 17k-character
    report, with no indication that the rest existed. The console's send tool
    already chunked (Telegram's hard cap is 4096); this path never did.
    """
    t = (text or "").strip()
    out: list[str] = []
    while len(t) > _MAX_REPLY:
        cut = -1
        for sep in ("\n\n", "\n", ". ", " "):
            pos = t.rfind(sep, 0, _MAX_REPLY)
            if pos >= _MAX_REPLY // 2:
                cut = pos + (1 if sep == ". " else 0)
                break
        if cut <= 0:
            cut = _MAX_REPLY
        out.append(t[:cut].rstrip())
        t = t[cut:].lstrip()
    if t:
        out.append(t)
    return out or [""]


def _self_base() -> str:
    """This agent's own base URL, for loopback self-calls.

    The gateway already points at the agent with `ARIA_AGENT_URL`; reading the
    same variable here means a deployment that moves the agent needs one change,
    and the default matches the standard port."""
    return os.environ.get("ARIA_AGENT_URL",
                          "http://127.0.0.1:8500").rstrip("/")


def conversation_id_for(channel: str, sender: str) -> str:
    """A stable conversation id for one person on one channel.

    Deterministic and *stable across processes* — `hash()` of a str is salted
    per interpreter (PYTHONHASHSEED), so an id derived from it would change on
    every restart and silently hand a returning user an empty history. The
    channel prefix keeps Telegram and Slack identities from colliding even if a
    platform ever reuses a numeric id, and the hex digest keeps the id within
    the plain-id rule the chat routes enforce.
    """
    digest = hashlib.sha1(f"{channel}:{sender}".encode("utf-8")).hexdigest()
    return f"{channel[:12]}-{digest[:12]}"


def _load_map() -> dict:
    try:
        data = json.loads(MAP_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_map(data: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = MAP_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(MAP_PATH)
    except Exception:
        pass


def remember(channel: str, sender: str, conversation_id: str) -> None:
    with _LOCK:
        data = _load_map()
        data[f"{channel}:{sender}"] = {
            "conversation_id": conversation_id,
            "updated": time.time(),
        }
        _save_map(data)


def known(channel: str, sender: str) -> str:
    with _LOCK:
        return str((_load_map().get(f"{channel}:{sender}") or {})
                   .get("conversation_id") or "")


# ── trust ────────────────────────────────────────────────────────────────

def trust_level(channel: str, sender: str) -> str:
    """owner | paired | untrusted, from the gateway's pairing store.

    Read from the gateway rather than trusting the request, because this
    endpoint is reachable by anything that can reach the agent. An unpaired
    sender gets no agent turn, no matter what they sent.
    """
    if not sender:
        return "untrusted"
    try:
        from notify import _gw_get
        data = _gw_get(f"/v1/control/trust?channel={channel}&sender={sender}")
        if not (data or {}).get("error"):
            return str((data or {}).get("trust") or "untrusted")
    except Exception:
        pass
    # Unknown is not "allowed". If the pairing store cannot be read the caller
    # gets a refusal, not a free turn.
    return "unknown"


# ── routing ──────────────────────────────────────────────────────────────

def route_inbound(channel: str, sender: str, text: str,
                  attachments: list | None = None) -> dict:
    """Where one inbound message goes: the ack thread, or a chat turn.

    An open acknowledgement thread for this (channel, sender) gets first
    refusal, but only over messages that are actually about the reminder:
    `ack_bridge` answers `unrelated` when the message is a new question or a
    new task, and that falls through here untouched. Ack-first for *everything*
    was the earlier design, and it meant that for the whole `ack_timeout_s`
    window (15 minutes by default) every question the user asked was handed to
    the reminder classifier — which usually answered `silent`, producing no
    reply at all. The user saw the agent ignore them.

    A message the ack path did consume is never re-handled as a chat turn,
    whatever its verdict, including `silent`.

    `attachments` bypasses the ack path entirely: someone who has just sent a
    file is not acknowledging a reminder, and the classifier would only see
    the caption and mis-file the upload.
    """
    if attachments:
        turn = handle_inbound(channel, sender, text, attachments=attachments)
        return {"path": "chat", "ack_verdict": "attachment", **turn}
    try:
        import ack_bridge
        acked = ack_bridge.handle_inbound(channel, sender, text)
    except Exception as e:
        # A crash in the ack path must not silently become a chat turn: the
        # user's reply belongs to the reminder, and a general answer to it
        # would be confusing. Report and stop.
        return {"path": "ack", "routed": False,
                "reason": f"ack routing failed: {type(e).__name__}: {e}"[:200]}
    if acked.get("schedule_id") or acked.get("routed"):
        return {"path": "ack", **acked}
    if acked.get("unrelated"):
        # Not about the reminder: carry on as an ordinary message. The thread
        # was left open by ack_bridge, so the reminder is still pending.
        turn = handle_inbound(channel, sender, text)
        return {"path": "chat", "ack_verdict": "unrelated", **turn}

    # No live thread: this is a person talking to the agent.
    turn = handle_inbound(channel, sender, text)
    return {"path": "chat", **turn}


# ── the turn ─────────────────────────────────────────────────────────────

def _run_turn(channel: str, sender: str, text: str,
              conversation_id: str, attachments: list | None = None) -> dict:
    """One agent turn on a channel, answered back over the same channel."""
    out = {"conversation_id": conversation_id, "ok": False, "answer": "",
           "error": ""}
    try:
        from notify import send_channel_message

        answer, err, produced = _ask_agent(text, conversation_id, attachments)
        if err:
            out["error"] = err
            send_channel_message(
                channel, f"I could not answer that: {err}"[:1200],
                to=sender, agent="channel-chat",
                session_id=conversation_id)
            return out
        if not answer:
            # No answer is a failure, but silence is the worst outcome: the
            # user waits forever for a message that is never coming.
            out["error"] = "empty answer from agent"
            answer = "(I had no answer for that — try rephrasing?)"

        out["ok"] = True
        out["answer"] = answer
        out["files"] = produced
        # Attach the artifacts when the channel can carry them. A Telegram
        # chat cannot open a link back to this server, so a rendered PDF
        # that stays here is indistinguishable from one that was never made.
        media = _channel_media(channel, produced)
        if media and len(answer) + 200 <= _MAX_REPLY:
            answer = (answer + "\n\n"
                      + " ".join(f"Attached: {m['filename']}"
                                 for m in media))
        pieces = _chunks(answer)
        sent = 0
        for idx, piece in enumerate(pieces):
            res = send_channel_message(
                channel, piece, to=sender, agent="channel-chat",
                session_id=conversation_id,
                media=(media if idx == 0 else None) or None)
            if not res.get("delivered"):
                out["error"] = (str(res.get("error") or "not delivered")
                                + (f" (after {sent} of {len(pieces)} messages)"
                                   if sent else ""))[:300]
                break
            sent += 1
        out["chunks"] = sent
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"[:300]
    return out


def _channel_media(channel: str, produced: list[dict]) -> list[dict]:
    """Turn artifact handles into the base64 payloads a channel can carry.

    Only Telegram implements media here, so anything else gets text alone
    rather than a reply promising an attachment that will not arrive. Bytes
    are inlined rather than linked because a chat client has no route back to
    this server.
    """
    if not produced or (channel or "").lower() != "telegram":
        return []
    import base64

    import artifacts as _arts
    out = []
    for item in produced[:3]:
        try:
            blob = _arts.get_bytes(str(item.get("artifact") or ""))
            if not blob:
                continue
            out.append({
                "filename": str(item.get("filename") or "document"),
                "data_b64": base64.b64encode(blob).decode("ascii"),
                "caption": str(item.get("filename") or ""),
            })
        except Exception:
            continue
    return out


def _describe_attachments(attachments: list | None) -> str:
    """Turn inbound file metadata into a line the agent can act on.

    The gateway has already downloaded each file into the Documents store by
    the time this runs, so the useful thing to tell the agent is that the file
    is there and searchable - not to pass bytes it cannot read anyway. Naming
    it lets the agent say "that's the contract you just sent" instead of
    claiming it received nothing.
    """
    lines = []
    for a in (attachments or []):
        name = str(a.get("name") or "").strip() or "(unnamed)"
        kind = str(a.get("kind") or "file")
        size = int(a.get("size") or 0)
        human = f"{size // 1024} KB" if size >= 1024 else f"{size} bytes"
        lines.append(f"- {name} ({kind}, {human}) — saved to the user's "
                     f"Documents, searchable with search_knowledge")
    if not lines:
        return ""
    return ("\n\nThe user sent you " +
            ("this file" if len(lines) == 1 else "these files") +
            " from their chat app:\n" + "\n".join(lines) +
            "\nThey are already saved in the Documents section.")


def _ask_agent(text: str, conversation_id: str,
               attachments: list | None = None) -> tuple[str, str, list]:
    """Drive the agent's own chat API over loopback.

    Returns (answer, error, produced_files). The third element is the artifact
    handles the answer produced, so a channel reply can attach them - a
    document rendered on a Telegram request used to stay on the server and the
    user got a caption with nothing attached.

    Reusing `/api/chat/simple/stream` rather than reimplementing the turn is the
    point: one implementation of history, memory, documents, the tool loop and
    cost. Streamed rather than buffered, so a long answer is not cut off by a
    proxy timeout and the tool-progress frames are visible as they happen."""
    import httpx
    from agent_server import _self_token

    body = {"query": (text or "") + _describe_attachments(attachments),
            "conversation_id": conversation_id}
    answer_parts: list[str] = []
    produced: list[dict] = []
    err = ""
    try:
        with httpx.Client(timeout=600.0,
                          headers={"X-Aria-Token": _self_token()}) as c:
            with c.stream("POST", f"{_self_base()}/api/chat/simple/stream",
                          json=body) as r:
                if r.status_code != 200:
                    # A 502 here is the gateway being down; 400 is a bad
                    # request. Both bodies are short and worth repeating.
                    detail = ""
                    try:
                        detail = r.json().get("error") or ""
                    except Exception:
                        detail = ""
                    return "", (f"agent returned HTTP {r.status_code}"
                                + (f": {detail}"[:200] if detail else "")), produced
                for raw in r.iter_lines():
                    if not raw or not raw.startswith("data:"):
                        continue
                    try:
                        frame = json.loads(raw[5:].strip())
                    except Exception:
                        continue
                    t = frame.get("type")
                    if t == "delta":
                        answer_parts.append(str(frame.get("text") or ""))
                    elif t == "done":
                        # Files the answer produced (a rendered PDF, a deck).
                        # The `done` frame has carried these since the chat
                        # visuals work; a channel reply threw them away, so
                        # "make me a deck" produced a file the user could not
                        # receive on the channel they asked from.
                        for f in (frame.get("files") or [])[:3]:
                            if not isinstance(f, dict):
                                continue
                            fid = str(f.get("artifact") or "")
                            if fid:
                                produced.append({
                                    "artifact": fid,
                                    "filename": str(f.get("filename") or "document"),
                                    "format": str(f.get("format") or ""),
                                })
                        if frame.get("answer"):
                            # The complete answer supersedes the deltas, which
                            # can be truncated by whatever cut the stream.
                            answer_parts = [str(frame["answer"])]
                    elif t == "error":
                        err = str(frame.get("text") or "agent error")[:300]
    except Exception as e:
        return "", f"{type(e).__name__}: {e}"[:300], produced
    if err and not answer_parts:
        return "", err, produced
    return "".join(answer_parts).strip(), "", produced


def _tell(channel: str, sender: str, text: str, session_id: str = "") -> None:
    """Best-effort note back to the sender. Never raises.

    Only ever called for a sender who has already passed the trust gate, or
    after the caller decided the channel is safe to talk on: a refusal the
    user cannot see is indistinguishable from the agent ignoring them, which
    is exactly the failure this exists to prevent.
    """
    try:
        from notify import send_channel_message
        send_channel_message(channel, text[:_MAX_REPLY], to=sender,
                             agent="channel-chat", session_id=session_id)
    except Exception:
        pass


def handle_inbound(channel: str, sender: str, text: str,
                   attachments: list | None = None) -> dict:
    """Accept an inbound channel message. Never raises; returns immediately.

    The heavy work happens on a daemon thread, because a chat platform retries
    a slow webhook and a duplicated reply is worse than a late one."""
    out = {"accepted": False, "reason": "", "conversation_id": "",
           "trust": ""}
    channel = (channel or "").strip().lower()
    sender = str(sender or "").strip()
    text = str(text or "").strip()
    attachments = [a for a in (attachments or []) if isinstance(a, dict)]
    if not channel or not sender or (not text and not attachments):
        out["reason"] = "channel, sender, and either text or a file, are required"
        # Nothing arrived that the agent could read. Say so, rather than
        # accepting nothing and leaving the user waiting: the previous
        # behaviour was silence, which reads as being ignored.
        if channel and sender and not text and not attachments:
            _tell(channel, sender,
                  "That came through empty — I can't read it. Send it again, "
                  "or send the file as a document with a caption.")
        return out

    level = trust_level(channel, sender)
    out["trust"] = level
    if level not in ("owner", "paired"):
        out["reason"] = (f"sender is not paired on {channel} "
                         f"(trust={level}); no agent turn was started")
        # No reply to an unpaired sender: that would make the bot a free
        # "message anyone" service for whoever finds it.
        return out

    key = f"{channel}:{sender}"
    now = time.time()
    with _LOCK:
        _INFLIGHT.update({k: v for k, v in _INFLIGHT.items()
                          if now - v < _INFLIGHT_TTL_S})
        if key in _INFLIGHT:
            out["reason"] = "a turn for this sender is already running"
            # Two messages in quick succession used to mean the second one
            # vanished with no explanation at all.
            _tell(channel, sender,
                  "Still working on your previous message — one at a time. "
                  "I'll come back to you in a moment.")
            return out
        _INFLIGHT[key] = now

    cid = known(channel, sender) or conversation_id_for(channel, sender)
    out["conversation_id"] = cid
    remember(channel, sender, cid)
    out["accepted"] = True
    out["reason"] = "turn started"

    threading.Thread(target=_safe_turn,
                     args=(channel, sender, text, cid, attachments),
                     daemon=True, name=f"chan-{channel}").start()
    return out


def _safe_turn(channel: str, sender: str, text: str, cid: str,
               attachments: list | None = None) -> None:
    key = f"{channel}:{sender}"
    try:
        _run_turn(channel, sender, text, cid, attachments)
    except Exception as e:  # pragma: no cover - must never kill the thread
        try:
            from notify import send_channel_message
            send_channel_message(channel,
                                 f"Something went wrong handling that: {e}"[:800],
                                 to=sender, agent="channel-chat")
        except Exception:
            pass
    finally:
        with _LOCK:
            _INFLIGHT.pop(key, None)
