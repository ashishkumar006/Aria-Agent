# Aria Desktop (Electron shell)

Desktop wrapper for the Aria agent console, phase 1:
**wrap, don't rewrite.** The FastAPI agent (`:8500/console`) and gateway
(`:8109`) remain the backends; Electron adds native window chrome, app
menu, and a global shortcut. The static console keeps working in any
browser in parallel.

## Run

```powershell
# 1. backends (as always)
cd ..\llm_gatewayV9; uv run main.py
cd ..\S9SharedCode\code; uv run agent_server.py

# 2. shell (first time downloads Electron, ~100MB, once)
cd ..\agent-desktop
npm ci
npm start
```

If the agent isn't up, the window shows a waiting page with the exact
commands + Retry instead of a blank screen.

## Roadmap (mirrors AGENT_CONSOLE_TARGET_PLAN.md)

- **Phase 1 (this):** shell over `/console`. Tray icon, auto-start of
  backends from the shell, deep links (`aria://chat/...`).
- **Phase 2:** Vite + React + Tailwind island per screen (Runs DAG via
  React Flow first — the canvas that most needs it), shell keeps serving
  the rest. Shared theme tokens already match, so islands blend in.
- **Phase 3:** full React shell; static pages become the offline fallback.
