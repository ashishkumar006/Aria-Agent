"""Aria's evolving, AI-written portrait of the user.

This is deliberately NOT a render of the memory drawers. Memory is the
friend's factual/episodic record — what was said, what happened, what was
asked for. The persona is the friend's *impression*: a short, human portrait
that develops across interactions, keeps what still reads true, revises what
changed, and says what is still uncertain.

Storage (under the agent state dir)::

    persona/current.md     the latest portrait
    persona/evolution.md   dated full versions, oldest first
    persona/meta.json      version, counters, provenance, history

Generation is one gateway call. The previous portrait is fed back in, so the
new version evolves rather than starting over. The prompt forbids clinical
labels, invented facts and memory dumps, and requires hedged language.

The store is path-injectable so tests never touch live state and never need a
gateway. `generate()` is synchronous; the agent server calls it from a worker
thread or `asyncio.to_thread`.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
DEFAULT_STATE_DIR = Path(os.environ.get("S9_STATE_DIR") or (ROOT / "state"))

SCAFFOLD = """# Aria's read on you — not enough interactions yet

I haven't formed a stable read on you yet. This page is my evolving
impression of you, not a copy of the memory list. After a few conversations —
or when you press **regenerate persona** — I'll write about how you come
across, how you think and work, how you like to be helped, and how my read on
you is changing.

Everything here is a working impression, not a diagnosis, and it is meant to
be corrected.
"""

PERSONA_PROMPT = """You are Aria, writing your private, evolving read on
someone you know well. This is not a memory dump and not a clinical profile.
It is the kind of understanding a close friend forms over time: how you come
across, what you seem to value, how you like to be helped, and where my read
on you has changed.

You will see your PREVIOUS PORTRAIT, a digest of RECENT INTERACTIONS, MEMORY
NOTES (things you told me, standing preferences, behavioural observations)
and FEEDBACK NOTES (explicit corrections or reactions).

Write the CURRENT PORTRAIT as markdown, 400-800 words, with exactly these
sections:

# Aria's read on you — <today's date>
## How you come across
## How you think and work
## How you like to be helped
## What seems to matter to you
## How my read on you is changing
## What I'm still unsure about

Rules:
- Write directly TO me — the reader — in the second person: "you", "your".
  Warm, direct, familiar. Never call me "the user", never write about me in
  the third person ("they", "their"), and never sound like a report.
- EVOLVE the previous portrait. Keep what still reads true. Revise what
  changed. If a previous impression is now contradicted, say so under "How my
  read on you is changing" rather than silently deleting it.
- Base every claim on the supplied interactions, memory or feedback. Never
  invent facts, names, jobs, relationships, events or quotes.
- Use hedged language: "you seem", "you tend to", "I've noticed", "may".
  Never present an inference as a fact.
- No clinical or personality-disorder labels, no claims about intelligence,
  motives or emotions, and no sensitive inference (health, sexuality, religion,
  politics, finances) unless you stated it explicitly and it matters to how
  you work.
- Do not dump or quote the memory list, do not enumerate every preference, and
  do not mention drawers, vectors, records, memory types or internal systems.
  Synthesise into a portrait.
- If there is very little material, say so plainly and keep it short.
- This portrait is allowed to be wrong. End with one short line inviting
  correction.
- Markdown only, no code fences, no preamble before the title.

PREVIOUS PORTRAIT:
{previous}

RECENT INTERACTIONS (oldest first):
{interactions}

MEMORY NOTES:
{memories}

FEEDBACK NOTES:
{feedback}

Today: {date}
"""

_TEST_THREAD = re.compile(
    r"(playwright|probe|test query|reply with exactly|reply with one short "
    r"sentence|reply with the word|answer with the colour only|"
    r"remember the number|session restore|qx-7|default_ok|\bconc_[0-9]|"
    r"idem|hello from playwright|trigger an error|"
    r"what do you know about how i prefer answers|"
    r"explain why the sky is blue|what is my favourite colour|"
    r"use the list_dir tool|count from 1 to|say the alphabet|"
    r"describe in \d+ words|write \d+ words|tell me a joke|"
    r"hello, how are you|hello, what is the weather|"
    r"size-test|chat mode test|research mode test)",
    re.IGNORECASE)
_TEST_TEXT = re.compile(
    r"(reply with exactly|reply with one short sentence|reply with the word|"
    r"answer with the colour only|remember the number|"
    r"ignore all previous instructions|what is the qx-7|default_ok|"
    r"\bconc_[0-9]|\bidem\b|trigger an error|hello from playwright|"
    r"playwright test|use the list_dir tool|"
    r"what is my favourite colour|count from 1 to|say the alphabet|"
    r"describe in \d+ words|write \d+ words|"
    r"hello, how are you|hello, what is the weather|tell me a joke|"
    r"size-test|chat mode test|research mode test)",
    re.IGNORECASE)
_TEST_ID = re.compile(
    r"(size-test|idem-|med-confirm|large-confirm|timeout-focus|"
    r"chat mode test|research mode test)",
    re.IGNORECASE)

CRITIC_PROMPT = """You are auditing Aria's evolving portrait for grounding and
voice.

Rewrite the DRAFT so that:
- every claimed preference, request, fact, name, number or event is actually
  present in the SOURCES (the previous portrait and the draft are not sources);
- unsupported claims are removed or rewritten as explicitly uncertain
  impressions ("may", "seems"), without pretending you stated them;
- the whole portrait speaks directly TO the reader in the second person
  ("you", "your"), like a close friend — never "the user", never "they" or
  "their" about the reader, never a clinical report.
Do not add new claims. Keep the same markdown sections, the warm human tone,
and roughly the same length. Return ONLY the corrected markdown.

SOURCES:
{source}

DRAFT:
{draft}
"""

_STATE_LOCK = threading.Lock()
_GENERATE_LOCK = threading.Lock()


def _read(path: Path, default):
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError:
        return default
    if not text.strip():
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(
        f"{path.name}.tmp-{os.getpid()}-{threading.get_ident()}-"
        f"{uuid.uuid4().hex[:8]}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _clip(text, limit: int) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return value if len(value) <= limit else value[:limit].rstrip() + "…"


def _clean_generated(text: str, date: str) -> str:
    value = str(text or "").strip()
    if value.startswith("```"):
        value = value.strip("`")
        if value.lower().startswith("markdown"):
            value = value[8:].lstrip()
    value = value.strip()
    if not value:
        return ""
    if not value.startswith("#"):
        value = f"# Aria's read on you — {date}\n\n{value}"
    return value[:12_000].rstrip() + "\n"


class PersonaStore:
    def __init__(self, *, state_dir=None, chat_threads_path=None,
                 turn_logs_path=None, feedback_path=None,
                 memory_api=None, chat_fn=None, critic_fn=None):
        base = Path(state_dir or DEFAULT_STATE_DIR)
        self.base = base
        self.dir = base / "persona"
        self.current_path = self.dir / "current.md"
        self.evolution_path = self.dir / "evolution.md"
        self.meta_path = self.dir / "meta.json"
        self.chat_threads_path = Path(
            chat_threads_path or (base / "chat_threads.json"))
        self.turn_logs_path = Path(
            turn_logs_path or (base / "turn_logs.json"))
        self.feedback_path = Path(feedback_path or (base / "feedback.jsonl"))
        self.memory_api = memory_api
        self.chat_fn = chat_fn
        self.critic_fn = critic_fn

    # ── read side ───────────────────────────────────────────────────────
    def _mem(self):
        if self.memory_api is not None:
            return self.memory_api
        import memory as memory_mod
        return memory_mod

    def has_persona(self) -> bool:
        try:
            text = self.current_path.read_text(encoding="utf-8-sig").strip()
        except OSError:
            return False
        return bool(text) and not text.startswith(
            "# Persona — not enough interactions yet")

    def current_markdown(self) -> str:
        try:
            text = self.current_path.read_text(encoding="utf-8-sig").strip()
        except OSError:
            return SCAFFOLD
        return text + "\n" if text else SCAFFOLD

    def evolution_markdown(self) -> str:
        try:
            return self.evolution_path.read_text(encoding="utf-8-sig")
        except OSError:
            return ""

    def meta(self) -> dict:
        data = _read(self.meta_path, {})
        if not isinstance(data, dict):
            data = {}
        data.setdefault("version", 0)
        data.setdefault("updated_at", 0)
        data.setdefault("updated_iso", "")
        data.setdefault("reason", "")
        data.setdefault("turns_since_update", 0)
        data.setdefault("total_turns", 0)
        data.setdefault("history", [])
        data.setdefault("based_on", {})
        return data

    def _save_meta(self, meta: dict) -> None:
        _atomic_text(self.meta_path,
                     json.dumps(meta, indent=2, ensure_ascii=False))

    def note_turn(self) -> tuple[int, int]:
        """Count one completed chat turn. Returns (since_update, total)."""
        with _STATE_LOCK:
            meta = self.meta()
            meta["turns_since_update"] = int(
                meta.get("turns_since_update") or 0) + 1
            meta["total_turns"] = int(meta.get("total_turns") or 0) + 1
            self._save_meta(meta)
            return (meta["turns_since_update"], meta["total_turns"])

    # ── inputs ──────────────────────────────────────────────────────────
    def _chat_interactions(self, limit: int = 28) -> list[tuple]:
        threads = _read(self.chat_threads_path, {})
        if not isinstance(threads, dict):
            return []
        rows: list[tuple] = []
        for cid, thread in threads.items():
            if not isinstance(thread, dict):
                continue
            title = str(thread.get("title") or "")
            if _TEST_THREAD.search(title) or _TEST_ID.search(str(cid)):
                continue
            updated = float(thread.get("updated") or 0)
            messages = thread.get("messages") or []
            if not isinstance(messages, list):
                continue
            for idx, msg in enumerate(messages[-8:]):
                if not isinstance(msg, dict):
                    continue
                role = str(msg.get("role") or "")
                if role not in ("user", "assistant"):
                    continue
                if msg.get("pending") or msg.get("kind") in ("error", "stopped"):
                    continue
                text = _clip(msg.get("content"), 320)
                if not text or _TEST_TEXT.search(text):
                    continue
                rows.append((updated, idx, title, role, text))
        rows.sort(key=lambda r: (r[0], r[1]))
        selected = rows[-limit:]
        lines = []
        for _, _, title, role, text in selected:
            who = "USER" if role == "user" else "ARIA"
            lines.append(f"[{_clip(title, 60)}] {who}: {text}")
        return lines

    def _dag_interactions(self, limit: int = 12) -> list[str]:
        data = _read(self.turn_logs_path, {})
        if not isinstance(data, dict):
            return []
        rows: list[tuple] = []
        for _sid, turns in data.items():
            if not isinstance(turns, list):
                continue
            for turn in turns[-12:]:
                if not isinstance(turn, dict):
                    continue
                q = str(turn.get("q") or "")
                a = str(turn.get("a") or "")
                if not q.strip() or _TEST_TEXT.search(q):
                    continue
                rows.append((str(turn.get("ts") or ""), q, a))
        rows.sort(key=lambda r: r[0])
        selected = rows[-limit:]
        return [f"[DAG] USER: {_clip(q, 300)}\nARIA: {_clip(a, 500)}"
                for _, q, a in selected]

    def _memory_digest(self) -> tuple[str, dict]:
        try:
            items = self._mem().list_recent(
                limit=240, hide_superseded=True) or []
        except Exception as e:                       # noqa: BLE001
            print(f"[persona] memory unavailable: {e!r}")
            items = []
        prefs: list[str] = []
        obs: list[str] = []
        facts: list[str] = []
        for item in items:
            kind = str(getattr(item, "kind", "") or "")
            descriptor = _clip(getattr(item, "descriptor", ""), 240)
            value = getattr(item, "value", None) or {}
            drawer = str(getattr(item, "drawer", "") or "")
            source = str(getattr(item, "source", "") or "")
            if not descriptor:
                continue
            if kind == "preference":
                if descriptor not in prefs:
                    prefs.append(descriptor)
            elif kind == "observation":
                evidence = _clip(value.get("evidence") if isinstance(value, dict)
                                 else "", 200)
                line = descriptor + (f' (their words: "{evidence}")'
                                     if evidence else "")
                if line not in obs:
                    obs.append(line)
            elif kind == "fact" and drawer != "document" \
                    and not source.startswith("document:") \
                    and source != "feedback_note":
                if descriptor not in facts:
                    facts.append(descriptor)
        parts = []
        if prefs:
            parts.append("Standing preferences:\n"
                         + "\n".join(f"- {p}" for p in prefs[:25]))
        if obs:
            parts.append("Behavioural observations:\n"
                         + "\n".join(f"- {o}" for o in obs[:15]))
        if facts:
            parts.append("Things they have told you:\n"
                         + "\n".join(f"- {f}" for f in facts[:20]))
        return ("\n\n".join(parts) if parts else "(none yet)"), {
            "preferences": len(prefs), "observations": len(obs),
            "facts": len(facts),
        }

    def _feedback_digest(self) -> tuple[str, int]:
        rows = []
        try:
            lines = self.feedback_path.read_text(
                encoding="utf-8-sig").splitlines()
        except OSError:
            lines = []
        for raw in lines[-50:]:
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            note = _clip(entry.get("note"), 280)
            if not note or _TEST_TEXT.search(note):
                continue
            vote = int(entry.get("vote") or 0)
            rows.append(f"- {'up' if vote == 1 else 'down' if vote == -1 else 'neutral'}: {note}")
        return ("\n".join(rows[-12:]) if rows else "(none yet)"), len(rows)

    # ── generation ──────────────────────────────────────────────────────
    def build_prompt(self) -> tuple[str, str, dict]:
        previous = self.current_markdown()
        if not self.has_persona():
            previous = "(no previous portrait — this is the first pass)"
        chat_lines = self._chat_interactions()
        dag_lines = self._dag_interactions()
        interaction_parts = []
        if chat_lines:
            interaction_parts.append("\n".join(chat_lines))
        if dag_lines:
            interaction_parts.append("\n".join(dag_lines))
        interactions = "\n\n".join(interaction_parts) or "(no recent interactions)"
        memories, counts = self._memory_digest()
        feedback, feedback_count = self._feedback_digest()
        counts["feedback_notes"] = feedback_count
        counts["interactions"] = len(chat_lines) + len(dag_lines)
        date = datetime.now().astimezone().strftime("%d %B %Y")
        sources = (
            "RECENT INTERACTIONS:\n" + interactions
            + "\n\nMEMORY NOTES:\n" + memories
            + "\n\nFEEDBACK NOTES:\n" + feedback
        )
        prompt = PERSONA_PROMPT.format(
            previous=previous[:6_000],
            interactions=interactions[:8_000],
            memories=memories[:5_000],
            feedback=feedback[:2_500],
            date=date,
        )
        return prompt, sources, counts

    def generate(self, reason: str = "manual") -> dict:
        """Write a new portrait. Keeps the old one on any failure."""
        with _GENERATE_LOCK:
            if self.chat_fn is None:
                return {"ok": False, "error": "no persona model configured"}
            prompt, sources, counts = self.build_prompt()
            try:
                text = self.chat_fn(prompt)
            except Exception as e:                   # noqa: BLE001
                return {"ok": False, "error": f"{type(e).__name__}: {e}"}
            if not str(text or "").strip():
                return {"ok": False,
                        "error": "persona model returned no usable portrait"}
            # Second pass: strip claims the source material does not support.
            # The generation prompt asks for grounding; this enforces it.
            if self.critic_fn is not None:
                try:
                    checked = self.critic_fn(CRITIC_PROMPT.format(
                        source=sources[:12_000], draft=str(text)[:8_000]))
                    if checked and len(checked.strip()) >= 120:
                        text = checked
                except Exception as e:               # noqa: BLE001
                    print(f"[persona] grounding pass skipped: {e!r}")
            date = datetime.now().astimezone().strftime("%d %B %Y")
            markdown = _clean_generated(text, date)
            if len(markdown.strip()) < 120:
                return {"ok": False,
                        "error": "persona model returned no usable portrait"}
            # The portrait must speak TO the reader. A model that falls back
            # into "the user" is writing a report, not a friend's read; keep
            # the previous version rather than shipping the wrong voice.
            if re.search(r"\bthe user\b", markdown, re.IGNORECASE):
                return {"ok": False,
                        "error": ("portrait voice check failed: it referred "
                                  "to the reader as 'the user'")}
            now = time.time()
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            with _STATE_LOCK:
                meta = self.meta()
                version = int(meta.get("version") or 0) + 1
                _atomic_text(self.current_path, markdown)
                evolution = self.evolution_markdown()
                block = (f"\n\n## Version {version} — {stamp} — {reason}\n\n"
                         + markdown.rstrip() + "\n")
                evolution = (evolution + block)
                if len(evolution) > 200_000:
                    evolution = evolution[-160_000:]
                _atomic_text(self.evolution_path, evolution)
                meta.update({
                    "version": version,
                    "updated_at": now,
                    "updated_iso": stamp,
                    "reason": reason,
                    "turns_since_update": 0,
                    "based_on": counts,
                })
                history = list(meta.get("history") or [])
                history.append({"version": version, "at": stamp,
                                "reason": reason, "based_on": counts})
                meta["history"] = history[-20:]
                self._save_meta(meta)
            return {"ok": True, "version": version, "markdown": markdown,
                    "meta": meta}
