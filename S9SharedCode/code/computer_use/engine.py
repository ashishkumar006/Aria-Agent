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
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import daemon
from . import layers
from . import prompts as P
from . import safety
from . import apps
from .safety import SafetyGates
from .safety.permissions import check_permissions, PermissionReport
from .daemon import PreconditionError, DaemonError

ROOT = Path(__file__).resolve().parent.parent

# Cheap model for the L2b judgment LLM (per spec: Gemini 3.1 Flash-Lite-class
# through V9). Override via env.
_JUDGE_MODEL = os.environ.get("COMPUTER_USE_JUDGE_MODEL", "gemini-3.1-flash-lite")
_VISION_MODEL = os.environ.get("COMPUTER_USE_VISION_MODEL", "gemini-3.1-flash-lite")


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

    # ── public entry ─────────────────────────────────────
    def run(self, goal: str, *, app_hint: str | None = None,
            max_turns: int = 12) -> ComputerResult:
        """Execute `goal` against the desktop. Returns ComputerResult."""
        trace: list[str] = []
        # 0) Safety gate: enabled?
        if not self.safety.enabled:
            return ComputerResult(False, "disabled",
                                  error="Computer-use is disabled. "
                                        "Set COMPUTER_USE_ENABLED=true to opt in.")
        # 0.5) Permission pre-flight (charter §8.1): fail fast with an
        # actionable error if AX / screenshot / daemon are not usable.
        perm = check_permissions()
        if not perm.ok():
            err = perm.error_message()
            if self._recorder:
                self._recorder.stop()
            return ComputerResult(False, "permission", trace=trace, error=err)
        trace.append("permissions ok")
        # 1) Ensure daemon; if unavailable, fall back to gated-shell L0.
        if not daemon.ensure_daemon():
            trace.append("daemon unavailable → gated-shell L0 fallback")
            return self._gated_shell_fallback(goal)
        self._daemon_available = True
        trace.append("daemon up")

        # 2) Pick a target app (launch if needed).
        pid, window_id = self._acquire_target(app_hint)
        if pid is None:
            return ComputerResult(False, "no-target",
                                  trace=trace, error="No suitable target app found.")
        trace.append(f"target pid={pid} window={window_id}")

        # 3) Run the cascade loop.
        recovery = layers.RecoveryPolicy()
        vision_fb = layers.VisionFallback()
        l2b_failures = 0
        last_state = None
        last_screenshot = None
        last_shot_mime = "image/png"
        last_act = None
        last_act_ok = False
        for turn in range(max_turns):
            # Re-ensure the daemon each turn: on this Windows build the
            # cua-driver daemon self-terminates ~30s after a UIA timeout, so a
            # long cascade can lose its pipe mid-run. Restart it if needed.
            if not daemon.ensure_daemon():
                return ComputerResult(False, "daemon-error", trace=trace,
                                      error="cua-driver daemon unavailable")
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
                    return ComputerResult(False, "daemon-error", trace=trace, error=str(e))
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
                return ComputerResult(False, "daemon-error", trace=trace,
                                      error="window state unavailable")

            # STALE WINDOW GUARD: cua-driver returns only {"text": ...} (no
            # tree_markdown / elements / snapshot_id) when the cached
            # window_id is stale (window closed or id changed). Re-acquire
            # the target before treating the empty tree as "still settling".
            if not (state.get("tree_markdown") or state.get("elements")
                    or state.get("snapshot_id")):
                trace.append("stale window_id detected → re-acquiring target")
                new_pid, new_wid = self._acquire_target(app_hint)
                if new_pid is not None:
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
                        return ComputerResult(False, "no-target",
                                              trace=trace,
                                              error="target window unavailable")
                else:
                    return ComputerResult(False, "no-target", trace=trace,
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
                    return ComputerResult(
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
                        return ComputerResult(True, "L2a", output={"display": disp},
                                             trace=trace)
                    return ComputerResult(True, "L2a", output={"plan": det.description},
                                         trace=trace)
                trace.append("L2a deterministic action failed; falling back to L2b")

            # L2b a11y (workhorse). Escalate to L3 if AX empty or judge says so.
            if count == 0:
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
                    if self._has_screenshot(state):
                        trace.append("L2b empty → L3 vision")
                        res = self._layer_vision(goal, pid, window_id, state)
                        if res.success:
                            return ComputerResult(True, "L3", output=res.output, trace=trace)
                        return ComputerResult(False, "L3", trace=trace,
                                              error=res.error or "vision fallback failed")
                    trace.append("L2b empty but no screenshot → stay on a11y")
                continue

            # L2b judgment call (cheap text model).
            # Pre-filter the AX tree by goal keywords to bound the LLM context
            # (charter §10: biggest cost knob is perception interpretation).
            filtered = layers.filter_ax_markdown(tree_md, query=goal, max_chars=6000)
            judge = self._judge_a11y(goal, filtered, pid, app_hint or "app")
            if judge is None:
                l2b_failures += 1
                trace.append("L2b judge returned no action")
                if vision_fb.should_escalate(l2b_failures, None):
                    if self._has_screenshot(state):
                        trace.append("L2b retries exhausted → L3 vision")
                        res = self._layer_vision(goal, pid, window_id, state)
                        return ComputerResult(res.success, "L3", output=res.output,
                                              trace=trace, error=res.error)
                    trace.append("L2b retries exhausted; no screenshot for L3 → abort")
                    return ComputerResult(False, "L2b", trace=trace,
                                          error="a11y judge unavailable and no screenshot for vision fallback")
                continue

            verdict = judge.get("verdict")
            if verdict == "done":
                trace.append("L2b: goal met")
                out = dict(judge)
                if self._dry_run_plan:
                    out["dry_run_plan"] = self._dry_run_plan
                return ComputerResult(True, "L2b", output=out, trace=trace)
            if verdict == "escalate":
                l2b_failures += 1
                trace.append(f"L2b escalate: {judge.get('action', {}).get('note')}")
                if vision_fb.should_escalate(l2b_failures, "element_missing"):
                    res = self._layer_vision(goal, pid, window_id, state)
                    return ComputerResult(res.success, "L3", output=res.output,
                                          trace=trace, error=res.error)
                continue

            # ACT (Invariant 2: re-scan happens next loop iteration)
            act = judge.get("action", {})
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
            if same_click_as_last and last_act_ok:
                trace.append(f"repeat of last click ({act.get('element_index')}) "
                             f"→ treating as done (avoid toggle loop)")
                out = dict(judge)
                out["verdict"] = "done"
                out["success"] = True
                return ComputerResult(True, "L2b", output=out, trace=trace)
            ok = self._dispatch_action(pid, window_id, act, snapshot_id)
            last_act, last_act_ok = act, ok
            trace.append(f"act {act.get('type')} -> {'ok' if ok else 'fail'}")
            if not ok:
                directive = recovery.handle("action failed")
                if directive == "abort":
                    return ComputerResult(False, "aborted", trace=trace,
                                          error="action failed repeatedly")
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
                return ComputerResult(True, "L2b-dry-run", output=out, trace=trace)

        out = {}
        if self._dry_run_plan:
            out["dry_run_plan"] = self._dry_run_plan
        return ComputerResult(False, "max-turns", output=out, trace=trace,
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
        prompt = (
            f"Find the icon for the application '{app_hint}' on this desktop "
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
            return True
        except Exception:
            return False

    def _acquire_target(self, app_hint: str | None):
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

        # Electron escape hatch: if the hinted app is a known Electron app,
        # relaunch with a debugging port and drive via CDP.
        if app_hint and apps.electron.is_electron(app_hint):
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
        if target is None and app_hint:
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
            target = wins[0]
        pid = target.get("pid")
        window_id = target.get("window_id")
        # macOS background-launch trap (charter §8.2): a backgrounded app's
        # window is not yet built in the AX hierarchy, so the first scan
        # returns the system menu bar and zero app buttons. AppleScript
        # activation realises the window; bring_to_front is Windows-only.
        if sys.platform == "darwin" and app_hint:
            try:
                import subprocess
                subprocess.run(["osascript", "-e",
                                f'tell application "{app_hint}" to activate'],
                               check=True, capture_output=True, timeout=10)
                time.sleep(0.5)
            except Exception:
                pass
        # Windows: bring to front (the spec's Windows-only call).
        if pid and window_id is not None:
            try:
                daemon.call("bring_to_front", {"pid": pid, "window_id": window_id}, timeout=10)
            except Exception:
                pass
        return pid, window_id

    # ── L2b judgment (cheap text LLM) ────────────────────
    def _judge_a11y(self, goal: str, tree_md: str, pid: int, app_name: str) -> dict | None:
        if self._llm_chat is None:
            return None
        user = P.USER_A11Y_TEMPLATE.format(goal=goal, app_name=app_name, pid=pid,
                                           tree_markdown=tree_md)
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

    def _layer_vision(self, goal: str, pid: int, window_id, state: dict) -> ComputerResult:
        raw = (state.get("screenshot") or state.get("image")
               or state.get("screenshot_png_b64"))
        if not raw or self._llm_vision is None:
            return ComputerResult(False, "L3", error="no screenshot or vision LLM")
        # Normalise to a base64 string (strip data: prefix if present).
        if raw.startswith("data:"):
            b64 = raw.split(",", 1)[1] if "," in raw else raw
        else:
            b64 = raw
        # Draw set-of-marks: numbered dashed boxes over UI elements.
        elements = state.get("elements") or []
        annotated_b64, legend = layers.draw_set_of_marks(b64, elements)
        screenshot = f"data:image/png;base64,{annotated_b64}"
        prompt = P.USER_VISION_TEMPLATE.format(goal=goal, legend=legend)
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
            # Click by pixel coordinates (desktop scope).
            try:
                args = {"pid": pid, "x": act["x"], "y": act["y"]}
                if window_id is not None:
                    args["window_id"] = window_id
                if state.get("snapshot_id"):
                    args["snapshot_id"] = state["snapshot_id"]
                daemon.call("click", args, timeout=15)
                return ComputerResult(True, "L3", output=verdict)
            except Exception as e:
                return ComputerResult(False, "L3", error=str(e))
        return ComputerResult(False, "L3", error="vision returned no clickable target")

    # ── action dispatch (cua-driver primitives) ─────────
    def _dispatch_action(self, pid: int, window_id, act: dict,
                          snapshot_id: str | None = None) -> bool:
        # DRY-RUN: describe the intended action, never touch the desktop.
        if self.safety.mode == "dry-run":
            trace = f"[dry-run] would {act.get('type')} " \
                    f"{act.get('element_index', act.get('value', act.get('keys', '')))}"
            self._dry_run_plan.append(trace)
            return True
        t = act.get("type")
        # Cua Driver 0.17 requires snapshot_id + window_id for element actions.
        base = {"pid": pid, "window_id": window_id}
        if snapshot_id:
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
                if act.get("element_index") is not None:
                    try:
                        fresh = daemon.call("get_window_state",
                                            {"pid": pid, "window_id": window_id,
                                             "include_screenshot": True}, timeout=20)
                        if fresh.get("snapshot_id"):
                            args["snapshot_id"] = fresh["snapshot_id"]
                    except Exception:
                        pass
                daemon.call("click", args, timeout=15)
            elif t == "replace_text":
                # cua-driver rejects "replace_text" as an unclassified action.
                # Translate it to the supported sequence: focus (click) the
                # element, select-all, then type the new value. This keeps
                # the L2b judge's "overwrite" intent working even if it emits
                # replace_text.
                eidx = act.get("element_index")
                if eidx is not None:
                    try:
                        fresh = daemon.call("get_window_state",
                                            {"pid": pid, "window_id": window_id,
                                             "include_screenshot": True}, timeout=20)
                        snap = fresh.get("snapshot_id")
                        daemon.call("click", {"pid": pid, "window_id": window_id,
                                             "element_index": eidx,
                                             **({"snapshot_id": snap} if snap else {})},
                                    timeout=15)
                        daemon.call("press_key", {"pid": pid, "window_id": window_id,
                                                 "key": "a", "modifiers": ["ctrl"],
                                                 **({"snapshot_id": snap} if snap else {})},
                                    timeout=15)
                    except Exception:
                        pass
                daemon.call("type_text",
                            {"pid": pid, "window_id": window_id,
                             "element_index": eidx,
                             "text": act.get("value", ""),
                             "clear": False}, timeout=15)
            elif t == "type":
                args = dict(base)
                if act.get("element_index") is not None:
                    args["element_index"] = act["element_index"]
                    # Element-targeted type needs a fresh snapshot too.
                    try:
                        fresh = daemon.call("get_window_state",
                                            {"pid": pid, "window_id": window_id,
                                             "include_screenshot": True}, timeout=20)
                        if fresh.get("snapshot_id"):
                            args["snapshot_id"] = fresh["snapshot_id"]
                    except Exception:
                        pass
                else:
                    # No element_index: type into the foreground window.
                    # Omit snapshot_id — the driver refuses type_text when a
                    # snapshot is present without element_index, and typing
                    # into the focused window needs no snapshot at all.
                    args.pop("snapshot_id", None)
                args["text"] = act.get("value", "")
                args["clear"] = act.get("clear", False)
                daemon.call("type_text", args, timeout=15)
            elif t == "press_key":
                # Re-fetch a fresh snapshot for key presses too — a stale
                # snapshot_id from the turn's initial scan can cause the
                # driver to refuse the keypress (same expiry as clicks).
                pargs = dict(base)
                try:
                    fresh = daemon.call("get_window_state",
                                        {"pid": pid, "window_id": window_id,
                                         "include_screenshot": True}, timeout=20)
                    if fresh.get("snapshot_id"):
                        pargs["snapshot_id"] = fresh["snapshot_id"]
                except Exception:
                    pass
                pargs["key"] = act.get("value")
                if act.get("modifiers"):
                    pargs["modifiers"] = act["modifiers"]
                daemon.call("press_key", pargs, timeout=15)
            elif t == "hotkey":
                daemon.call("hotkey", {**base, "keys": act.get("keys")}, timeout=15)
            elif t == "scroll":
                daemon.call("scroll",
                            {**base,
                             "direction": act.get("direction", "down"),
                             "amount": act.get("amount", 3)}, timeout=15)
            elif t == "wait":
                time.sleep(act.get("seconds", 1))
            else:
                return False
            return True
        except Exception:
            return False

    # ── L0 gated-shell fallback (no daemon) ─────────────
    def _gated_shell_fallback(self, goal: str) -> ComputerResult:
        """When the daemon can't run, expose the safe shell surface only."""
        return ComputerResult(
            False, "L0-disabled",
            error="Computer-use daemon unavailable. Gated-shell mode requires "
                  "COMPUTER_USE_ENABLED=true AND a running cua-driver daemon. "
                  "No destructive action was taken.",
        )

    # ── direct gated-shell actions (used by MCP computer_action L0) ──
    def shell_run_command(self, command: str, *, force: bool = False) -> dict:
        if self.safety.cmd_blocked(command):
            return {"status": "blocked", "message": self.safety.cmd_blocked(command)}
        if not force and self.safety.needs_approval("run_command", {"command": command}):
            aid = self.safety.create_approval("run_command", {"command": command})
            return {"status": "pending", "approval_id": aid,
                    "message": "Action requires your approval."}
        if self.safety.mode == "dry-run":
            return {"status": "dry-run", "command": command,
                    "message": f"[dry-run] would run: {command}"}
        proc = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=60)
        return {"status": "done", "returncode": proc.returncode,
                "stdout": proc.stdout[:8000], "stderr": proc.stderr[:4000]}

    def shell_read_file(self, path: str, *, force: bool = False) -> dict:
        if self.safety.path_blocked(path):
            return {"status": "blocked", "message": f"path '{path}' is protected"}
        p = Path(os.path.expanduser(path))
        if not p.exists():
            return {"status": "error", "message": "file not found"}
        return {"status": "done", "path": str(p),
                "content": p.read_text(encoding="utf-8", errors="replace")[:8000]}

    def shell_write_file(self, path: str, content: str, *, force: bool = False) -> dict:
        if self.safety.path_blocked(path):
            return {"status": "blocked", "message": f"path '{path}' is protected"}
        p = Path(os.path.expanduser(path))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return {"status": "done", "path": str(p), "bytes": len(content)}

    def shell_open_app(self, app: str, *, force: bool = False) -> dict:
        if os.name == "nt":
            subprocess.Popen(["cmd", "/c", "start", "", app], shell=False)
        else:
            subprocess.Popen(["open", app] if os.name == "posix" else ["xdg-open", app])
        return {"status": "done", "opened": app}
