"""Memory: thin client over the gateway memory service.

All durable state, the FAISS indexes, the embed calls and the retrieval
strategy live on llm_gatewayV9 (``memory/`` + ``memory_api.py``). This
module keeps only what belongs to the agent:

- the LLM classifier prompt for ambiguous free-form writes (fast-moving
  retrieval strategy — prompt wording, not infrastructure);
- the deterministic keyword extractor used when the classifier is down;
- typed forwarding of ``read`` / ``remember`` / ``record_outcome`` /
  ``add_fact`` / ``clear`` / ``list_recent`` to ``/v1/memory/*``.

Phase 1 (seven drawers): every write declares its principal role and the
call path sets it honestly — ``remember`` (classified) → ``agent``,
``record_outcome`` (deterministic tool result) → ``runtime`` (fixed
server-side), ``add_fact`` (indexer path) → ``indexer``. The gateway
enforces the per-drawer writer map fail-closed.

Public signatures are unchanged from the flat era, plus an optional
``session_id`` on every call and an optional ``drawers`` scope on reads.

Failure contract: ``read`` is fail-soft (gateway down → ``[]`` with a
log line; session start must never die). Writes raise on transport
failure so callers decide — ``flow._safe_remember`` already swallows and
logs, MCP tools surface the error to the model honestly.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import httpx
from pydantic import BaseModel, Field

from gateway import LLM, GATEWAY_URL, ensure_gateway, _client
from schemas import MemoryItem, ToolCall

STATE_PATH = Path(__file__).parent / "state" / "memory.json"
"""Legacy path. The store moved to the gateway; this constant is kept so
old diagnostics fail with a clear message instead of an ImportError. It
is NOT read or written anymore."""

# Mirror of the gateway plane's SESSION_DRAWERS (memory/plane.py): the
# drawer set for session-start recall. Working notes are run-scoped by
# definition, so a new run recalls everything else. Duplicated (not
# fetched) so session start never depends on an extra round-trip.
SESSION_DRAWERS = ["policy", "fact", "playbook", "document", "episode",
                   "legacy"]


def _base() -> str:
    return os.environ.get("LLM_GATEWAY_V9_URL", GATEWAY_URL).rstrip("/")


def _post(path: str, body: dict, timeout: float = 60.0) -> dict:
    ensure_gateway()
    r = _client().post(f"{_base()}{path}", json=body, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _get(path: str, params: dict | None = None,
         timeout: float = 30.0) -> dict:
    ensure_gateway()
    r = _client().get(f"{_base()}{path}", params=params or {}, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _delete(path: str, params: dict | None = None,
            timeout: float = 30.0) -> dict:
    ensure_gateway()
    r = _client().delete(f"{_base()}{path}", params=params or {}, timeout=timeout)
    r.raise_for_status()
    return r.json()


# ── keyword extraction (classifier-fallback only) ─────────────────────────

_STOPWORDS = {
    "the", "is", "a", "an", "of", "to", "and", "or", "in", "on", "for", "at",
    "with", "by", "from", "what", "how", "when", "where", "why", "this", "that",
    "it", "be", "as", "are", "was", "were", "i", "you", "me", "my", "your",
}


def _tokens(text: str) -> set[str]:
    return {
        w for w in re.findall(r"\w+", text.lower())
        if w not in _STOPWORDS and len(w) > 2
    }


# ── reads ─────────────────────────────────────────────────────────────────

def read(
    query: str,
    history: list[dict] | None = None,
    *,
    kinds: list[str] | None = None,
    top_k: int = 8,
    session_id: str | None = None,
    drawers: list[str] | None = None,
    include_stale: bool = False,
    doc_ids: set[str] | None = None,
) -> list[MemoryItem]:
    """Drawer-aware recall (gateway), legacy merged in. Gateway-side each
    store fuses vector + keyword hits with RRF. Fail-soft: any
    transport failure returns ``[]`` so session start never dies."""
    try:
        data = _post("/v1/memory/search", {
            "query": query, "history": history,
            "kinds": kinds, "top_k": top_k, "session_id": session_id,
            "drawers": drawers, "include_stale": include_stale,
            # Omitted means no filtering; an empty list means "no documents",
            # which is what a disabled document or a conversation with
            # documents turned off must produce.
            **({"doc_ids": sorted(doc_ids)} if doc_ids is not None else {}),
        })
        return [MemoryItem.model_validate(r) for r in data.get("items", [])]
    except Exception as e:
        # Fail-soft by design: recall runs at session start and must never
        # kill a run because the gateway is briefly down. This is the one
        # place where "cannot tell" and "no matches" are genuinely the same
        # thing to the caller. list_recent does NOT fail soft, because a
        # dashboard that renders an empty list on failure is a lie.
        print(f"[memory.read] gateway unreachable ({e!r}); continuing without hits")
        return []


class MemoryBackendError(RuntimeError):
    """The memory backend could not answer. Distinct from "no results": a
    caller that treats these the same will show an empty Memory panel and the
    user will conclude their memory was deleted.

    ``status_code`` carries the gateway's own status when there was one, so a
    client error (400 for an unknown kind) is not re-reported as a gateway
    fault. 0 means "no usable status" (transport failure)."""

    def __init__(self, message: str, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code


def list_recent(limit: int = 50,
                session_id: str | None = None,
                drawers: list[str] | None = None,
                kinds: list[str] | None = None,
                hide_superseded: bool = False) -> list[MemoryItem]:
    """Newest-first items for the dashboard Memory panel.

    `kinds` filters the agent-facing vocabulary (fact / preference /
    tool_outcome / scratchpad) as opposed to `drawers`, which filters the
    cabinet. Both are needed because `preference` records live in the `fact`
    drawer, so a drawer filter cannot isolate them.

    Raises MemoryBackendError on transport/backend failure. It used to
    fail-soft to `[]`, which is what made a gateway 400 (an unknown kind) or
    500 render as an empty list -- indistinguishable from "you have no
    memories". Callers that genuinely must not fail (recall at session start)
    should catch this explicitly.
    """
    try:
        params: dict = {"limit": limit}
        if session_id:
            params["session_id"] = session_id
        if drawers:
            params["drawers"] = ",".join(drawers)
        if kinds:
            params["kinds"] = ",".join(kinds)
        if hide_superseded:
            params["hide_superseded"] = "true"

        data = _get("/v1/memory", params)
        return [MemoryItem.model_validate(r) for r in data.get("items", [])]
    except Exception as e:
        print(f"[memory] list failed ({e!r})")
        code = getattr(getattr(e, "response", None), "status_code", 0) or 0
        raise MemoryBackendError(str(e), status_code=code) from e


def policies(limit: int = 50) -> list[MemoryItem]:
    """Active policy records (revoked ones hidden). Injected into EVERY
    skill prompt with no exclusions. Fail-soft — a policy outage must
    never block a run, but it is loud about it."""
    # A policy outage must never block a run, so this one stays fail-soft.
    try:
        return list_recent(limit=limit, drawers=["policy"],
                           hide_superseded=True)
    except MemoryBackendError as e:
        print(f"[memory] policy list unavailable ({e})")
        return []


def episodes(session_id: str | None = None,
             limit: int = 50) -> list[MemoryItem]:
    """Run history without vector work (Phase 4 drawers). Fail-soft."""
    try:
        params: dict = {"limit": limit}
        if session_id:
            params["session_id"] = session_id
        data = _get("/v1/memory/episodes", params)
        return [MemoryItem.model_validate(r) for r in data.get("items", [])]
    except Exception as e:
        print(f"[memory] episodes failed ({e!r})")
        return []


def propose_playbook(
    descriptor: str,
    *,
    procedure: dict | None = None,
    evidence_ids: list[str] | None = None,
    source: str,
    run_id: str,
) -> MemoryItem:
    """Phase A consolidation: propose a reusable procedure. Agent-writable
    working record; promotion needs a separate system/operator approval —
    the model can never self-promote."""
    data = _post("/v1/memory/playbook/propose", {
        "descriptor": descriptor, "procedure": procedure or {},
        "evidence_ids": evidence_ids or [],
        "source": source, "run_id": run_id,
        "principal_role": "agent",
    })
    return MemoryItem.model_validate(data["item"])


def approve_playbook(
    proposal_id: str,
    *,
    run_id: str,
    principal_role: str = "agent",
) -> MemoryItem:
    """Promote a proposal to a playbook record. System/operator only
    gateway-side (fail-closed); the default agent role is denied."""
    data = _post("/v1/memory/playbook/approve", {
        "proposal_id": proposal_id, "run_id": run_id,
        "principal_role": principal_role,
    })
    return MemoryItem.model_validate(data["item"])


# ── writes ────────────────────────────────────────────────────────────────

class _Classification(BaseModel):
    """What the LLM classifier returns for an ambiguous free-form write."""

    kind: str
    descriptor: str
    keywords: list[str] = Field(default_factory=list)
    value: dict = Field(default_factory=dict)


def _fallback_remember(
    raw_text: str, *, source: str, run_id: str, goal_id: str | None,
    session_id: str | None = None,
) -> MemoryItem:
    """Deterministic write when the classifier LLM is unavailable.
    Keyword extraction is naive (top word tokens); kind defaults to fact.
    Embedding still happens — gateway-side."""
    toks = list(_tokens(raw_text))[:10]
    data = _post("/v1/memory/remember", {
        "kind": "fact", "descriptor": raw_text[:200], "keywords": toks,
        "value": {"raw": raw_text}, "source": source, "run_id": run_id,
        "goal_id": goal_id, "session_id": session_id,
        "principal_role": "agent",
    })
    return MemoryItem.model_validate(data["item"])


def remember_preference(
    descriptor: str,
    *,
    keywords: list[str] | None = None,
    value: dict | None = None,
    source: str = "agent",
    run_id: str = "chat",
    session_id: str | None = None,
    supersedes: str | None = None,
) -> MemoryItem:
    """Explicit-kind preference write — no LLM classifier.

    `remember()` runs free-form text through a classifier that decides the
    kind. That is right for a whole user query and wrong for a preference the
    agent has already identified: a classifier asked to label "prefers
    metric units, no imperial" can just as easily return `fact`, and a
    preference stored as a fact is invisible to the preference view and does
    not read back as a standing instruction.

    This path names the kind itself. Zero model calls, one round-trip, and
    the descriptor is written exactly as the agent phrased it.

    `supersedes` retires an earlier preference id, so a changed preference
    replaces rather than accumulates ("now prefers X" after "prefers Y").
    """
    if not (descriptor or "").strip():
        raise ValueError("preference descriptor required")
    payload = {
        "kind": "preference",
        "descriptor": descriptor.strip()[:400],
        "keywords": [str(k)[:60] for k in (keywords or [])][:12],
        "value": value or {"statement": descriptor.strip()[:400]},
        "source": source,
        "run_id": run_id,
        "goal_id": None,
        "session_id": session_id,
        "supersedes": supersedes,
    }
    return MemoryItem.model_validate(_post("/v1/memory/remember", payload)["item"])


# ── deterministic preference capture ────────────────────────────────────────
# The MCP tool exists and works, but measured live: a natural
# "from now on always give me the publication date and never use emoji" got a
# correct acknowledgement and stored NOTHING, because the model chose not to
# call the tool. That is the same failure mode as the tool-outcome loop, and
# the same fix: do not depend on the model remembering to do bookkeeping.
#
# These patterns are deliberately narrow. A loose detector would file half
# the user's requests as standing instructions, which is worse than missing
# some — a wrong preference actively misinforms every later run.
_PREF_PATTERNS = (
    r"\bfrom now on\b",
    r"\bgoing forward\b",
    r"\bfrom henceforth\b",
    r"\bi (?:always|never|prefer|like|want|hate|need) (?:to |you to |my )?\w+",
    r"\bi'?d rather\b",
    r"\bplease always\b",
    r"\balways (?:give|show|use|include|put|return|format|answer|use)\b",
    r"\bnever use\b",
    r"\bdo not ever\b",
    r"\bdon'?t ever\b",
    r"\bnever send\b",
    r"\bnever (?:include|show|add|create|send|reply)\b",
    r"\bstop (?:using|doing|sending|showing)\b",
    r"\bno more (?:of )?\w+",
    r"\bby default,? (?:use|show|give|return|assume)\b",
    r"\bkeep (?:doing|using) \w+",
    r"\bdefault to\b",
)
_PREF_RE = re.compile("|".join(_PREF_PATTERNS), re.IGNORECASE)
# Asking about preferences is not stating one.
_PREF_META_RE = re.compile(
    r"^\s*(what|which|do you know|list|show|tell)\b.*\b"
    r"(prefer|preference|memory|remember about me)\b|\?",
    re.IGNORECASE)
# Reported speech is not a statement by the user. "The user asked me to always
# use tabs" is the agent talking about someone else, and filing that as the
# user's standing rule would be flatly wrong.
_PREF_THIRD_PARTY_RE = re.compile(
    r"\b(the user|our user|the customer|they (?:said|asked|prefer)|"
    r"he (?:said|asked|prefer)|she (?:said|asked|prefer)|"
    r"asked me to|the doc(?:ument|umentation) says)\b",
    re.IGNORECASE)
_PREF_MAX_CHARS = 400


def detect_preference(text: str) -> str | None:
    """Return the preference to store, or None.

    Conservative by design: only a clear, first-person, standing statement
    counts. A question, a hypothetical, reported speech, or a one-off
    instruction about the current task must not become a durable rule.
    """
    t = (text or "").strip()
    if not t or len(t) > _PREF_MAX_CHARS:
        return None
    if _PREF_META_RE.search(t):
        return None
    if _PREF_THIRD_PARTY_RE.search(t):
        return None
    if not _PREF_RE.search(t):
        return None
    # A bare statement, not a task. "Always give me the date" is a standing
    # rule; "always check the docs first and then write the migration" is a
    # one-off instruction to this turn.
    statement = t.rstrip(" .!?")
    if len(statement.split()) > 60:
        return None
    return statement[:_PREF_MAX_CHARS]


def capture_preference_from_turn(text: str, *, source: str = "chat",
                                 run_id: str = "chat",
                                 session_id: str | None = None) -> str | None:
    """Detect and store a preference in one step. Returns the new id, or None.

    Fail-soft and quiet: a memory write must never break a chat turn, and a
    miss is a normal outcome, not an error worth logging loudly.
    """
    try:
        stmt = detect_preference(text)
        if not stmt:
            return None
        item = remember_preference(stmt, keywords=_tokens(stmt) or None,
                                  source=source, run_id=run_id,
                                  session_id=session_id)
        return item.id
    except Exception as e:
        print(f"[memory.preference] capture skipped: {type(e).__name__}: {e}")
        return None


def remember(
    raw_text: str,
    *,
    source: str,
    run_id: str,
    goal_id: str | None = None,
    session_id: str | None = None,
    drawer: str | None = None,
) -> MemoryItem:
    """LLM-classified write for ambiguous content (user input, free-form
    observation). One classifier call; the gateway embeds + persists. If
    the classifier fails, the deterministic fallback handles the write.
    ``drawer`` overrides kind→drawer routing (dashboard policy editor);
    permission is enforced gateway-side."""
    ensure_gateway()
    schema = _Classification.model_json_schema()
    try:
        reply = _llm_classify(raw_text, schema)
    except Exception as e:
        print(f"[memory.remember] classifier failed ({e!r}); falling back to fact-write")
        return _fallback_remember(raw_text, source=source, run_id=run_id,
                                  goal_id=goal_id, session_id=session_id)

    parsed = reply.get("parsed") or {}
    # NOTES_RUNS §6 (2): the classifier at temp=1.0 sometimes returns an
    # empty `value` dict (the C-run-1 birthday case), discarding the only
    # structured handle to the raw content. If `value` is empty or missing,
    # fall back to {"raw": raw_text} so the originating text is at least
    # always retrievable from the saved item.
    parsed_value = parsed.get("value")
    if not parsed_value:
        parsed_value = {"raw": raw_text}
    c = _Classification.model_validate({
        "kind": parsed.get("kind", "fact"),
        "descriptor": parsed.get("descriptor", raw_text[:120]),
        "keywords": parsed.get("keywords") or list(_tokens(raw_text))[:10],
        "value": parsed_value,
    })

    data = _post("/v1/memory/remember", {
        "kind": c.kind, "descriptor": c.descriptor,
        "keywords": [k.lower() for k in c.keywords], "value": c.value,
        "source": source, "run_id": run_id, "goal_id": goal_id,
        "session_id": session_id, "principal_role": "agent",
        "drawer": drawer,
    })
    return MemoryItem.model_validate(data["item"])


def _llm_classify(raw_text: str, schema: dict) -> dict:
    return LLM().chat(
        prompt=(
            "Classify the following content into a JSON memory record.\n\n"
            f"CONTENT: {raw_text!r}\n\n"
            "Return a JSON object with these fields:\n"
            "- kind ∈ {fact, preference, tool_outcome, scratchpad}.\n"
            "- descriptor: one short human-readable line. MUST include any\n"
            "  specific dates (e.g. '15 May 2026'), numbers, names, places,\n"
            "  or other concrete entities present in the content — these are\n"
            "  what later retrieval will key off. 'Mom's birthday is on 15\n"
            "  May 2026' is a good descriptor; 'birthday and reminder\n"
            "  schedule' is a bad descriptor.\n"
            "- keywords: 3-8 lowercase search keywords pulled from the content.\n"
            "- value: a dict with structured fields (entities, dates,\n"
            "  attributes). MUST NOT be empty when the content has any\n"
            "  identifiable entity — if you cannot classify a specific\n"
            "  attribute, include {\"raw\": <the original content>}."
        ),
        auto_route="memory",
        response_format={
            "type": "json_schema",
            "schema": schema,
            "name": "Classification",
            "strict": True,
        },
        temperature=1.0,
    )


def write_policy(
    text: str,
    *,
    supersedes: str | None = None,
    source: str = "dashboard",
    run_id: str,
) -> MemoryItem:
    """Operator policy write (Phase 3 drawers). No LLM classifier — the
    operator's rule is deliberate, so kind is fact-by-construction and the
    principal is operator. With ``supersedes``, the old rule drops out of
    behavior injection (search) while staying visible for review (list)."""
    data = _post("/v1/memory/remember", {
        "kind": "fact", "descriptor": text[:500],
        "keywords": list(_tokens(text))[:10],
        "value": {"raw": text}, "source": source, "run_id": run_id,
        "goal_id": None, "session_id": None,
        "drawer": "policy", "principal_role": "operator",
        "supersedes": supersedes,
    })
    return MemoryItem.model_validate(data["item"])


def record_outcome(
    *,
    tool_call: ToolCall,
    result_text: str,
    artifact_id: str | None,
    run_id: str,
    goal_id: str | None,
    session_id: str | None = None,
) -> MemoryItem:
    """Zero-LLM-classify write for a deterministic tool outcome. Kind is
    `tool_outcome` by construction; descriptor/keywords are built
    gateway-side so every writer shares one construction rule."""
    data = _post("/v1/memory/record_outcome", {
        "tool": tool_call.name, "arguments": tool_call.arguments,
        "result_text": result_text, "artifact_id": artifact_id,
        "run_id": run_id, "goal_id": goal_id, "session_id": session_id,
    })
    return MemoryItem.model_validate(data["item"])


def add_fact(
    descriptor: str,
    *,
    value: dict | None = None,
    keywords: list[str] | None = None,
    source: str,
    run_id: str,
    goal_id: str | None = None,
    session_id: str | None = None,
    doc: dict | None = None,
) -> MemoryItem:
    """Direct fact write used by document-indexing tools. Skips the LLM
    classifier (kind is known); the gateway still embeds the descriptor.
    Principal is ``indexer`` (chunk payloads route to the document drawer,
    plain facts to the fact drawer). ``doc`` carries the Phase 5 span
    ({doc_id, version, chunk_index, total_chunks})."""
    data = _post("/v1/memory/remember", {
        "kind": "fact", "descriptor": descriptor,
        "keywords": keywords or [], "value": value or {},
        "source": source, "run_id": run_id, "goal_id": goal_id,
        "session_id": session_id, "principal_role": "indexer",
        "doc": doc,
    })
    return MemoryItem.model_validate(data["item"])


def clear(session_id: str | None = None,
          drawers: list[str] | None = None,
          older_than: str | None = None) -> None:
    """Wipe queryable memory (globally, one session's items, one drawer
    subset, and/or only records older than an ISO timestamp — the run-end
    working purge passes all three)."""
    params: dict = {}
    if session_id:
        params["session_id"] = session_id
    if drawers:
        params["drawers"] = ",".join(drawers)
    if older_than:
        params["older_than"] = older_than
    _delete("/v1/memory", params or None)
