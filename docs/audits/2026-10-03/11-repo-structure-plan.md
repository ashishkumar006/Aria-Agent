# Repository structure hygiene — plan — 11

*Written by the coordinator after the assigned subagent timed out upstream. Every path claim was
verified by filesystem inspection or grep; **nothing in the repository was moved, renamed or deleted.**
Recommendations that require deleting user data are marked as needing an operator decision, not
cleanup.*

---

## 1. Inventory of the clutter

### Repo root

| Entry | Size / count | Note |
|---|---|---|
| `README.md`, `ARCHITECTURE.md`, `ARCHITECTURE_V2.md`, `AGENTS.md`, `BENCHMARKS.md`, `COMPUTER_USE_ARCHITECTURE.md`, `ADAPTOR_KEYS_GUIDE.md`, `AGENT_BREAK_TEST_REPORT.md` | 8 `.md` at the root | competing/overlapping docs |
| `probe_agent.py`, `probe_agent_debug.py`, `test_agent_page.py` | 3 ad-hoc scripts | debugging leftovers |
| **`x`** | **1 byte**, contents: `x` | an accidental shell redirect (`… > x`) |
| `.gitignore` | 54 lines | contains one `U+FFFD` (see §8) |
| `S9SharedCode/`, `llm_gatewayV9/` | the two product services | **do not rename or relocate** — see §5 |
| `tests/` | ~86 scratch/debug files | root-level scratch pad, **not** gitignored |
| `archive/` | quarantined legacy tree incl. a parallel product (`root_scratch/GLC_V3*`) | correctly ignored |
| `agent-desktop/`, `build_llm_deck/` | Electron shell + deck builder | experiments, unclear status |
| `AppData/` | — | at the repo root; verify and delete if it is a stray tool directory |
| `.pytest_cache/` | — | **not** gitignored |

### Generated / vendored trees (measured)

| Path | Files | Size |
|---|---|---|
| `S9SharedCode/code/state/` | 1 534 | **32.5 MB** |
| — of which `state/templates.json` | 1 | **23.4 MB** |
| — `state/sessions/` | 862 | 9.12 MB |
| — `state/threads/` | 656 | 0.24 MB |
| `S9SharedCode/code/tests/` | 260 | **18.8 MB** (mostly `.png` captures) |
| `S9SharedCode/code/logs/` | 5 | **5.97 MB** (`agent.err` alone 894 KB) |
| `S9SharedCode/code/sandbox/.cache/` | 19 | 0.18 MB |
| root `tests/` | ~86 | scratch |

### Competing documentation

* `ARCHITECTURE.md` vs `ARCHITECTURE_V2.md` — report 12 established that only V2 reflects the current
  system; the original predates the React console, the scheduler, apps and flags.
* `S9SharedCode/code/` carries six overlapping planning documents: `TEST_PLAN.md`, `TEST_PLAN_EXECUTION.md`,
  `TEST_PLAN_FULL.md`, `E2E_TEST_PLAN.md`, `AGENT_CONSOLE_TARGET_PLAN.md`, `BUGS.md`.
* `BENCHMARKS.md` cites 178 captured sessions and a reproduce command (`python _full_benchmark.py`) whose
  script does not exist — no number in it is re-derivable (report 12).

### Load-bearing files that are untracked in git

From `git status --porcelain`: **`console-frontend/`** (the entire React console), plus
`llm_gatewayV9/adaptors/`, `documents/`, `memory/`, `policy/`, `integrations/`, `llm_gatewayV9/gateway_auth.py`
(the gateway's entire authentication), and 17 gateway test files. `git status` also shows a single commit
(`43028ec`, 2026-08-15) against a working tree of ~71 modified / 19 deleted / 170 untracked files.

---

## 2. Classification

| Entry | Class | Why |
|---|---|---|
| `S9SharedCode/`, `llm_gatewayV9/` | **product source** | the two services; hard-coded relative to each other |
| `console-frontend/` | **product source** | the primary UI; built to `dist/` and served by the agent |
| `docs/` (new) | **product docs** | established by this audit batch |
| `AGENTS.md`, `README.md` | **product docs (keep at root)** | tooling reads these at the root |
| The other 7 root `.md` files | **product docs, misplaced** | should live under `docs/` |
| `probe_agent*.py`, `test_agent_page.py` | **scratch** | superseded by `tests/` scratch and the audit tooling |
| root `tests/` | **scratch** | 86 ad-hoc debug scripts; overlaps the real test suite |
| `archive/` | **archive** | superseded; correctly ignored |
| `agent-desktop/`, `build_llm_deck/` | **experiment** | no reference from either service |
| `state/`, `logs/`, `sandbox/`, `*.db`, `*.png`, `*.wav`, `.pytest_cache/`, `node_modules/`, `dist/` | **generated** | reproducible or runtime-only |
| `x` | **accident** | 1-byte stray redirect |
| `AppData/` | **verify** | a root-level `AppData/` is almost certainly a tool's stray output |

---

## 3. Proposed target tree

The governing constraint from §5 is that **the two service trees cannot move**, so this plan cleans the
*periphery* only. Trying to restructure the core would break the relative `_CODE_ROOTS` tuple, the venv
locations and the documented commands for no benefit.

```
/
├── README.md                     # keep at root — entry point
├── AGENTS.md                     # keep at root — tooling reads it here
├── .gitignore
├── docs/                         # ALL other human-facing docs, one file per topic
│   ├── ARCHITECTURE.md           # merged from ARCHITECTURE.md + ARCHITECTURE_V2.md
│   ├── ADAPTOR_KEYS_GUIDE.md
│   ├── COMPUTER_USE_ARCHITECTURE.md
│   ├── BENCHMARKS.md             # or delete: no figure in it is reproducible
│   ├── BREAK_TEST_2026-10-03.md  # was AGENT_BREAK_TEST_REPORT.md at the root
│   └── audits/2026-10-03/…       # this batch
├── S9SharedCode/code/            # UNCHANGED — product
├── llm_gatewayV9/                # UNCHANGED — product
├── tools/                        # agent-desktop/, build_llm_deck/  (experiments, clearly labelled)
├── scratch/                      # root tests/ + the three probe scripts, clearly non-product
└── archive/                      # UNCHANGED, already ignored
```

One-line rules:
* `docs/` — human-facing prose only; never code, never generated output; one file per topic, no v2 suffixes.
* `tools/` — runnable helpers that are not part of either service.
* `scratch/` — explicitly disposable; safe to delete wholesale; never imported by product code.
* The two service trees keep their own `docs`-shaped files where they are (prompts, plans) until they
  have an owner; do not shuffle them in this pass.

---

## 4. File-by-file migration table

| Current path | Proposed path | Action | Risk if moved |
|---|---|---|---|
| `README.md` | *(root)* | keep | none |
| `AGENTS.md` | *(root)* | keep | tooling reads it at the root |
| `.gitignore` | *(root)* | keep + amend (§6) | none |
| `ARCHITECTURE.md` | `docs/ARCHITECTURE.md` | move, then **merge V2 into it and delete V2** | low — docs only, but cross-links must be fixed |
| `ARCHITECTURE_V2.md` | — | delete after merge | low |
| `BENCHMARKS.md` | `docs/BENCHMARKS.md` | move **or delete** — no figure is reproducible | low; needs your call |
| `COMPUTER_USE_ARCHITECTURE.md` | `docs/` | move | low |
| `ADAPTOR_KEYS_GUIDE.md` | `docs/` | move | low — check for inbound links |
| `AGENT_BREAK_TEST_REPORT.md` | `docs/BREAK_TEST_2026-10-03.md` | move | low — one cross-link from `docs/audits/2026-10-03/INDEX.md` to update |
| `probe_agent.py`, `probe_agent_debug.py`, `test_agent_page.py` | `scratch/` or delete | move or delete | low |
| `x` | — | **delete** (1 byte, contents `x`) | none |
| `tests/` (root, ~86 files) | `scratch/tests/` | move, or delete and gitignore | low — but see the caveat below |
| `agent-desktop/` | `tools/agent-desktop/` | move | low; verify no absolute paths inside |
| `build_llm_deck/` | `tools/build_llm_deck/` | move | low; `create_llm_deck.mjs` may use relative output paths — check |
| `AppData/` | — | verify, then delete if it is stray tool output | low |
| `S9SharedCode/code/*.md` (6 planning docs) | leave for now | consolidate in a later pass | they sit beside the code they describe; a separate decision |
| `archive/` | *(unchanged)* | keep | already ignored |

**Caveat on moving root `tests/`:** do not move it into `S9SharedCode/code/tests/` — that directory is a
real, pytest-discovered suite with its own `pyproject.toml` config, and merging 86 scratch scripts into it
would change what `uv run python -m pytest tests/ -q` collects. Keep it separate or delete it.

---

## 5. Must-not-move — verified, with citations

This is the section that matters. A refactor here breaks the running system.

| Constraint | Evidence | Consequence |
|---|---|---|
| **The two service trees are referenced by relative name** | `S9SharedCode/code/agent_server.py:2749` — `_CODE_ROOTS: tuple[str, ...] = ("S9SharedCode/code", "llm_gatewayV9")`, resolved against `_CODE_ROOT` and served by `/api/code/*` (`:2855`, `:2887`, `:3180`) | Renaming or moving `S9SharedCode/` or `llm_gatewayV9/` breaks the Code workspace silently — the routes return empty roots, not errors |
| **State lives at `ROOT/state`** | `agent_server.py:134` — `STATE_DIR = Path(os.environ.get("S9_STATE_DIR") or (ROOT / "state"))` | `state/` must stay, or `S9_STATE_DIR` must be set on every launch |
| **This is the only hard-coded project path in the agent codebase** | grep for `S9SharedCode[/\\]code|project3` across `S9SharedCode/code/*.py` returns exactly one hit: line 2749 | Good news: the repo is more relocatable than it looks. One grep, one place to fix |
| **Two independent venvs, each beside its code** | `S9SharedCode/code/pyproject.toml` (`name = "eagv3-s9"`), `llm_gatewayV9/pyproject.toml` (`name = "llm-gateway-v9"`), both with `[tool.pytest.ini_options]` | `uv run` resolves the venv from the working directory. `AGENTS.md` documents this: "Run agent commands from this dir" / "Run gateway commands from `llm_gatewayV9`" |
| **Entrypoints are path-specific** | `AGENTS.md`: `uv run agent_server.py` from `S9SharedCode/code`, `uv run main.py` from `llm_gatewayV9` | Moving either file breaks the documented run commands |
| **The frontend build and e2e suite are relative to `console-frontend/`** | `package.json` scripts `dev`/`build` (`tsc && vite build`)/`preview`/`test` (`playwright test`); `playwright.config.ts:35` `testDir: './e2e'`, `:42` `baseURL: 'http://localhost:8500'` | Move `console-frontend/` and the e2e spec is no longer discovered; the spec must stay in `console-frontend/e2e/` |
| **The built SPA is served from disk** | `agent_server.py` reports `spa_built` in `/api/health`; `console-frontend/dist/` must exist and be current | Any frontend change needs `npm run build` before the change is visible — and see report 08 finding on the shell/token cache |
| **A convention already exists and is documented in-code** | `agent_server.py:133` — `# from it — never hardcode ".../state/..." elsewhere.` | Follow it; don't add a second source of truth |

**Net conclusion:** the *core* is more constrained and *more rigid* than it first appears, but also much
simpler than feared — a single hard-coded path tuple and an env-overridable state dir. That means the
hygiene work should be confined to the root periphery, which is exactly what §3 proposes.

---

## 6. `.gitignore` additions

Current gaps, derived from what `git status` actually reports as untracked:

```gitignore
# ── Audit/agent scratch — disposable debug harnesses, not product ──────────
/tests/                      # root-level scratch (≈86 files); distinct from the real suite
/sccratch/
/x

# ── Test captures ───────────────────────────────────────────────────────────
*.png                       # console e2e screenshots (S9SharedCode/code/tests/*.png)
*.wav
!docs/**/*.png              # …but keep documentation images

# ── Demos / experiments ─────────────────────────────────────────────────────
S9SharedCode/code/voice_demos/

# ── Tool caches ─────────────────────────────────────────────────────────────
.pytest_cache/
.ruff_cache/
.mypy_cache/

# ── Log directory: *.log/*.err/*.out are already ignored, but these hold
#    runtime JSON state and must not be committed either ─────────────────────
S9SharedCode/code/logs/*.json
```

Also **fix an existing defect:** `.gitignore` line 50 contains a literal `U+FFFD`
(`# Operational databases (live ledger <U+FFFD> never source control)`), verified by codepoint — the
file is valid UTF-8, so this is stored corruption, not a display artefact. It should be an em dash.

**One judgement call, not cleanup:** `state/` and `sandbox/` are already ignored (lines 14–15) — good.
`state/templates.json` at 23.4 MB is ignored by that rule, which is why a 23 MB generated file never
reached git. The *on-disk* growth is a product problem (report 07 F4), not a hygiene problem.

---

## 7. Staged execution order

Each stage is independently revertible and labelled with whether it needs the services stopped.

| Stage | Action | Services must be down? | Revert |
|---|---|---|---|
| S1 | Delete `x`; add the §6 `.gitignore` rules; fix the `U+FFFD` on line 50; delete `AppData/` if it is stray | **No** | re-add one line |
| S2 | Create `docs/` and `scratch/`; move the three probe scripts into `scratch/` | **No** | move back |
| S3 | Move `agent-desktop/` and `build_llm_deck/` into `tools/` (after grepping them for absolute paths) | No | move back |
| S4 | Move the seven root `.md` files into `docs/`; fix inbound links | No | move back |
| S5 | Merge `ARCHITECTURE.md` + `ARCHITECTURE_V2.md` → one file; decide `BENCHMARKS.md`'s fate | No | restore from backup |
| S6 | Move root `tests/` → `scratch/tests/`, or delete it and rely on S1 | No | move back |
| S7 | Consolidate the six `S9SharedCode/code/*.md` planning docs | **Yes** — and only after the audit batch is finished | from backup |
| S8 | Commit the ~170 untracked product files (**including `gateway_auth.py`**) | No | — |

**S8 is the one that actually matters for risk.** `llm_gatewayV9/gateway_auth.py` is the gateway's entire
authentication layer and it is untracked: a fresh clone gets an unauthenticated gateway (report 12).
`console-frontend/` — the primary UI — is untracked too. Everything else in this plan is tidiness.

---

## 8. Text hygiene

**Verified independently by codepoint, not by eye.**

| File | Status |
|---|---|
| `S9SharedCode/code/agent_server.py` | **repaired earlier this session** — 4 411 C1 chars → 0; 17 NEL → 0; AST identical, token census identical, compiles |
| `llm_gatewayV9/adaptors/{discord,line,matrix,signal}/README.md` | repaired — raw cp1252 `0x97` bytes and `U+FFFD` → em dash |
| `S9SharedCode/code/console-frontend/e2e/console.spec.ts` | repaired — 1 `U+FFFD` → em dash |
| `.opencode/agent/*.md` (13 personas) | **clean** — the `â€”` visible in terminal output is a PowerShell rendering artefact; codepoint inspection shows proper em dashes |
| `.gitignore` | **1 × `U+FFFD` at line 50, col 37** — real, see §6 |
| `S9SharedCode/code/state/sessions/**/nodes/*.json` (8 files) | still carry mojibake |
| `S9SharedCode/code/sandbox/.cache/fetch/*` (4 blobs) | still carry mojibake |

The last two groups are **historical run artefacts** — recorded outputs from sessions that ran while the
corruption was live. **Recommendation, not action:** leave them alone. They are evidence of what the
agent actually wrote, they are already gitignored, and rewriting recorded history is worse than the
defect. If they are ever loaded for replay, treat their strings as untrusted.

---

## Recommended work, ordered

| # | Action | Value | Effort |
|---|--------|-------|--------|
| 1 | **S8: commit the untracked product files, `gateway_auth.py` first** | Removes "a fresh clone has no gateway auth" | 30 min |
| 2 | S1: delete `x`, fix the `.gitignore` `U+FFFD`, add the missing ignore rules | Stops `tests/` and 19 MB of `.png` captures from ever being committed | 15 min |
| 3 | S4 + S5: consolidate root docs into `docs/`, merge the two ARCHITECTURE files | Removes the v2-suffix pattern and the stale duplicate | 1 h |
| 4 | S2 + S6: quarantine the three probe scripts and root `tests/` | Removes 86 scratch files from the root | 30 min |
| 5 | S3: move the two experiments into `tools/` | Clarifies what is product | 20 min |
| 6 | S7: consolidate the six in-tree planning docs | Last, needs services stopped | 1 h |

**Do not** attempt to rename or relocate `S9SharedCode/code`, `llm_gatewayV9` or `console-frontend` —
§5 lists the exact single grep hit, the env-overridden state dir, and the venv-relative commands that
depend on their current positions. The clutter is at the periphery; that is also where it should be fixed.