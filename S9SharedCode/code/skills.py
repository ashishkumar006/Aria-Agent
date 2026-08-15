"""Session 8 skill registry + per-skill execution.

The orchestrator (flow.py) treats every node as a `Skill` object loaded
from agent_config.yaml. There is no Python class per skill — that
abstraction would have to be added at the point where a skill needs
behaviour the orchestrator can't infer from the yaml. Today every skill
either calls the gateway or (for sandbox_executor) calls sandbox.py.

What lives here:
  - Skill / SkillRegistry
  - input resolution (`n:...`, `art:...`, `USER_QUERY`, literals)
  - prompt rendering (template + inputs + optional failure report)
  - JSON parsing of the model's reply (single top-level object)
  - the MCP tool schemas exposed to tool-using skills
  - `run_skill(...)` — the dispatcher
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import yaml
from pydantic import ValidationError

import artifacts as artifacts_svc
from gateway import LLM
from schemas import AgentResult, NodeSpec


# ── V9 gateway helpers for the Computer-Use judgment LLM ────────────────────
# The ComputerUseSkill engine is network-free (LLM callables injected). We
# wire it to the V9 gateway here so the engine can call a cheap text model
# for L2b and a vision model for L3 — no new gateway, same ledger tagging.
import os
import threading

# BUG-FIX (E): default to "" (not a concrete model) so the gateway's
# agent_routing.yaml + failover ladder picks a working provider. An explicit
# COMPUTER_USE_JUDGE_MODEL / COMPUTER_USE_VISION_MODEL env override is still
# honoured. The previous non-empty default hard-pinned a single model and
# silently disabled failover for the entire computer-use skill.
_COMPUTER_JUDGE_MODEL = os.environ.get("COMPUTER_USE_JUDGE_MODEL") or ""
_COMPUTER_VISION_MODEL = os.environ.get("COMPUTER_USE_VISION_MODEL") or ""

# Cost ledger: track per-run LLM calls for the computer skill (charter §10).
# The V9 gateway already records usage; this is a lightweight in-process
# tally so the orchestrator can surface "computer used N L2b + M L3 calls".
_computer_cost_lock = threading.Lock()
_computer_cost = {"l2b_calls": 0, "l3_calls": 0, "total_tokens": 0}


def _computer_cost_reset():
    global _computer_cost
    with _computer_cost_lock:
        _computer_cost = {"l2b_calls": 0, "l3_calls": 0, "total_tokens": 0}


def _computer_cost_snapshot() -> dict:
    with _computer_cost_lock:
        return dict(_computer_cost)


def _extract_json(text: str) -> dict | None:
    """Best-effort JSON extraction: handles bare JSON, ```json fences, and
    trailing prose after the closing brace."""
    if not text:
        return None
    t = text.strip()
    # Try direct parse first.
    try:
        return json.loads(t)
    except Exception:
        pass
    # Strip a ```json ... ``` fence if present.
    if "```" in t:
        import re
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", t, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                pass
    # Fall back to the first balanced {...} span.
    start = t.find("{")
    end = t.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(t[start:end + 1])
        except Exception:
            return None
    return None


def _v9_judge_chat(system: str, user: str, schema: dict) -> dict | None:
    """Cheap text-model judgment call for L2b. Returns parsed JSON or None.

    Tries a strict JSON schema first, then falls back to a non-strict schema,
    then to free-form JSON extraction — so a picky provider schema doesn't
    silently turn a valid answer into None.

    NOTE: we do NOT pin `model=` here. The gateway's agent_routing.yaml +
    failover ladder picks a working provider for agent="computer", so a
    single rate-limited provider (e.g. Gemini free-tier) doesn't kill the run.
    An explicit COMPUTER_USE_JUDGE_MODEL env override is still honoured.
    """
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": user}]
    # Only pin a model if the operator explicitly set one; otherwise let the
    # gateway route (failover across providers).
    model_kw = {"model": _COMPUTER_JUDGE_MODEL} if _COMPUTER_JUDGE_MODEL else {}
    common = dict(agent="computer", max_tokens=800, temperature=0.0, **model_kw)
    # 1) strict schema
    try:
        reply = LLM().chat(messages=messages,
                           response_format={"type": "json_schema", "schema": schema,
                                            "name": "action", "strict": True},
                           **common)
        parsed = _extract_json(reply.get("text") or "")
        if parsed:
            with _computer_cost_lock:
                _computer_cost["l2b_calls"] += 1
                _computer_cost["total_tokens"] += int(reply.get("usage", {}).get("total_tokens", 0) or 0)
            return parsed
    except Exception:
        pass
    # 2) non-strict schema
    try:
        reply = LLM().chat(messages=messages,
                           response_format={"type": "json_schema", "schema": schema,
                                            "name": "action"},
                                           **common)
        parsed = _extract_json(reply.get("text") or "")
        if parsed:
            with _computer_cost_lock:
                _computer_cost["l2b_calls"] += 1
                _computer_cost["total_tokens"] += int(reply.get("usage", {}).get("total_tokens", 0) or 0)
            return parsed
    except Exception:
        pass
    # 3) no schema — ask for JSON in the prompt, extract it
    try:
        reply = LLM().chat(messages=messages,
                           prompt=user + "\n\nRespond with ONLY a JSON object.", **common)
        parsed = _extract_json(reply.get("text") or "")
        if parsed:
            with _computer_cost_lock:
                _computer_cost["l2b_calls"] += 1
                _computer_cost["total_tokens"] += int(reply.get("usage", {}).get("total_tokens", 0) or 0)
        return parsed
    except Exception:
        return None


def _v9_judge_vision(image_data_url: str, prompt: str, system: str) -> dict | None:
    """Vision-model judgment call for L3 set-of-marks. Returns parsed JSON.

    Routes through the V9 gateway's dedicated /v1/vision endpoint (which
    forces a vision-capable provider and supports a JSON schema for typed
    output), so the image actually reaches the model. No model is pinned so
    the gateway fails over to any available vision-capable provider.
    """
    try:
        model_kw = {"model": _COMPUTER_VISION_MODEL} if _COMPUTER_VISION_MODEL else {}
        reply = LLM().vision(
            image=image_data_url,
            prompt=prompt,
            system=system,
            agent="computer",
            max_tokens=800,
            temperature=0.0,
            **model_kw,
        )
        with _computer_cost_lock:
            _computer_cost["l3_calls"] += 1
            _computer_cost["total_tokens"] += int(reply.get("usage", {}).get("total_tokens", 0) or 0)
        return _extract_json(reply.get("text") or "")
    except Exception:
        return None

ROOT = Path(__file__).parent
AGENT_CONFIG_PATH = ROOT / "agent_config.yaml"


# ── catalogue ────────────────────────────────────────────────────────────────

class Skill:
    def __init__(self, name: str, cfg: dict):
        self.name = name
        self.prompt_path = ROOT / cfg["prompt"]
        self.description = cfg.get("description", "")
        self.tools_allowed: list[str] = cfg.get("tools_allowed", []) or []
        self.internal_successors: list[str] = cfg.get("internal_successors", []) or []
        self.critic: bool = bool(cfg.get("critic", False))
        self.provider_pin: str | None = cfg.get("provider_pin")
        # P2 #10: per-skill temperature / max_tokens come from the yaml so
        # tuning a single skill no longer requires a code edit. Defaults
        # are deliberately conservative; a skill that wants exploration
        # (Researcher) bumps temperature; a skill that wants determinism
        # (Critic, Distiller) drops it to ~0.
        self.temperature: float = float(cfg.get("temperature", 0.3))
        self.max_tokens: int = int(cfg.get("max_tokens", 2048))

    def prompt_template(self) -> str:
        if not self.prompt_path.exists():
            return f"You are the {self.name} skill. (Prompt file missing.)"
        return self.prompt_path.read_text()


class SkillRegistry:
    def __init__(self):
        cfg = yaml.safe_load(AGENT_CONFIG_PATH.read_text())
        self._skills: dict[str, Skill] = {n: Skill(n, c) for n, c in cfg.items()}

    def get(self, name: str) -> Skill:
        if name not in self._skills:
            raise KeyError(f"unknown skill: {name}")
        return self._skills[name]

    def names(self) -> list[str]:
        return list(self._skills)


# ── input resolution + prompt rendering ──────────────────────────────────────

def resolve_inputs(node_inputs: list[str], graph_nodes, query: str) -> list[dict]:
    """Materialise each input id into a dict the prompt can serialise.

    Recognised input forms:
      - "USER_QUERY"  → the original user query text
      - "n:<i>"       → the AgentResult.output of that completed node
      - "art:<sha>"   → the bytes of an artifact, decoded as utf-8 best-effort
      - any other     → passed through as a free-form string

    `graph_nodes` is the nx node-view dict from flow.Graph; we read each
    upstream node's `result` attribute (set when the orchestrator marks
    the node complete).
    """
    out = []
    for inp in node_inputs:
        if inp == "USER_QUERY":
            out.append({"id": "USER_QUERY", "kind": "query", "value": query})
        elif inp.startswith("n:") and inp in graph_nodes:
            upstream = graph_nodes[inp].get("result")
            if isinstance(upstream, AgentResult):
                out_obj = upstream.output
                # Browser nodes serialise a huge `content` a11y dump BEFORE
                # the compact `actions` summary in the JSON. render_prompt caps
                # INPUTS at 20 KB, so `content` eats the whole budget and the
                # `actions` list (the actual extracted titles/prices) gets
                # truncated out. Downstream Distiller nodes then see no
                # structured data, emit `{}`, and the auto-Critic fails —
                # triggering a recovery replan that re-runs the same
                # browser→distiller chain and loops. Truncate `content` here
                # so the `actions` summary survives within the budget.
                if isinstance(out_obj, dict) and upstream.agent_name == "browser":
                    content = out_obj.get("content")
                    if isinstance(content, str) and len(content) > 1500:
                        out_obj = {**out_obj,
                                   "content": content[:1500] + " …[truncated]"}
                out.append({"id": inp, "kind": "upstream",
                            "skill": upstream.agent_name, "output": out_obj})
            else:
                out.append({"id": inp, "kind": "upstream-missing", "output": None})
        elif inp.startswith("art:"):
            try:
                blob = artifacts_svc.get_bytes(inp)
                text = blob.decode("utf-8", errors="replace")
                out.append({"id": inp, "kind": "artifact", "text": text[:20_000]})
            except Exception as e:
                out.append({"id": inp, "kind": "artifact-missing", "error": str(e)})
        else:
            out.append({"id": inp, "kind": "literal", "value": inp})
    return out


def _format_memory_hits(hits: list) -> str:
    """Compact rendering of FAISS-ranked MemoryItem hits for the prompt.

    Each hit is shown as one line: kind, descriptor, source, plus a 400-char
    preview of `value.chunk` when present (indexed-document chunks) or of
    `value.raw` (classifier facts). The full chunk would blow the prompt,
    but the descriptor + preview is enough for the Planner to decide
    whether memory already covers the query and for downstream skills to
    synthesise from indexed material without an extra Retriever round-trip.
    """
    if not hits:
        return ""
    lines = []
    for h in hits[:8]:  # cap to keep the prompt bounded
        kind = getattr(h, "kind", "?")
        desc = (getattr(h, "descriptor", "") or "")[:200]
        source = getattr(h, "source", "")
        val = getattr(h, "value", {}) or {}
        chunk = val.get("chunk")
        raw = val.get("raw")
        line = f"  - [{kind}] {desc}"
        if source:
            line += f"\n      source: {source}"
        if isinstance(chunk, str) and chunk.strip():
            preview = chunk[:2000].replace("\n", " ")
            more = " …" if len(chunk) > 2000 else ""
            line += f"\n      chunk: {preview}{more}"
        elif isinstance(raw, str) and raw.strip():
            raw_more = " …" if len(raw) > 2000 else ""
            line += f"\n      raw: {raw[:2000]}{raw_more}"
        lines.append(line)
    return "\n".join(lines)


def render_prompt(skill: Skill, query: str, resolved: list[dict],
                  failure_report: str | None = None,
                  memory_hits: list | None = None,
                  question: str | None = None) -> str:
    parts = [skill.prompt_template().rstrip()]
    # USER_QUERY top-line: only when the Planner wired USER_QUERY into this
    # node's inputs. Earlier versions added it unconditionally, which
    # leaked the full original query into every fan-out worker — three
    # researcher siblings spawned to "find population of A / B / C" all
    # saw the same "compare A, B, C" query and each one ended up
    # searching for all three. Per-node scoping now travels through
    # `metadata.question` (rendered as QUESTION below) and the INPUTS
    # block; USER_QUERY is present only when the Planner asked for it.
    user_query_in_inputs = any(
        isinstance(r, dict) and r.get("id") == "USER_QUERY" for r in resolved
    )
    if user_query_in_inputs:
        parts += ["", f"USER_QUERY: {query}"]
    # QUESTION: the per-node sub-question the Planner attached via
    # `metadata.question`. This is how a fan-out worker learns *its*
    # slice of the user's request without seeing the whole query.
    if isinstance(question, str) and question.strip():
        parts += ["", f"QUESTION: {question.strip()}"]
    if failure_report:
        parts += ["", f"FAILURE:\n{failure_report}"]
    # Memory hits — FAISS-ranked MemoryItems from session-start memory.read.
    # Same hits flow into every skill's prompt this run (the S7 contract:
    # every cognitive role can see what the agent already knows).
    hits_block = _format_memory_hits(memory_hits or [])
    if hits_block:
        parts += ["", f"MEMORY HITS ({len(memory_hits)} from FAISS):", hits_block]
    parts += ["", "INPUTS:", json.dumps(resolved, indent=2, default=str)[:20_000]]
    return "\n".join(parts)


def parse_skill_json(text: str) -> dict:
    """Skills return a single top-level JSON object. Strip markdown fences
    if the model added them despite being told not to."""
    t = (text or "").strip()
    # BUG-FIX (K): the old code did `t.strip("`")` which removed *every*
    # leading/trailing backtick — not just the fence — and could corrupt a
    # JSON value that legitimately starts/ends with a backtick. Parse the
    # fence explicitly instead.
    if t.startswith("```"):
        # Drop the opening fence line (``` or ```json).
        nl = t.find("\n")
        if nl != -1:
            t = t[nl + 1:]
        # Drop a trailing fence if present.
        if t.endswith("```"):
            t = t[:-3]
        t = t.strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(t[start:end + 1])
            except json.JSONDecodeError:
                pass
    return {}


# ── MCP tool schemas exposed through the gateway tools= channel ──────────────

_TOOL_CATALOG = {
    "web_search": {
        "name": "web_search",
        "description": "Search the web (Tavily primary, DDG fallback). Hard-capped at 5 results.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "default": 3},
            },
            "required": ["query"],
        },
    },
    "fetch_url": {
        "name": "fetch_url",
        "description": "Fetch clean markdown from a URL via crawl4ai.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    "get_time": {
        "name": "get_time",
        "description": "Current time in a named IANA timezone.",
        "input_schema": {
            "type": "object",
            "properties": {"timezone": {"type": "string", "default": "UTC"}},
            "required": [],
        },
    },
    "currency_convert": {
        "name": "currency_convert",
        "description": "Convert money between ISO-3 currencies via frankfurter.dev.",
        "input_schema": {
            "type": "object",
            "properties": {
                "amount": {"type": "number"},
                "from_currency": {"type": "string"},
                "to_currency": {"type": "string"},
            },
            "required": ["amount", "from_currency", "to_currency"],
        },
    },
    "search_knowledge": {
        "name": "search_knowledge",
        "description": "Vector search over the agent's indexed knowledge base.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "k": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    },
    "send_telegram": {
        "name": "send_telegram",
        "description": "Send a text message to a Telegram chat via the Bot API. Requires TELEGRAM_BOT_TOKEN.",
        "input_schema": {
            "type": "object",
            "properties": {
                "chat_id": {"type": "string"},
                "message": {"type": "string"},
            },
            "required": ["chat_id", "message"],
        },
    },
    "send_email": {
        "name": "send_email",
        "description": "Send an email via the Gmail API using OAuth (no SMTP / app password). Requires GMAIL_TOKEN with gmail.send scope.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["to", "subject", "body"],
        },
    },
    "create_calendar_event": {
        "name": "create_calendar_event",
        "description": "Create a Google Calendar event via the REST API. Requires GOOGLE_CALENDAR_TOKEN.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "start": {"type": "string"},
                "end": {"type": "string"},
                "description": {"type": "string"},
                "location": {"type": "string"},
                "timezone": {"type": "string", "default": "UTC"},
            },
            "required": ["summary", "start", "end"],
        },
    },
    "get_weather": {
        "name": "get_weather",
        "description": "Current weather for a city via Open-Meteo (no API key). units: metric|imperial.",
        "input_schema": {
            "type": "object",
            "properties": {
                "location": {"type": "string"},
                "units": {"type": "string", "default": "metric"},
            },
            "required": ["location"],
        },
    },
    "computer_action": {
        "name": "computer_action",
        "description": "Gated computer-use on the user's machine. High-level: drive_app (natural-language goal against a desktop app), run_command, read_file, write_file, open_app. Low-level daemon primitives: launch_app, get_accessibility_tree, get_window_state, click, type_text, press_key, hotkey, scroll, get_desktop_state, bring_to_front, kill_app, start_recording, stop_recording, replay_trajectory, list_apps. Returns status; 'pending' means awaiting user approval.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string",
                           "enum": ["drive_app", "run_command", "read_file", "write_file",
                                    "open_app", "launch_app", "get_accessibility_tree",
                                    "get_window_state", "click", "type_text", "press_key",
                                    "hotkey", "scroll", "get_desktop_state", "bring_to_front",
                                    "kill_app", "start_recording", "stop_recording",
                                    "replay_trajectory", "list_apps"]},
                "params": {"type": "object"},
            },
            "required": ["action", "params"],
        },
    },
    "gmail_query": {
        "name": "gmail_query",
        "description": "Read Gmail via the Gmail API (OAuth). api_method 'list' returns message ids for a search query (e.g. 'is:unread'); api_method 'read' returns sender/subject/date/snippet for a message id. Requires GMAIL_TOKEN with gmail.readonly scope.",
        "input_schema": {
            "type": "object",
            "properties": {
                "api_method": {"type": "string", "enum": ["list", "read"]},
                "query": {"type": "string", "description": "search query for 'list', or message id for 'read'"},
                "max_results": {"type": "integer", "default": 5},
            },
            "required": ["api_method", "query"],
        },
    },
    "gmail_refresh_token": {
        "name": "gmail_refresh_token",
        "description": "Renew the Gmail OAuth access token from GMAIL_REFRESH_TOKEN and write it back to GMAIL_TOKEN. Call this if send_email or gmail_query returns a 401/expired error.",
        "input_schema": {
            "type": "object",
            "properties": {
                "write_env": {"type": "boolean", "default": True},
            },
            "required": [],
        },
    },
    "github_query": {
        "name": "github_query",
        "description": "GitHub via REST API. api_method: list_issues, get_issue, create_issue, list_repos, search_code. Requires GITHUB_TOKEN.",
        "input_schema": {
            "type": "object",
            "properties": {
                "api_method": {"type": "string", "enum": ["list_issues", "get_issue", "create_issue", "list_repos", "search_code"]},
                "owner": {"type": "string"},
                "repo": {"type": "string"},
                "issue_number": {"type": "integer", "default": 0},
                "title": {"type": "string"},
                "body": {"type": "string"},
                "state": {"type": "string", "default": "open"},
            },
            "required": ["api_method"],
        },
    },
    "slack_message": {
        "name": "slack_message",
        "description": "Post a message to a Slack channel via the Bot API. Requires SLACK_BOT_TOKEN with chat:write scope.",
        "input_schema": {
            "type": "object",
            "properties": {
                "channel": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["channel", "text"],
        },
    },
    "notion_query": {
        "name": "notion_query",
        "description": "Notion via REST API. api_method: list_pages, get_page, create_page, query_database, append_text. Requires NOTION_TOKEN.",
        "input_schema": {
            "type": "object",
            "properties": {
                "api_method": {"type": "string", "enum": ["list_pages", "get_page", "create_page", "query_database", "append_text"]},
                "page_id": {"type": "string"},
                "database_id": {"type": "string"},
                "title": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["api_method"],
        },
    },
}


def tool_payload(tool_names: list[str]) -> list[dict] | None:
    if not tool_names:
        return None
    return [_TOOL_CATALOG[n] for n in tool_names if n in _TOOL_CATALOG]


# ── per-node execution ───────────────────────────────────────────────────────

async def run_skill(skill: Skill, node_id: str, graph_nodes,
                    session_id: str, query: str,
                    failure_report: str | None,
                    *, memory_hits: list | None = None) -> tuple[AgentResult, str]:
    """Dispatch one node. Returns (result, rendered_prompt).

    `memory_hits` is the FAISS-ranked MemoryItem list captured once at
    session start by Executor.run and threaded through here so every
    skill's prompt can see the same hits. This is the S7 promise carried
    forward — Memory works in S8 because the orchestrator delivers the
    hits, not just because the FAISS index is on disk.

    sandbox_executor bypasses the gateway: it picks the `code` field out of
    its upstream coder node and runs sandbox.run_python directly. All other
    skills are LLM-backed and route through the V8 gateway with
    agent=<skill_name> so agent_routing.yaml + cost-by-agent kick in."""
    resolved = resolve_inputs(graph_nodes[node_id]["inputs"], graph_nodes, query)
    # Per-node sub-question from the Planner's `metadata.question`. Travels
    # into the rendered prompt as a QUESTION: block so a fan-out worker
    # (e.g. one of three researchers spawned to cover three cities) can
    # see *its* slice of the user's request even when USER_QUERY is not
    # in its inputs.
    node_meta = graph_nodes[node_id].get("metadata") or {}
    question = node_meta.get("question") if isinstance(node_meta, dict) else None
    rendered = render_prompt(skill, query, resolved, failure_report,
                             memory_hits=memory_hits, question=question)
    started = time.time()

    if skill.name == "sandbox_executor":
        code = ""
        for r in resolved:
            if r.get("kind") == "upstream" and isinstance(r.get("output"), dict):
                code = r["output"].get("code") or code
        if not code:
            return AgentResult(
                success=False, agent_name=skill.name,
                error="no code in upstream coder output",
                elapsed_s=time.time() - started,
            ), rendered
        from sandbox import run_python
        out = run_python(code)
        return AgentResult(
            success=(out["exit_code"] == 0 and not out["timed_out"]),
            agent_name=skill.name, output=out,
            elapsed_s=time.time() - started,
        ), rendered

    if skill.name == "browser":
        # Same shape as sandbox_executor: the Browser skill owns its own
        # cascade (extract → deterministic → a11y → vision) and never
        # touches the LLM tool/text channel — so we bypass render_prompt
        # and the gateway-chat dispatch entirely and hand off to
        # BrowserSkill.run(NodeSpec).
        node_dict = graph_nodes[node_id]
        node_spec = NodeSpec(
            skill="browser",
            inputs=node_dict.get("inputs") or [],
            metadata=node_dict.get("metadata") or {},
        )
        from browser.skill import BrowserSkill
        sk = BrowserSkill(
            artifacts_root=str(ROOT / "state" / "sessions" / session_id / "browser"),
            session=session_id,
        )
        result = await sk.run(node_spec)
        if not result.elapsed_s:
            result.elapsed_s = time.time() - started
        return result, rendered

    if skill.name == "computer":
        # Same shape as browser: the Computer-Use skill owns its layered
        # cascade (L1 extract → L2a deterministic → L2b a11y → L3 vision)
        # over cua-driver and never touches the LLM tool/text channel — so
        # we bypass render_prompt and the gateway-chat dispatch and hand
        # off to ComputerUseSkill.run(goal). The V9 gateway is used
        # *inside* the engine for the L2b judgment + L3 vision calls.
        node_dict = graph_nodes[node_id]
        node_meta = node_dict.get("metadata") or {}
        goal = node_meta.get("goal") or node_meta.get("question") or query
        app_hint = node_meta.get("app")
        from computer_use.engine import ComputerUseSkill
        from computer_use import safety

        _computer_cost_reset()
        sk = ComputerUseSkill(
            session_id=session_id,
            llm_chat=_v9_judge_chat,
            llm_vision=_v9_judge_vision,
            safety=safety.shared_gates(),
        )
        cres = sk.run(goal, app_hint=app_hint, max_turns=12)
        # Wrap into the orchestrator's AgentResult contract.
        cost = _computer_cost_snapshot()
        return AgentResult(
            success=cres.success,
            agent_name=skill.name,
            output={"layer": cres.layer, "result": cres.output,
                    "trace": cres.trace, "error": cres.error,
                    "cost": cost},
            elapsed_s=time.time() - started,
        ), rendered

    tools = tool_payload(skill.tools_allowed)
    if tools:
        # Multi-turn tool-use loop. mcp_runner opens one MCP stdio session
        # per skill invocation, dispatches each tool_call the model emits,
        # and feeds the results back until the model produces final text.
        from mcp_runner import run_with_tools
        reply = await run_with_tools(
            prompt=rendered,
            tools_payload=tools,
            agent=skill.name,
            session_id=session_id,
            provider_pin=skill.provider_pin,
            max_tokens=skill.max_tokens,
            temperature=skill.temperature,
        )
    else:
        reply = await asyncio.to_thread(
            LLM().chat,
            prompt=rendered,
            agent=skill.name,
            session=session_id,
            provider=skill.provider_pin,
            max_tokens=skill.max_tokens,
            temperature=skill.temperature,
        )
    parsed = parse_skill_json(reply.get("text", ""))

    # Lift orchestrator-recognised fields out of the skill's JSON.
    # NOTES_RUNS feedback P0 #1: malformed successors used to be silently
    # dropped, which left students chasing "missing node" bugs for an hour.
    # Now: log the offending JSON + the validation error, then fail the
    # node so the failure path (and replay) surfaces it.
    raw_successors = parsed.pop("successors", []) or []
    successors: list[NodeSpec] = []
    rejected: list[str] = []
    for s in raw_successors:
        try:
            successors.append(NodeSpec.model_validate(s))
        except ValidationError as ve:
            rejected.append(f"successor={s!r}  error={ve}")
    if skill.name == "planner":
        for s in parsed.get("nodes", []) or []:
            try:
                successors.append(NodeSpec.model_validate(s))
            except ValidationError as ve:
                rejected.append(f"node={s!r}  error={ve}")

    if rejected:
        err = (
            f"{skill.name}: {len(rejected)} malformed NodeSpec(s) emitted.\n"
            + "\n".join(f"  - {line}" for line in rejected)
        )
        print(f"[skills] {err}")
        return AgentResult(
            success=False, agent_name=skill.name,
            output=parsed, successors=successors,
            elapsed_s=time.time() - started,
            provider=reply.get("provider", ""),
            error=err,
        ), rendered

    return AgentResult(
        success=True,
        agent_name=skill.name,
        output=parsed,
        successors=successors,
        elapsed_s=time.time() - started,
        provider=reply.get("provider", ""),
    ), rendered
