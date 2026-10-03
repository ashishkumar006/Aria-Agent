# AX-Tree / cua-driver Test Spec (for Vision-LLM execution)

**Purpose:** Verify every computer-use capability autonomously. The executor
has VISION (can read screenshots) but the agent's own judge may be mocked or
real. Each test must be run with the daemon up and `COMPUTER_USE_MODE=live`.

**How to run any test:**
```powershell
cd c:\Users\AISHWARYA\Downloads\project3\S9SharedCode\code
$env:PYTHONIOENCODING='utf-8'
$env:COMPUTER_USE_ENABLED='true'
$env:COMPUTER_USE_MODE='live'
uv run --project . python <test_script>.py
```
The daemon (`cua-driver`) auto-starts via `ensure_daemon()`. If a test needs
the real LLM judge, also start the gateway first:
```powershell
# in llm_gatewayV9 with .env present
uv run --project ..\llm_gatewayV9 python main.py   # or use run.sh
```

**PASS/FAIL rule:** A test PASSES only if the *observed* desktop state matches
the expected outcome (read via AX tree AND/OR screenshot). "Tool returned ok"
is NOT sufficient — verify the actual UI change.

---

## TEST 1 — Calculator (XAML host beyond Notepad)
**Goal:** Confirm the XAML-host handling works on a second app.
**Script outline:**
- `native.launch_fresh("calculator")` → get pid, wid (the fresh-`.txt`
  helper only takes effect for notepad/wordpad/paint; other apps launch
  normally)
- Read AX tree; find digit buttons (0-9) and operators (+, =) by `element_index`
- Click `2`, `+`, `2`, `=` via `dispatch_action` (fresh snapshot each)
- Read the result `Text` element
**Expected:** result shows `4`.
**Surfaces:** XAML chord behavior, element_index stability, result readback.
**PASS:** AX tree result element == "4". **FAIL:** wrong value or no result.

## TEST 2 — Settings app (toggles + verify_state)
**Goal:** Drive a navigation-heavy XAML app and confirm state via `verify_state`.
**Script outline:**
- `native.launch_fresh("settings")` (or `launch("Settings")`)
- Navigate to a toggle (e.g. Bluetooth / Night light)
- Click the toggle (`element_index`, fresh snapshot)
- Call `verify_state` asserting the toggle is now ON (value/selected)
**Expected:** toggle flips; `verify_state` returns `satisfied`.
**Surfaces:** toggle `actions=[toggle]`, `verify_state` reliability (we saw
`unknown`/`untrusted_source` earlier — note if it happens here).
**PASS:** toggle ON + verify_state satisfied. **FAIL:** toggle stuck or verify unknown.

## TEST 3 — double_click / right_click outcomes
**Goal:** Confirm these produce real UI changes, not just "fire".
**Script outline (Notepad fresh file):**
- `double_click` on Document element → expect a word selected (read selection state)
- `right_click` on Document → expect a context menu window to appear
**Expected:** double-click selects a word; right-click opens context menu.
**Surfaces:** whether fresh-snapshot rule holds for these; context-menu detection.
**PASS:** word selected / context menu visible in AX tree. **FAIL:** no change.

## TEST 4 — drag (pixel mapping + focus)
**Goal:** Confirm drag coordinates map to window-local pixels correctly.
**Script outline (Notepad with 2 lines of text, or a slider in Settings):**
- `drag` from (x1,y1) to (x2,y2) on a draggable element
**Expected:** element moves / selection extends as intended.
**Surfaces:** from/to coordinate mapping; whether element must be focused first.
**PASS:** expected drag result observed. **FAIL:** no movement or wrong target.

## TEST 5 — scroll on long content
**Goal:** Confirm scroll behaves on content longer than the viewport.
**Script outline (Notepad with ~50 lines):**
- `scroll` down amount=5 on Document element (fresh snapshot + element_index)
**Expected:** viewport scrolls; line indicator changes.
**Surfaces:** `amount` units; whether element must be focused.
**PASS:** scroll position advanced. **FAIL:** no scroll or error.

## TEST 6 — L3 vision path (screenshot → vision judge → pixel click)
**Goal:** Verify the vision fallback end-to-end (needs gateway + vision LLM).
**Script outline:**
- Fresh Notepad; take `get_window_state` screenshot
- Send screenshot + goal ("click the Document area") to V9 vision judge
- Receive (x,y); call `click_pixel(pid, wid, x, y)`
- Verify click landed (cursor/selection in document)
**Expected:** pixel click hits the intended region.
**Surfaces:** screenshot→coordinate mapping accuracy; vision judge JSON validity.
**PASS:** click lands in document. **FAIL:** miss / malformed coords.

## TEST 7 — replace_text on TextBox/Edit (non-Document)
**Goal:** Confirm overwrite works outside Notepad's Document element.
**Script outline (Calculator's expression box, or a Settings search field):**
- `replace_text` with a value
- Read the field back
**Expected:** field shows ONLY the new value (old cleared).
**Surfaces:** whether select-all+delete works in TextBox vs Document.
**PASS:** field == new value, old gone. **FAIL:** appended or partial.

## TEST 8 — verify_state as auto done-checking
**Goal:** Decide if `verify_state` is trustworthy enough to auto-confirm goals.
**Script outline:**
- After TEST 2 toggle, call `verify_state` with the predicate
- Also test a negative case (assert wrong value → expect unsatisfied)
**Expected:** correct satisfied/unsatisfied; minimal `unknown`.
**Surfaces:** predicate reliability; whether we can rely on it vs judge readback.
**PASS:** matches reality. **FAIL:** returns unknown when state is known.

## TEST 9 — invoke_menu as agent action (WIRE FIRST)
**Goal:** Add `invoke_menu` to `dispatch_action`, then test on a classic-menu app.
**Pre-req:** Wire `invoke_menu` into `engine._dispatch_action` in
`computer_use/engine.py` (pid, window_id, path[]) — and add it to the
judge schema in `computer_use/prompts/__init__.py`, or the judge can
never emit it.
**Script outline (e.g. legacy app or Notepad File menu if present):**
- `invoke_menu` path ["File", "New"] or ["Edit", "Undo"]
**Expected:** menu item invoked.
**Surfaces:** which apps resolve menu paths (classic Win32) vs fail (ribbon).
**PASS:** action performed. **FAIL:** menu_path_unavailable (note app type).

## TEST 10 — multi-window apps (bring_to_front, list_windows)
**Goal:** Confirm correct window targeting when a pid owns several windows.
**Script outline (open 2 Notepad fresh files):**
- `list_windows` → find both; `bring_to_front` one; act on it
**Expected:** action hits the intended window, not the other.
**Surfaces:** window selection edge cases in multi-window apps.
**PASS:** correct window acted on. **FAIL:** wrong window / focus loss.

## TEST 11 — Electron/CDP path (browser apps)
**Goal:** Confirm Chrome-internal DOM is reachable via `electron.py`.
**Pre-req:** `electron.py` coded but untested.
**Script outline (launch Chrome with debug port):**
- `launch_with_debug_port`, `page_navigate`, `page_click`, `page_eval`
**Expected:** can click a DOM element / read page state.
**Surfaces:** whether web content is drivable via CDP alongside AX path.
**PASS:** DOM interaction works. **FAIL:** not reachable / crashes.

## TEST 12 — AX_TREE_CONTRACT.md
**Goal:** Document all verified behavior as the reference for future work.
**Content:** snapshot-freshness rule, write-path (type/replace_text), XAML chord
handling, param shapes (scroll/zoom), ribbon-menu caveat, per-app notes.
**Owner:** agent (no vision needed).

---

## Summary of what each test surfaces
| Test | Surfaces |
|---|---|
| 1 Calculator | XAML host #2, result readback |
| 2 Settings | toggles, verify_state reliability |
| 3 dbl/right click | real outcomes, context menu |
| 4 drag | pixel mapping, focus |
| 5 scroll | amount units, focus |
| 6 vision | screenshot→coord accuracy |
| 7 replace_text | TextBox vs Document |
| 8 verify_state | done-signal trust |
| 9 invoke_menu | classic vs ribbon menus |
| 10 multi-window | window targeting |
| 11 Electron | web DOM via CDP |
| 12 contract | documentation |
