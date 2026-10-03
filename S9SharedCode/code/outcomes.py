"""Deterministic tool-outcome recording.

`memory.record_outcome()` and the gateway's `/v1/memory/record_outcome` have
existed since Session 7, with a careful shared construction rule, and nothing
in the agent ever called them: outcomes were only recorded if the model
happened to choose `remember(kind=tool_outcome)` itself. That makes the
learning loop depend on the LLM remembering to do bookkeeping, which is
exactly the wrong place for it.

This module wires the write to the one place every tool result passes
through (`mcp_runner.run_tool_loop`), so a tool that silently returns garbage
teaches the agent that fact on the next run instead of being rediscovered.

Three constraints shape the design:

  * Never on the hot path. A tool result is already on the critical path of a
    research node, and this write costs a gateway round-trip (classifier +
    embed, 3-7s). Everything is queued to a single daemon thread.
  * Never break a run. Every failure here is swallowed and counted.
  * Never persist a secret. Tool arguments can contain OAuth tokens, API keys
    and message bodies, so arguments are redacted and result text is
    truncated before it is stored.
"""
from __future__ import annotations

import queue
import re
import threading
import time
from typing import Any

# Tools whose arguments are almost always bulk content rather than something
# worth learning from, or which can carry user content we should not retain.
# Recording them is allowed but the excerpt is heavily truncated.
_BULK_TOOLS = {"fetch_url", "fetch_pdf", "extract_tables", "wayback_fetch",
               "web_search", "news_search", "wikipedia_search",
               "openalex_search", "arxiv_search", "read_file",
               "search_knowledge", "list_dir", "index_document"}

_SECRET_KEYS = re.compile(
    r"(token|secret|password|passwd|api[_-]?key|authorization|auth|"
    r"bearer|cookie|session[_-]?key|private[_-]?key|credential|refresh)", re.I)
# ghp_/gho_/sk-/xoxb-/AKIA/eyJ… and long opaque blobs.
_SECRETISH = re.compile(
    r"\b(gh[pousr]_[A-Za-z0-9]{16,}|sk-[A-Za-z0-9]{16,}|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{12,}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
    r"|[A-Fa-f0-9]{40,})\b")

MAX_ARGS_CHARS = 600
MAX_RESULT_CHARS = 600
QUEUE_MAX = 500


def _redact(value: Any, depth: int = 0) -> Any:
    """Recursively redact secrets from tool arguments before storage."""
    if depth > 4:
        return "…"
    if isinstance(value, dict):
        out = {}
        for k, v in list(value.items())[:20]:
            if isinstance(k, str) and _SECRET_KEYS.search(k):
                out[k] = "[redacted]"
            else:
                out[k] = _redact(v, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_redact(v, depth + 1) for v in list(value)[:10]]
    if isinstance(value, str):
        s = _SECRETISH.sub("[redacted]", value)
        return s[:MAX_ARGS_CHARS] + ("…" if len(s) > MAX_ARGS_CHARS else "")
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:MAX_ARGS_CHARS]


def _summarise_result(tool: str, text: str) -> str:
    """A short, structural digest — not the payload.

    Stored verbatim, result text can be a 20KB page or a private message.
    What is worth learning is the shape: did it work, how big, did it report
    an error, how long did it take.
    """
    t = (text or "").strip()
    if not t:
        return "empty result"
    n = len(t)
    # An explicit error the tool reported in its own payload.
    m = re.search(r'"(?:error|err)"\s*:\s*"([^"]{1,160})"', t)
    err = m.group(1) if m else None
    if err:
        return f"error: {err}"
    if t.lstrip().startswith(("tool error:", "Error:", "error:")):
        return f"error: {t[:200]}"
    # Otherwise: size + a content-free prefix.
    return f"{n} chars: {t[:160]}"


class _Recorder:
    def __init__(self) -> None:
        self.q: queue.Queue = queue.Queue(maxsize=QUEUE_MAX)
        self.dropped = 0
        self.written = 0
        self.errors = 0
        self._t: threading.Thread | None = None
        self._lock = threading.Lock()

    def _ensure(self) -> None:
        with self._lock:
            if self._t and self._t.is_alive():
                return
            self._t = threading.Thread(target=self._drain, name="outcome-writer",
                                       daemon=True)
            self._t.start()

    def submit(self, *, tool: str, arguments: dict, result_text: str,
               ok: bool, latency_s: float, session_id: str | None,
               run_id: str | None) -> None:
        payload = {
            "tool": tool,
            "arguments": _redact(arguments or {}),
            "result_text": _summarise_result(tool, result_text),
            "ok": ok,
            "latency_s": round(float(latency_s or 0.0), 3),
            "at": time.time(),
            "session_id": session_id,
        }
        try:
            self.q.put_nowait(payload)
        except queue.Full:
            with self._lock:
                self.dropped += 1
            return
        self._ensure()

    def _drain(self) -> None:
        # Imported here so module import never pulls the gateway client (and
        # its HTTP stack) into a process that only wants the CLI.
        import memory as memory_svc
        from schemas import ToolCall
        while True:
            try:
                item = self.q.get(timeout=1.0)
            except queue.Empty:
                return  # idle: let the thread die, restart on next submit
            try:
                memory_svc.record_outcome(
                    tool_call=ToolCall(name=item["tool"],
                                       arguments=item["arguments"]),
                    result_text=item["result_text"],
                    artifact_id=None,
                    run_id=item.get("run_id") or item.get("session_id") or "chat",
                    goal_id=None,
                    session_id=item.get("session_id"),
                )
                with self._lock:
                    self.written += 1
            except Exception:
                with self._lock:
                    self.errors += 1
            finally:
                self.q.task_done()

    def stats(self) -> dict:
        with self._lock:
            return {"queued": self.q.qsize(), "written": self.written,
                    "dropped": self.dropped, "errors": self.errors}


_RECORDER = _Recorder()


def record(*, tool: str, arguments: dict | None = None,
           result_text: str = "", ok: bool = True, latency_s: float = 0.0,
           session_id: str | None = None, run_id: str | None = None) -> None:
    """Queue one tool outcome for memory. Never raises, never blocks."""
    try:
        if not tool:
            return
        _RECORDER.submit(tool=tool, arguments=arguments or {},
                         result_text=result_text, ok=ok,
                         latency_s=latency_s, session_id=session_id,
                         run_id=run_id)
    except Exception:
        pass


def on_tool_outcome(*, name: str, arguments: dict | None, ok: bool,
                    result_text: str, latency_s: float,
                    session_id: str | None = None,
                    run_id: str | None = None) -> None:
    """Signature-compatible callback for mcp_runner.run_tool_loop."""
    record(tool=name, arguments=arguments, result_text=result_text, ok=ok,
           latency_s=latency_s, session_id=session_id, run_id=run_id)


def stats() -> dict:
    return _RECORDER.stats()
