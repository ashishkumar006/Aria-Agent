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
import threading
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
SESSION_DRAWERS = ["policy", "profile", "fact", "playbook", "document",
                   "episode", "legacy"]


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


# ── observations (behavioural profile, evidence-backed) ───────────────────
#
# What this is NOT: a psychological assessment. Inferring "the user has an
# avoidant attachment style" from chat text produces an unfalsifiable claim
# that then silently steers every future answer — it feels insightful and it
# quietly distorts the relationship. So the vocabulary here is deliberately
# behavioural and observable: how the user likes answers, what they ask for,
# when they push back, what they have already corrected.
#
# Every record carries a VERBATIM quote from the user as evidence, and the
# quote is checked against the source text before anything is written. A
# model that paraphrases, merges two messages or invents support produces a
# record whose evidence does not verify, and the record is dropped. That check
# is the entire safety property: it is the difference between "the user said
# this" and "a model inferred this about the user".
#
# Opt-in, behind `memory.observations`. Off by default because it retains
# conversation content the user never asked to be retained.

# Trait/clinical vocabulary that must never appear in a descriptor. The model
# is told not to produce these and this enforces it: a record like "the user
# is anxious about deadlines" is unfalsifiable, unfalsifiable claims about a
# real person are harmful, and there is no reading of the data that makes the
# label true in a way the user would recognise.
#
# Every alternative is a STEM, not a whole word, and takes \w*: "anxie" has to
# catch anxious/anxiety/anxiously, and a trailing \b on the stem would make it
# match none of them — which is how the first version of this guard passed
# every test that used the exact word it was written against.
_TRAIT_BANNED = re.compile(
    r"\b("
    r"narcissis\w*|anxi\w*|depress\w*|neurot\w*|psych\w*|therap\w*|"
    r"diagnos\w*|disorder\w*|attachment style|avoidant\w*|"
    r"emotionally unavailable|codependent\w*|borderline\w*|"
    r"gaslight\w*|macho\w*|weak-?willed|lazy|unmotivated|irrational|"
    r"manipulat\w*|self-?sabotag\w*|procrastinat\w*|impulsive|"
    r"hostile|aggressive\b|argumentative|emotional\b|introvert\w*|"
    r"extrovert\w*|OCD|ADHD"
    r")",
    re.IGNORECASE)

# One observation per turn ceiling. The extractor is a classifier over a
# single message; without a cap a long, discursive turn produced five
# overlapping observations and the profile became a transcript summary.
_MAX_OBSERVATIONS_PER_TURN = 2
_OBS_MIN_CONFIDENCE = 0.4
_OBS_MAX_CHARS = 300

# A claim seen ONCE is not a pattern. The extractor's own confidence grades how
# well the sentence matches its instructions, not how often the behaviour has
# actually occurred - so asked to describe "I don't need the preamble" once, it
# returns 0.9 and the profile reads as though a settled preference had been
# established. One incident is an anecdote; "prefers" is a claim about a
# tendency, and only repetition supports one.
#
# So sightings bound the score from BOTH sides: a lone sighting cannot exceed
# its ceiling however confident the model sounds, and repetition cannot leave
# it stranded low. The extractor's number moves the score within the band; it
# does not decide it.
_SEENING_BAND = {
    1: (0.40, 0.50),
    2: (0.65, 0.80),
    3: (0.78, 0.90),
}
_HARD_CEILING = 0.95


def _confidence_for(model_conf: float, seen: int = 1) -> float:
    """Confidence, bounded by how many times this has actually been observed."""
    lo, hi = _SEENING_BAND[1]
    for n in sorted(_SEENING_BAND):
        # No `break`: this walks the bands from the lowest upward and each
        # matching one overwrites the last, so a claim seen five times ends up
        # in the 3+ band rather than stopping at the first one it satisfies.
        if seen >= n:
            lo, hi = _SEENING_BAND[n]
    value = max(float(model_conf or 0.0), lo)
    return round(min(value, hi, _HARD_CEILING), 2)

# Serialises the read-compare-write section only; the model call stays outside
# it so a slow extraction never blocks a chat turn.
_EXTRACT_LOCK = threading.Lock()


def _evidence_verifies(quote: str, source_text: str) -> bool:
    """Is `quote` actually in what the user said?

    Case- and whitespace-insensitive, because the model normalises quotes and
    spacing when it copies. Anything looser would let a paraphrase pass as a
    verbatim quote, which defeats the point of requiring one.
    """
    q = re.sub(r"\s+", " ", (quote or "").strip().lower())
    s = re.sub(r"\s+", " ", (source_text or "").strip().lower())
    if len(q) < 8:
        return False
    return q in s


def observations_enabled() -> bool:
    """Opt-in gate. Never raises - a missing flag module means off, not on."""
    try:
        import flags
        return flags.is_enabled("memory.observations", default=False)
    except Exception:
        return False


def remember_observation(
    observation: str,
    evidence: str,
    *,
    keywords: list[str] | None = None,
    confidence: float = 1.0,
    source: str = "chat",
    run_id: str = "chat",
    session_id: str | None = None,
    supersedes: str | None = None,
) -> MemoryItem:
    """Store one evidence-backed observation about how the user works.

    Explicit kind, no classifier: the caller has already decided this is an
    observation, so running it back through a classifier that defaults to
    `fact` would file it as a fact and make it invisible as a profile entry.

    `evidence` is the user's own words, verbatim. It is stored alongside the
    observation so any claim can be checked against the conversation it came
    from, and so the user can see why the agent believes something about them.
    """
    if not (observation or "").strip():
        raise ValueError("observation text required")
    if not (evidence or "").strip():
        raise ValueError(
            "evidence required: an observation without a verbatim quote is an "
            "inference about the user, which this store does not accept")
    if _TRAIT_BANNED.search(observation):
        raise ValueError(
            "observations record observable behaviour, not personality "
            "labels. Rewrite as something the user actually did or asked for.")
    return MemoryItem.model_validate(_post("/v1/memory/remember", {
        "kind": "observation",
        "descriptor": observation.strip()[:_OBS_MAX_CHARS],
        "keywords": [str(k)[:60] for k in (keywords or [])][:12],
        "value": {
            "observation": observation.strip()[:_OBS_MAX_CHARS],
            "evidence": evidence.strip()[:600],
        },
        "source": source,
        "run_id": run_id,
        "goal_id": None,
        "session_id": session_id,
        "supersedes": supersedes,
        "confidence": max(0.0, min(1.0, float(confidence))),
    })["item"])


def _keywords_grounded_in_quote(keywords: list[str] | None, quote: str) -> bool:
    """Do the model's own keywords actually appear in the quote it supplied?

    `_evidence_verifies` proves the quote is REAL - that it is something the
    user actually said. It does not prove the quote is ABOUT the observation.
    Live: the table message produced "wants detailed reasoning rather than
    just conclusions" carrying the quote "never give me a table when there
    are only two options". Real quote, unrelated claim - and because the
    dedup then saw a claim it already had, the genuinely correct observation
    about reasoning was discarded in favour of the wrong one.

    So the model's chosen keywords have to be findable in its own quote. A
    claim about reasoning cannot be evidenced by a sentence about tables.
    """
    kws = _content_words(" ".join(str(k) for k in (keywords or [])))
    kws -= _GENERIC_TOPIC
    if not kws:
        # No usable keywords to check against; fall back to the observation
        # text rather than waving everything through.
        return True
    quote_words = _content_words(quote)
    return bool(kws & quote_words)


def _quote_supports_claim(observation: str, quote: str) -> bool:
    """Does the quote actually say the thing the observation claims?

    The last deterministic guard could not do this and nothing else can.
    A faithful paraphrase shares no words with its quote - "prefers concise
    responses without introductory text" against "I really don't need the
    preamble" has zero overlap - while a *wrong* pairing can share a common
    noun and look grounded: the live failure was "wants detailed decision
    reasoning rather than just the final conclusion" carrying "never give me
    a table when there are only two options". The keywords matched on
    "options" and every word-overlap rule passed it.

    One cheap call at temperature 0, off the request path, only for
    candidates that already passed the free checks. Fail open on a transport
    error: an unreachable gateway must not silently discard everything the
    other guards accepted.
    """
    try:
        reply = LLM().chat(
            prompt=(
                "Does the QUOTE support the CLAIM? Answer yes or no, nothing "
                "else.\n\n"
                f"CLAIM: {observation[:300]}\n\n"
                f"QUOTE: {quote[:400]}\n\n"
                "Answer no if the quote is about a different subject, even if "
                "they share a word."),
            auto_route="memory",
            temperature=0.0,
            max_tokens=8)
        text = str(reply.get("text") or "").strip().lower()
        return text.startswith("y")
    except Exception as e:
        print(f"[memory.observations] support check skipped: {e!r}")
        return True


_EXTRACT_PROMPT = (
    "Read the user's message below and decide whether it says anything about "
    "HOW THEY WANT TO WORK that would still be true next month.\n\n"
    "USER MESSAGE:\n{text}\n\n"
    "Return JSON: {{\"observations\": [{{\"observation\": str, "
    "\"evidence\": str, \"keywords\": [str], \"confidence\": float}}]}}\n\n"
    "The evidence quote must be about the SAME SUBJECT as the observation: a "
    "claim about reasoning cannot be evidenced by a sentence about tables. "
    "Every word in your `keywords` must appear in your evidence quote - this "
    "is checked, and a mismatch means the observation is discarded.\n"
    "An observation is a short third-person sentence about observable "
    "behaviour: what they asked for, what they corrected, how they like "
    "answers framed, how much detail they want, what they push back on.\n"
    "Examples of GOOD observations:\n"
    "  'wants publication dates on every claim'\n"
    "  'prefers short answers; pushes back on long preamble'\n"
    "  'asks for metric units'\n"
    "  'corrects verbosity when answers run long'\n"
    "Examples of observations you must NEVER produce:\n"
    "  anything about intelligence, personality type, motive, emotion or "
    "mental health ('anxious', 'avoidant', 'ruthless', 'irrational', "
    "'procrastinating')\n"
    "  anything not supported by this single message\n"
    "  anything about a third party ('their manager said...')\n"
    "  anything that is really a preference about facts or output format - "
    "that belongs in the preference store, not here\n"
    "  a one-off instruction for THIS turn ('summarise this in three bullets') "
    "- that is a task, not a standing pattern\n"
    "  anything that refers to a conversation you cannot see ('despite their "
    "earlier preference') - you only have the one message\n"
    "'evidence' MUST be copied character-for-character from the user's "
    "message above. Do not paraphrase, do not merge two sentences. If you "
    "cannot quote it exactly, the observation is not supported and must be "
    "left out. Returning an empty list is correct and expected most of the "
    "time."
)

# A one-off instruction for THIS turn, not a standing pattern. "summarise that
# in exactly three bullet points" is a task instruction; recording it as
# "prefers bullet points" is wrong, and it was the single worst output of the
# first live run — it even inverted a real standing preference the user had
# just stated.
_ONE_OFF = re.compile(
    r"\b(this (?:time|once|response|answer|message|task|summar|section)|"
    r"just (?:this|for now)|for now|right now|today|"
    r"this (?:document|summary|section|query)|"
    r"when asked|in exactly \w+|for this (?:task|summary|document|answer))\b",
    re.IGNORECASE)

# The extractor sees ONE message. A claim that leans on something outside it
# is reasoning the evidence cannot support — the model does not have the
# earlier turns, so "despite an earlier preference" is invented context. That
# exact observation came out of the first live run.
_OUTSIDE_CONTEXT = re.compile(
    r"\b(earlier|previously|before|used to|until now|"
    r"despite|although|however|but now|since (?:then|earlier)|"
    r"again|as (?:we|you) (?:discussed|mentioned))\b",
    re.IGNORECASE)


# Hand-written, not generated by pydantic.
#
# A nested pydantic model emits `$defs` + `$ref`, and strict JSON-schema mode
# requires `required` on every object and `additionalProperties: false`. Gemini
# accepts neither `$ref` nor a missing `required`: the gateway retries, the
# retries produce the same failure, and every extraction dies with
# "structured output failed validation" — which reads like the gateway is down
# and is not, because the plain `json_object` path returns perfect JSON for
# the identical prompt.
#
# This cost a full test-suite pass to find: every unit test stubs the LLM, so
# a schema the provider rejects passes them all. Only a live call shows it.
_OBSERVATION_SCHEMA = {
    "type": "object",
    "properties": {
        "observations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "observation": {"type": "string"},
                    "evidence": {"type": "string"},
                    "keywords": {"type": "array",
                                 "items": {"type": "string"}},
                    "confidence": {"type": "number"},
                },
                "required": ["observation", "evidence", "keywords",
                             "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["observations"],
    "additionalProperties": False,
}


def _content_words(s: str) -> set[str]:
    """Content words, lightly stemmed.

    The singular/plural fold matters: "prefers to avoid emojis in
    communication" and "prefers not to have emojis used in responses" are the
    same claim, and `emoji` vs `emojis` was enough to keep them apart in every
    comparison tried.
    """
    out: set[str] = set()
    for w in re.findall(r"[a-z0-9+]+", (s or "").lower()):
        if w in _STOPWORDS or len(w) <= 2:
            continue
        out.add(w)
        if w.endswith("ies") and len(w) > 4:
            out.add(w[:-3] + "y")
        elif w.endswith("s") and not w.endswith("ss") and len(w) > 3:
            out.add(w[:-1])
    return out


# Topic words so generic that two DIFFERENT claims routinely share them.
# Counting these as agreement merges "prefers bullet points" with "prefers
# prose", which are opposites.
_GENERIC_TOPIC = {
    "user", "users", "preference", "preferences", "prefers", "prefer",
    "style", "format", "formats", "formatting", "response", "responses",
    "answer", "answers", "communication", "general", "default", "defaults",
    "thing", "things", "way", "ways", "output", "content", "text", "writing",
}


def _shared_topic(kws: set[str], rk: set[str]) -> bool:
    """Do these two keyword sets name the same subject?"""
    shared = (kws & rk) - _GENERIC_TOPIC
    return bool(shared)


def _observation_is_new(text: str, keywords: list[str] | None,
                        existing: list[dict]) -> bool:
    """Is this claim saying something the profile does not already say?

    The store has no dedup anywhere by design ("the server owns policy"), but
    that is wrong for the profile specifically. One live conversation produced
    "prefers Python over JavaScript" AND "prefers Python over JavaScript for
    programming tasks" as separate rows, and 13 rows from 15 messages. A
    profile that repeats itself is noise, and every duplicate is another
    chance to be wrong about a real person.

    Descriptor text alone cannot do this. The real pair was "prefers very
    short, preamble-free responses" against "prefers concise responses
    without introductory text" - word overlap 0.22, because the model
    paraphrases freely. What DOES connect them is the topic they share, which
    is what the extractor's `keywords` carry. So: keyword overlap first, and
    descriptor overlap only as a fallback for records written before this.
    """
    kws = {str(k).lower().strip() for k in (keywords or []) if str(k).strip()}
    a = _content_words(text)
    if not a:
        return True
    for row in existing:
        rk = {str(k).lower().strip() for k in (row.get("keywords") or [])
              if str(k).strip()}
        # Two shared topics, or one when both sides named only one - but a
        # shared word has to be a TOPIC. "format" is on half the rows in this
        # store and means nothing about what the claim is about.
        if kws and rk and _shared_topic(kws, rk):
            return False
        b = _content_words(row.get("observation") or "")
        if not b:
            continue
        union = a | b
        if not union:
            continue
        if len(a & b) / len(union) >= 0.5:
            return False
    return True


def _reinforce_observation(existing: list[dict], obs: str, quote: str,
                           conf: float, keywords: list[str] | None = None
                           ) -> None:
    """Fold a repeat sighting into the existing entry.

    Confidence moves a step toward the higher of the two, never past 0.98 -
    repeated agreement is evidence, but "certain about a person" is not a
    thing this store should ever assert. Keeps the FIRST observation text
    (it was the one that described the pattern) and the LATEST quote.

    The row that gets reinforced is the NEWEST match, by creation time. The
    old code took the first match in the list, and the list is not purely
    ordered by age: a legacy record sorts to the front, it can never be
    linked (the gateway refuses the supersede), and every repeat sighting
    therefore reinforced THAT row and left a fresh duplicate behind. The
    live profile drawer held 80 copies of a single claim because of it.
    """
    a = _content_words(obs)
    if not a:
        return
    kws = {str(k).lower().strip() for k in (keywords or []) if str(k).strip()}
    # Newest first: a repeat sighting must land on the live row.
    ordered = sorted(
        existing,
        key=lambda r: str(r.get("created_at") or ""),
        reverse=True)
    for row in ordered:
        rk = {str(k).lower().strip() for k in (row.get("keywords") or [])
              if str(k).strip()}
        same = False
        if kws and rk and _shared_topic(kws, rk):
            same = True
        if not same:
            b = _content_words(row.get("observation") or "")
            union = a | b if b else set()
            same = bool(b) and bool(union) and len(a & b) / len(union) >= 0.5
        if not same:
            continue
        try:
            rid = row.get("id")
            if not rid:
                return
            seen = row.get("seen_count") or 0
            repeats = seen + 1
            body = {
                "kind": "observation",
                "descriptor": row.get("observation") or obs,
                "keywords": row.get("keywords") or [],
                "value": {"observation": row.get("observation") or obs,
                          # The FIRST quote is kept as the evidence, not the
                          # latest. A repeat sighting can carry a worse
                          # quote than the one that established the claim,
                          # and overwriting a good verbatim quote with a
                          # looser one is a downgrade, not a refinement.
                          "evidence": row.get("evidence") or quote[:600],
                          "latest_quote": quote[:600],
                          "previous_evidence": row.get("evidence", ""),
                          "seen_count": repeats},
                "source": "chat",
                "run_id": "chat",
                "session_id": row.get("session_id"),
                "supersedes": rid,
                "confidence": _confidence_for(
                    max(float(row.get("confidence") or 0.0), conf), repeats),
            }
            try:
                _post("/v1/memory/remember", body)
            except Exception as e:
                # 404: the row we meant to supersede is gone (a purge, a
                # drawer rebuild, a legacy record that was never linkable).
                # The claim is still true and still worth keeping, so store
                # it as a new row instead of dropping it on the floor.
                if getattr(getattr(e, "response", None), "status_code", None) == 404:
                    fresh = dict(body)
                    fresh.pop("supersedes", None)
                    fresh["value"] = dict(body["value"])
                    fresh["value"]["seen_count"] = 1
                    _post("/v1/memory/remember", fresh)
                    print("[memory.observations] the previous entry was "
                          "gone; stored the claim as a new row")
                    return
                raise
            print(f"[memory.observations] reinforced an existing entry "
                  f"({seen + 1} sightings)")
        except Exception as e:
            print(f"[memory.observations] reinforce failed: {e!r}")
        return


def submit_observation(observation: str, evidence: str, *,
                       keywords: list[str] | None = None,
                       confidence: float = 0.7, seen: int = 1,
                       source: str = "chat", run_id: str = "chat",
                       session_id: str | None = None) -> dict:
    """The ONE way an observation enters the store. Returns a verdict.

    There are two callers that used to write independently - the background
    extractor and the agent's `record_observation` tool - and they produced the
    same claim twice, once from each. The tool's default confidence was 0.8,
    which is exactly how the duplicates could be told apart in the store. A
    memory with two writers and no shared gate is two memories; every gate
    below therefore lives here, not in the callers.
    """
    obs = (observation or "").strip()
    quote = (evidence or "").strip()
    if not obs or not quote:
        return {"status": "rejected", "reason": "observation and evidence required"}
    if _TRAIT_BANNED.search(obs):
        return {"status": "rejected",
                "reason": "observations record observable behaviour, not "
                          "personality labels"}
    if _ONE_OFF.search(obs):
        return {"status": "rejected",
                "reason": "a one-off instruction for this turn is not a "
                          "standing pattern"}
    if _OUTSIDE_CONTEXT.search(obs):
        return {"status": "rejected",
                "reason": "the claim leans on context this message does not "
                          "contain"}
    with _EXTRACT_LOCK:
        try:
            existing = list_observations(k=200)
        except Exception:
            existing = []
        if not _observation_is_new(obs, keywords, existing):
            _reinforce_observation(existing, obs, quote, confidence, keywords)
            return {"status": "reinforced", "observation": obs}
        try:
            item = remember_observation(
                obs, quote, keywords=keywords,
                confidence=_confidence_for(confidence, seen),
                source=source, run_id=run_id, session_id=session_id)
        except ValueError as e:
            return {"status": "rejected", "reason": str(e)}
        return {"status": "new", "id": item.id, "observation": item.descriptor}


def extract_observations(text: str, *, session_id: str | None = None,
                         source: str = "chat") -> list[MemoryItem]:
    """Pull evidence-backed observations out of one user message.

    Off the request path, and silent on failure: this is bookkeeping, and a
    memory write is never a reason for a chat turn to error.

    Temperature is 0.1, not the 1.0 the fact classifier uses. That classifier
    is allowed to be creative because a misfiled fact is cheap; here a wrong
    extraction becomes a durable claim about a person, and creativity is the
    one property that must not be in the loop.
    """
    t = (text or "").strip()
    if not observations_enabled() or len(t) < 12:
        return []
    # Never mine the agent's own words back into observations about the user.
    if t.startswith("[") or "REFERENCE MATERIAL" in t:
        return []
    try:
        reply = LLM().chat(
            prompt=_EXTRACT_PROMPT.format(text=t[:4000]),
            auto_route="memory",
            response_format={"type": "json_schema",
                             "schema": _OBSERVATION_SCHEMA,
                             "name": "Observations", "strict": True},
            temperature=0.1,
        )
        parsed = (reply.get("parsed") or {})
        candidates = parsed.get("observations") or []
        if not isinstance(candidates, list):
            print(f"[memory.extract_observations] expected a list, got "
                  f"{type(candidates).__name__}")
            return []
    except Exception as e:
        print(f"[memory.extract_observations] extractor failed: {e!r}")
        return []

# The read-compare-write section is serialised. Two chat turns a second
    # apart each spawn their own extractor thread, and without this both read
    # the same "existing" snapshot and both append - which is exactly how one
    # message ended up stored twice ("prefers Python over JavaScript for
    # coding tasks" and "prefers Python over JavaScript for programming
    # tasks"). The LLM call above is deliberately OUTSIDE the lock so a slow
    # extraction never blocks an unrelated chat turn.
    written: list[MemoryItem] = []
    for cand in candidates[:_MAX_OBSERVATIONS_PER_TURN]:
        try:
            obs = str(cand.get("observation") or "").strip()
            quote = str(cand.get("evidence") or "").strip()
            conf = float(cand.get("confidence") or 0.5)
        except Exception:
            continue
        if not obs or conf < _OBS_MIN_CONFIDENCE:
            continue
        # The safety property, and the only place it is enforced: nothing is
        # stored whose evidence is not literally in what the user said. This
        # is the check the tool path cannot do, because the tool is handed a
        # quote by a model that may have paraphrased it.
        if not _evidence_verifies(quote, t):
            continue
        # Real is not the same as relevant: see _keywords_grounded_in_quote.
        if not _keywords_grounded_in_quote(cand.get("keywords"), quote):
            continue
        if not _quote_supports_claim(obs, quote):
            continue
        # One gate for both writers. The extractor used to do its own
        # compare-and-write here and the agent's `record_observation` tool did
        # its own, so the same correction arriving in one message was stored
        # twice - once at the extractor's capped confidence and once at the
        # tool's 0.8 default.
        try:
            verdict = submit_observation(
                obs, quote, keywords=cand.get("keywords") or [],
                confidence=conf, source=source, session_id=session_id)
        except Exception as e:
            print(f"[memory.extract_observations] write failed: {e!r}")
            continue
        if verdict.get("status") == "new" and verdict.get("id"):
            written.append(MemoryItem.model_validate({
                "id": verdict["id"], "kind": "observation",
                "descriptor": verdict.get("observation", obs),
                "keywords": cand.get("keywords") or [],
                "value": {"observation": obs, "evidence": quote},
                "source": source, "run_id": "chat",
                "session_id": session_id,
                "confidence": _confidence_for(conf)}))
    return written


def recall_observations(k: int = 10, *, min_confidence: float = 0.0) -> list[dict]:
    """Read the behavioural profile back, most recent first.

    `min_confidence` exists so a caller can ignore the shakier entries. The
    default is 0.0 rather than a floor because dropping the low-confidence tail
    silently is worse than showing it with its score attached.
    """
    rows = list_observations(k=max(1, min(int(k), 50)),
                             min_confidence=min_confidence)
    return rows


def render_profile_document() -> str:
    """The whole user profile as a readable markdown document.

    Observations alone are half the picture - the standing `preference` store
    is the other half, and the two answer different questions ("what have you
    noticed about me" vs "what have I told you to do"). Filed together they
    are the thing a person would want to keep or correct, and they are far
    easier to argue with on paper than in a list of rows.

    Deliberately plain markdown, no front matter: it is meant to be read by a
    human who wants to check whether the agent has it right.
    """
    lines = [
        "# What Aria knows about how I work",
        "",
        "Generated from the memory store. Every claim below was drawn from "
        "something I actually said; the quote is the evidence.",
        "",
        "Behavioural only - requests, corrections and answer shape. This is "
        "not a psychological profile and contains no traits, diagnosis or "
        "inferred motives.",
        "",
    ]
    obs = list_observations(k=200)
    prefs = []
    try:
        prefs = list_recent(limit=100, kinds=["preference"])
    except Exception:
        prefs = []

    lines += ["## What you've noticed about me", ""]
    if obs:
        for o in sorted(obs, key=lambda r: -int(r.get("seen_count") or 1)):
            seen = int(o.get("seen_count") or 1)
            tag = ("seen once - a single incident, not an established "
                   "preference" if seen <= 1 else f"seen {seen} times")
            lines.append(f"- **{o['observation']}**  ({tag})")
            if o.get("evidence"):
                lines.append(f'  > "{o["evidence"]}"')
    else:
        lines.append("_Nothing recorded yet._")

    lines += ["", "## Standing preferences I've stated", ""]
    if prefs:
        for p in prefs:
            lines.append(f"- {p.descriptor}")
    else:
        lines.append("_None recorded._")

    lines += [
        "",
        "---",
        "",
        "If any of this is wrong, tell Aria and it will be corrected or "
        "removed. Everything here can be deleted from the Memory view.",
    ]
    return "\n".join(lines) + "\n"


def save_profile_document() -> dict:
    """Write the profile document into the console's Documents section.

    Reuses the one function that creates documents, so the filename
    sanitising and supported-type rules are identical to a manual upload.
    """
    import httpx as _httpx
    from gateway import GATEWAY_URL
    from mcp_server import _gw_headers

    text = render_profile_document().encode("utf-8")
    with _httpx.Client(timeout=120, follow_redirects=True,
                       headers=_gw_headers()) as client:
        r = client.post(f"{GATEWAY_URL.rstrip('/')}/v1/documents",
                        files={"file": ("what-aria-knows-about-me.md", text)})
    if r.status_code >= 400:
        try:
            detail = (r.json() or {}).get("error") or r.text[:200]
        except Exception:
            detail = r.text[:200]
        return {"ok": False, "status": r.status_code, "error": detail}
    try:
        doc = (r.json() or {}).get("document") or {}
    except Exception:
        doc = {}
    return {"ok": True, "document_id": doc.get("id", ""),
            "filename": "what-aria-knows-about-me.md",
            "bytes": len(text),
            "observations": len(list_observations(k=200))}


def list_observations(k: int = 50, *, min_confidence: float = 0.0) -> list[dict]:
    """Fetch observation records directly from the profile drawer."""
    try:
        body = _get("/v1/memory", {
            "drawers": "profile",
            "limit": max(1, min(int(k), 500)),
            # Reinforcement works by superseding: the old row stays in the
            # store for audit but must not appear in the profile, or every
            # repeat sighting leaves a duplicate behind it and the list
            # grows without ever converging.
            "hide_superseded": "true",
        })
    except Exception:
        return []
    rows = body.get("items") or body.get("memories") or []
    out = []
    for r in rows:
        try:
            if float(r.get("confidence", 1.0)) < min_confidence:
                continue
            value = r.get("value") or {}
            out.append({
                "id": r.get("id"),
                "observation": value.get("observation")
                or r.get("descriptor", ""),
                "evidence": value.get("evidence", ""),
                "confidence": r.get("confidence", 1.0),
                "created_at": r.get("created_at"),
                # How many separate conversations this has turned up in. One
                # is an anecdote, not a tendency, and the UI says so rather
                # than presenting a single incident as an established
                # preference.
                "seen_count": value.get("seen_count", 1),
                "keywords": r.get("keywords") or [],
                "session_id": r.get("session_id"),
            })
        except Exception:
            continue
    return out


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
