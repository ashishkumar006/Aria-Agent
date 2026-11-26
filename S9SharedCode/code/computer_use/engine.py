"""Computer-use engine — the L1→L3 cascade + scan-act-verify loop.

This is the heart of the desktop agent. It mirrors the Browser skill's
cascade shape (extract → deterministic → a11y → vision) but drives a real
desktop app via cua-driver instead of a web page.

Layers:
  L0 gated-shell  — fallback when the daemon is unavailable (run_command /
                    read_file / write_file / open_app), behind safety gates.
  L1 extract      — read AX tree text / clipboard / file directly (0 LLM).
  L2a deterministic — known hotkey sequences (0 LLM).
  L2b a11y        — get_window_state markdown + cheap LLM → element_index act.
  L3 vision       — screenshot + set-of-marks → V9 /v1/vision → click (x,y).

The engine is orchestrated by `ComputerUseSkill.run(goal, ...)` which the
S9 `skills.py` dispatch calls (mirroring the browser branch).

All cua-driver calls go through `computer_use.daemon.call`. The LLM calls
(text judgment + vision) go through the V9 gateway client
(`browser.client.V9Client`) — no new gateway.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import daemon  # shim re-exporting core.daemon (canonical) — keeps monkeypatch compat
from . import layers
from . import prompts as P
from . import safety
from . import apps
from .safety import SafetyGates
from .safety.permissions import check_permissions, PermissionReport
from .daemon import PreconditionError, DaemonError

ROOT = Path(__file__).resolve().parent.parent

# NOTE: the L2b/L3 model choice lives in skills.py (_COMPUTER_JUDGE_MODEL /
# _COMPUTER_VISION_MODEL, defaulting to gemini-3.5-flash-lite — it drives
# dialogs reliably where the weaker free model escalated). An env override wins.
# Earlier revisions kept separate defaults here ("gemini-3.1-flash-lite")
# that were never read — the engine only calls the injected llm_chat /
# llm_vision callables — so they were removed to end the drift.

# Loop-guard caps (charter §11, AX_TREE_CONTRACT §contract).
MAX_ACTION_REPEATS = 3  # same action 3x in a row -> abort as loop
MAX_L3_CALLS = 6  # max vision escalations per run
MAX_TURNS_HARD = 12


# When the planner omits `metadata.app`, fall back to the app named in the goal
# ("Open Notepad …" -> "Notepad") so the launch cascade (cua-driver launch_app /
# shell start) still fires instead of targeting whatever window is current and
# gambling on vision. Conservative on purpose: a single word after an
# open/launch/start verb, minus articles — the planner setting metadata.app is
# the primary path; this is the fallback.
_APP_FROM_GOAL_RE = re.compile(
    r"\b(?:open|launch|start|fire\s+up)\s+([A-Za-z][A-Za-z0-9]{1,})", re.I)
_APP_STOPWORDS = {"a", "an", "the", "my", "this", "that", "new", "up", "app"}


def _app_from_goal(goal: str) -> str | None:
    m = _APP_FROM_GOAL_RE.search(goal or "")
    if not m:
        return None
    cand = m.group(1).strip()
    if not cand or cand.lower() in _APP_STOPWORDS:
        return None
    return cand


def _driver_refusal(res: dict | None) -> str:
    """A cua-driver refusal is an HTTP success with a refusal body.

    Treating it as success is how `replace_text` reported `ok` while the
    document stayed empty: the driver had refused the call with
    `snapshot_id_required`, the cascade ignored it, and the judge retried the
    identical action until the loop-guard aborted.
    """
    if not isinstance(res, dict):
        return ""
    if str(res.get("status") or "").lower() == "refused":
        return str((res.get("refusal") or {}).get("message")
                   or res.get("message") or "refused by cua-driver")
    if res.get("refusal"):
        return str((res.get("refusal") or {}).get("message") or "refused")
    return ""


def _wants_foreground(res: dict | None) -> bool:
    esc = (res or {}).get("escalation") or {}
    return str(esc.get("target") or esc.get("recommended") or "").lower() == "foreground"


def _pick_editable(elements: list[dict] | None,
                   element_index: int | None = None) -> dict | None:
    """The element a text action should target.

    Modern Notepad, Calculator and Settings are XAML hosts: WM_CHAR is
    dropped (the driver answers `effect: unverifiable` plus an escalation),
    and only an element-targeted call routed through UIA lands. The judge
    names the role ("Document", "Edit") but often omits the index, so resolve
    it from the snapshot instead of typing into the void.
    """
    els = elements or []
    if element_index is not None:
        for e in els:
            if e.get("element_index") == element_index:
                return e
    for e in els:
        if str(e.get("role") or "").lower() in ("document", "edit"):
            return e
    return None


def _call_checked(tool: str, args: dict, *, timeout: float = 15.0) -> dict:
    """daemon.call + refusal -> exception, so the dispatcher cannot report ok."""
    res = daemon.call(tool, args, timeout=timeout)
    msg = _driver_refusal(res)
    if msg:
        raise DaemonError(f"{tool}: {msg}")
    return res


def _call_type(args: dict) -> dict:
    """type_text, honouring the driver's explicit foreground escalation.

    Returns only a delivered result. When the target drops background input
    and the foreground retry is also unverified, raise instead of leaving the
    judge to repeat the same action until the loop-guard aborts.
    """
    res = _call_checked("type_text", args)
    if _wants_foreground(res):
        retry = dict(args)
        retry["delivery_mode"] = "foreground"
        res = _call_checked("type_text", retry)
        if _wants_foreground(res) or str(res.get("effect")) == "unverifiable":
            raise DaemonError(
                "type_text: target drops background input and foreground "
                "delivery was unverified")
    return res


def _call_key(tool: str, args: dict, *, timeout: float = 15.0) -> dict:
    """press_key/hotkey with the same one-shot foreground escalation."""
    res = _call_checked(tool, args, timeout=timeout)
    if _wants_foreground(res):
        retry = dict(args)
        retry["delivery_mode"] = "foreground"
        res = _call_checked(tool, retry, timeout=timeout)
        if str(res.get("effect")) == "unverifiable":
            raise DaemonError(
                f"{tool}: target drops background keys and foreground "
                "delivery was unverified")
    return res


@dataclass
class ComputerResult:
    success: bool
    layer: str
    output: dict = field(default_factory=dict)
    trace: list[str] = field(default_factory=list)
    error: str = ""


class ComputerUseSkill:
    """Drives a desktop app toward a goal using the layered cascade."""

    def __init__(self, *, session_id: str = "s8-default",
                 llm_chat: Callable | None = None,
                 llm_vision: Callable | None = None,
                 safety: safety.SafetyGates | None = None):
        self.session_id = session_id
        self.safety = safety or SafetyGates()
        # Injected LLM callables (so we stay network-free in unit tests).
        # Signature: llm_chat(system, user, schema) -> dict (parsed JSON)
        #            llm_vision(image_data_url, prompt, system) -> dict
        self._llm_chat = llm_chat
        self._llm_vision = llm_vision
        self._daemon_available = False
        self._dry_run_plan: list[str] = []
        self._recorder = None  # recording is opt-in via run(record=True)
        self._last_dispatch_error: str = ""

    def _stop_recorder(self) -> None:
        """Idempotent recorder shutdown — every run() exit path funnels here
        via _done() so the daemon never leaks an open recording."""
        rec, self._recorder = self._recorder, None
        if rec is not None:
            try:
                rec.stop()
            except Exception:
                pass

    def _done(self, success: bool, layer: str, **kw) -> ComputerResult:
        self._stop_recorder()
        return ComputerResult(success, layer, **kw)

    # ── public entry ─────────────────────────────────────
    def run(self, goal: str, *, app_hint: str | None = None,
            max_turns: int = 12, record: bool = False) -> ComputerResult:
        """Execute `goal` against the desktop. Returns ComputerResult.

        `record=True` arms the trajectory Recorder (replay viewer). Kept for
        backwards-compat with test_computer_use_replay.py which passes it.
        """
        trace: list[str] = []
        # Opt-in recording (previously _recorder was never armed, and the id
        # carried no session correlation). Every exit below funnels through
        # _done(), which stops the recorder, so no path leaks it.
        if record:
            try:
                from .core.recording import Recorder
                self._recorder = Recorder(f"{self.session_id}-{int(time.time())}")
                if hasattr(self._recorder, "start"):
                    self._recorder.start()
            except Exception:
                self._recorder = None
        # 0) Safety gate: enabled?
        if not self.safety.enabled:
            return self._done(False, "disabled",
                              error="Computer-use is disabled. "
                                    "Set COMPUTER_USE_ENABLED=true to opt in.")
        # 0.5) Permission pre-flight (charter §8.1): fail fast with an
        # actionable error if AX / screenshot / daemon are not usable.
        perm = check_permissions()
        if not perm.ok():
            err = perm.error_message()
            return self._done(False, "permission", trace=trace, error=err)
        trace.append("permissions ok")
        # 1) Ensure daemon; if unavailable, fall back to gated-shell L0.
        if not daemon.ensure_daemon():
            trace.append("daemon unavailable → gated-shell L0 fallback")
            return self._gated_shell_fallback(goal)
        self._daemon_available = True
        trace.append("daemon up")

        # 2) Pick a target app (launch if needed).
        # If the planner didn't name the app (metadata.app), recover it from the
        # goal so the launch cascade (cua-driver launch_app / shell start) still
        # fires — otherwise the engine targets the current window and the judge
        # has to open the app via the UI (vision), which is flaky.
        if not app_hint:
            _from_goal = _app_from_goal(goal)
            if _from_goal:
                app_hint = _from_goal
                trace.append(f"app from goal: {app_hint}")
        pid, window_id = self._acquire_target(app_hint)
        if pid is None:
            return self._done(False, "no-target",
                                  trace=trace, error="No suitable target app found.")
        trace.append(f"target pid={pid} window={window_id}")

        # 3) Run the cascade loop.
        recovery = layers.RecoveryPolicy()
        vision_fb = layers.VisionFallback()
        l2b_failures = 0
        l3_calls = 0
        last_state = None
        last_screenshot = None
        last_shot_mime = "image/png"
        last_act = None
        last_act_ok = False
        # Factual log of what this run has done (oldest first), shown to the
        # judge each turn so the otherwise-stateless judge knows what happened.
        history: list[str] = []
        # Generic loop-guard: track consecutive identical actions (any type).
        last_act_key: str | None = None
        repeat_count = 0
        max_turns = min(max_turns, MAX_TURNS_HARD)
        for turn in range(max_turns):
            need = goal
            # No per-turn ensure_daemon() here: daemon.call() already probes the
            # pipe and restarts a dead/zombie daemon before every invocation, so
            # an explicit re-probe just added two subprocess spawns per turn. A
            # daemon that dies mid-run is recovered by the scan's call() below;
            # if it cannot restart, that raises DaemonError -> daemon-error.
            # SCAN (Invariant 1). The driver occasionally returns an empty AX
            # tree on the first scan right after launch; retry a couple times
            # (with a short settle delay) before treating it as truly empty.
            state = None
            for _scan_try in range(3):
                try:
                    state = daemon.call("get_window_state",
                                        {"pid": pid, "window_id": window_id,
                                         "include_screenshot": True}, timeout=30)
                except PreconditionError:
                    state = None
                except DaemonError as e:
                    return self._done(False, "daemon-error", trace=trace, error=str(e))
                if state is None:
                    continue
                # Cua Driver 0.17 requires snapshot_id alongside element_index
                # for any action (bare element_index is not accepted).
                snapshot_id = state.get("snapshot_id")
                # Retain the most recent screenshot across scans: the driver
                # sometimes omits the screenshot on a re-fetch, and L3 vision
                # needs it even when the current AX scan is empty.
                shot = (state.get("screenshot") or state.get("image")
                        or state.get("screenshot_png_b64"))
                if shot:
                    last_screenshot = shot
                    last_shot_mime = state.get("screenshot_mime_type") or "image/png"
                tree_md = state.get("tree_markdown") or ""
                if layers.element_count(tree_md) > 0:
                    break
                # Empty AX — wait briefly and re-scan (window still settling).
                time.sleep(1.0)
            if state is None:
                return self._done(False, "daemon-error", trace=trace,
                                      error="window state unavailable")

            # STALE WINDOW GUARD: cua-driver returns only {"text": ...} (no
            # tree_markdown / elements / snapshot_id) when the cached
            # window_id is stale (window closed or id changed) OR the window is
            # still POPULATING after a launch. Re-scan with a settle first; only
            # re-acquire if it is genuinely gone, and re-acquire WITHOUT
            # relaunching (allow_launch=False) — relaunching an already-running
            # app closes and reopens it (the taskbar flicker) and restarts the
            # wait, so a slow-launching window looped forever.
            if not (state.get("tree_markdown") or state.get("elements")
                    or state.get("snapshot_id")):
                for _settle in range(3):
                    time.sleep(1.0)
                    try:
                        state = daemon.call("get_window_state",
                                            {"pid": pid, "window_id": window_id,
                                             "include_screenshot": True}, timeout=30)
                    except Exception:
                        state = None
                    if state and (state.get("tree_markdown")
                                  or state.get("elements")
                                  or state.get("snapshot_id")):
                        break
                if not (state and (state.get("tree_markdown")
                                   or state.get("elements")
                                   or state.get("snapshot_id"))):
                    trace.append("stale window_id → re-acquiring (no relaunch)")
                    new_pid, new_wid = self._acquire_target(app_hint, allow_launch=False)
                    if new_pid is not None:
                        trace.append(f"re-acquired pid={new_pid} window={new_wid}")
                        pid, window_id = new_pid, new_wid
                        # Re-scan with the fresh window_id.
                        try:
                            state = daemon.call("get_window_state",
                                                {"pid": pid, "window_id": window_id,
                                                 "include_screenshot": True}, timeout=30)
                        except Exception:
                            state = None
                        if state is None or not (state.get("tree_markdown")
                                                or state.get("elements")
                                                or state.get("snapshot_id")):
                            return self._done(False, "no-target",
                                              trace=trace,
                                              error="target window unavailable")
                    else:
                        return self._done(False, "no-target", trace=trace,
                                          error="target window unavailable")

            tree_md = state.get("tree_markdown") or ""
            count = layers.element_count(tree_md)
            trace.append(f"turn {turn}: element_count={count}")

            # ── L1 EXTRACT (zero-LLM read) ───────────────────────────────
            # For read-only goals ("what does this say", "read cell B3"),
            # try to pull the content directly from the AX tree / clipboard
            # before spending an LLM call. If we get a result, return it.
            if count > 0:
                extracted = layers.try_extract(goal, tree_md, pid=pid,
                                               window_id=window_id)
                if extracted is not None:
                    trace.append(f"L1 extract: {extracted['method']}")
                    return self._done(
                        True, "L1", output={"content": extracted["content"],
                                           "method": extracted["method"]},
                        trace=trace)

            # ── L2a DETERMINISTIC (zero-LLM hotkey sequence) ─────────────
            # For known goals (calculator arithmetic, notepad write+save,
            # select-all+copy), dispatch a fixed action sequence with no LLM.
            det = layers.try_deterministic(goal, app_hint, state=state)
            if det is not None:
                trace.append(f"L2a deterministic: {det.description}")
                ok_all = True
                for act in det.actions:
                    ok = self._dispatch_action(pid, window_id, act, snapshot_id)
                    if not ok:
                        ok_all = False
                        break
                    # Settle: the target app (Calculator, Notepad) needs a brief
                    # beat to process each input before the next arrives, or
                    # rapid clicks are dropped/ignored and the result is wrong.
                    time.sleep(0.4)
                if ok_all:
                    if det.app == "calculator":
                        # Re-scan and check the display shows a result.
                        time.sleep(0.5)
                        st2 = daemon.call("get_window_state",
                                         {"pid": pid, "window_id": window_id,
                                          "include_screenshot": True}, timeout=20)
                        disp = ""
                        for line in (st2.get("tree_markdown") or "").splitlines():
                            if "Display is" in line:
                                # Line looks like: [8] Text "Display is 1,32,678" [id=...]
                                # The number follows "Display is " and ends at the
                                # closing double-quote (or end of the quoted span).
                                after = line.split("Display is", 1)[1]
                                after = after.strip()
                                if after.startswith('"'):
                                    after = after[1:]
                                q = after.find('"')
                                disp = after[:q].strip() if q >= 0 else after.strip()
                                break
                        return self._done(True, "L2a", output={"display": disp},
                                             trace=trace)
                    return self._done(True, "L2a", output={"plan": det.description},
                                         trace=trace)
                trace.append("L2a deterministic action failed; falling back to L2b")

            # L2b a11y (workhorse). Escalate to L3 if AX empty or judge says so.
            if count == 0:
                # A SUSPENDED target (Windows suspended it to save resources)
    # looks running but has an empty AX tree. Resume ONLY the target pid
    # once and re-scan; never touch other suspended windows.
                # SUSPENDED-TARGET RECOVERY (see _resume_suspended): empty AX
    # tree = the target is likely suspended. Resume ONLY the target pid
    # once, re-scan; if still empty it is genuinely empty -> escalate.
                if getattr(self, "_resumed_pid", None) != pid:
                    self._resume_suspended(pid)
                    self._resumed_pid = pid
                    trace.append("target looked suspended -> resumed; re-scanning")
                    time.sleep(1.5)
                    continue
                l2b_failures += 1
                if vision_fb.should_escalate(l2b_failures, "ax_tree_empty"):
                    # The first scan after launch can return an empty AX tree
                    # AND no screenshot. Re-fetch once (the window often needs
                    # a moment to populate its accessibility tree + capture).
                    if not self._has_screenshot(state):
                        time.sleep(1.0)
                        try:
                            state = daemon.call("get_window_state",
                                                {"pid": pid, "window_id": window_id,
                                                 "include_screenshot": True}, timeout=30)
                            shot = (state.get("screenshot") or state.get("image")
                                    or state.get("screenshot_png_b64"))
                            if shot:
                                last_screenshot = shot
                                last_shot_mime = state.get("screenshot_mime_type") or "image/png"
                        except Exception:
                            pass
                    # Fall back to the most recent screenshot we captured.
                    if not self._has_screenshot(state) and last_screenshot is not None:
                        state = dict(state)
                        state["screenshot_png_b64"] = last_screenshot
                        state["screenshot_mime_type"] = last_shot_mime
                    # Last resort: grab a desktop screenshot directly. cua-driver
                    # returns screenshots on get_desktop_state even when the
                    # per-window fetch omits them — without this L3 would almost
                    # never engage.
                    if not self._has_screenshot(state):
                        try:
                            desk = daemon.call("get_desktop_state", {}, timeout=15)
                            desk_shot = (desk.get("screenshot") or desk.get("image")
                                         or desk.get("screenshot_png_b64"))
                            if desk_shot:
                                state = dict(state)
                                state["screenshot_png_b64"] = desk_shot
                                state["screenshot_mime_type"] = "image/png"
                                last_screenshot = desk_shot
                                last_shot_mime = "image/png"
                        except Exception:
                            pass
                    if self._has_screenshot(state):
                        if l3_calls >= MAX_L3_CALLS:
                            trace.append(f"L3 cap {MAX_L3_CALLS} hit → abort")
                            return self._done(False, "max-turns", trace=trace,
                                                  error="L3 vision call cap reached")
                        trace.append("L2b empty → L3 vision (one action)")
                        l3_calls += 1
                        res = self._layer_vision(goal, pid, window_id, state, need=need)
                        if not res.success:
                            return self._done(False, "L3", trace=trace,
                                              error=res.error or "vision fallback failed")
                        if (res.output or {}).get("verdict") == "done":
                            return self._done(True, "L3", output=res.output, trace=trace)
                        trace.append("L3 acted → back to L2 with fresh scan")
                        last_state = state
                        continue

                    trace.append("L2b empty but no screenshot → stay on a11y")
                continue

            # L2b judgment call (cheap text model).
            # FILTER REMOVED (test): send the FULL tree so the model sees the
            # editor's value in full (the length-based filter truncated it, so
            # the model couldn't confirm the typed text landed and re-typed).
            filtered = tree_md
            judge = self._judge_a11y(goal, filtered, pid, app_hint or "app", last_act,
                                   vision_ctx=getattr(self, "_last_vision", ""),
                                   history=history)
            if judge is None:
                l2b_failures += 1
                trace.append("L2b judge returned no action")
                if vision_fb.should_escalate(l2b_failures, None):
                    if l3_calls >= MAX_L3_CALLS:
                        return self._done(False, "max-turns", trace=trace,
                                              error="L3 vision call cap reached")
                    if self._has_screenshot(state):
                        trace.append("L2b escalate → L3 vision (one action)")
                        l3_calls += 1
                        res = self._layer_vision(goal, pid, window_id, state, need=need)
                        if not res.success:
                            return self._done(False, "L3", trace=trace,
                                              error=res.error or "vision failed")
                        if (res.output or {}).get("verdict") == "done":
                            return self._done(True, "L3", output=res.output, trace=trace)
                        trace.append("L3 acted → back to L2 with fresh scan")
                        last_state = state
                        continue

                    trace.append("L2b retries exhausted; no screenshot for L3 → abort")
                    return self._done(False, "L2b", trace=trace,
                                          error="a11y judge unavailable and no screenshot for vision fallback")
                continue

            verdict = judge.get("verdict")
            if verdict == "done":
                _why = str(judge.get("thinking") or "").strip()[:240]
                trace.append("L2b: goal met" + (f" — {_why}" if _why else ""))
                out = dict(judge)
                if self._dry_run_plan:
                    out["dry_run_plan"] = self._dry_run_plan
                return self._done(True, "L2b", output=out, trace=trace)
            if verdict == "escalate":
                l2b_failures += 1
                need = str(judge.get("action", {}).get("note") or goal)
                trace.append(f"L2b escalate: {judge.get('action', {}).get('note')}")
                if vision_fb.should_escalate(l2b_failures, "element_missing"):
                    if l3_calls >= MAX_L3_CALLS:
                        return self._done(False, "max-turns", trace=trace,
                                              error="L3 vision call cap reached")
                    l3_calls += 1
                    res = self._layer_vision(goal, pid, window_id, state, need=need)
                    if not res.success:
                        return self._done(False, "L3", trace=trace,
                                          error=res.error or "vision failed")
                    if (res.output or {}).get("verdict") == "done":
                        return self._done(True, "L3", output=res.output, trace=trace)
                    trace.append("L3 acted → back to L2 with fresh scan")
                    last_state = state
                    continue
                continue

            # ACT (Invariant 2: re-scan happens next loop iteration)
            act = judge.get("action", {})
            # VALIDATE the action against its type. A click with no target (no
    # int element_index and no x/y) would no-op and loop — the model sometimes
    # mis-fills the schema (e.g. value="element_index", x:1). Re-ask once with
    # the same tree; if it is still targetless, skip the dispatch and re-scan.
            if (act.get("type") == "click"
                    and not isinstance(act.get("element_index"), int)
                    and not ("x" in act and "y" in act)):
                trace.append("malformed click (no target) -> re-asking judge")
                _re = self._judge_a11y(goal, filtered, pid, app_hint or "app",
                                       last_act,
                                       vision_ctx=getattr(self, "_last_vision", ""),
                                       history=history)
                _new = (_re or {}).get("action")
                if _new:
                    act = _new
                if (act.get("type") == "click"
                        and not isinstance(act.get("element_index"), int)
                        and not ("x" in act and "y" in act)):
                    trace.append("still targetless click -> skip dispatch, re-scan")
                    self._last_dispatch_error = "click had no target (element_index / x,y)"
                    continue
            # ANTI-TOGGLE GUARD: only for CLICK actions. If the judge emits
            # the SAME click twice in a row (e.g. clicking Play, then Play
            # again after it already toggled to Pause), the goal is already
            # met — do NOT click again (that would just toggle playback off).
            # We deliberately scope this to "click" ONLY: a repeated "type"
            # or "scroll" is NOT a toggle and must never be auto-marked done
            # (that would falsely report success, e.g. typing into search
            # then declaring "song played").
            same_click_as_last = (
                act.get("type") == "click"
                and last_act is not None
                and last_act.get("type") == "click"
                and act.get("element_index") == last_act.get("element_index")
            )
            # Generic loop-guard: same action key 3x in a row -> abort.
            try:
                import json as _j
                act_key = _j.dumps(act, sort_keys=True, default=str)
            except Exception:
                act_key = str(act)
            if act_key == last_act_key:
                repeat_count += 1
            else:
                last_act_key = act_key
                repeat_count = 1
            if repeat_count >= MAX_ACTION_REPEATS:
                trace.append(f"loop-guard: same action {repeat_count}x → abort")
                return self._done(False, "aborted", trace=trace,
                                      error=f"loop-guard: repeated action {act.get('type')} {repeat_count}x")
            if same_click_as_last and last_act_ok:
                # Do NOT re-dispatch the same click (that just toggles/repeats),
                # and do NOT fake success: re-scan and let the judge choose again.
                trace.append("repeat click → re-asking judge with fresh scan")
                last_state = state
                continue
            ok = self._dispatch_action(pid, window_id, act, snapshot_id,
                                       turn_state=state)
            last_act, last_act_ok = act, ok
            trace.append(f"act {act.get('type')} -> {'ok' if ok else 'fail'}")
            _hkey = act.get('element_index', act.get('value', act.get('keys', '')))
            history.append(f"{act.get('type')} {_hkey} -> {'ok' if ok else 'fail'}")
            if ok:
                # The target's AX tree LAGS the action (~100ms native to ~1-2s
                # heavy). Instead of a fixed sleep (a guess that's too short for
                # heavy apps), poll until the tree actually CHANGES from the
                # pre-action one and stabilizes — then the next scan is current.
                self._wait_for_tree_change(pid, window_id, state)
            if not ok:
                # Pass the structured reason (daemon error text), not a bare
                # "action failed" — recovery.handle branches on substrings
                # like element_index/permission and otherwise always rescan.
                _reason = f"action failed ({act.get('type')})"
                if self._last_dispatch_error:
                    _reason += f": {self._last_dispatch_error}"
                directive = recovery.handle(_reason)
                if directive == "abort":
                    return self._done(False, "aborted", trace=trace,
                                      error=f"action failed repeatedly ({self._last_dispatch_error or 'no detail'})")
            last_state = state

            # DRY-RUN: the plan IS the deliverable. We never execute on the
            # desktop, so the goal can't be "met" by re-scanning. Once we have
            # a concrete action planned, return success with the dry-run plan
            # instead of looping all 12 turns (which would just re-plan the
            # same action and report max-turns failure).
            if self.safety.mode == "dry-run" and self._dry_run_plan:
                out = dict(judge)
                out["dry_run_plan"] = self._dry_run_plan
                trace.append("dry-run: plan produced, stopping")
                return self._done(True, "L2b-dry-run", output=out, trace=trace)

        out = {}
        if self._dry_run_plan:
            out["dry_run_plan"] = self._dry_run_plan
        return self._done(False, "max-turns", output=out, trace=trace,
                          error="Reached max turns without completing goal.")

    # ── target acquisition ───────────────────────────────
    def _launch_by_shell(self, app_hint: str) -> bool:
        """Best-effort launch via the OS shell (works for ANY installed app).

        `cmd /c start "<name>"` resolves Start-menu / registered app names
        that cua-driver's launch_app may not know (e.g. Store apps, pinned
        apps). Returns True if the spawn call succeeded.
        """
        try:
            if os.name == "nt":
                # Launch on the PRIVATE desktop when isolation is on, so the
                # app the agent opens never lands on the user's desktop.
                try:
                    from . import private_desktop as _pd
                    if _pd.enabled():
                        pid = _pd.launch_on_private_desktop(
                            "cmd.exe", f'/c start "" "{app_hint}"')
                        if pid:
                            return True
                except Exception:
                    pass
                subprocess.Popen(["cmd", "/c", "start", "", app_hint],
                                 shell=False, creationflags=subprocess.DETACHED_PROCESS)
            else:
                subprocess.Popen(["open", app_hint] if os.name == "posix"
                                 else ["xdg-open", app_hint])
            return True
        except Exception:
            return False

    def _launch_by_vision(self, app_hint: str) -> bool:
        """Last-resort launch: locate the app's icon on the desktop and click it.

        Takes a full desktop screenshot, asks the vision model where the
        `app_hint` icon is (taskbar / desktop / start menu), then performs a
        screen-coordinate click (scope=desktop). This is GENERAL — it works
        for any app the user can see pinned, with no per-app hardcoding.
        Returns True if a click was issued.
        """
        # Safety: vision-click launch is a real desktop side-effect — respect
        # dry-run and approval gates.
        if self.safety.mode == "dry-run":
            self._dry_run_plan.append(f"[dry-run] would vision-click icon for '{app_hint}'")
            return False
        if (self.safety.needs_approval("vision_launch", {"app": app_hint})
                and not self.safety.already_approved(
                    "vision_launch", {"app": app_hint})):
            aid = self.safety.create_approval("vision_launch", {"app": app_hint})
            # Don't block the cascade on approval plumbing; record and skip.
            self._dry_run_plan.append(f"[pending {aid}] vision-click for '{app_hint}'")
            return False
        if self._llm_vision is None:
            return False
        try:
            desk = daemon.call("get_desktop_state", {}, timeout=15)
        except Exception:
            return False
        b64 = (desk.get("screenshot_png_b64") or desk.get("screenshot")
               or desk.get("image"))
        if not b64:
            return False
        if isinstance(b64, str) and b64.startswith("data:"):
            b64 = b64.split(",", 1)[1]
        shot = f"data:image/png;base64,{b64}"
        # Sanitise app_hint: strip whitespace, quote characters, and control
        # characters so a weird app name can't break out of the f-string
        # prompt or inject instructions to the vision model.
        safe_hint = re.sub(r"[\s'\"\\\x00-\x1f]+", "_", str(app_hint or ""))[:80]
        prompt = (
            f"Find the icon for the application '{safe_hint}' on this desktop "
            f"(it may be pinned on the taskbar, on the desktop, or in the "
            f"system tray). Reply with ONLY JSON: "
            f'{{"found": true, "x": <pixel_x>, "y": <pixel_y>}} or '
            f'{{"found": false}}.'
        )
        try:
            verdict = self._llm_vision(shot, prompt, P.SYSTEM_VISION)
        except Exception:
            return False
        if isinstance(verdict, str):
            try:
                verdict = json.loads(verdict)
            except Exception:
                return False
        if not isinstance(verdict, dict) or not verdict.get("found"):
            return False
        try:
            x, y = int(verdict["x"]), int(verdict["y"])
            daemon.call("click", {"x": x, "y": y, "scope": "desktop"},
                        timeout=15)
            self.safety.record_action("vision_launch",
                                          {"app": app_hint}, "done")
            return True
        except Exception as e:
            self.safety.record_action("vision_launch",
                                          {"app": app_hint},
                                          f"error: {type(e).__name__}")
            return False

    def _acquire_target(self, app_hint: str | None, allow_launch: bool = True):
        """Find or launch the target app; return (pid, window_id).

        GENERAL launch cascade (no per-app hardcoding):
          1. match an already-open window by title,
          2. cua-driver launch_app by name,
          3. OS shell `start "<name>"` (any installed app),
          4. vision-locate the icon on the desktop and click it.
        Uses `get_accessibility_tree` (which reliably reports pid+window_id
        per visible window) as the primary source; `list_apps` windows field
        is empty on this driver build, so we don't rely on it.

        Electron apps (VS Code, Slack, Discord, ...) are detected and
        relaunched with a CDP debugging port so the DOM can be driven via
        Chrome DevTools Protocol (charter §9).
        """
        try:
            desktop = daemon.call("get_accessibility_tree", {}, timeout=15)
        except DaemonError:
            return None, None
        wins = desktop.get("windows") or []
        if not wins:
            return None, None

        # Electron escape hatch: if the hinted app is a known Electron
        # app, relaunch with a debugging port and drive via CDP.
        # The relaunch is a REAL launch plus an unauthenticated
        # CDP endpoint (arbitrary JS execution inside the app), so
        # it passes the same dry-run/approval gates as any other
        # launch - a gated path that skipped only the plain launches
        # would still open the debug port.
        if (app_hint and apps.electron.is_electron(app_hint)
                and self.safety.mode != "dry-run"
                and not self.safety.needs_approval(
                    "launch_app", {"name": app_hint})):
            try:
                launched = apps.electron.launch_with_debug_port(app_hint, port=9222)
                pid = launched["pid"]
                window_id = launched["window_id"]
                # Bring to front (Windows-only; no-op on macOS/Linux).
                try:
                    daemon.call("bring_to_front",
                                {"pid": pid, "window_id": window_id}, timeout=10)
                except Exception:
                    pass
                return pid, window_id
            except Exception:
                pass  # fall through to normal launch below
        elif app_hint and apps.electron.is_electron(app_hint):
            # Gated: record the intended relaunch on the plan.
            if self.safety.mode == "dry-run":
                self._dry_run_plan.append(
                    f"[dry-run] would relaunch '{app_hint}' with a CDP "
                    f"debug port")
            else:
                aid = self.safety.create_approval(
                    "launch_app", {"name": app_hint})
                self._dry_run_plan.append(
                    f"[pending {aid}] CDP relaunch of '{app_hint}' "
                    f"skipped awaiting approval")

        # Match by hint against the window title (app name usually present).
        target = None
        if app_hint:
            for w in wins:
                if app_hint.lower() in (w.get("title") or "").lower():
                    target = w
                    break
        # No matching window: GENERAL launch cascade (no per-app hardcoding).
        # 1) cua-driver launch_app by name. 2) OS shell `start "<name>" (any
        #    installed app, incl. Store/pinned). 3) vision-locate the icon on
        #    the desktop and click it. Re-scan after each attempt; only fall
        #    back to the first window if every launch path fails.
        if target is None and app_hint and allow_launch:
            if self.safety.mode == "dry-run":
                # DRY-RUN: `launch_app` and the OS-shell start are
                # real launches (they spawn processes); only the
                # vision path below carries its own gate. Record
                # the intended launch and leave the target unset -
                # the cascade then falls back to the first window,
                # which is what a failed launch already does.
                self._dry_run_plan.append(
                    f"[dry-run] would launch '{app_hint}'")
            else:
                launched = False
                # (1) cua-driver launch_app
                try:
                    daemon.call("launch_app", {"name": app_hint}, timeout=20)
                    launched = True
                except Exception:
                    pass
                # (2) OS shell start — covers names launch_app doesn't know
                if not launched:
                    launched = self._launch_by_shell(app_hint)
                # (3) vision-locate + click the icon (works for pinned apps)
                if not launched:
                    self._launch_by_vision(app_hint)
            # Re-scan for the new window (give it a moment to appear).
            time.sleep(1.5)
            try:
                desktop2 = daemon.call("get_accessibility_tree", {}, timeout=15)
                for w in (desktop2.get("windows") or []):
                    if app_hint.lower() in (w.get("title") or "").lower():
                        target = w
                        break
            except Exception:
                pass
        if target is None:
            if not allow_launch:
                # Mid-run re-acquire: do NOT fall back to a random window
                # (that would hand the stale-window guard the wrong app). If
                # the target isn't in the tree, report none and let it re-scan.
                return None, None
            target = wins[0]
        pid = target.get("pid")
        window_id = target.get("window_id")
        # macOS background-launch trap (charter §8.2): a backgrounded app's
        # window is not yet built in the AX hierarchy, so the first scan
        # returns the system menu bar and zero app buttons. AppleScript
        # activation realises the window; bring_to_front is Windows-only.
        # Both are real desktop mutations - skip them in dry-run.
        if (sys.platform == "darwin" and app_hint
                and self.safety.mode != "dry-run"):
            try:
                import subprocess
                subprocess.run(["osascript", "-e",
                                f'tell application "{app_hint}" to activate'],
                               check=True, capture_output=True, timeout=10)
                time.sleep(0.5)
            except Exception:
                pass
        # Windows: bring to front (the spec's Windows-only call).
        if (pid and window_id is not None
                and self.safety.mode != "dry-run"):
            try:
                daemon.call("bring_to_front", {"pid": pid, "window_id": window_id}, timeout=10)
            except Exception:
                pass
        return pid, window_id

    # ── L2b judgment (cheap text LLM) ────────────────────
    def _judge_a11y(self, goal: str, tree_md: str, pid: int, app_name: str,
                    last_act: dict | None = None,
                    vision_ctx: str = "", history: list[str] | None = None) -> dict | None:
        if self._llm_chat is None:
            return None
        user = P.USER_A11Y_TEMPLATE.format(goal=goal, app_name=app_name, pid=pid,
                                           tree_markdown=tree_md)
        # The judge is stateless per turn (goal + current tree only). A factual
        # HISTORY of what this run has already done gives it the context it
        # lacks — "opened the File menu", "typed the filename" — so it advances
        # instead of repeating. This is a log, NOT a "you failed" nudge.
        if history:
            hist = "\n".join(f"- {h}" for h in history[-12:])
            user += ("\n\nHISTORY (what you have already done this run, oldest "
                     "first):\n" + hist + "\nUse it to avoid repeating a step and "
                     "to decide the next one.")
        if vision_ctx:
            user += (f"\n\nVISION LAYER: {vision_ctx}. The screen changed after that; "
                     "re-read the tree above before deciding.")
        try:
            return self._llm_chat(P.SYSTEM_A11Y, user, P.ACTION_SCHEMA_JSON)
        except Exception:
            return None

    # ── L3 vision fallback ───────────────────────────────
    @staticmethod
    def _has_screenshot(state: dict) -> bool:
        """True only if the driver actually returned a screenshot we can use."""
        return bool(state.get("screenshot") or state.get("image")
                    or state.get("screenshot_png_b64"))

    def _best_screenshot(self, state: dict, pid: int, window_id) -> tuple[str | None, list[dict], bool]:
        """Return (screenshot_png_b64, elements), preferring the window state
        but falling back to a fresh desktop screenshot. cua-driver returns
        screenshots on ``get_desktop_state`` even when ``get_window_state``
        omits them, so without this fallback L3 vision would almost always
        fail with "no screenshot"."""
        raw = (state.get("screenshot") or state.get("image")
               or state.get("screenshot_png_b64"))
        elements = state.get("elements") or []
        if raw:
            return raw, elements, False
        # Window state had no screenshot — fetch the desktop shot. Set-of-marks
        # will be drawn over the (empty) element list, which is fine: the vision
        # model can still locate the target by pixel coordinates.
        try:
            desk = daemon.call("get_desktop_state", {}, timeout=15)
            desk_raw = (desk.get("screenshot") or desk.get("image")
                        or desk.get("screenshot_png_b64"))
            if desk_raw:
                return desk_raw, elements, True
        except Exception:
            pass
        return raw, elements, False

    @staticmethod
    def _window_logical_origin(pid, scale):
        """The visible window's top-left in LOGICAL screen coords (for the
        resized logical screenshot). Element bboxes are window-relative, so
        they must be offset by this to land on the real window."""
        try:
            import ctypes
            from ctypes import wintypes
            u = ctypes.windll.user32
            best = None
            EP = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            def _cb(hwnd, lp):
                nonlocal best
                wp = wintypes.DWORD(); u.GetWindowThreadProcessId(hwnd, ctypes.byref(wp))
                if wp.value == pid and u.IsWindowVisible(hwnd):
                    rr = wintypes.RECT(); u.GetWindowRect(hwnd, ctypes.byref(rr))
                    area = (rr.right-rr.left)*(rr.bottom-rr.top)
                    if best is None or area > best[0]:
                        best = (area, rr.left, rr.top)
                return True
            u.EnumWindows(EP(_cb), 0)
            if best and scale:
                return int(best[1]/scale), int(best[2]/scale)
        except Exception:
            pass
        return 0, 0

    @staticmethod
    def _resume_suspended(pid):
        """Resume a SUSPENDED process (un-suspend its threads).

        WHY (observed live): Windows suspends background UWP apps to save /
        optimise performance. A suspended process STILL appears in the window
        list, so the layer reads it as "running" and L2 escalates — but its
        accessibility tree is EMPTY, so it fails with "no clickable target"
        even though the app is fine, merely suspended (seen with Settings).

        FIX: when the TARGET's AX tree is empty (the suspended signature),
        call this to un-suspend ONLY the target pid, then re-scan (see the
        `count == 0` branch in run()). NEVER touch unrelated suspended
        windows (e.g. GlobalDesktopSpotlight) — they are suspended for a
        reason and resuming them would interfere with Windows' scheduling.

        STATUS: implemented + compiles (computer-use suite green) but NOT yet
        verified end-to-end against a real suspended target — the live test
        could not force a suspend (the Settings process is "SystemSettings",
        not "Settings"). Verify when a target is next found suspended. """
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.windll.kernel32
            TH32CS_SNAPTHREAD = 0x00000004
            class _TE(ctypes.Structure):
                _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                            ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
                            ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG),
                            ("dwFlags", wintypes.DWORD)]
            snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
            if snap == -1:
                return
            try:
                te = _TE(); te.dwSize = ctypes.sizeof(te)
                if k32.Thread32First(snap, ctypes.byref(te)):
                    while True:
                        if te.th32OwnerProcessID == pid:
                            h = k32.OpenThread(0x0001, False, te.th32ThreadID)
                            if h:
                                k32.ResumeThread(h); k32.CloseHandle(h)
                        if not k32.Thread32Next(snap, ctypes.byref(te)):
                            break
            finally:
                k32.CloseHandle(snap)
        except Exception:
            pass

    @staticmethod
    def _resize_to_logical(b64, elements, is_desktop):
        """Resize the screenshot to the LOGICAL coordinate space so the
        element bboxes, the model's returned pixels and the click all align.
        The AX coords are logical (DPI-scaled); screenshots are physical, so
        without this the marks land at 1/scale in the top-left."""
        try:
            import io as _io, base64 as _b64
            from PIL import Image
            img = Image.open(_io.BytesIO(_b64.b64decode(b64))).convert("RGB")
            if is_desktop:
                import ctypes
                lw = int(ctypes.windll.user32.GetSystemMetrics(0))
                lh = int(ctypes.windll.user32.GetSystemMetrics(1))
            else:
                lw = lh = 0
                for e in elements:
                    bb = (e.get("frame") or e.get("bbox") or e.get("bounds")
                          or e.get("rectangle") or e.get("rect") or {})
                    px, py, pw, ph = layers.vision._parse_bbox(bb)
                    if px is not None:
                        lw = max(lw, px + pw); lh = max(lh, py + ph)
            if lw and lh and (lw, lh) != img.size:
                img = img.resize((lw, lh), Image.LANCZOS)
                buf = _io.BytesIO(); img.save(buf, format="PNG")
                return _b64.b64encode(buf.getvalue()).decode("ascii")
        except Exception:
            pass
        return b64

    def _layer_vision(self, goal: str, pid: int, window_id, state: dict,
                      need: str = "") -> ComputerResult:
        # NOTE: helper — constructs ComputerResult directly (NOT via _done):
        # only run() exits may stop the recorder.
        b64, elements, is_desktop = self._best_screenshot(state, pid, window_id)
        if not b64 or self._llm_vision is None:
            return ComputerResult(False, "L3", error="no screenshot or vision LLM")
        # Normalise to a base64 string (strip data: prefix if present).
        if isinstance(b64, str) and b64.startswith("data:"):
            b64 = b64.split(",", 1)[1] if "," in b64 else b64
        b64 = self._resize_to_logical(b64, elements, is_desktop)
        # Draw set-of-marks: numbered dashed boxes over UI elements.
        _scale = 1.0
        try:
            from PIL import Image as _I
            import io as _io, base64 as _b64
            _pw = _I.open(_io.BytesIO(_b64.b64decode(b64))).size[0]
            import ctypes
            _lw = int(ctypes.windll.user32.GetSystemMetrics(0))
            if _lw:
                _scale = _pw / _lw
        except Exception:
            pass
        _off = self._window_logical_origin(pid, _scale) if is_desktop else (0, 0)
        annotated_b64, legend = layers.draw_set_of_marks(b64, elements, offset=_off)
        screenshot = f"data:image/png;base64,{annotated_b64}"
        prompt = P.USER_VISION_TEMPLATE.format(goal=goal, legend=legend, need=need or goal)
        try:
            verdict = self._llm_vision(screenshot, prompt, P.SYSTEM_VISION)
        except Exception as e:
            return ComputerResult(False, "L3", error=str(e))
        # Normalise: the vision judge may return a dict, a JSON string, or a
        # plain description. Coerce to a dict we can reason about.
        if isinstance(verdict, str):
            import json as _json
            try:
                verdict = _json.loads(verdict)
            except Exception:
                return ComputerResult(False, "L3",
                                       error=f"vision returned non-JSON: {verdict[:120]}")
        if not isinstance(verdict, dict):
            return ComputerResult(False, "L3",
                                  error=f"vision returned unexpected type: {type(verdict)}")
        if verdict.get("verdict") == "done":
            return ComputerResult(True, "L3", output=verdict)
        act = verdict.get("action", {}) or {}
        if isinstance(act, str):
            try:
                act = _json.loads(act)
            except Exception:
                act = {}
        if act.get("type") == "click" and "x" in act and "y" in act:
            # DRY-RUN/APPROVAL: this path dispatches a raw pixel
            # click OUTSIDE `_dispatch_action`, so it must carry the
            # same gates — otherwise the identical physical action is
            # gated in one path and executed ungated in the other.
            if self.safety.mode == "dry-run":
                self._dry_run_plan.append(
                    f"[dry-run] would vision-click ({act['x']}, {act['y']})")
                return ComputerResult(True, "L3", output=verdict)
            click_args = {"x": act["x"], "y": act["y"]}
            if (self.safety.needs_approval("click", click_args)
                    and not self.safety.already_approved(
                        "click", click_args)):
                aid = self.safety.create_approval("click", click_args)
                self._dry_run_plan.append(
                    f"[pending {aid}] vision-click skipped awaiting approval")
                return ComputerResult(False, "L3",
                                       error="vision click awaiting approval")
            try:
                args = {"pid": pid, "x": act["x"], "y": act["y"]}
                if window_id is not None and not is_desktop:
                    args["window_id"] = window_id
                # A coordinate click must NOT carry snapshot_id: the driver
                # reads a snapshot as an element-handle claim and refuses it
                # with "snapshot_id requires element_index". That turned every
                # successful L3 vision target into a failed click.
                _call_checked("click", args)
                self.safety.record_action("click", args, "done")
                _lbl = ""
                for _e in elements:
                    _bb = (_e.get("frame") or _e.get("bbox") or _e.get("bounds")
                           or _e.get("rectangle") or _e.get("rect") or {})
                    _px, _py, _pw, _ph = layers.vision._parse_bbox(_bb)
                    if (_px is not None and _px <= act["x"] <= _px + _pw
                            and _py <= act["y"] <= _py + _ph):
                        _lbl = str(_e.get("label") or _e.get("name")
                                   or _e.get("role") or "")
                        break
                self._last_vision = (f"vision clicked {_lbl or 'a point'} "
                                     f"at ({act['x']},{act['y']})")
                return ComputerResult(True, "L3", output=verdict)
            except Exception as e:
                self.safety.record_action(
                    "click", args, f"error: {type(e).__name__}")
                return ComputerResult(False, "L3", error=str(e))
        return ComputerResult(False, "L3", error="vision returned no clickable target")

    # ── change-detection settle (replace fixed sleeps) ──────
    @staticmethod
    def _state_sig(state) -> str:
        """A content signature of the AX tree (ignores the per-scan snapshot_id,
        which changes every call). Two scans with the same visible tree hash the
        same; a real UI change hashes differently."""
        md = str((state or {}).get("tree_markdown") or "")
        if not md:
            return ""
        import hashlib
        return hashlib.md5(md.encode("utf-8", "replace")).hexdigest()

    def _wait_for_tree_change(self, pid, window_id, prev_state, *,
                              cap_polls: int = 15, interval: float = 0.15) -> None:
        """After an action, poll the AX tree until it CHANGES from the pre-action
        state and stabilizes (same content twice), instead of a fixed sleep.

        Fast apps flip in ~100ms; heavy apps take 1-2s — this waits for the real
        change rather than guessing. Caps out so a no-op action can't hang. Uses
        include_screenshot=False (the tree is all we need to detect change; the
        next full scan still captures the screenshot).
        """
        prev_sig = self._state_sig(prev_state)
        last_sig = prev_sig
        stable = 0
        for _ in range(cap_polls):
            time.sleep(interval)
            try:
                ns = daemon.call("get_window_state",
                                 {"pid": pid, "window_id": window_id,
                                  "include_screenshot": False}, timeout=20)
            except Exception:
                return
            if not ns:
                continue
            sig = self._state_sig(ns)
            if sig == last_sig:
                stable += 1
            else:
                last_sig = sig
                stable = 0
            # changed from the pre-action tree AND held for 2 polls -> ready
            if sig != prev_sig and stable >= 1:
                return

    # ── action dispatch (cua-driver primitives) ─────────
    def _dispatch_action(self, pid: int, window_id, act: dict,
                          snapshot_id: str | None = None, *,
                          turn_state: dict | None = None) -> bool:
        # DRY-RUN: describe the intended action, never touch the desktop.
        if self.safety.mode == "dry-run":
            trace = f"[dry-run] would {act.get('type')} " \
                    f"{act.get('element_index', act.get('value', act.get('keys', '')))}"
            self._dry_run_plan.append(trace)
            return True
        t = act.get("type")
        # APPROVAL: the low-level `click`/`type_text`/… primitives require
        # approval when they match a pattern (mcp_server.py), so the engine
        # path that performs the identical physical action must too —
        # otherwise the same click is gated in one path and ungated in the
        # other. Like vision-launch above: record and skip, don't block the
        # cascade on approval plumbing.
        if (self.safety.needs_approval(t or "", dict(act))
                and not self.safety.already_approved(t or "", dict(act))):
            aid = self.safety.create_approval(t or "", dict(act))
            self._dry_run_plan.append(f"[pending {aid}] {t} skipped awaiting approval")
            return False
        # A judge action (no `match` key) addresses the index the LLM just read
        # from THIS turn's opening scan, and no action has fired yet this turn,
        # so that snapshot/element_token is still current — reuse it instead of
        # paying for a second ~1.9s UIA scan. Deterministic `match` actions are
        # the exception: the tree reflows after every click, so they must always
        # re-fetch + re-resolve (turn_state is never passed on that path).
        reuse = turn_state if (turn_state is not None and not act.get("match")) else None
        # Cua Driver 0.17 requires snapshot_id + window_id for element actions.
        # Coordinate clicks (x,y without element_index) must NOT carry snapshot_id,
        # or the driver refuses with "snapshot_id requires element_index".
        base = {"pid": pid, "window_id": window_id}
        if snapshot_id and act.get("element_index") is not None:
            base["snapshot_id"] = snapshot_id
        try:
            if t == "click":
                args = dict(base)
                if act.get("element_index") is not None:
                    args["element_index"] = act["element_index"]
                if "x" in act and "y" in act:
                    args["x"], args["y"] = act["x"], act["y"]
                # Cua Driver snapshots expire after the first action in a
                # turn, so re-fetch a fresh snapshot immediately before each
                # element-targeted click. Without a current snapshot_id the
                # driver refuses element_index clicks.
                # RE-RESOLVE: the same fresh snapshot also lets us re-resolve a
                # stale element_index. Deterministic plans (Calculator, Notepad)
                # carry their button/role in a ``match`` key; the AX tree reflows
                # after every click, so an index captured at plan time can point
                # at the wrong button by the time a later click fires. Resolving
                # from THIS snapshot's elements guarantees the index and the
                # snapshot_id are consistent (both from the same scan) — without
                # this, multi-click plans mis-click and produce wrong results.
                if act.get("element_index") is not None:
                    try:
                        fresh = reuse if reuse is not None else daemon.call(
                            "get_window_state",
                            {"pid": pid, "window_id": window_id,
                             "include_screenshot": False}, timeout=20)
                        if fresh.get("snapshot_id"):
                            args["snapshot_id"] = fresh["snapshot_id"]
                        if act.get("match") and fresh.get("elements"):
                            new_idx = layers.resolve_index(fresh["elements"], act)
                            if new_idx is not None:
                                args["element_index"] = new_idx
                    except Exception:
                        pass
                _call_checked("click", args)
            elif t == "replace_text":
                # Overwrite the editable element's value. cua-driver has no
                # replace_text tool, and modern Notepad/Calculator/Settings
                # are XAML hosts: WM_CHAR is dropped there, so the old
                # focus/select-all/type emulation silently did nothing (and
                # the ignored `snapshot_id_required` refusal made it look
                # successful). ValuePattern.SetValue is the atomic write both
                # native and XAML controls accept; it needs a FRESH
                # element_token because the next snapshot invalidates it.
                text = act.get("value", "")
                fresh = reuse if reuse is not None else _call_checked(
                    "get_window_state",
                    {"pid": pid, "window_id": window_id,
                     "include_screenshot": False}, timeout=20)
                els = fresh.get("elements") or []
                el = _pick_editable(els, act.get("element_index"))
                if act.get("match") and els:
                    new_idx = layers.resolve_index(els, act)
                    if new_idx is not None:
                        el = _pick_editable(els, new_idx) or el
                token = (el or {}).get("element_token")
                if token:
                    # A token carries its window/snapshot, so drop any stale
                    # snapshot_id from the turn's opening scan: the driver
                    # refuses handles that disagree.
                    tok_args = {k: v for k, v in base.items()
                                if k != "snapshot_id"}
                    res = daemon.call("set_value",
                                      {**tok_args, "element_token": token,
                                       "value": text}, timeout=15)
                    # set_value answers `effect: unverifiable` even when the
                    # next scan's value readback proves it landed, so only a
                    # refusal counts as failure here.
                    if not _driver_refusal(res):
                        self._last_dispatch_error = ""
                        self.safety.record_action(t, dict(act), "done")
                        return True
                # Fallback for controls without a token: focus, select-all,
                # delete, then type - all against the SAME fresh snapshot.
                eidx = (el or {}).get("element_index")
                snap = fresh.get("snapshot_id")
                if eidx is not None:
                    _call_checked("click",
                                  {**base, "element_index": eidx,
                                   **({"snapshot_id": snap} if snap else {})})
                    _call_key("press_key",
                              {**base, "key": "a", "modifiers": ["ctrl"],
                               **({"snapshot_id": snap} if snap else {})})
                    _call_key("press_key",
                              {**base, "key": "Delete",
                               **({"snapshot_id": snap} if snap else {})})
                targs = ({k: v for k, v in base.items() if k != "snapshot_id"}
                         if token else dict(base))
                if token:
                    targs["element_token"] = token
                targs["text"] = text
                _call_type(targs)
            elif t == "type":
                # Text entry. XAML hosts accept text only when the call
                # targets the editable element (their CoreInput drops posted
                # WM_CHAR), so resolve a token when the judge named only a
                # role. Native controls keep the plain path.
                args = dict(base)
                eidx = act.get("element_index")
                fresh = None
                if eidx is not None:
                    fresh = reuse if reuse is not None else _call_checked(
                        "get_window_state",
                        {"pid": pid, "window_id": window_id,
                         "include_screenshot": False}, timeout=20)
                    els = fresh.get("elements") or []
                    if act.get("match") and els:
                        new_idx = layers.resolve_index(els, act)
                        if new_idx is not None:
                            eidx = new_idx
                    el = _pick_editable(els, eidx)
                    token = (el or {}).get("element_token")
                    if token:
                        args = {k: v for k, v in args.items()
                                if k != "snapshot_id"}
                        args["element_token"] = token
                    else:
                        args["element_index"] = eidx
                        if fresh.get("snapshot_id"):
                            args["snapshot_id"] = fresh["snapshot_id"]
                else:
                    # No element_index: type into the focused window.
                    # Omit snapshot_id — the driver refuses type_text when a
                    # snapshot is present without element_index.
                    args.pop("snapshot_id", None)
                args["text"] = act.get("value", "")
                args["clear"] = act.get("clear", False)
                try:
                    _call_type(args)
                except DaemonError:
                    # The target dropped plain input (XAML host). Retry once
                    # against the editable element from a fresh snapshot.
                    if eidx is not None:
                        raise
                    fresh = _call_checked(
                        "get_window_state",
                        {"pid": pid, "window_id": window_id,
                         "include_screenshot": False}, timeout=20)
                    el = _pick_editable(fresh.get("elements"))
                    token = (el or {}).get("element_token")
                    if not token:
                        raise
                    retry = {k: v for k, v in base.items()
                             if k != "snapshot_id"}
                    retry["element_token"] = token
                    retry["text"] = args["text"]
                    _call_type(retry)
            elif t == "press_key":
                # Global key press (no element): do NOT pass snapshot_id —
                # the driver refuses "snapshot_id requires element_index" for a
                # non-element key. _call_key escalates to foreground delivery
                # when the surface drops background keys.
                pargs = dict(base)
                # Accept both `key` (driver shape, used by extract.py) and
                # `value` (judge/deterministic shape) — same physical key.
                _key = act.get("key", act.get("value"))
                if _key is None:
                    return False
                pargs["key"] = _key
                if act.get("modifiers"):
                    pargs["modifiers"] = act["modifiers"]
                _call_key("press_key", pargs)
            elif t == "hotkey":
                _call_key("hotkey", {**base, "keys": act.get("keys")})
            elif t == "scroll":
                # Pass element_index through when the judge supplied one —
                # the driver refuses snapshot-less element scrolls, and
                # dropping the index silently turned them into no-ops.
                _sargs: dict = {**base,
                                "direction": act.get("direction", "down"),
                                "amount": act.get("amount", 3)}
                if act.get("element_index") is not None:
                    _sargs["element_index"] = act["element_index"]
                    # Fresh snapshot like click/type/press_key — a stale
                    # turn-start snapshot gets element scrolls refused.
                    try:
                        _fresh = reuse if reuse is not None else daemon.call(
                            "get_window_state",
                            {"pid": pid, "window_id": window_id,
                             "include_screenshot": False}, timeout=20)
                        if _fresh.get("snapshot_id"):
                            _sargs["snapshot_id"] = _fresh["snapshot_id"]
                    except Exception:
                        if snapshot_id:
                            _sargs["snapshot_id"] = snapshot_id
                _call_checked("scroll", _sargs)
            elif t == "wait":
                # Clamp LLM-controlled sleep (same 5s cap as the browser
                # driver) so a runaway "wait 1000" can't stall the cascade.
                try:
                    _secs = float(act.get("seconds", 1))
                except (TypeError, ValueError):
                    _secs = 1.0
                time.sleep(max(0.0, min(_secs, 5.0)))
            elif t == "open_app":
                # Deterministic app launch — no vision, no Start-menu hunting.
                # The agent calls this when the goal needs an app that is not
                # running (or the current target window is not that app): the
                # old behaviour escalated to L3 to find the Start menu, and a
                # flaky vision call then dead-ended the whole run. Opening the
                # app directly (gated L0 open_app) is reliable and instant; the
                # cascade re-scans and finds the new window on the next turn.
                app = str(act.get("app") or act.get("value") or "").strip()
                if not app:
                    self._last_dispatch_error = "open_app requires an app name"
                    return False
                res = self.shell_open_app(app)
                if not (isinstance(res, dict)
                        and res.get("status") in ("done", "dry-run", "pending")):
                    self._last_dispatch_error = (
                        f"open_app {app!r} failed: "
                        f"{str(res)[:200]}")
                    return False
                return True
            else:
                self._last_dispatch_error = f"unsupported action type {t!r}"
                return False
            self._last_dispatch_error = ""
            self.safety.record_action(t or "", dict(act), "done")
            return True
        except Exception as e:
            self._last_dispatch_error = f"{type(e).__name__}: {e}"[:300]
            self.safety.record_action(t or "", dict(act),
                                          f"error: {type(e).__name__}")
            return False

    # ── L0 gated-shell fallback (no daemon) ─────────────
    def _gated_shell_fallback(self, goal: str) -> ComputerResult:
        """When the daemon can't run, expose the safe shell surface only."""
        self._stop_recorder()
        return ComputerResult(
            False, "L0-disabled",
            error="Computer-use daemon unavailable. Gated-shell mode requires "
                  "COMPUTER_USE_ENABLED=true AND a running cua-driver daemon. "
                  "No destructive action was taken.",
        )

    # ── direct gated-shell actions (used by MCP computer_action L0) ──
    # Single source of truth: delegate to GatedShell so fixes can't drift.
    def _gated(self):
        from .shell import GatedShell
        return GatedShell(self.safety)

    def shell_run_command(self, command: str, *, force: bool = False) -> dict:
        return self._gated().run_command(command, force=force)

    def shell_read_file(self, path: str, *, force: bool = False) -> dict:
        return self._gated().read_file(path, force=force)

    def shell_write_file(self, path: str, content: str, *, force: bool = False) -> dict:
        return self._gated().write_file(path, content, force=force)

    def shell_open_app(self, app: str, *, force: bool = False) -> dict:
        return self._gated().open_app(app, force=force)
