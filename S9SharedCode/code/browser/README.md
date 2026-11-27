# Browser Skill (`browser/`)

A framework-free web skill that fetches and interacts with pages through a
four-layer cascade, escalating only when a layer's output is empty or
insufficient. Talks to the gateway over plain `httpx` (no provider SDKs).

## Files
| File | Responsibility |
|---|---|
| `skill.py` | The Browser skill — translates the orchestrator's `NodeSpec` into `BrowserOutput`/`AgentResult`, owns the cascade. |
| `client.py` | `V9Client` — `chat()` → `/v1/chat` (L2b), `vision()` → `/v1/vision` (L3), `cost_by_agent()` → ledger. |
| `driver.py` | Layer 2b (`A11yDriver`) and Layer 3 (`SetOfMarksDriver`); share the element enumerator, action vocabulary, dispatcher, and loop — differ only in `_decide()`. |
| `dom.py` | `enumerate_interactives` + the action vocabulary/schema. |
| `highlight.py` | Set-of-marks annotation (Pillow) for the vision layer. |

## Cascade
1. **Layer 1 — HTML extract** via `trafilatura` (no LLM).
2. **Layer 2a — deterministic selectors** (only if `metadata.selectors` given).
3. **Layer 2b — `A11yDriver`** (text-only, `/v1/chat`, cheap).
4. **Layer 3 — `SetOfMarksDriver`** (vision, `/v1/vision`).

**Escalation:** a layer escalates when its output is empty/insufficient; the skill
stops at the first useful answer. **Gateway-blocked:** a known CAPTCHA/login-wall
marker returns `error_code="gateway_blocked"` immediately (the recovery path keys
on the literal token and re-invokes the Planner).

## Switches
`browser.tools` and `browser.headed` live in `flags.py` (not `agent_config.yaml`)
so the operator kill-switch works even with the config service unreachable.

## Stack
Playwright (browser) + Pillow (annotation) + httpx (gateway). Nothing else on the
shipped path.