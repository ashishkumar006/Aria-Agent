# Computer-Use Architecture — Aria General Agent (Session 9)

> **Status:** Design v1. Maps the cua-driver spec onto the existing S9
> orchestrator + V9 gateway. Replaces the earlier gated shell/file
> computer_use.py with a layered, perception-driven desktop agent.

---

## 1. Why this design
The earlier computer_use.py was a *gated shell*: it ran commands and
read/wrote files behind approval gates. That is safe but shallow — it
cannot actually *drive* a desktop app the way a human does. The 2026
reality (per the spec):
- **OS accessibility APIs** (AX tree) have mature Python bindings on
  macOS / Linux / Windows.
- **Frontier VLMs** are reliable on UI screenshots.
- **cua-driver** unifies all three OS AX APIs behind one JSON tool
  surface (34 tools, same control mechanisms cross-platform), speaking
  JSON over a Unix socket to a long-running **daemon** that holds the
  per-window element_index cache.

So the 2024 perception stack (icon detection + OCR + button classifiers +
VLM) collapses into **two paths**: read the AX tree and ask a cheap text
model what to click, or screenshot and ask a vision model.

**Key consequence:** cua-driver does *perception + action* only. It does
**not** do planning, goal decomposition, perception interpretation, error
recovery, or vision. Those five layers are *our* job, built on top.

---

## 2. What cua-driver gives us (and what it doesn't)
| Capability | Provided by driver | Ours to build |
|---|---|---|
| Launch apps, walk AX trees | yes | - |
| Synthesise click / keystroke / drag / scroll / hotkey | yes | - |
| Screenshot, set-of-marks annotation | yes | - |
| Record / replay trajectories | yes | - |
| **Planning / goal decomposition** | no | Layer A |
| **Perception interpretation** | no | Layer B |
| **Action sequencing** | no | Layer C |
| **Error recovery** | no | Layer D |
| **Vision fallback** | no | Layer E |

We always talk to a running daemon (cua-driver serve). Start it once per
session via ensure_daemon().

---

## 3. The four perception layers (mirrors Browser cascade)
| Layer | Desktop mechanism | LLM cost | When to use |
|---|---|---|---|
| **L1 extract** | Read AX tree text / clipboard / file directly. No click. | 0 | "What does this email say?" |
| **L2a deterministic** | Known hotkey sequences (press_key). No LLM. | 0 | "Compute 42x18 in Calculator" |
| **L2b a11y tree** | get_window_state -> AX Markdown with [element_index N]; cheap text LLM emits JSON action; dispatch by element_index. | low | **Workhorse.** |
| **L3 vision** | Screenshot -> numbered set-of-marks -> V9 /v1/vision -> click (x,y). | ~10x L2b | AX empty, element missing, visual goal. |

**Cost discipline:** L3 is ~10x L2b per turn. Default to L2b; escalate
only on explicit triggers.

---

## 4. The scan-act-verify loop (core invariant)
Every turn, for every window:
`
scan   -> get_window_state(pid, window_id)     # builds element_index cache
act    -> click / type_text / press_key        # addressed by element_index
verify -> get_window_state(pid, window_id)     # confirms state changed
`
**Two invariants:**
1. Call get_window_state once per turn per window BEFORE any
   element-indexed action (builds the cache).
2. Every new get_window_state replaces the previous index map. An
   element_index is a **turn-scoped token** — re-scan after every
   state-changing action.

**Verify is the most important step.** A click returning success does not
mean the *intent* succeeded. Re-read the AX tree and check one
post-condition.

---

## 5. The trap table (same symptom, different cause)
Universal guard:
`python
state = call("get_window_state", {...})
if state["element_count"] == 0:
    raise PreconditionError(
        "cua-driver returned an empty AX tree. Check: "
        "(1) permissions granted, (2) app activated, "
        "(3) QT_ACCESSIBILITY=1 if Linux/Qt, "
        "(4) Electron debugging port if Electron."
    )
`
| Symptom | Likely cause | Guard |
|---|---|---|
| element_count: 0 on first scan | Permissions (TCC/portal/UAC) | Raise PermissionsError, link grant |
| element_count: 0 after launch (macOS) | App backgrounded | osascript activate, sleep, re-scan |
| element_count: 0 on Qt (Linux) | QT_ACCESSIBILITY=1 unset | Launch with env var |
| Cache miss on prior click | UI reflowed | Re-scan before action |
| element_count: 0 on Electron | Opaque AXWebArea | Relaunch w/ electron_debugging_port |
| element_count: 0 on game/Figma | Paints own pixels | L3 vision |

---

## 6. What we can drive
| Target | Driveable? | Through |
|---|---|---|
| Native apps (Calculator, Notes, Mail, Settings, Office) | yes | AX tree |
| Electron apps (VS Code, Slack, Discord, Notepad, etc.) | yes w/ flag | CDP via electron_debugging_port |
| Chrome/Safari/Firefox | yes w/ flag | CDP / Remote Automation |
| Games / Canvas (Figma, Maps) | Vision only | Screenshot + coord |
| DRM / banking / login / Touch ID | no | Deliberately disabled |
| Elevated apps (installers, settings) | only if agent elevated | Match privilege |

**Rule:** standard UI toolkits expose full AX tree; pixel-painters don't.

---

## 7. The five layers we build (cost knobs)
| Layer | Job | Cost knob |
|---|---|---|
| A. Goal decomposition | NL -> ordered subgoals | Frontier vs cheap planner |
| B. Perception interpretation | Filter AX markdown -> actionable | Pre-filter, summarise, regex. **Biggest.** |
| C. Action sequencing | scan-act-verify, re-scan invariant | Re-scan vs cache aggressiveness |
| D. Error recovery | reflow/modal/crash state carry | State carried across failure |
| E. Vision fallback | screenshot -> SoM -> V9 -> verdict | Escalation threshold |

L2b LLM emits ct (element_index) or escalate (reason). Dispatch routes.

---

## 8. Recording & replay
start_recording / eplay_trajectory. Every run records (tool, args)
per turn. Failure -> evidence; success -> regression test.

---

## 9. Wiring into S9 (one line)
- gent_config.yaml: computer entry (prompt + description, no pin).
- skills.py: if skill.name == "computer": -> ComputerUseSkill.run().
- V9 gateway handles LLM + vision. Cost ledger tags gent: computer.

---

## 10. Safety model (real machine)
- Fresh OS user account for runs; grant cua-driver perms under it.
- Backup any data the agent might touch.
- Verify on every action, especially destructive.
- kill_app + Ctrl-Z are recovery primitives; cua-driver shutdown stops
  the agent within a second.
- Keep gated approval: destructive actions surface a pending UI approval
  via /api/computer/* before the daemon executes.

---

## 11. Platform reality (this machine)
- cua-driver==0.19.3 installable via uv (verified). Binary not on PATH.
- Spec is macOS-heavy; this host is **Windows** -> branch on os.name
  (Windows uses ring_to_front).
- Build full layered skill against the JSON surface; guard OS-specific
  calls; keep gated-shell fallback if daemon can't start.

---

## 12. Novel ideas
1. **Cross-app workflow DAG** — chain computer actions with integration
   tools (read Calculator -> send_email).
2. **Semantic element cache** — stable role+name signature across
   snapshots to survive reflow (attacks Invariant 2).
3. **Cost-budget auto-escalation** — start L2b; permit L3 only if budget
   allows + L2b failed N times.
4. **Replay-as-test** — turn successful trajectories into pytest guards.
5. **Permission pre-flight** — capabilities() probe the Planner reads.
6. **Voice-driven desktop control** — Kokoro + Web Speech (deferred).
