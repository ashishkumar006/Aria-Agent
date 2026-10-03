# Computer-Use Live Test & Fix Plan

**Date:** 2026-08-29  
**Scope:** Full computer-use module audit, offline tests, live tests, and bug fixes  
**Safety:** NO destructive actions, NO writes outside temp dirs, STOP on unexpected behavior

---

## Phase 0 — Code Audit (READ-ONLY)

### 0.1 Audit every computer_use/ file

| # | File | Lines | Key things to verify |
|---|---|---|---|
| 1 | engine.py | ~769 | Cascade dispatch, scan-act-verify, dry-run, target acquisition |
| 2 | daemon.py | ~300 | ensure_daemon, call wrapper, error handling, pipe probe |
| 3 | safety/gates.py | ~150 | ENABLED, MODE, approval, protected paths, audit |
| 4 | safety/permissions.py | ~100 | check_permissions, capabilities probe |
| 5 | layers/deterministic.py | ~220 | Calculator plan, Notepad plan, registry |
| 6 | layers/extract.py | ~150 | AX tree reading, clipboard, field value |
| 7 | layers/perception.py | ~60 | AX filtering, semantic filter |
| 8 | layers/sequencing.py | ~40 | Action sequencing |
| 9 | layers/recovery.py | ~50 | Recovery policy |
| 10 | layers/vision.py | ~170 | Set-of-marks, VisionFallback |
| 11 | layers/goal.py | ~40 | Goal decomposition |
| 12 | apps/electron.py | ~100 | Electron app detection, CDP launch |
| 13 | apps/native.py | ~50 | Native app handling |

### 0.2 Check integration points
- [ ] skills.py computer dispatch branch
- [ ] agent_server.py computer-use endpoints
- [ ] agent_config.yaml computer skill entry

### 0.3 Document all bugs found
| # | File | Line | Bug | Severity |
|---|---|---|---|---|
| 1 | | | | |
| 2 | | | | |

---

## Phase 1 — Offline Tests (Mock Daemon, Dry-Run)

### 1.1 L0 Shell
- [ ] Disk space command
- [ ] Tasklist command
- [ ] File read

### 1.2 L1 Extract
- [ ] Read calculator display from AX tree
- [ ] Read field value from markdown

### 1.3 L2a Deterministic
- [ ] Calculator: 2 + 2
- [ ] Calculator: 234 * 567
- [ ] Calculator: clear then compute

### 1.4 L2b A11y Judge
- [ ] Notepad: type "hello"
- [ ] Generic: click element by index

### 1.5 L3 Vision
- [ ] Click at pixel coords
- [ ] Set-of-marks drawing

### 1.6 Safety
- [ ] Protected path blocked
- [ ] Approval gate triggered

---

## Phase 2 — Live Tests (Real Desktop)

### 2.1 Pre-flight
- [ ] Ensure cua-driver daemon is running
- [ ] Verify permissions (AX tree accessible)
- [ ] Close sensitive windows

### 2.2 L0 Shell (live, read-only)
- [ ] Disk space
- [ ] Tasklist
- [ ] File read

### 2.3 L2a Calculator (live)
- [ ] 2 + 2 = 4
- [ ] 234 * 567 = 132678
- [ ] Clear + 5 * 5 = 25

### 2.4 L2b Notepad (live)
- [ ] Open + type "Hello from Aria"
- [ ] Read back content

### 2.5 L3 Vision (live)
- [ ] Screenshot + locate button + click

### 2.6 Safety (live)
- [ ] Write to C:\Windows\test.txt → blocked
- [ ] Run destructive command → approval gate

---

## Phase 3 — Bug Fixes

For each bug found in Phase 0-2:
1. Fix the code
2. Add a test that catches the regression
3. Re-run all tests to confirm no regression

---

## Phase 4 — Advanced Code (if time permits)

### 4.1 Potential improvements
- [ ] Better error messages for common failures
- [ ] Auto-retry with exponential backoff
- [ ] Screenshot capture on failure for debugging
- [ ] Session recording for replay

---

## Sign-off
- [ ] User approves this plan
- [ ] User confirms no sensitive windows are open
- [ ] User is ready to observe
