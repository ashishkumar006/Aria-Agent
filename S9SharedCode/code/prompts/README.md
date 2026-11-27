# Prompts (`prompts/`)

The per-skill system prompts. Each skill in `agent_config.yaml` names one prompt
file here; there is **no Python class per skill** — the prompt + the YAML entry
define the skill. Prompts are rendered per node with the resolved inputs, memory
hits, the per-node `question`, and the granted tools.

> The code agent's prompt is `studio_coder.md` (rendered by
> `agent_server.render_studio_coder_prompt()` with `{{TOKEN}}` placeholders,
> re-read on mtime change). `coder.md` is the **different** DAG Coder skill —
> the Studio does not use it.

## Files
| Prompt | Skill | Notes |
|---|---|---|
| `planner.md` | planner | The lean planner (capability table + node metadata + graph shape + recovery). |
| `researcher.md` | researcher | Multi-source research; files findings as documents. |
| `retriever.md` | retriever | Indexed knowledge + docs; preference recall/write. |
| `distiller.md` | distiller | Structured extraction (a Critic is auto-inserted). |
| `critic.md` | critic | Pass/fail with a specific rationale. |
| `summariser.md` | summariser | Condense. |
| `formatter.md` | formatter | Final answer; multi-section when several upstream results. |
| `author.md` / `author_section.md` | author | Document spec + per-section expansion. |
| `deck.md` | deck | Slide decks. |
| `coder.md` | coder | Sandbox code (emits `{"code","rationale"}`). |
| `studio_coder.md` / `studio_coder_delegate.md` | studio_coder | Full-workspace coding agent. |
| `sandbox_executor.md` | sandbox_executor | Runs the coder's code. |
| `browser.md` | browser | Web cascade. |
| `computer.md`, `computer_use_a11y.md`, `computer_use_vision.md`, `computer_use_user_a11y.md`, `computer_use_user_vision.md` | computer | Desktop control + judge/vision. |
| `vision_file.md` | vision_file | Local image understanding. |
| `action.md` | action | Real-world side effects; progressive tool disclosure. |
| `part_analyst.md` / `part_engineer.md` / `part_blender.md` | parts | 3D mesh analysis / repair / Blender. |
| `app_worker.md` / `app_builder.md` / `app_builder_skill.md` | apps | A2UI app surfaces. |
| `compaction.md` | — | Context compaction helper. |
| `author/` | author | Authoring sub-prompts/fragments. |

## Rule
`tests/test_prompt_truth.py` (and `test_studio_prompt_truth.py`) fail if a prompt
names a tool the payload does not carry — "documented" and "reachable" must match.