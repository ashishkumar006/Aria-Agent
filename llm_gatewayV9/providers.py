"""Provider adapters for llm_gatewayV9.

Each provider implements:
  async chat(messages, *, max_tokens, temperature, model, tools, tool_choice,
             reasoning, response_format, system_blocks) -> dict

The returned dict is normalised:
  {
    "text": str,
    "tool_calls": [ {"id","name","arguments"} ],
    "input_tokens": int, "output_tokens": int,
    "cache_creation_input_tokens": int, "cache_read_input_tokens": int,
    "stop_reason": "tool_use"|"end_turn"|"max_tokens",
    "model": str,
    "tool_call_dialect": "native"|"prompted_fallback"|"none",
    "reasoning_applied": bool,
  }

`messages` may include role="tool" entries with `tool_call_id` and `content`;
each adapter translates them to its native shape.
"""
from __future__ import annotations
import os, json, uuid, hashlib, re, base64
from typing import AsyncIterator, Optional, Any
import httpx


# Upstream timeout for hosted chat calls. This used to be a flat 180s total,
# which turned ONE hanging provider key into a three-minute stall before
# failover — the entire explanation for 300s+ researcher nodes (each LLM hop
# can hit a hanging key; the ledger shows healthy calls finish in 2-6s).
# Per-phase timeouts fix it without touching slow-but-alive streams: the
# read timeout resets on every received byte, so a key that sends NOTHING
# fails over after 60s while a legitimately slow large answer keeps
# streaming. Connect gets 10s (a TCP+TLS handshake slower than that is
# never going to be the fast path).
_UPSTREAM_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


# ────────────────────────────────────────────────────────────────────────────
# V9 multimodal helpers
# ────────────────────────────────────────────────────────────────────────────
# Canonical input form for image content (matches OpenAI/LangChain shape):
#   {"role": "user", "content": [
#       {"type": "text", "text": "..."},
#       {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
#   ]}
# By the time content reaches a provider, http(s) URLs have been pre-resolved
# to data: URLs by main._resolve_image_urls — so providers only see data: URLs.

VISION_MODEL_HINTS = (
    "gpt-4o", "gpt-4.1", "gpt-5", "gpt-4-turbo",
    "claude-3", "claude-4", "claude-opus", "claude-sonnet", "claude-haiku",
    "gemini",
    "llava", "qwen2-vl", "qwen2.5-vl", "qwen3-vl",
    "llama-3.2-11b-vision", "llama-3.2-90b-vision",
    "minicpm-v", "molmo", "pixtral", "internvl",
    "gemma3", "phi-4-multimodal",
    "-vl", "vision", "vlm",
)


def _model_supports_vision(provider: str, model: str) -> bool:
    m = (model or "").lower()
    if provider == "gemini":
        return True  # all current gemini chat models are multimodal
    if provider in ("cerebras",):
        return False  # text-only catalogue
    return any(h in m for h in VISION_MODEL_HINTS)


def _content_has_image(content: Any) -> bool:
    if not isinstance(content, list):
        return False
    for b in content:
        if isinstance(b, dict) and b.get("type") in ("image_url", "image", "input_image"):
            return True
    return False


def _iter_image_blocks(content: Any):
    """Yield (media_type, base64_data) for each image block in content.
    Assumes URLs have already been resolved to data: form."""
    if not isinstance(content, list):
        return
    for b in content:
        if not isinstance(b, dict):
            continue
        btype = b.get("type")
        url = None
        if btype == "image_url":
            iu = b.get("image_url")
            url = iu.get("url") if isinstance(iu, dict) else iu
        elif btype in ("image", "input_image"):
            # Anthropic-style {"source": {"type":"base64","media_type":..,"data":..}}
            src = b.get("source") or {}
            if src.get("type") == "base64":
                yield src.get("media_type", "image/png"), src.get("data", "")
                continue
            url = b.get("url") or src.get("url")
        if not url:
            continue
        if url.startswith("data:"):
            head, _, b64 = url.partition(",")
            mt = head[5:].split(";")[0] or "image/png"
            yield mt, b64
        # Non-data URLs should not reach here; they're pre-resolved upstream.


def _extract_text_blocks(content: Any) -> str:
    """Concatenate the text portions of a multimodal content list."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for b in content:
        if isinstance(b, str):
            parts.append(b)
        elif isinstance(b, dict):
            if b.get("type") == "text" and "text" in b:
                parts.append(b["text"])
            elif "text" in b and b.get("type") not in ("image_url", "image", "input_image"):
                parts.append(b["text"])
    return "\n".join(parts)


class ProviderError(Exception):
    def __init__(self, msg, status=None, retryable=True):
        super().__init__(msg)
        self.status = status
        self.retryable = retryable


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────

def _flatten_system(system_blocks) -> tuple[str, list[dict], bool]:
    """Returns (joined_text, raw_blocks, has_cache_marker)."""
    if system_blocks is None:
        return "", [], False
    if isinstance(system_blocks, str):
        return system_blocks, [{"text": system_blocks, "cache": False}], False
    blocks = []
    has_cache = False
    parts = []
    for b in system_blocks:
        if isinstance(b, dict):
            t = b.get("text", "")
            c = bool(b.get("cache", False))
        else:
            t = getattr(b, "text", "")
            c = bool(getattr(b, "cache", False))
        blocks.append({"text": t, "cache": c})
        parts.append(t)
        if c:
            has_cache = True
    return "\n".join(parts), blocks, has_cache


def _inline_system_turns(messages) -> list[str]:
    """System turns carried inline in `messages`, in order, empties dropped.

    A request may deliver its instructions two ways: inside `messages` as
    `role: "system"` turns, or in the top-level `system` field (which becomes
    `system_blocks`). They are not interchangeable, and a caller is entitled to
    use both. Every provider adapter must therefore preserve the inline ones —
    `GeminiProvider` cannot put them in `contents` at all, so it re-attaches
    them to `systemInstruction`, and the OpenAI-compatible and Ollama adapters
    merge them into their system message.

    Dropping them is the worst kind of bug here: nothing errors, the request is
    still billed and still counted, and the model simply answers as if the
    instruction had never been sent.
    """
    out: list[str] = []
    for m in messages or []:
        if not isinstance(m, dict) or m.get("role") != "system":
            continue
        c = m.get("content")
        if isinstance(c, list):
            c = _extract_text_blocks(c)
        if not isinstance(c, str):
            c = "" if c is None else json.dumps(c)
        c = c.strip()
        if c:
            out.append(c)
    return out


def _empty_result(model: str) -> dict:
    return {
        "text": "", "tool_calls": [],
        "input_tokens": 0, "output_tokens": 0,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
        "stop_reason": "end_turn", "model": model,
        "tool_call_dialect": "none", "reasoning_applied": False,
    }


# ────────────────────────────────────────────────────────────────────────────
# Base
# ────────────────────────────────────────────────────────────────────────────

class BaseProvider:
    name: str = ""

    def __init__(self, api_key: str, model: str, base_url: str = ""):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url

    async def chat(self, messages, *, max_tokens=2048, temperature=0.7, model=None,
                   tools=None, tool_choice=None, reasoning=None, response_format=None,
                   system_blocks=None, cache_system=False) -> dict:
        raise NotImplementedError

    async def stream(self, messages, *, max_tokens=2048, temperature=0.7, model=None,
                     tools=None, tool_choice=None, reasoning=None, response_format=None,
                     system_blocks=None, cache_system=False) -> AsyncIterator[str]:
        # Default fallback: do non-streaming and yield once.
        result = await self.chat(messages, max_tokens=max_tokens, temperature=temperature,
                                 model=model, tools=tools, tool_choice=tool_choice,
                                 reasoning=reasoning, response_format=response_format,
                                 system_blocks=system_blocks, cache_system=cache_system)
        if result["text"]:
            yield result["text"]


# ────────────────────────────────────────────────────────────────────────────
# OpenAI-compatible providers
# ────────────────────────────────────────────────────────────────────────────

REASONING_MODEL_HINTS = ("gpt-oss", "qwen3-think", "deepseek-r1", "deepseek-r2",
                        "qwen3", "o1", "o3", "o4", "gpt-5")


def _model_supports_reasoning(model: str) -> bool:
    m = (model or "").lower()
    return any(h in m for h in REASONING_MODEL_HINTS)


class OpenAICompatProvider(BaseProvider):
    capabilities = {
        "tools": True, "caching": True, "reasoning": False,
        "structured": True, "parallel_tools": True, "vision": False,
    }

    def _headers(self):
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def _translate_tools(self, tools):
        out = []
        for t in tools or []:
            d = t if isinstance(t, dict) else t.model_dump()
            out.append({
                "type": "function",
                "function": {
                    "name": d["name"],
                    "description": d.get("description", ""),
                    "parameters": d.get("input_schema") or {"type": "object", "properties": {}},
                },
            })
        return out

    def _translate_messages(self, messages, system_text):
        """Translate canonical messages (incl role=tool) to OpenAI shape.

        Multimodal content lists (text + image_url blocks) are passed through
        as-is on user messages — the OpenAI Chat API natively accepts them.
        Tool/assistant messages are forced to plain strings.
        """
        out = []
        # Inline `role:"system"` turns must ALWAYS reach the model. They used to
        # be discarded whenever `system_blocks` was also present, which is the
        # same defect just fixed in GeminiProvider: Aria's persona and its
        # retrieved-document block arrive as inline system turns, so a request
        # carrying both `system=` and `messages=[{"role":"system",...}]` was
        # logged, billed and counted in prompt_chars, then answered as if the
        # document had never been sent. Merge them into the prepended system
        # message instead of dropping them.
        inline = _inline_system_turns(messages)
        if system_text:
            merged = "\n\n".join([system_text] + inline)
            out.append({"role": "system", "content": merged})
        elif inline:
            out.append({"role": "system", "content": "\n\n".join(inline)})
        for m in messages:
            r = m.get("role")
            if r == "system":
                continue          # already merged above, in order
            if r == "tool":
                out.append({
                    "role": "tool",
                    "tool_call_id": m.get("tool_call_id") or m.get("id") or "",
                    "content": m.get("content", "") if isinstance(m.get("content"), str) else json.dumps(m.get("content")),
                })
                continue
            if r == "assistant" and m.get("tool_calls"):
                # Carry assistant tool_calls back through.
                tcs = []
                for tc in m["tool_calls"]:
                    tcs.append({
                        "id": tc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": json.dumps(tc.get("arguments") or {}),
                        },
                    })
                # assistant content must be a string for OpenAI
                content = m.get("content") or ""
                if isinstance(content, list):
                    content = _extract_text_blocks(content)
                out.append({"role": "assistant", "content": content, "tool_calls": tcs})
                continue
            content = m.get("content", "")
            if isinstance(content, list):
                # User multimodal: pass list of blocks through unchanged for OpenAI.
                out.append({"role": r, "content": content})
            else:
                out.append({"role": r, "content": content})
        return out

    def _apply_response_format(self, body, response_format):
        if not response_format:
            return
        rf = response_format if isinstance(response_format, dict) else response_format.model_dump(by_alias=True)
        if rf.get("type") == "json_schema" and rf.get("schema"):
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": rf.get("name", "out"),
                    "schema": rf["schema"],
                    "strict": bool(rf.get("strict", True)),
                },
            }
        elif rf.get("type") == "json_object":
            body["response_format"] = {"type": "json_object"}

    def _apply_reasoning(self, body, reasoning, model):
        if not reasoning or reasoning == "off":
            return False
        if not _model_supports_reasoning(model):
            return False
        body["reasoning_effort"] = reasoning
        return True

    async def _post_with_fallbacks(self, client, body):
        """POST with sequential simplification retries (gateway-owns-quirks):
        strip reasoning_effort, then downgrade strict json_schema, then hint
        json_object in prose — one attempt each, in that order. Returns the
        final response (success or failure; the caller raises)."""
        r = await client.post(f"{self.base_url}/chat/completions", headers=self._headers(), json=body)
        if r.status_code == 200:
            return r
        txt = r.text
        if "reasoning_effort" in body and "reasoning_effort" in txt:
            body.pop("reasoning_effort", None)
            r = await client.post(f"{self.base_url}/chat/completions", headers=self._headers(), json=body)
            if r.status_code == 200:
                return r
            txt = r.text
        if (body.get("response_format") or {}).get("type") == "json_schema":
            body["response_format"] = {"type": "json_object"}
            r = await client.post(f"{self.base_url}/chat/completions", headers=self._headers(), json=body)
            if r.status_code == 200:
                return r
            txt = r.text
        # V9: github / azure-openai-flavoured surfaces refuse
        # response_format=json_object unless the literal word "json"
        # appears in `messages`. Inject a one-line hint into the
        # system message and retry. (Gateway-owns-quirks rule.)
        if r.status_code == 400 and "json" in txt.lower() and (
            body.get("response_format") or {}
        ).get("type") == "json_object":
            _msgs = body.get("messages") or []
            if _msgs and _msgs[0].get("role") == "system":
                _msgs[0]["content"] = (
                    (_msgs[0].get("content") or "")
                    + "\n\nReturn your reply as a single JSON object."
                )
            else:
                _msgs.insert(0, {
                    "role": "system",
                    "content": "Return your reply as a single JSON object.",
                })
            body["messages"] = _msgs
            r = await client.post(f"{self.base_url}/chat/completions", headers=self._headers(), json=body)
        return r

    async def chat(self, messages, *, max_tokens=2048, temperature=0.7, model=None,
                   tools=None, tool_choice=None, reasoning=None, response_format=None,
                   system_blocks=None, cache_system=False):
        m = model or self.model
        system_text, _, _ = _flatten_system(system_blocks)
        body = {
            "model": m,
            "messages": self._translate_messages(messages, system_text),
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        if tools:
            body["tools"] = self._translate_tools(tools)
            if tool_choice is not None:
                body["tool_choice"] = tool_choice if isinstance(tool_choice, (str, dict)) else "auto"
        self._apply_response_format(body, response_format)
        self._apply_reasoning(body, reasoning, m)

        async with httpx.AsyncClient(timeout=_UPSTREAM_TIMEOUT) as c:
            r = await self._post_with_fallbacks(c, body)
            if r.status_code != 200:
                raise ProviderError(
                    f"{self.name} HTTP {r.status_code}: {r.text[:300]}",
                    status=r.status_code,
                    retryable=(r.status_code not in (400, 401)),
                )
            d = r.json()
            choice = (d.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            text = msg.get("content") or ""
            # V9: some free models (e.g. stepfun/step-3.7-flash:free via Kilo)
            # emit their answer in a `reasoning` field and leave `content`
            # empty. Fall back to `reasoning` so the text isn't lost.
            if not text and isinstance(msg.get("reasoning"), str):
                text = msg["reasoning"]
            tool_calls_out = []
            for tc in (msg.get("tool_calls") or []):
                fn = tc.get("function") or {}
                args_str = fn.get("arguments") or "{}"
                try:
                    args = json.loads(args_str) if isinstance(args_str, str) else args_str
                except Exception:
                    args = {"_raw": args_str}
                tool_calls_out.append({
                    "id": tc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                    "name": fn.get("name", ""),
                    "arguments": args,
                })
            usage = d.get("usage") or {}
            details = usage.get("prompt_tokens_details") or {}
            cache_read = details.get("cached_tokens", 0) or 0
            stop = choice.get("finish_reason") or "stop"
            stop_norm = "tool_use" if tool_calls_out else (
                "max_tokens" if stop == "length" else "end_turn"
            )
            return {
                "text": text or "",
                "tool_calls": tool_calls_out,
                "input_tokens": usage.get("prompt_tokens", 0) or 0,
                "output_tokens": usage.get("completion_tokens", 0) or 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": cache_read,
                "stop_reason": stop_norm,
                "model": m,
                "tool_call_dialect": "native",
                # Read off the final body: a stripped reasoning_effort
                # reports False, matching the old flag-threading behavior.
                "reasoning_applied": "reasoning_effort" in body,
            }

    async def stream(self, messages, *, max_tokens=2048, temperature=0.7, model=None,
                     tools=None, tool_choice=None, reasoning=None, response_format=None,
                     system_blocks=None, cache_system=False):
        m = model or self.model
        system_text, _, _ = _flatten_system(system_blocks)
        body = {
            "model": m,
            "messages": self._translate_messages(messages, system_text),
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }
        if tools:
            body["tools"] = self._translate_tools(tools)
            if tool_choice is not None:
                body["tool_choice"] = tool_choice if isinstance(tool_choice, (str, dict)) else "auto"
        self._apply_response_format(body, response_format)
        self._apply_reasoning(body, reasoning, m)
        async with httpx.AsyncClient(timeout=_UPSTREAM_TIMEOUT) as c:
            async with c.stream("POST", f"{self.base_url}/chat/completions",
                                headers=self._headers(), json=body) as r:
                if r.status_code != 200:
                    text = (await r.aread()).decode("utf-8", "ignore")[:300]
                    raise ProviderError(f"{self.name} HTTP {r.status_code}: {text}", status=r.status_code)
                async for line in r.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    payload = line[6:]
                    if payload.strip() == "[DONE]":
                        return
                    try:
                        d = json.loads(payload)
                        delta = d["choices"][0].get("delta", {})
                        if delta.get("content"):
                            yield delta["content"]
                        if delta.get("tool_calls"):
                            yield "[[TOOL_CALL_DELTA]] " + json.dumps(delta["tool_calls"])
                    except Exception:
                        continue


class GroqProvider(OpenAICompatProvider):
    name = "groq"
    capabilities = {**OpenAICompatProvider.capabilities, "reasoning": True}
    def __init__(self, api_key, model):
        super().__init__(api_key, model, "https://api.groq.com/openai/v1")


class CerebrasProvider(OpenAICompatProvider):
    name = "cerebras"
    capabilities = {**OpenAICompatProvider.capabilities, "reasoning": True}
    def __init__(self, api_key, model):
        super().__init__(api_key, model, "https://api.cerebras.ai/v1")


class NvidiaProvider(OpenAICompatProvider):
    name = "nvidia"
    capabilities = {**OpenAICompatProvider.capabilities, "reasoning": True}
    def __init__(self, api_key, model):
        super().__init__(api_key, model, "https://integrate.api.nvidia.com/v1")


class OpenRouterProvider(OpenAICompatProvider):
    name = "openrouter"
    capabilities = {**OpenAICompatProvider.capabilities, "reasoning": True}
    def __init__(self, api_key, model):
        super().__init__(api_key, model, "https://openrouter.ai/api/v1")

    def _headers(self):
        h = super()._headers()
        h["HTTP-Referer"] = "http://localhost"
        h["X-Title"] = "LLM Gateway V9"
        return h


class GitHubProvider(OpenAICompatProvider):
    name = "github"
    capabilities = {**OpenAICompatProvider.capabilities, "reasoning": True}
    def __init__(self, api_key, model):
        super().__init__(api_key, model, "https://models.github.ai/inference")


class KiloProvider(OpenAICompatProvider):
    name = "kilo"
    capabilities = {**OpenAICompatProvider.capabilities, "reasoning": True}

    def __init__(self, api_key, model, base_url="https://api.kilo.ai/api/gateway"):
        super().__init__(api_key, model, base_url)

    def _apply_reasoning(self, body, reasoning, model):
        if not reasoning or reasoning == "off":
            return False
        body["reasoning_effort"] = reasoning
        return True


# ────────────────────────────────────────────────────────────────────────────
# Gemini
# ────────────────────────────────────────────────────────────────────────────

class GeminiProvider(BaseProvider):
    name = "gemini"
    capabilities = {
        "tools": True, "caching": True, "reasoning": True,
        "structured": True, "parallel_tools": True, "vision": True,
    }

    def __init__(self, api_key, model, cache_store):
        super().__init__(api_key, model, "https://generativelanguage.googleapis.com/v1beta")
        self.cache_store = cache_store  # cache.GeminiCache

    def _translate_tools(self, tools):
        if not tools:
            return None
        decls = []
        for t in tools:
            d = t if isinstance(t, dict) else t.model_dump()
            decls.append({
                "name": d["name"],
                "description": d.get("description", ""),
                "parameters": d.get("input_schema") or {"type": "object", "properties": {}},
            })
        return [{"function_declarations": decls}]

    def _inline_system(self, messages) -> list[str]:
        """System turns carried inline in `messages`, in order, empties dropped.

        Gemini's `contents` only accepts user/model roles, so
        `_translate_messages` has to drop system turns rather than emit an
        invalid role - which is correct for the *conversation* and catastrophic
        for the *instruction*: every inline system message then vanishes with
        no error anywhere. Only `system_blocks` reaches `systemInstruction`.

        Aria puts its persona AND the retrieved-document block in inline system
        turns, so on this provider both were being discarded. The request was
        logged, billed and counted (`prompt_chars` included the retrieved text),
        then the model answered as if it had never been sent: it replied "I
        don't have access to your menu" to a question whose answer was sitting
        in the same prompt. Collecting them here and folding them into
        `systemInstruction` is the only place they can survive.
        """
        return _inline_system_turns(messages)

    def _translate_messages(self, messages):
        contents = []
        # `functionResponse.name` must match the `functionCall` it answers.
        # The canonical tool message carries only `tool_call_id` + `content`
        # (that is what the module docstring documents and what
        # `mcp_runner.py` actually sends), so the name has to be recovered from
        # the assistant turn that made the call. Falling straight through to the
        # literal "tool" sent `name: "tool"` beside a `functionCall` for
        # `web_search`, which Gemini rejects - so every Gemini-backed tool skill
        # 400s on the second hop and surfaces as a 502, while the identical
        # conversation worked on every OpenAI-compatible provider.
        names_by_id: dict[str, str] = {}
        for prev in messages:
            for tc in (prev.get("tool_calls") or []) if isinstance(prev, dict) else []:
                if tc.get("id") and tc.get("name"):
                    names_by_id[str(tc["id"])] = tc["name"]
        for m in messages:
            r = m.get("role")
            if r == "system":
                continue
            if r == "tool":
                tcid = str(m.get("tool_call_id") or m.get("id") or "")
                name = (m.get("tool_name") or m.get("name")
                        or names_by_id.get(tcid) or "tool")
                contents.append({
                    "role": "user",
                    "parts": [{
                        "function_response": {
                            "name": name,
                            "response": _coerce_obj(m.get("content")),
                        }
                    }],
                })
                continue
            if r == "assistant":
                parts = []
                if m.get("content"):
                    parts.append({"text": m["content"]})
                for tc in (m.get("tool_calls") or []):
                    part = {
                        "functionCall": {
                            "name": tc["name"],
                            "args": tc.get("arguments") or {},
                        }
                    }
                    meta = tc.get("provider_meta") or {}
                    if meta.get("thoughtSignature"):
                        part["thoughtSignature"] = meta["thoughtSignature"]
                    parts.append(part)
                if not parts:
                    parts = [{"text": ""}]
                contents.append({"role": "model", "parts": parts})
                continue
            content = m.get("content", "")
            parts = []
            if isinstance(content, str):
                if content:
                    parts.append({"text": content})
            elif isinstance(content, list):
                # Multimodal: emit text parts and inline_data parts for images.
                text = _extract_text_blocks(content)
                if text:
                    parts.append({"text": text})
                for mt, b64 in _iter_image_blocks(content):
                    parts.append({"inline_data": {"mime_type": mt, "data": b64}})
            else:
                parts.append({"text": json.dumps(content)})
            if not parts:
                parts = [{"text": ""}]
            contents.append({"role": "user", "parts": parts})
        return contents

    async def _post_with_fallbacks(self, client, url, body, system_text,
                                    reasoning_applied: bool, instr_parts=None):
        """POST with one simplification retry on 400: strip thinkingConfig
        and/or cachedContent, then re-POST once. Returns
        (response, reasoning_applied, cache_kept).

        `instr_parts` is the assembled system instruction. It must be threaded
        through because the retry rebuilds `systemInstruction` from it - see the
        comment at the strip site for what happens if it doesn't."""
        r = await client.post(url, json=body)
        if r.status_code == 200:
            return r, reasoning_applied, True
        if r.status_code == 400:
            if reasoning_applied:
                body["generationConfig"].pop("thinkingConfig", None)
                reasoning_applied = False
            cache_kept = True
            if "cachedContent" in body and "cache" in r.text.lower():
                body.pop("cachedContent", None)
                # The instruction must be rebuilt from the SAME parts used on
                # the first attempt. Re-deriving it from `system_text` alone
                # dropped the inline system turns again — so the very first
                # provider that 400s on a cache silently restored the original
                # bug (persona and retrieved document gone, still billed, still
                # counted in prompt_chars) and the retry still returned 200.
                if instr_parts:
                    body["systemInstruction"] = {"parts": instr_parts}
                else:
                    body.pop("systemInstruction", None)
                cache_kept = False
            r = await client.post(url, json=body)
            return r, reasoning_applied, cache_kept
        return r, reasoning_applied, True

    async def chat(self, messages, *, max_tokens=2048, temperature=0.7, model=None,
                   tools=None, tool_choice=None, reasoning=None, response_format=None,
                   system_blocks=None, cache_system=False):
        m = model or self.model
        system_text, blocks, has_cache_marker = _flatten_system(system_blocks)
        cacheable_text = None
        if cache_system or has_cache_marker:
            # Concatenate the cacheable portion (or the entire system_text if cache_system bool).
            if has_cache_marker:
                cacheable_text = "\n".join(b["text"] for b in blocks if b["cache"])
            else:
                cacheable_text = system_text
        cache_name = None
        cache_create_tokens = 0
        cache_read_tokens = 0
        if cacheable_text and len(cacheable_text) > 1000:
            cache_name, cache_create_tokens = await self.cache_store.get_or_create(
                self.api_key, m, cacheable_text, self.base_url
            )
            if cache_name and cache_create_tokens == 0:
                # Reused an existing cache entry — count its tokens as cache_read.
                cache_read_tokens = len(cacheable_text) // 4

        body: dict[str, Any] = {
            "contents": self._translate_messages(messages),
            "generationConfig": {"maxOutputTokens": max_tokens, "temperature": temperature},
        }
        # Inline system turns are dropped from `contents` (see `_inline_system`),
        # so they MUST be re-attached to the instruction or they are lost.
        # system_blocks first, then inline turns, preserving request order.
        inline_sys = self._inline_system(messages)
        # Always bound, so the cache-strip retry can rebuild the instruction
        # from the same parts rather than re-deriving it and losing them.
        instr_parts: list[dict] = []
        if cache_name:
            body["cachedContent"] = cache_name
            # Strip cached part from system instruction to avoid double-billing.
            remaining_sys = "\n".join(b["text"] for b in blocks if not b["cache"]) if has_cache_marker else ""
            instr_parts = ([{"text": remaining_sys}] if remaining_sys else [])
            instr_parts.extend({"text": t} for t in inline_sys)
            if instr_parts:
                body["systemInstruction"] = {"parts": instr_parts}
        else:
            combined = "\n\n".join(([system_text] if system_text else []) + inline_sys)
            if combined:
                instr_parts = [{"text": combined}]
                body["systemInstruction"] = {"parts": instr_parts}

        if tools:
            body["tools"] = self._translate_tools(tools)
            mode = "AUTO"
            if tool_choice == "none":
                mode = "NONE"
            elif isinstance(tool_choice, dict):
                mode = "ANY"
            body["toolConfig"] = {"function_calling_config": {"mode": mode}}

        if response_format:
            rf = response_format if isinstance(response_format, dict) else response_format.model_dump(by_alias=True)
            if rf.get("schema"):
                body["generationConfig"]["responseMimeType"] = "application/json"
                body["generationConfig"]["responseSchema"] = _gemini_clean_schema(rf["schema"])
            elif rf.get("type") == "json_object":
                body["generationConfig"]["responseMimeType"] = "application/json"

        reasoning_applied = False
        if reasoning and reasoning != "off":
            knob = _gemini_thinking_knob(m)
            if knob == "level":
                body["generationConfig"]["thinkingConfig"] = {"thinkingLevel": reasoning}
                reasoning_applied = True
            elif knob == "budget":
                body["generationConfig"]["thinkingConfig"] = {"thinkingBudget": _GEMINI_BUDGETS[reasoning]}
                reasoning_applied = True

        url = f"{self.base_url}/models/{m}:generateContent?key={self.api_key}"
        async with httpx.AsyncClient(timeout=_UPSTREAM_TIMEOUT) as c:
            r, reasoning_applied, cache_kept = await self._post_with_fallbacks(
                c, url, body, system_text,
                reasoning_applied=reasoning_applied,
                instr_parts=(instr_parts or None) or None)
            if not cache_kept:
                cache_name = None
                cache_read_tokens = 0
            if r.status_code != 200:
                raise ProviderError(
                    f"gemini HTTP {r.status_code}: {r.text[:400]}",
                    status=r.status_code,
                    retryable=(r.status_code not in (400, 401)),
                )
            d = r.json()
            cands = d.get("candidates") or []
            if not cands:
                raise ProviderError(f"gemini no candidates: {json.dumps(d)[:200]}", status=200, retryable=True)
            parts = cands[0].get("content", {}).get("parts", []) or []
            text = "".join(p.get("text", "") for p in parts if "text" in p)
            tool_calls_out = []
            for p in parts:
                fc = p.get("functionCall") or p.get("function_call")
                if fc:
                    tc = {
                        "id": f"call_{uuid.uuid4().hex[:8]}",
                        "name": fc.get("name", ""),
                        "arguments": fc.get("args") or fc.get("arguments") or {},
                    }
                    sig = p.get("thoughtSignature") or p.get("thought_signature")
                    if sig:
                        tc["provider_meta"] = {"thoughtSignature": sig}
                    tool_calls_out.append(tc)
            usage = d.get("usageMetadata") or {}
            in_tok = usage.get("promptTokenCount", 0) or 0
            out_tok = usage.get("candidatesTokenCount", 0) or 0
            cached_tok = usage.get("cachedContentTokenCount", 0) or 0
            if cached_tok and not cache_read_tokens:
                cache_read_tokens = cached_tok
            stop = (cands[0].get("finishReason") or "STOP").upper()
            stop_norm = "tool_use" if tool_calls_out else (
                "max_tokens" if stop == "MAX_TOKENS" else "end_turn"
            )
            return {
                "text": text,
                "tool_calls": tool_calls_out,
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "cache_creation_input_tokens": cache_create_tokens,
                "cache_read_input_tokens": cache_read_tokens,
                "stop_reason": stop_norm,
                "model": m,
                "tool_call_dialect": "native",
                "reasoning_applied": reasoning_applied,
            }


def _gemini_supports_thinking(model: str) -> bool:
    return _gemini_thinking_knob(model) is not None


def _gemini_thinking_knob(model: str) -> Optional[str]:
    """Returns 'level' for thinkingLevel-capable models (2.5-pro, 3.x non-lite),
    'budget' for thinkingBudget-only models (2.5-flash), or None for non-thinking."""
    m = (model or "").lower()
    if "gemini" not in m:
        return None
    if "flash-lite" in m:
        return None
    if "2.5-pro" in m or "3-pro" in m or "3.1-pro" in m:
        return "level"
    if "3-flash" in m or "3.1-flash" in m:
        return "level"  # 3.x flash (non-lite) supports thinkingLevel
    if "2.5-flash" in m:
        return "budget"
    return None


_GEMINI_BUDGETS = {"low": 2048, "medium": 8192, "high": 24576}


def _gemini_inline_refs(schema: dict) -> dict:
    """Resolve `$ref` references to `$defs` / `definitions` inline.

    Pydantic emits refs for nested models. Gemini's responseSchema endpoint
    rejects `$ref`, so we inline before cleaning. This must run BEFORE
    `_gemini_clean_schema` (which strips `$defs`).
    """
    if not isinstance(schema, dict):
        return schema
    defs = dict(schema.get("$defs") or schema.get("definitions") or {})

    def walk(node, seen: frozenset[str] = frozenset()) -> dict | list:
        if isinstance(node, dict):
            if "$ref" in node:
                target = node["$ref"]
                name = None
                if target.startswith("#/$defs/"):
                    name = target.removeprefix("#/$defs/")
                elif target.startswith("#/definitions/"):
                    name = target.removeprefix("#/definitions/")
                if name and name in defs and name not in seen:
                    resolved = walk(defs[name], seen | {name})
                    extras = {k: walk(v, seen) for k, v in node.items() if k != "$ref"}
                    if isinstance(resolved, dict):
                        return {**resolved, **extras}
                    return resolved
                # Unresolvable ref — drop it; Gemini would 400 anyway.
                return {k: walk(v, seen) for k, v in node.items() if k != "$ref"}
            return {k: walk(v, seen) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(x, seen) for x in node]
        return node

    return walk(schema)  # type: ignore[return-value]


def _gemini_clean_schema(schema: dict) -> dict:
    """Strip JSON-Schema keys Gemini rejects, after inlining `$ref` / `$defs`."""
    schema = _gemini_inline_refs(schema)
    if not isinstance(schema, dict):
        return schema
    drop = {"additionalProperties", "$schema", "title", "definitions", "$defs", "examples", "default"}

    def strip(node):
        if isinstance(node, dict):
            return {k: strip(v) for k, v in node.items() if k not in drop}
        if isinstance(node, list):
            return [strip(x) for x in node]
        return node

    return strip(schema)


def _coerce_obj(v):
    """Coerce a tool result into a JSON object.

    Gemini's `functionResponse.response` must be a Struct, but a list or a bare
    scalar passed straight through. A tool whose result happens to be a number,
    a boolean or a JSON array — a calculator, a count, a list endpoint — then
    produced an invalid request body and a 502, while the identical result
    worked on every OpenAI-compatible provider, which forwards the string
    as-is. Anything that is not an object gets wrapped rather than trusted.
    """
    if isinstance(v, dict):
        return v
    if isinstance(v, list):
        return {"result": v}
    if isinstance(v, str):
        try:
            out = json.loads(v)
        except Exception:
            return {"text": v}
        return out if isinstance(out, dict) else {"result": out}
    return {"value": v}


# ────────────────────────────────────────────────────────────────────────────
# Ollama
# ────────────────────────────────────────────────────────────────────────────

OLLAMA_TOOL_MODELS = ("llama3.1", "llama3.2", "llama3.3", "qwen2.5", "qwen3",
                     "mistral-nemo", "mistral-small", "command-r", "firefunction")


def _ollama_native_tools(model: str) -> bool:
    m = (model or "").lower()
    return any(h in m for h in OLLAMA_TOOL_MODELS)


class OllamaProvider(BaseProvider):
    name = "ollama"
    capabilities = {
        "tools": True, "caching": False, "reasoning": False,
        "structured": True, "parallel_tools": False, "vision": False,
    }

    def __init__(self, model, base_url="http://localhost:11434"):
        super().__init__("", model, base_url)

    def _translate_messages(self, messages, system_text, prompted_fallback=False):
        out = []
        # Same rule as the OpenAI-compatible adapter: inline system turns are
        # merged, never dropped. See `_inline_system_turns`.
        inline = _inline_system_turns(messages)
        if system_text:
            out.append({"role": "system",
                        "content": "\n\n".join([system_text] + inline)})
        elif inline:
            out.append({"role": "system", "content": "\n\n".join(inline)})
        for m in messages:
            r = m.get("role")
            if r == "system":
                continue
            if r == "tool":
                if prompted_fallback:
                    # Fold tool result into next user message.
                    out.append({
                        "role": "user",
                        "content": f"tool_result {{\"id\":\"{m.get('tool_call_id','')}\",\"output\":{json.dumps(m.get('content',''))}}}",
                    })
                else:
                    out.append({"role": "tool", "content": m.get("content", "") if isinstance(m.get("content"), str) else json.dumps(m.get("content"))})
                continue
            if r == "assistant" and m.get("tool_calls"):
                tcs = []
                for tc in m["tool_calls"]:
                    tcs.append({"function": {"name": tc["name"], "arguments": tc.get("arguments") or {}}})
                content = m.get("content") or ""
                if isinstance(content, list):
                    content = _extract_text_blocks(content)
                out.append({"role": "assistant", "content": content, "tool_calls": tcs})
                continue
            content = m.get("content", "")
            if isinstance(content, list):
                # Ollama vision format: separate `images` field with base64 strings.
                msg = {"role": r, "content": _extract_text_blocks(content)}
                images = [b64 for _, b64 in _iter_image_blocks(content)]
                if images:
                    msg["images"] = images
                out.append(msg)
            else:
                out.append({"role": r, "content": content})
        return out

    async def chat(self, messages, *, max_tokens=2048, temperature=0.7, model=None,
                   tools=None, tool_choice=None, reasoning=None, response_format=None,
                   system_blocks=None, cache_system=False):
        m = model or self.model
        system_text, _, _ = _flatten_system(system_blocks)
        native = _ollama_native_tools(m) and tools
        prompted_fallback = bool(tools) and not native

        if prompted_fallback:
            system_text = (system_text + "\n\n" if system_text else "") + _prompted_tool_system(tools)

        body = {
            "model": m,
            "messages": self._translate_messages(messages, system_text, prompted_fallback=prompted_fallback),
            "options": {"temperature": temperature, "num_predict": max_tokens},
            "stream": False,
        }
        if native:
            body["tools"] = [{
                "type": "function",
                "function": {
                    "name": (t if isinstance(t, dict) else t.model_dump())["name"],
                    "description": (t if isinstance(t, dict) else t.model_dump()).get("description", ""),
                    "parameters": (t if isinstance(t, dict) else t.model_dump()).get("input_schema") or {"type": "object", "properties": {}},
                },
            } for t in tools]

        if response_format:
            rf = response_format if isinstance(response_format, dict) else response_format.model_dump(by_alias=True)
            if rf.get("schema"):
                body["format"] = rf["schema"]
            elif rf.get("type") == "json_object":
                body["format"] = "json"

        # Local model loads can take minutes on first touch (weights paging
        # in); the 600s ceiling here is deliberate, unlike the 180s used
        # for hosted providers.
        async with httpx.AsyncClient(timeout=600) as c:
            r = await c.post(f"{self.base_url}/api/chat", json=body)
            if r.status_code != 200:
                raise ProviderError(f"ollama HTTP {r.status_code}: {r.text[:300]}", status=r.status_code)
            d = r.json()
            msg = d.get("message", {}) or {}
            text = msg.get("content", "") or ""
            tool_calls_out = []
            for tc in (msg.get("tool_calls") or []):
                fn = tc.get("function") or {}
                args = fn.get("arguments") or {}
                if isinstance(args, str):
                    try: args = json.loads(args)
                    except Exception: args = {"_raw": args}
                tool_calls_out.append({
                    "id": tc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                    "name": fn.get("name", ""),
                    "arguments": args,
                })

            dialect = "native" if native else ("prompted_fallback" if prompted_fallback else "none")
            if prompted_fallback and not tool_calls_out:
                parsed = _parse_prompted_tool_call(text)
                if parsed:
                    tool_calls_out = [parsed]
                    text = ""

            return {
                "text": text,
                "tool_calls": tool_calls_out,
                "input_tokens": d.get("prompt_eval_count", 0) or 0,
                "output_tokens": d.get("eval_count", 0) or 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "stop_reason": "tool_use" if tool_calls_out else "end_turn",
                "model": m,
                "tool_call_dialect": dialect,
                "reasoning_applied": False,
            }


def _prompted_tool_system(tools):
    descs = []
    for t in tools:
        d = t if isinstance(t, dict) else t.model_dump()
        descs.append(f"- {d['name']}: {d.get('description','')} schema={json.dumps(d.get('input_schema') or {})}")
    return (
        "When you need to call a tool, respond with ONLY a JSON line of the form "
        '{"tool_call":{"name":"<tool>","arguments":{...}}}. '
        "Do not add prose. Tools available:\n" + "\n".join(descs)
    )


def _parse_prompted_tool_call(text: str):
    if not text:
        return None
    m = re.search(r'\{[\s\S]*"tool_call"[\s\S]*\}', text)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
        tc = d.get("tool_call") or {}
        return {
            "id": f"call_{uuid.uuid4().hex[:8]}",
            "name": tc.get("name", ""),
            "arguments": tc.get("arguments") or {},
        }
    except Exception:
        return None


# ────────────────────────────────────────────────────────────────────────────
# Per-model capability resolution
# ────────────────────────────────────────────────────────────────────────────

# Allow per-model overrides where defaults differ.
def model_capabilities(provider_name: str, model: str, default_caps: dict) -> dict:
    # Key-pool siblings resolve to their canonical provider (gemini-2 → gemini).
    provider_name = _base_provider(provider_name)
    caps = dict(default_caps)
    m = (model or "").lower()
    if provider_name in ("gemini", "gemini35lite"):
        caps["reasoning"] = _gemini_supports_thinking(model)
    if provider_name == "ollama":
        caps["tools"] = True  # we always have prompted fallback
        caps["reasoning"] = False
    if provider_name in ("groq", "cerebras", "nvidia", "openrouter", "github"):
        caps["reasoning"] = _model_supports_reasoning(model)
    # V9: vision is fully model-dependent. Override per configured model.
    caps["vision"] = _model_supports_vision(provider_name, model)
    return caps


# V9: model-name → provider routing. Populated explicitly by
# register_model_routes() at the end of build_providers() (not as a
# hidden side effect mid-build) so a caller can pass only `model=` and
# the gateway picks the right backend.
MODEL_ROUTES: dict[str, str] = {}


def register_model_routes(providers: dict) -> None:
    """(Re)build the model-name → provider table from the wired pool."""
    MODEL_ROUTES.clear()
    if "kilo" in providers:
        # A single Kilo instance forwards ANY model name passed via
        # `model=`, so both known Kilo models are registered for
        # model-name-only routing.
        kilo_model = getattr(providers["kilo"], "model", "")
        for _m in ("stepfun/step-3.7-flash:free", "tencent/hy3:free",
                   kilo_model):
            if _m:
                MODEL_ROUTES[_m] = "kilo"
    if "gemini35lite" in providers:
        # Caller passes model="gemini-3.5-flash-lite" with no provider and
        # it lands on the gemini35lite instance.
        MODEL_ROUTES["gemini-3.5-flash-lite"] = "gemini35lite"


def _key_list(single: str, plural: str) -> list[str]:
    """All API keys for one provider: SINGULAR first, then the PLURAL var
    split on commas/whitespace. Dedupes preserving order.

    Values are never logged — report only len() of the result.
    """
    keys: list[str] = []
    if v := (os.getenv(single) or "").strip():
        keys.append(v)
    if p := os.getenv(plural):
        for part in re.split(r"[,\s]+", p):
            part = part.strip().strip('"').strip("'")
            if part:
                keys.append(part)
    seen: set[str] = set()
    out: list[str] = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _base_provider(name: str) -> str:
    """Pool-member name → canonical provider (`gemini-2` → `gemini`).
    Only strips a trailing -N suffix; plain names pass through."""
    m = re.fullmatch(r"(.+)-(\d+)", name or "")
    return m.group(1) if m else (name or "")


# Providers allowed in Gemini-only mode (GATEWAY_GEMINI_ONLY=true):
# the gemini worker pair plus any of their key-pool siblings.
GEMINI_FAMILY = ("gemini", "gemini35lite")


def keep_gemini_only(pool: dict) -> dict:
    """Drop every pool member outside the Gemini family (canonical +
    siblings). Router pools are emptied entirely — tier classification
    then uses the deterministic token-count fallback (no non-Gemini
    key is ever touched)."""
    return {n: p for n, p in pool.items()
            if _base_provider(n) in GEMINI_FAMILY}


def _fan_out(out: dict, base: str, keys: list[str], make) -> None:
    """Register one pool member per key: `base` for the first key,
    `base-2`, `base-3`, … after. The first key keeps the canonical name so
    existing pins, shortcuts and agent_routing.yaml keep working; siblings
    join the same failover ladders via expand_order() in router.py."""
    for i, k in enumerate(keys, 1):
        out[base if i == 1 else f"{base}-{i}"] = make(k)


def build_providers(cache_store):
    """Worker pool — the LLMs that do real work for the agent.

    V3 changes vs V2:
    - groq worker default: openai/gpt-oss-120b (was llama-3.3-70b-versatile,
      now moved to router pool)
    (2026-09-08: the old cerebras default zai-glm-4.7 is archived upstream;
    current default gpt-oss-120b — see note at the cerebras entry below.)
    """
    out = {}
    _gemini_keys = _key_list("GEMINI_API_KEY", "GEMINI_API_KEYS")
    if _gemini_keys:
        # Each Gemini key yields a gemini + gemini35lite pair sharing the
        # same suffix, so key rotation covers both models together.
        for i, k in enumerate(_gemini_keys, 1):
            suf = "" if i == 1 else f"-{i}"
            out["gemini" + suf] = GeminiProvider(k, os.getenv("GEMINI_MODEL", "gemini-2.5-flash"), cache_store)
            # V9: Gemini 3.5 Flash-Lite — fastest, most cost-effective 3.5 model for
            # high-throughput execution. Registered as a separate provider instance
            # so it coexists with the default gemini and is selectable by model
            # name or the `gemini35lite` shortcut.
            out["gemini35lite" + suf] = GeminiProvider(k, os.getenv("GEMINI_35_LITE_MODEL", "gemini-3.5-flash-lite"), cache_store)
    _fan_out(out, "nvidia",
             _key_list("NVIDIA_API_KEY", "NVIDIA_API_KEYS"),
             lambda k: NvidiaProvider(k, os.getenv("NVIDIA_MODEL", "deepseek-ai/deepseek-v3.2")))
    _fan_out(out, "groq",
             _key_list("GROQ_API_KEY", "GROQ_API_KEYS"),
             lambda k: GroqProvider(k, os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")))
    # 2026-09-08: zai-glm-4.7 is ARCHIVED (verified 404). gpt-oss-120b
    # is a valid catalogue id, but THIS account currently 402s on every
    # Cerebras model (billing/quota) — picks back off for 5 min via
    # _backoff_for(402) and failover covers. Fix billing (or the pin)
    # and this entry works with no code change. CEREBRAS_MODEL override
    # still wins when set.
    _fan_out(out, "cerebras",
             _key_list("CEREBRAS_API_KEY", "CEREBRAS_API_KEYS"),
             lambda k: CerebrasProvider(k, os.getenv("CEREBRAS_MODEL", "gpt-oss-120b")))
    _fan_out(out, "openrouter",
             _key_list("OPEN_ROUTER_API_KEY", "OPEN_ROUTER_API_KEYS"),
             lambda k: OpenRouterProvider(k, os.getenv("OPENROUTER_MODEL", "nvidia/nemotron-3-super-120b-a12b:free")))
    _fan_out(out, "github",
             _key_list("GITHUB_ACCESS_TOKEN", "GITHUB_ACCESS_TOKENS"),
             lambda k: GitHubProvider(k, os.getenv("GITHUB_MODEL", "openai/gpt-4.1-mini")))
    if om := os.getenv("OLLAMA_MODEL"):
        # V9: Ollama is still used for embeddings (see embedders.py), but it is
        # disabled as an LLM *worker* by default — set ENABLE_OLLAMA_LLM=true to
        # re-enable it as a chat/answer provider. This keeps the LLM router on the
        # hosted providers (gemini/nvidia/groq/cerebras/openrouter/github/kilo).
        # Ollama is keyless (local daemon) so it never fans out.
        if os.getenv("ENABLE_OLLAMA_LLM", "false").lower() in ("1", "true", "yes"):
            out["ollama"] = OllamaProvider(om, os.getenv("OLLAMA_URL", "http://localhost:11434"))
    # V9: Kilo Code Gateway — OpenAI-compatible. Serves free models like
    # stepfun/step-3.7-flash:free and tencent/hy3:free. A single Kilo provider
    # instance forwards ANY model name passed via `model=`.
    _kilo_base = os.getenv("KILO_BASE_URL", "https://api.kilo.ai/api/gateway")
    _kilo_model = os.getenv("KILO_MODEL", "tencent/hy3:free")
    _fan_out(out, "kilo",
             _key_list("KILO_API_KEY", "KILO_API_KEYS"),
             lambda k: KiloProvider(k, _kilo_model, _kilo_base))
    # V9: bake per-model capability overrides (vision/reasoning) into each
    # instance, so Router.pick() — which reads provider.capabilities directly —
    # sees the resolved truth instead of the class-level default.
    # Sibling members (gemini-2, …) resolve to their canonical provider.
    for name, p in out.items():
        p.capabilities = model_capabilities(_base_provider(name), p.model, getattr(p, "capabilities", {}))
    register_model_routes(out)
    return out


# V3 router pool — small/fast LLMs used only for routing decisions.
# Separate from the worker pool: separate quotas, separate dashboard section,
# separate per-call markers. Routers receive a bounded envelope (token_count +
# 800-char sample) and emit a single word (TINY/LARGE/HUGE).
ROUTER_DEFAULTS = {
    # 2026-09-08: llama3.1-8b is GONE from this account's catalogue
    # (pre-May deprecation note came true). qwen-3.8-27b is the smallest
    # live model id — though this account currently 402s on ALL Cerebras
    # models, so the router pool fails over to groq/nvidia in practice
    # (router exceptions now back the dead entry off for 5 min).
    "cerebras": "qwen-3.8-27b",
    "groq": "llama-3.3-70b-versatile",
    "nvidia": "nvidia/llama-3.1-nemotron-nano-8b-v1",
    "github": "microsoft/Phi-4-mini-instruct",
}


def build_router_providers():
    """Router pool — same provider classes as workers, but separate instances
    with router-specific (smaller/faster) model defaults. Uses the same API keys
    as workers; per-provider rate budgets are independent because the providers
    we picked (Cerebras, Groq, NVIDIA, GitHub) all meter per-model, not per-key.
    """
    out = {}
    _fan_out(out, "cerebras",
             _key_list("CEREBRAS_API_KEY", "CEREBRAS_API_KEYS"),
             lambda k: CerebrasProvider(k, os.getenv("ROUTER_CEREBRAS_MODEL", ROUTER_DEFAULTS["cerebras"])))
    _fan_out(out, "groq",
             _key_list("GROQ_API_KEY", "GROQ_API_KEYS"),
             lambda k: GroqProvider(k, os.getenv("ROUTER_GROQ_MODEL", ROUTER_DEFAULTS["groq"])))
    _fan_out(out, "nvidia",
             _key_list("NVIDIA_API_KEY", "NVIDIA_API_KEYS"),
             lambda k: NvidiaProvider(k, os.getenv("ROUTER_NVIDIA_MODEL", ROUTER_DEFAULTS["nvidia"])))
    _fan_out(out, "github",
             _key_list("GITHUB_ACCESS_TOKEN", "GITHUB_ACCESS_TOKENS"),
             lambda k: GitHubProvider(k, os.getenv("ROUTER_GITHUB_MODEL", ROUTER_DEFAULTS["github"])))
    return out
