"""Feature flags + dynamic config for the console, backed by Prefab
(prefab.cloud) with a local JSON fallback.

- Cloud mode: PREFAB_API_KEY is set → the prefab-cloud-python client
  evaluates flags/configs in-process (fast; it syncs in the background and
  returns defaults until unlocked). Evaluation NEVER raises: any error
  falls back to the local default.
- Local mode: no key → definitions + state/flags.json overrides only.
- Operator overrides (Apps page) win in BOTH modes: a local override beats
  the cloud value until cleared, so the console always has a kill-switch
  even when Prefab is unreachable.

Conventions match agent_server: S9_STATE_DIR overrides the state dir so
tests never touch live flags.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

ROOT = Path(__file__).parent
STATE_DIR = Path(os.environ.get("S9_STATE_DIR") or (ROOT / "state"))
FLAGS_PATH = STATE_DIR / "flags.json"

DEFINITIONS: dict[str, dict] = {
    "chat.tools": {
        "type": "bool",
        "default": True,
        # Kept honest against `_CHAT_TOOLS` in agent_server.py. This text used
        # to name `get_time` and `currency_convert`, which do not exist, and
        # call the catalog "read-only" when it includes create_file, edit_file,
        # schedule_task and render_document. A kill switch whose description
        # misstates its blast radius is worse than no description: the user
        # toggles it expecting to hide lookups and instead has no idea whether
        # file writes are about to be switched off too.
        "description": "Whether Chat may use its tools (web search and "
                       "fetch, Gmail/GitHub/Notion/Slack/Calendar lookups, "
                       "file create/edit/search, memory write, artifact read, "
                       "document rendering and filing, task scheduling, and "
                       "reading the behavioural profile). Off = plain "
                       "text answers with no tools at all. The full list is "
                       "GET /api/skills; the authoritative catalog is "
                       "_CHAT_TOOLS in agent_server.py. Chat is read-only on "
                       "GitHub and Notion: the write api_methods are stripped "
                       "from the schema it is handed.",
    },
    "chat.agui": {
        "type": "bool",
        "default": True,
        "description": "POST /api/agui serves chat turns in AG-UI's event "
                       "vocabulary (RUN_STARTED / TEXT_MESSAGE_* / "
                       "TOOL_CALL_* / RUN_FINISHED) for standards-compliant "
                       "clients. Additive; the console does not use it. "
                       "GET /api/capabilities stays available either way.",
    },
    "chat.recall_gate": {
        "type": "bool",
        "default": True,
        # Scope is one prompt block on one path. It does not touch the profile
        # block, the retrieval tools, or any run outside a plain chat turn:
        # with this off, every turn recalls exactly as it did before.
        "description": "Skip the memory-corpus search for a chat turn that is "
                       "only small talk ('hi', 'thanks', 'ok'). The search is "
                       "one query embed fanned out over every drawer including "
                       "the document corpus, and it runs on the request path: "
                       "2.1s warm here, 25-51s when Ollama has evicted the "
                       "embed model, and it is spent before a greeting is "
                       "answered. The behavioural profile block still attaches "
                       "— only the embedding-backed recall is gated. Off = "
                       "recall again runs on every turn, greetings included.",
    },
    "agent.whats_new": {
        "type": "bool",
        "default": True,
        # Read-only by construction, so the blast radius is one tool and one
        # GET. It is named anyway because every capability the agent can use is
        # a capability someone must be able to take back.
        "description": "The agent can ask what has changed since a moment ago "
                       "(the `whats_new` tool and GET /api/whats-new): a bounded "
                       "index of documents, memory, runs, apps and artifacts, "
                       "pointers only, never contents, and never the audit or "
                       "document memory drawers. Read-only — it changes nothing. "
                       "Off removes the tool from the chat payload and the route "
                       "refuses; the watermark file it writes is left alone.",
    },
    "agent.self_wake": {
        "type": "bool",
        "default": False,
        # The one switch that grants the agent a turn it decided to take. Every
        # other capability in this file is something the agent can do *while the
        # user is talking*; this is the only one that lets it act later, on its
        # own schedule, so it ships off and its blast radius is spelled out.
        "description": "The agent may schedule its own future wake-up "
                       "(`schedule_wake`) and the scheduler may fire those "
                       "wakes. A wake is a poll with a reason: nothing is spent "
                       "when the watch finds no change, and a wake that finds "
                       "change gets a read-only turn in the conversation it was "
                       "armed in, recorded there as Aria waking. Off means no "
                       "new wake can be armed, existing wake rows stay stored "
                       "but never fire, and the tool is not offered to the "
                       "model. Wakes are one-shot and capped "
                       "(wakes_left, a 10-minute interval floor, a 24-hour "
                       "watch horizon), and every one is listed with a cancel "
                       "path on the Scheduler page.",
    },
    "agent.delta_readonly": {
        "type": "bool",
        "default": True,
        # Kept true through v1. The actions work is the only thing that should
        # ever flip it, and it is named so that the diff widening autonomy is
        # one obvious line rather than a silent capability creep.
        "description": "A scheduled wake may only read, notice and report. Its "
                       "tool set is composed from an explicit read-only "
                       "allowlist, with one deliberate exception: it may arm "
                       "another wake (schedule_wake), because a watch that "
                       "cannot extend itself dies at its first unmet tick. "
                       "Writes, sends, run_command, computer_action, document "
                       "rendering and the handoff tools stay off the wake's "
                       "list. Setting this false is how the later action work "
                       "widens autonomy — do it per-tool through the gateway "
                       "policy, not by turning this off.",
    },
    "agent.fold_tool_results": {
        "type": "bool",
        "default": True,
        # Turn it off and every tool result stays in the prompt for the rest of
        # the node's life, which is the behaviour that produced 283k-token
        # researcher calls. Nothing is deleted by folding: the full text goes to
        # the artifact store and the prompt keeps a handle plus a preview, so
        # off is a return to the old cost profile, not a return to lost data.
        # The destructive fallback (`_elide_oldest_tool_output`, which tells the
        # model to re-run the call) is untouched by this switch and still guards
        # any caller that sets an explicit context budget.
        "description": "A tool result the node has already read is replaced in "
                       "later turns by an artifact handle plus a preview, "
                       "instead of being re-sent in full on every hop. The text "
                       "is stored, not truncated. Off restores the old "
                       "behaviour: whole results re-sent until the node is out "
                       "of context.",
    },
    "agent.llm_compaction": {
        "type": "bool",
        "default": True,
        "description": "When the coding agent's context approaches its budget, "
                       "summarize the older turns into one structured state "
                       "summary using prompts/compaction.md (goals, files, "
                       "decisions, tests, next step) instead of only head+tail "
                       "clipping. Falls back to mechanical compaction if the "
                       "summarizer fails. Off = mechanical tiers only.",
    },
    "agent.stop_in_nodes": {
        "type": "bool",
        "default": True,
        # Stop has always been honoured BETWEEN nodes, which is the one place
        # it never helps: a long document is a single author node making one
        # model call per section, and a deep researcher is a single node making
        # thirty tool hops. Off, those run to completion after the console says
        # "stopped", keep spending, and keep holding the run's idempotency claim
        # so re-asking the same question is refused for the length of the TTL.
        # Nothing is stored or configured by this switch, so off is simply the
        # old batch-boundary behaviour and leaves no residue.
        "description": "Pressing Stop ends a run at its next own boundary — "
                       "before the next tool hop, before the next dispatch of "
                       "tools, before the next document section — instead of "
                       "only between node batches. Sections and answers already "
                       "paid for are kept and still rendered. One in-flight "
                       "model call always finishes first; asyncio cannot "
                       "interrupt a request already sent. Off restores the old "
                       "behaviour: the current node runs to completion and only "
                       "the nodes after it are skipped.",
    },
    "documents.image_search": {
        "type": "bool",
        "default": True,
        # The one switch for the whole image-supply path: both tools refuse when
        # it is off, so nothing is fetched, nothing is filed into Documents and
        # no outbound image request leaves the process. Placement of images that
        # are ALREADY stored (a figure lifted from a document the user uploaded)
        # is untouched by it — that path fetches nothing, so switching this off
        # must not silently cost people the pictures they own. Off leaves no
        # residue: the author is simply not offered the tools.
        "description": "The agent may find CC-licensed pictures (Openverse) and "
                       "fetch one into Documents so a document can embed it: "
                       "search_images and fetch_image. On = both tools work. Off "
                       "= both refuse and neither is advertised, and documents "
                       "can still use figures from the user's own uploads.",
    },
    "ui.authoring_focus": {
        "type": "bool",
        "default": True,
        # Console-only. The route, the discussion endpoint and the stored
        # thread are the same either way, so switching this off loses nothing
        # and leaves no half-built page behind: /authoring/focus simply
        # renders the normal Authoring screen.
        "description": "Authoring offers a Focus page (/authoring/focus) that "
                       "fills the window with only the open document on the "
                       "left and Aria on the right — no rail, no top bar, no "
                       "tab strip, no run list. Off removes the entry point "
                       "and the URL falls back to the ordinary Authoring page. "
                       "The conversation is the same thread in both layouts, "
                       "so this never starts or ends a discussion.",
    },
    "ui.pane_hiding": {
        "type": "bool",
        "default": True,
        # The blast radius is the console's own chrome and nothing else: no
        # route, tool, model call or stored record changes behaviour when this
        # flips. It is stated because a kill switch whose description overstates
        # or understates its reach is the one thing that makes an operator
        # afraid to use it.
        "description": "Console users may hide or collapse the non-essential "
                       "panes offered per view (the Layout control in a view's "
                       "top bar). Panes never listed as offerable — navigation, "
                       "tab strips, the primary input, every sole retry, undo "
                       "or delete path — are not affected by this flag at all. "
                       "Off = every pane renders as it did before and the "
                       "Layout control disappears; stored layouts are kept but "
                       "ignored, so switching it back on restores them. Hiding "
                       "is per browser (localStorage 'aria.panes.v1'), not "
                       "per user: there is no server-side preference store.",
    },
    "ui.code_ide": {
        "type": "bool",
        "default": False,
        # Ships off, and off is not a smaller UI — it is the OLD UI. With this
        # off, `/code` is the Code page that exists today, byte for byte, and
        # every one of its routes resolves against the process-wide workspace
        # rather than a session's. Turning it on adds a separate route
        # (`/studio`) that owns its own workspace sessions, folders and buffers,
        # and makes `/code` redirect into it. The reason it is spelled out at
        # this length is that the two modes differ in WHERE A FILE IS WRITTEN,
        # which is not the kind of difference an operator should discover.
        "description": "Aria Code Studio: a full-page coding workspace at "
                       "/studio — its own sidebar-free chrome, per-session "
                       "folders (several projects at once), editor tabs, and "
                       "agent review in the file rather than a separate rail. "
                       "On also redirects the old /code page into it. Off "
                       "leaves /code exactly as it is and the studio route "
                       "renders the Code page, so no session workspaces are "
                       "created and nothing writes outside the shared code "
                       "workspace. Sessions and folders already created are "
                       "left on disk under state/code-studio; they are simply "
                       "not consulted.",
    },
    "browser.tools": {
        "type": "bool",
        "default": False,
        # Off means the coding agent has NO browser verbs at all — not hidden
        # from the popup, absent from the payload it is given — and the studio
        # browser pane's routes answer `disabled: true` without touching a
        # process. On is the widest door in the product, so the blast radius
        # is spelled out: seventeen tools over ONE real Chromium per studio
        # session, which reaches http://localhost:* and any site this machine
        # can, holds cookies and logins between calls, and can press buttons
        # that act in the world. The mutating half (click, fill, press,
        # scroll, history, evaluate, open) joins the exec approval gate, so a
        # manual-mode session still parks on every one of them.
        "description": "Let the coding agent use the browser tools: open and "
                       "read pages, take screenshots, see console errors and "
                       "failed requests, wait for elements, verify a page "
                       "after an edit, and click or type in a form. Off "
                       "removes them from the agent's tool payload and turns "
                       "the studio browser pane into a disabled notice. Pages "
                       "it can reach include http://localhost:* and anything "
                       "else this machine can resolve, so enabling it grants "
                       "access to local services as well as the public web.",
    },
    "browser.headed": {
        "type": "bool",
        "default": False,
        # Only consulted when a browser is STARTED: an invisible context
        # already running is not converted into a visible one under the
        # person's feet, because that would close the page they were looking
        # at. `browser_status` reports `need_restart` in that case.
        "description": "Open the agent's Chromium in a visible window instead "
                       "of headless, so a person can watch it work on the "
                       "screen rather than only in the studio's live view. "
                       "Takes effect on the next browser a session starts.",
    },
    "apps.a2ui": {
        "type": "bool",
        "default": True,
        "description": "Apps can ask the agent for a generated view "
                       "(POST /api/a2ui/generate). The surface is validated "
                       "against a closed catalog before it is returned, so "
                       "nothing executable can reach the client. Turn off to "
                       "return to hand-built views only.",
    },
    "feedback.learning": {
        "type": "bool",
        "default": True,
        "description": "Turn thumbs votes that carry a free-text note into "
                       "durable memory. The note is interpreted together with "
                       "the rated answer, vote, skill and request; only a "
                       "generalisable preference or a user-asserted factual "
                       "correction is written. Off = votes/notes are still "
                       "recorded and shown in the persona, but nothing is "
                       "applied to memory automatically.",
    },
    "feedback.persona_autosave": {
        "type": "bool",
        "default": True,
        "description": "After the persona is regenerated, publish "
                       "`Persona — current.md` and `Persona — evolution.md` "
                       "to Documents without a separate click. Off = the new "
                       "portrait stays local until you press 'save persona "
                       "docs'.",
    },
    "persona.auto_update": {
        "type": "bool",
        "default": True,
        "description": "Regenerate Aria's evolving portrait automatically "
                       "after every four completed chat turns (and after "
                       "feedback is applied to memory). Off = the persona "
                       "only changes when you press 'regenerate persona'. "
                       "The persona is an AI-written impression, not a render "
                       "of the memory drawers.",
    },
    "memory.observations": {
        "type": "bool",
        # OFF by default. This is the only flag that changes how much of your
        # conversations is retained: on, each chat turn is read for durable
        # statements about how you like to work, and each one is stored with a
        # verbatim quote from you as evidence. It records behaviour ("wants
        # publication dates on every claim"), never personality labels,
        # diagnosis or inferred intent. Turn it on deliberately.
        "default": False,
        "description": "Keep an evidence-backed behavioural profile of the "
                       "user. Every entry stores a verbatim quote from the "
                       "conversation as proof; an entry whose quote cannot be "
                       "found in the source is discarded rather than saved. "
                       "Observations are behavioural only - no personality "
                       "type, no mental-health or motive inference, which the "
                       "writer rejects outright. Off by default because it "
                       "retains conversation text you never asked to be "
                       "kept. Stored in the 'profile' drawer: listable, "
                       "deletable and wipeable from the Memory view without "
                       "touching your facts.",
    },
}

_lock = threading.Lock()
_client = None
_client_failed = False


def _cloud_client():
    """Prefab client singleton, or None when unkeyed/unusable. Never raises."""
    global _client, _client_failed
    if _client is not None or _client_failed:
        return _client
    with _lock:
        if _client is not None or _client_failed:
            return _client
        key = (os.environ.get("PREFAB_API_KEY") or "").strip()
        if not key:
            return None
        try:
            import prefab_cloud_python as _pc
            _pc.set_options(_pc.Options(api_key=key))
            _client = _pc.get_client()
        except Exception:
            _client_failed = True
            _client = None
        return _client


def prefab_status() -> dict:
    """Cloud wiring state for the Apps board. Never raises."""
    keyed = bool((os.environ.get("PREFAB_API_KEY") or "").strip())
    client = _cloud_client() if keyed else None
    return {"configured": keyed, "live": client is not None}


def _read_overrides() -> dict:
    try:
        data = json.loads(FLAGS_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_overrides(data: dict) -> None:
    FLAGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = FLAGS_PATH.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, FLAGS_PATH)


def _coerce(ftype: str, value):
    if ftype == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            v = value.strip().lower()
            if v in ("1", "true", "yes", "on"):
                return True
            if v in ("0", "false", "no", "off"):
                return False
        raise ValueError(f"not a bool: {value!r}")
    if ftype == "int":
        return int(value)
    if ftype == "float":
        return float(value)
    return str(value)


def _cloud_value(name: str):
    """Raw Prefab value or None when absent/unusable. Never raises."""
    try:
        client = _cloud_client()
        if client is None:
            return None
        return client.get(name, None)
    except Exception:
        return None


def _definition(name: str) -> dict | None:
    return DEFINITIONS.get(name)


def get_value(name: str, default=None):
    """Effective value: local override > Prefab cloud > definition default
    > explicit default. Never raises."""
    try:
        ov = _read_overrides()
        if name in ov:
            return ov[name]
        definition = _definition(name)
        fallback = definition["default"] if definition else default
        cv = _cloud_value(name)
        if cv is None:
            return fallback
        if definition:
            try:
                return _coerce(definition["type"], cv)
            except (ValueError, TypeError):
                return fallback
        return cv
    except Exception:
        definition = _definition(name)
        return definition["default"] if definition else default


def is_enabled(name: str, default: bool = False) -> bool:
    """Boolean gate. Never raises."""
    definition = _definition(name)
    if definition is None:
        return bool(get_value(name, default))
    try:
        return bool(get_value(name, definition["default"]))
    except Exception:
        return bool(definition["default"])


def set_override(name: str, value) -> dict:
    """Operator override (Apps page). Validated against the definition;
    unknown names and mistyped values raise ValueError (→ HTTP 400)."""
    definition = _definition(name)
    if definition is None:
        raise ValueError(f"unknown flag '{name}'")
    coerced = _coerce(definition["type"], value)
    with _lock:
        ov = _read_overrides()
        ov[name] = coerced
        _write_overrides(ov)
    return {"name": name, "value": coerced}


def clear_override(name: str) -> bool:
    with _lock:
        ov = _read_overrides()
        if name not in ov:
            return False
        del ov[name]
        _write_overrides(ov)
        return True


def all_flags() -> list[dict]:
    """Merged board view: every definition plus any stray local overrides."""
    try:
        ov = _read_overrides()
    except Exception:
        ov = {}
    rows = []
    for name in sorted(set(DEFINITIONS) | set(ov)):
        definition = _definition(name)
        ftype = definition["type"] if definition else "str"
        default = definition["default"] if definition else None
        if name in ov:
            rows.append({"name": name, "type": ftype, "value": ov[name],
                         "default": default, "source": "override",
                         "description": (definition or {}).get("description", "")})
            continue
        cv = _cloud_value(name)
        if cv is not None and definition:
            try:
                cv = _coerce(ftype, cv)
            except (ValueError, TypeError):
                cv = None
        if cv is not None:
            rows.append({"name": name, "type": ftype, "value": cv,
                         "default": default, "source": "prefab",
                         "description": (definition or {}).get("description", "")})
        else:
            rows.append({"name": name, "type": ftype, "value": default,
                         "default": default, "source": "default",
                         "description": (definition or {}).get("description", "")})
    return rows
