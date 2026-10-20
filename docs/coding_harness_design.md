# Coding harness: where we are, and what a 7B model needs

Target: make the Code agent work well with a small model (Qwen-Coder 3.5 7B class),
not just frontier models.

## 1. What the research says

Four independent 2026 results, all pointing the same way.

### "Beyond the Model: Demystifying Harness Effects" (NanoHarness, mini-SWE-agent vs OpenCode, Qwen + DeepSeek)
Harness is a first-class factor in SE-agent performance, not scaffolding around it.

| Component | Effect |
|---|---|
| Structured tool registry | **+4.6 / +3.5 pts** - most consistent single win |
| Task-specific subagents | **+5.9 / +4.5 pts** - largest single-component gain |
| Explicit planning | small positive, model-dependent |
| Lazy skills | positive, model-dependent |
| Context compression | **-4.9 / -3.9 pts - actively harmful** |
| Generic subagents | **negative - coordination overhead, off-task artifacts** |

Gains came from *regularizing exploration and action*, not from more tokens:
tool calls +131% while prompt tokens grew only +16%.

### "Who Should Own the Loop?" (Qwen 3.5 0.8B-27B, Gemma 4 E2B-E4B-E31B)
The most directly relevant paper - it tests exactly our target size.

Four stages: **localize -> reproduce -> patch -> verify**.
A free-form agent lets the model own all four. That is where small models collapse.

Three findings:
1. **A single model owning every stage already beats the free-form agent at the
   small end**: Gemma 4 E2B resolves 27.4% stage-decomposed vs 5.0% free-form.
2. **Stages are independently routable.** With a 9B localizer + 27B reproducer,
   Qwen 3.5 0.8B goes 0.7% -> 14.8%; Gemma E2B goes 5.0% -> 39.8%.
3. **"Structure, not information, is the binding constraint below 4B."**
   Handing the small model the correct file locations and a validated reproduction
   signal inside the *free-form loop* changes nothing (E2B stays under 7%). The
   *same artifacts*, delivered by a deterministic harness, take E2B to 42.4%
   resolved with ~3x fewer tokens. At 9B and above the advantage narrows, then
   reverses - the free-form loop gets its lead back only when the model can
   actually drive it.

Also: the model expresses edits as **search/replace blocks**, which the harness
locates by textual matching and converts to a diff. Not model-authored diffs.

### "SWE-Protégé: Small Models as Coding Agents" (Qwen2.5-Coder-7B)
7B coder: **17.0% -> 42.4%** Pass@1 on SWE-bench Verified (+25.4 pts), beating a 32B
baseline, via **one added tool**: `ask_expert`.

- Small models fail through *pervasive action looping* - 31.0% of trajectories
  contained a repeated-action run longer than 10 steps.
- Post-training drove loops 31.0% -> 0.8% and mean steps 60 -> 20.
- Expert called ~4x per task, ~11% of tokens. Median cost/task $1.24 -> $0.15.
- A **rule-based** loop trigger was much worse than a learned one (and no better
  than baseline without full history). Detection has to be cheap and reliable;
  brute-force re-running does not work.

### "More Is Not Always Better" (cross-component interference, Llama 3.1 8B/70B, Qwen2.5 3B/7B)
Full factorial over 32 subsets. **In every one of 24 configurations, some proper
subset matched or beat the all-in system.** On HotpotQA the single-tool agent beat
all-in by 32%. The gap is largest at 8B (32%) and shrinks with scale.

Practical consequence: do not stack components by default. Each one must earn its
place, and we should be suspicious of anything that *compresses* context.

### Synthesis for a 7B target

The mechanisms that matter, in order of measured effect:
1. **Structured tools** - named, typed, validated, bounded. Never free-form bash.
2. **Harness-owned control flow** - the loop advances stages; the model does not
   choose when to move on.
3. **Deterministic localization** - the harness resolves which files matter.
4. **Execution feedback** - a reproduction signal the harness runs and judges.
5. **Search/replace edits**, located by the harness, never model-authored diffs.
6. **Loop and stall detection** with escalation.
7. **No aggressive context compression.**
8. **Task-specific delegation only**, never a generic "spawn subagent" tool.

## 2. Our implementation today

### What we have

- **Tool registry**: 54 tools on the MCP server; `coder` allowlist.
- **Bounded tool loop**: 7 hops, with repeat detection
  (`MAX_SAME_CALL_REPEATS`).
- **Execution feedback**: `run_checks` runs the project's real lint/typecheck/tests.
- **Structured code intel**: `document_symbols`, `find_references`, `blast_radius`,
  `preview_edit`, `apply_symbol_edit`. Python via `ast`, conservative scan otherwise.
- **Write gate**: `apply_symbol_edit` is preview-first, apply requires `confirm`.
- **Explicit planning**: a planner node exists.
- **Conversation history**: client-side, persisted, multi-turn.

### The actual gap

**The Code panel does not use any of it.**

`agent_server.py:4891` `/api/code/ask` is a **single LLM call**. It gathers the
active buffer, up to 6 attached files, and 400 file *names*, then asks for prose.
It never enters a tool loop. No `run_checks`. No `preview_edit`. No localization.
No verification.

So the panel has: a chat box, a read-only code index, and a diff viewer that never
receives a diff because nothing generates one.

Everything good above is present in the *MCP/skills* layer and unreachable from
the view the user actually drives. The hard parts of the harness are built; they
are simply not connected to the product surface.

Secondary gaps, even once connected:
- No staged protocol. The model picks every action.
- No `ask_expert` escalation.
- 7 hops is too few for a real task; small models need more hops *and* tighter
  loop control, not a bigger cap.
- Edits are not expressed as search/replace blocks for the harness to locate.
- `run_checks` reports, but there is no reproduce-then-iterate-until-green loop.
- Context clipping exists (the 4KB spill). Research says this class of
  compression is the most reliably *harmful* component. Ours is mild, but it is
  the wrong direction.

## 3. Plan

Ordered by measured effect. Each step is independently verifiable.

**1. Connect the loop.** `/api/code/agent` runs the real tool loop against the
editor's file set, streaming tool calls as SSE `step` frames. `/api/code/ask`
becomes a thin read-only wrapper, or is retired. This alone is the largest win,
because it makes every capability we already built reachable.

**2. Harness-owned stages.** Drive localize -> reproduce -> patch -> verify from
the server, not the model. The harness supplies the localized file set and runs the
checks; the model only chooses *within* the current stage. Per "Who Should Own
the Loop?": structure, not information, is the constraint.

**3. Search/replace edit protocol.** The model emits fenced
`<<<<<<< SEARCH / ======= / >>>>>>> REPLACE` blocks. The harness locates each
target by exact match, fails loudly when the anchor is missing or ambiguous, and
converts to a unified diff for the existing preview/confirm/apply path. Removes
the model's need to produce syntactically valid diffs - the most common 7B
failure mode.

**4. Loop and stall detection with escalation.** Replace the bare repeat counter
with normalized-action equivalence classes (per SWE-Protégé: navigation-like
operations collapse into one class). On a stall, stop retrying and expose
`ask_expert` - one bounded call to a stronger model with the task plus the last ~5
messages. The point is not to make the 7B weaker-model-in-a-loop work better; it
is to make the stall *visible and recoverable* rather than burning the budget.

**5. Audit the compression.** Remove or bound any clipping that drops content the
model needs. Prefer clip-then-keep-both-than-summarize. Expected small gain, and
it is the one component the evidence says is reliably negative.

**6. Task-specific delegation only.** If we add subagents, they must be fixed
specialists (locator, tester, reviewer) with read-only toolsets. No generic
"spawn an agent" tool.

**7. Eval.** We need a loop to tune this. Small fixed task set over this repo, each
with a checkable end state, run against the 7B. Without this we are guessing, and
"add scaffolding" has a documented failure mode of making things worse.

## 4. What to expect

Not a 25-point jump from one change. The realistic claim: **stages 1-3 put us in
the regime the paper measured, where stage decomposition beat free-form at every
size, and deterministic delivery of localized files + reproduction beat merely
having that information available.** Step 4 addresses the specific failure mode
that separates small models from frontier ones (looping), and step 7 is what tells
us whether the rest helps or hurts.