# AX-Tree / cua-driver Contract

Verified behavior of the cua-driver AX-tree surface on Windows (cua-driver 0.19.3, modern Notepad/Calculator/Settings tested). This is the reference for all computer-use work.

## 1. Snapshot freshness (CRITICAL)

**Rule:** Every element-targeted action (`click`, `double_click`, `right_click`, `scroll`, `drag`, `set_value`) MUST use a **fresh `snapshot_id`** taken immediately before the call.

**Why:** The driver caches element tokens per-snapshot. A stale `snapshot_id` causes:
- `window_minimized` refusal
- `stale_element_token` refusal
- `element_index_required` refusal

**Fix in code:** `cascade.py` passes a `fresh_snapshot` callable into `dispatch_action`, which re-fetches `get_window_state` right before each element action.

## 2. Write paths (text input)

**Primary:** `type_text` with `element_index` + `snapshot_id` + `window_id` + `pid`.
- Routes through UIA `ValuePattern` on XAML hosts (modern Notepad, Calculator).
- Returns `effect: confirmed` with `value_readback` evidence.
- **This is the ONLY reliable way to write document text.**

**Overwrite:** `replace_text` (our helper) = select-all (`press_key` Ctrl+A foreground) + Delete + `type_text`.
- Verified to clear the document completely on modern Notepad.

**Never use `set_value` for document text:**
- The Notepad Document element does NOT implement `ValuePattern`.
- `set_value` returns: "element does not implement ValuePattern or RangeValuePattern."
- Use `type_text` or `replace_text` instead.

## 3. Keyboard chords on XAML/UWP hosts

**Problem:** `hotkey` fails on modern XAML apps (Notepad, Calculator, Settings) because it looks for a menu `AcceleratorKey` that doesn't exist (ribbon apps have no classic menu accelerators).

**Solution:** `press_key` with `modifiers` array + `delivery_mode: "foreground"`.
- Example: `press_key(key="a", modifiers=["ctrl"], delivery_mode="foreground")` = Ctrl+A
- Verified: Ctrl+A + Delete clears the document on modern Notepad.

**XAML hosts** (notepad, calculator, wordpad, paint, photos, settings)
need foreground key delivery (`delivery_mode: "foreground"`) and fresh
snapshots per action. (Note: no `_is_xaml()` helper exists in code — match
window titles inline where needed.)

## 4. Parameter shapes (gotchas)

| Tool | Correct shape | Common mistake |
|---|---|---|
| `scroll` | `snapshot_id` + `element_index` + `direction` + `amount` | Missing `element_index` → `element_index_required` |
| `zoom` | `x1`, `y1`, `x2`, `y2` | Using `x`, `y`, `width`, `height` → "Missing required region coordinates" |
| `set_window_frame` | `x`, `y`, `width`, `height` | Calling on maximized window → "restore it first" |
| `hotkey` | `keys: ["ctrl", "a"]` | Using `keys: "ctrl+a"` (string) |
| `press_key` | `key: "a"`, optional `modifiers: ["ctrl"]` | Using `value` field (not a real param) |

## 5. App-specific behaviors

### Notepad (modern, XAML/UWP)
- Restores last session/file on launch → **must use `launch_fresh`** with a sandbox file.
- Document element: `[0] Document "Text editor" [actions=[set_value,text,scroll]]`
- Tabs in a single window (not separate windows).
- `invoke_menu` unreliable (ribbon, not classic menu).

### Calculator (XAML/UWP)
- Launches normally (no session restore).
- Digit buttons: indices 43–52 (`num0Button`–`num9Button`).
- Operators: `[37]` Divide, `[38]` Multiply, `[39]` Minus, `[40]` Plus, `[41]` Equals.
- Result: `[8] Text "Display is N" [id=CalculatorResults]`
- Keyboard chords work via `press_key` + modifiers + foreground.

### Settings (XAML/UWP)
- Toggle switches are `Button` elements with `actions=[toggle]`, NOT `role=Toggle`.
- `verify_state` with `selected: true` works on these buttons.
- Navigation via `ListItem` with `actions=[select]`.
- Search box: `[6] Edit "Search box, Find a setting" [id=CommandSearchTextBox]`

## 6. `verify_state` reliability

**Works:**
- `exists: true` predicate → `satisfied` (stable).
- `selected: true` on toggle buttons → `satisfied` (stable).

**Does NOT work:**
- `value_equals` on Document element → returns `unknown` with `untrusted_source`.
- Use AX tree readback (`tree_markdown` value field) for document text verification instead.

**Conclusion:** `verify_state` is reliable for element existence and toggle state, but NOT for document text content. Use AX tree readback for text.

## 7. `invoke_menu` limitations

- Works on classic Win32 menus (File, Edit, View with sub-items).
- **Fails on ribbon apps** (modern Notepad, Settings) — returns `menu_path_unavailable`.
- NOT wired into `engine._dispatch_action` (no `invoke_menu` branch there);
  §13 below describes driver-level verification only. Do not emit it from
  the L2b judge — dispatch rejects it.

## 8. `double_click` / `right_click` / `drag` / `scroll`

- `scroll` is dispatched by the engine (element_index passed through with a
  fresh snapshot). `double_click` / `right_click` / `drag` have NO engine
  branch — dispatch rejects them; the judge schema forbids emitting them.
- **AX tree does NOT expose selection state or viewport position**, so
  scroll outcomes are unverifiable via tree alone.
- Requires screenshot/vision model to confirm real-world effect.
- `scroll` needs `element_index` + `snapshot_id` or it refuses.

## 9. Fresh app launch strategy

**`launch_fresh(app_name)`** in `apps/native.py` (used by tests and
callers that want it — the engine's `_acquire_target` does NOT call it;
it uses `daemon launch_app` → shell `start` → vision-icon-click, and does
not kill existing windows):
- For `notepad`, `wordpad`, `paint`: opens a new empty file in `state/computer_use_sandbox/`.
- For all other apps: launches normally.
- `launch_fresh` itself kills existing windows for the app first (inside
  `apps/native.py`, not in `_acquire_target`).

## 10. Safety mode

**`COMPUTER_USE_MODE=live`** is required for actions to execute.
- `dry-run` (default) only describes actions — never executes.
- Set via env var or `SafetyGates(mode="live")`.

## 11. Convergence guards

- `MAX_ACTION_REPEATS=3` — same action 3× in a row → abort.
- `MAX_L3_CALLS=6` — cap expensive vision calls.
- Live logging via `print(flush=True)`.

## 12. What's NOT covered by AX tree

- **Web page DOM** — needs `browser_*` / CDP tools (`electron.py`).
- **Vision-only goals** — needs L3 vision path (screenshot → V9 vision → pixel click).
- **Recording/replay** — separate daemon-level surface.
- **OS auth dialogs / CAPTCHAs** — cannot be bypassed.
## 13. L3 vision path (TEST 6 — VERIFIED)

**Flow:** `get_window_state` (screenshot) → vision judge → parse `x,y` →
`daemon call "click"` with `{pid, window_id, x, y, snapshot_id}`
(the tool is named `click`, not `click_pixel`).

**Verified:** Screenshot (base64 PNG) → gemini-3.5-flash-lite vision judge → returned `{"x": 226, "y": 172}` → `click_pixel` landed → typing appeared in document.

**Notes:**
- Screenshot is `screenshot_png_b64` from `get_window_state` (include_screenshot=True).
- Vision judge returns JSON in `text` field; parse with regex for `{"x": N, "y": N}`.
- `click_pixel` uses window-local coordinates (same space as the screenshot).
- Coordinate mapping is accurate (no offset correction needed on this display).

## 14. invoke_menu (TEST 9 — VERIFIED at driver level, app-dependent)

**Verified at the cua-driver tool level** as `{"type": "invoke_menu", "path": ["File", "New"]}`
— but NOT wired into `engine._dispatch_action`, so the L2b judge must not emit it.

**Verified:**
- **Paint (classic menu):** `['File']` expanded to show New, Open, Save, etc. — full path resolution works.
- **Notepad (ribbon):** `['File','New']` returns `ok` but doesn't resolve (ribbon apps have no classic menu).

**Rule:** Use `invoke_menu` ONLY on classic Win32/menu apps. For ribbon apps (modern Notepad, Settings), use keyboard shortcuts (`press_key` + modifiers) instead.

## 15. Electron/CDP path (TEST 11 — VERIFIED, read-only)

**Flow:** `electron.launch_with_debug_port(app, port)` → `page` tool with
`{pid, action, selector/text/expression/url}` (see `apps/electron.py`
`page_click/page_type/page_eval/page_navigate` — NOT `cdp_port`/
`get_text`/`query_dom`/`execute_javascript`/`list_windows` shapes).

**Verified on VS Code (running instance):**
- `get_text` works (returns UI text: File, Edit, Explorer, etc.)
- `query_dom` works but **Windows UIA backend rejects `.class` selectors** — use simple tags (`a`, `button`, `input`, `textarea`, `h1-h6`, `img`, `li`, `p`, `span`, `select`), `tag#id`, or `[attr=value]`.
- `execute_javascript` requires **unrestricted mode** (launch-time risk acceptance via `CUA_DRIVER_ENABLE_LEGACY_PAGE_MUTATIONS=1`).

**Notes:**
- `page` tool needs `window_id` for `get_text`/`query_dom` (not just `pid`).
- Mutating actions (click_element, insert_text, type_keystrokes) need unrestricted mode.
- `launch_app` with `electron_debugging_port` fails if the app is already running (no new window) — attach to existing instance instead.

## 16. Test status (historical run — see AX_TREE_TEST_SPEC.md for pre-reqs;
TEST 9 needs driver-level wiring first, TEST 11 was untested at writing)

| Test | Result |
|---|---|
| 1 Calculator | ✅ PASS (2+2=4) |
| 2 Settings | ✅ PASS (toggle + verify_state) |
| 3 double/right click | ⚠️ right_click works; double_click selection not in AX tree |
| 4 drag | ⚠️ fires, needs screenshot to verify |
| 5 scroll | ⚠️ fires, needs screenshot to verify |
| 6 L3 vision | ✅ PASS (screenshot→vision→pixel click) |
| 7 replace_text Edit | ✅ PASS (Settings search box) |
| 8 verify_state | ✅ exists/selected work; value_equals unknown |
| 9 invoke_menu | ✅ PASS on Paint; fails on ribbon apps |
| 10 multi-window | ✅ completed (Notepad uses tabs) |
| 11 Electron/CDP | ✅ PASS (read-only; mutations need unrestricted) |
| 12 contract doc | ✅ written |