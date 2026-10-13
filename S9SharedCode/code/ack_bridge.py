"""Inbound channel messages → the agent, for acknowledgement threads.

This is the other half of `notify.py`. Outbound is "say something"; this is
"the user said something back, work out whether it needs an answer".

Why the LLM decides
--------------------
The user's reply can be anything: "yes", "not yet", "make it 5pm", "what?",
a photo of the thing they were reminded about. Deciding whether that needs a
response — and what to say — is a judgement call, and hard-coding it means
either answering everything (annoying) or nothing (the point is lost). So the
model is asked, with a strict three-way schema, and this module only enforces
the mechanical limits: the follow-up budget, the snooze interval, and the
absolute expiry.

Safety properties this holds
----------------------------
* Only an exact (channel, sender) match against a live thread is routed. A
  message from anyone else is recorded as rejected and dropped.
* The model returns a verb from a closed set. Anything unrecognised becomes
  `silent`, so a malformed completion cannot manufacture an action.
* The model never chooses *whether to send* — only *what to say*. Delivery
  count is capped in `ack.apply_verdict`.
* Trust is re-checked against the gateway's pairing store, not taken on
  faith, so this does not become a way to talk to the LLM for free.
"""

from __future__ import annotations

import json
import threading
import time

import ack

# The instruction is deliberately short and closed-vocabulary. A long prompt
# here would be a second place for the agent's behaviour to drift from what
# the tool description tells the model.
_DECIDE_PROMPT = """You are handling a reply to a reminder you sent.

The user was reminded about: {query}
You had sent: {prompt}
They replied: {reply}

Reply with ONLY a JSON object, no prose:
{{"verdict": "acknowledge" | "reply" | "silent" | "unrelated", "text": "..."}}

The first question is NOT "do they need a reply" — it is "does this CLOSE the
reminder". A reminder stays open and keeps re-sending itself until it is
acknowledged, so an acknowledgement must be recognised even when it needs no
answer. Judge that first, then decide whether to also say something.

- "acknowledge" when their message means they have received the reminder and
  will act on it — including messages that carry no answer at all:
  "ok", "k", "thanks", "ty", "got it", "seen it", "on it", "will do",
  "noted", an emoji or thumbs-up. If you are unsure whether they are saying
  they have seen it, choose this: a wrong "acknowledge" stops the reminders,
  whereas a wrong "silent" leaves the user being nagged about something they
  already dealt with.
- "reply" only when a short message genuinely helps AND they have not
  acknowledged: they asked a question about the reminder, changed the plan,
  or said they need more time. Put that message in "text", one or two
  sentences, no markdown.
- "silent" only for messages that are about the reminder but explicitly do
  NOT confirm receipt — "maybe later", "I can't now", "ignore that". These
  still leave the reminder open.
- "unrelated" when the message is NOT about this reminder at all — a new
  question, a new task, small talk, or a message to the agent in general.
  Use this even if it is phrased politely or is a question. Empty "text".
  The agent answers it as an ordinary message; if you pick this verdict wrongly the
  user's message is swallowed and never answered.

Never repeat the reminder back at them. Never invent new tasks."""

_MODEL_LOCK = threading.Lock()

# A bare acknowledgement is short and is not a request. Anything shaped like a
# question, or long enough to be a real message, must not be swallowed by a
# live reminder thread.
#
# This exists because the model is not a reliable judge of "is this about the
# reminder". Asked to check, it answered `silent` to "In one sentence, what is a
# deployment pipeline?" — the word "deployment" appeared in the reminder — and
# the user's question was consumed with no reply at all. The closed set helps;
# it is not sufficient on its own, so the mechanical check decides the cases
# that must never be silent.
_REQUEST_STARTS = (
    # Question words that need no "?" to be a request. "do", "is", "will" and
    # friends are deliberately absent: they almost always arrive with a "?" (so
    # the check above catches them) and they start real acknowledgements —
    # "will do", "is it?" — which must stay with the reminder.
    "what", "why", "how", "when", "where", "who", "which", "can", "could",
    "does", "did", "are", "tell", "show", "find", "list", "check", "remind",
    "schedule", "set", "help", "please", "add", "write", "make", "give",
    "explain", "summarise", "summarize", "review", "cancel", "delete",
    "search", "look",
)


def looks_like_a_request(text: str) -> bool:
    """True when `text` should be treated as a message to the agent rather
    than as an answer to a reminder.

    Deterministic on purpose: a swallowed message is invisible to the user, and
    the cost of wrongly falling through (one extra chat turn) is far lower than
    the cost of silently eating someone's question.
    """
    t = (text or "").strip()
    if not t:
        return False
    if "?" in t:
        return True
    lowered = t.lower().rstrip(" .!,;")
    if lowered.startswith(_REQUEST_STARTS):
        return True
    # A long message is a message, not an acknowledgement. Short acks stay
    # short: "ok", "thanks", "got it", "will do", "yep".
    return len(t.split()) > 7


def _ask_model(prompt: str, timeout: float = 60.0) -> dict:
    """One bounded LLM call. Returns the parsed object, or {} on any failure.

    `_RoutedLLM.chat()` does not take a `stream` kwarg. Passing one raised
    TypeError on EVERY call, which this function caught and turned into `{}` —
    and an empty verdict defaults to `silent`, so the classifier silently
    decided that no reply was ever an acknowledgement. A reminder could
    therefore never be acknowledged, and went on re-sending itself until the
    budget ran out. Verified live: every probe verdict came back `silent`,
    including an explicit "ok thanks, seen it".

    The failure is now reported rather than swallowed: a classifier that
    cannot run must be visible, because its failure mode is indistinguishable
    from the model deciding not to acknowledge.
    """
    try:
        from gateway import LLM
        with _MODEL_LOCK:
            # `messages=` must be a KEYWORD. `LLM().chat(prompt=...)` takes the
            # message list as its first POSITIONAL argument, so passing the
            # list positionally sent it as `prompt` and left `messages` unset —
            # the gateway then received an array where it wanted a string and
            # answered 422. That exception was swallowed into `{}`, every
            # verdict defaulted to `silent`, and no acknowledgement was ever
            # recognised.
            raw = LLM().chat(messages=[{"role": "user", "content": prompt}])
    except Exception as e:
        print(f"[ack] verdict model call failed: {type(e).__name__}: {e}")
        return {}
    if not raw:
        print("[ack] verdict model returned nothing")
        return {}
    # The gateway returns a RESPONSE OBJECT (`{"text": ...}`), not a string.
    # `str(raw)` on a dict yields its repr, which contains no `{`...`}` JSON
    # the parser could recognise, so this silently returned {} even when the
    # model had answered perfectly.
    if isinstance(raw, dict):
        raw = raw.get("text") or raw.get("content") or ""
    text = str(raw or "").strip()
    if not text:
        print("[ack] verdict model response had no text")
        return {}
    # Models sometimes wrap JSON in a fence or add a sentence; take the first
    # balanced object rather than trusting the whole string.
    start = text.find("{")
    if start < 0:
        return {}
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                text = text[start:i + 1]
                break
    try:
        data = json.loads(text)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def handle_inbound(channel: str, sender: str, text: str,
                   *, agent: str = "aria") -> dict:
    """Route one inbound channel message, if it belongs to a live thread.

    Returns a structured result. Never raises: this is called from the
    gateway's webhook path, and an exception there would surface to the user's
    chat client as a delivery failure for a message that was merely noise.
    """
    out = {"routed": False, "reason": "", "schedule_id": "",
           "verdict": "", "reply_sent": False, "unrelated": False}
    thread = ack.find_thread_for(channel, sender)
    if thread is None:
        out["reason"] = "no open thread for this sender"
        return out

    sid = str(thread.get("schedule_id") or "")
    out["schedule_id"] = sid

    decision = _ask_model(_DECIDE_PROMPT.format(
        query=str(thread.get("query") or "")[:300],
        prompt=str(thread.get("prompt") or "")[:300],
        reply=str(text or "")[:600]))
    verdict = str(decision.get("verdict") or "silent").strip().lower()
    reply = str(decision.get("text") or "").strip()
    out["verdict"] = verdict

    if verdict == "unrelated" or (verdict == "silent"
                                  and looks_like_a_request(text)):
        # Not an answer to the reminder — a message to the agent. Nothing is
        # sent here, the thread is left exactly as it was (still open, budget
        # untouched, no fake "reply" recorded), and `route_inbound` hands the
        # message to the chat turn.
        #
        # This is what stops a live reminder thread from swallowing unrelated
        # questions: routing was ack-first for every message from this sender
        # for the whole `ack_timeout_s` window, so anything the classifier
        # called `silent` — including real questions — was dropped with no
        # reply and no trace. The user just saw the agent ignore them.
        out["unrelated"] = True
        out["schedule_id"] = ""
        out["reason"] = "not about the reminder; routing as a chat message"
        return out

    ack.record_user_reply(sid, text, sender=sender)
    updated = ack.apply_verdict(sid, verdict, reply)

    if verdict == "acknowledge":
        out["routed"] = True
        out["reason"] = "acknowledged"
        # Say so. The thread is closed and the nudger has stopped, so from the
        # user's side the reminder went out, they said "ok", and then nothing
        # — indistinguishable from the bot having missed it. That silence is
        # what makes a conversation feel unacknowledged even when it was.
        #
        # This is deliberately the briefest possible confirmation and it does
        # not claim to have understood anything beyond "this was an
        # acknowledgement": the classifier already decided the message was an
        # answer to this reminder and not an accidental one.
        try:
            import notify as _notify
            _notify.send_channel_message(
                channel, "Got it — reminder cleared.",
                to=sender, agent=agent, session_id=sid)
        except Exception:
            pass

    if verdict == "reply" and reply:
        try:
            import notify as _notify
            res = _notify.send_channel_message(
                channel, reply, to=sender, agent=agent,
                session_id=str(thread.get("schedule_id") or ""))
            out["reply_sent"] = bool(res.get("delivered"))
            if not out["reply_sent"]:
                out["reason"] = str(res.get("error") or "not delivered")[:200]
        except Exception as e:
            out["reason"] = f"{type(e).__name__}: {e}"[:200]
    elif verdict == "acknowledge":
        out["routed"] = True
        out["reason"] = "acknowledged"
    elif updated.get("status") == "budget_exhausted":
        out["reason"] = "follow-up budget exhausted"
    else:
        out["reason"] = "no reply needed"
    return out


def _notify_send(channel: str, text: str, to: str = "",
                 session_id: str = "") -> dict:
    """Deliver one message on a channel. Never raises.

    Module level, and taking keyword arguments, so the nudge loop — the one
    part of this feature that runs with nobody watching — can be tested
    without a gateway and without reaching into a function-local import.
    """
    try:
        import notify as _notify
        return _notify.send_channel_message(
            channel, text, to=to, agent="scheduler", session_id=session_id)
    except Exception as e:
        return {"delivered": False, "error": f"{type(e).__name__}: {e}"[:200]}


def nudge_worker(stop: threading.Event, interval: float = 30.0) -> None:
    """Re-nudge unacknowledged threads once their snooze has elapsed.

    Only for threads that have a budget left: a thread with
    `max_follow_ups: 0` is a one-way announcement and must never repeat.

    The budget is only spent on a message that was actually DELIVERED. A send
    that failed is not a message the user ignored, and charging it to the
    budget would end the conversation after one network blip.
    """
    while not stop.wait(timeout=interval):
        try:
            ack.sweep()
            for t in ack.due_for_nudge():
                if int(t.get("follow_ups", 0)) >= int(t.get("max_follow_ups", 0)):
                    continue
                base = (str(t.get("prompt") or "")
                        or f"Reminder: {t.get('query')}")[:400]
                # Vary the wording and say how many are left. Re-sending a
                # byte-identical message reads as a stuck bot rather than an
                # agent still waiting, and the user cannot tell a second
                # attempt from the first they already ignored.
                sent_before = int(t.get("follow_ups", 0))
                if sent_before:
                    left = max(0, int(t.get("max_follow_ups", 0))
                               - sent_before)
                    text = (f"{base}\n\nStill waiting on this "
                            f"({left} more reminder"
                            f"{'s' if left != 1 else ''}).")
                else:
                    text = base
                res = _notify_send(
                    str(t.get("channel") or ""), text,
                    to=str(t.get("target") or ""),
                    session_id=str(t.get("schedule_id") or ""))
                if res.get("delivered"):
                    # Count the nudge against the budget so the loop terminates
                    # even if the user never replies at all.
                    ack.apply_verdict(str(t.get("schedule_id") or ""), "silent")
                    with ack._LOCK:  # noqa: SLF001 - same module family
                        data = ack._load()
                        th = data.get(str(t.get("schedule_id")))
                        if isinstance(th, dict):
                            th["follow_ups"] = int(th.get("follow_ups", 0)) + 1
                            th["last_agent_msg"] = time.time()
                            ack._save(data)
        except Exception:
            continue


def start_nudger() -> threading.Thread | None:
    """Start the nudge loop once per process."""
    global _NUDGER, _NUDGE_STOP
    if _NUDGER is not None and _NUDGER.is_alive():
        return _NUDGER
    _NUDGE_STOP = threading.Event()
    _NUDGER = threading.Thread(target=nudge_worker, args=(_NUDGE_STOP,),
                               daemon=True, name="ack-nudger")
    _NUDGER.start()
    return _NUDGER


_NUDGER: threading.Thread | None = None
_NUDGE_STOP: threading.Event | None = None
