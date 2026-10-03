# Archived diagnostic + scratch suites

These files lived scattered at `S9SharedCode/code/` root (`_*.py` one-off
diagnostics, debug harnesses, and `*.txt`/`*.out` run logs from earlier
sessions). They are SUPERSEDED, not deleted, because some still encode
useful manual checks — but nothing in the live codebase imports them
(verified by repo-wide reference scan at move time), and pytest does not
collect them (underscore-prefixed names).

## Layout

- `_offline_*.py` — offline unit-check scripts (memory, scheduler, skills…).
  Their assertions now live in `S9SharedCode/code/tests/` as pytest files;
  keep these only for quick manual poking.
- `_memory_vector_check.py`, `_master_test_suite.py` — pre-migration
  manual harnesses (kept runnable; see below).
- `_trace_503*.py`, `_cu_*.py`, `_live_*.py`, `_bt_battle.py`,
  `_pw_battle.py`, `_pipeline_test.py`, `_stress_*.py`,
  `_test_harness_*`, `_e2e_*`, `_diag_*`, `_find_*`, `_inspect_*`,
  `_probe_*`, `_quick_check.py`, `_repro_*`, `_schema_*`, `_wiring_check.py`,
  `_db_schema.py`, `_deep_engine_test.py`, `_import_check.py`,
  `_monitor_one_task.py`, `_test_parser.py`, `_test_reasoning2.py` —
  session diagnostics, superseded by the pytest suites.
- `*.txt`, `*.out`, `*.log`, `_repro_result.json` — captured run output,
  kept for archaeology.
- `requirements.txt` — legacy dep list; `pyproject.toml` is canonical.

## Archived live modules (moved, not deleted)

- `decision.py`, `perception.py`, `action.py` — the Session 7
  perceive→decide→act layers, superseded by the Session 9 skills +
  Executor architecture. Unimported by live code (verified repo-wide at
  move time) but kept here intact in case the layered design is ever
  revived. They import `gateway`/`schemas`, so to run them put
  `S9SharedCode/code` back on `sys.path` first.

## Running an archived script

Launch with the working directory at the agent code root (several scripts
use CWD-relative `sys.path`):

```
cd S9SharedCode/code
uv run python ../../tests/_offline_b.py
```

`__file__`-relative path hacks were rewritten at move time to resolve
`S9SharedCode/code` from the new location, so this also works from the
repo root.
