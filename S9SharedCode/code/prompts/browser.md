The Browser skill fetches and interacts with web pages. It walks a
four-layer cascade starting from the cheapest path (HTML extraction)
and escalating only when needed (deterministic selectors, accessibility
tree, then visual set-of-marks with a vision model). The escalation
is internal; you pass `url` and `goal`, the skill chooses the layer.

Inputs: `metadata.url` (required), `metadata.goal` (required, free-text
description of what to extract or do). Output: `BrowserOutput` with
`content` (for extraction goals) or `actions` plus `final_url` (for
interaction goals), and `path` reporting the cascade layer that
actually ran. When the page is gated by CAPTCHA or login, the skill
returns `error_code="gateway_blocked"` and no content; the Planner
should route around by trying a different source URL or by handing
back to the user.

Optional inputs:
- `metadata.selectors`: list of {action: "click"|"fill"|"key", selector,
  value?} for the deterministic layer (e.g. fill a known search box).
  Only the three listed actions are supported; anything else is ignored
  and the cascade falls through to the a11y driver.
- `metadata.force_path`: "extract"|"a11y"|"vision" to pin one layer
  (debugging only). "extract" never escalates; "a11y" never runs vision;
  "vision" skips a11y. Omit for the natural cascade.
- `metadata.max_steps_a11y` / `metadata.max_steps_vision` (1-12),
  `metadata.wall_clock_s` (10-600): per-node budgets. Defaults 12/12/90.
- `metadata.a11y_provider_pin` / `metadata.vision_provider_pin`: gateway
  provider override for that layer (default: a11y pinned to gemini,
  vision unpinned).
