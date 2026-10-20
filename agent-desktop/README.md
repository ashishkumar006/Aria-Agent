# Aria Desktop (Electron shell)

A native window over the Aria console. The FastAPI agent (`:8500/console`)
and gateway (`:8109`) remain the backends; Electron adds window chrome, a
tray menu, and deep links. The same console still works in a browser.

## Build the portable app

From the repo root:

```powershell
powershell -ExecutionPolicy Bypass -File .\build_desktop.ps1
```

This copies the Electron runtime already installed in
`agent-desktop\node_modules`, renames it to `Aria.exe`, writes the app files
to `agent-desktop\dist\Aria\resources\app`, and creates Desktop and Start
Menu shortcuts. It is portable: no installer and no admin rights; delete
`dist\Aria` to remove it.

Launch `agent-desktop\dist\Aria\Aria.exe` or the **Aria** shortcut.

The packaged `main.cjs` finds the repo through `aria-root.txt` beside the
executable, so the app and backends can be updated independently.

## Features

- Native titlebar and window controls; `Aria | Code` when the Code view is open.
- Tray menu: Open Aria, Open Code, Quit.
- Deep links: `aria://chat`, `aria://code`, `aria://authoring`,
  `aria://documents`, `aria://parts`, and the other console routes.
- Health-gated startup: if the backends are down, the window shows the exact
  start commands and a Retry button instead of a blank page.
- Single instance: a second launch focuses the existing window.

## Develop

```powershell
# backends (from the repo root, in two terminals)
cd llm_gatewayV9; uv run main.py
cd S9SharedCode\code; uv run agent_server.py
# or: powershell -ExecutionPolicy Bypass -File .\start_aria.ps1

# shell
cd agent-desktop
npm ci
npm start
```

For UI debugging, `npm start` or `Aria.exe --remote-debugging-port=9222`
exposes CDP on `127.0.0.1:9222`. Do not leave the debug port open in normal
use.

## Limits

- Portable build only: no installer, auto-update, code signing, or
  per-user keychain yet.
- The app is a shell, not a second backend. Stopping the agent or gateway
  stops the UI from working.
- `icon.png` / `icon.ico` are generated from the console favicon; replace
  them and rebuild for a different icon.

## Roadmap

- **Phase 1 (done):** shell over `/console`, tray, deep links, health-gated
  startup, packaged portable build and shortcuts.
- **Phase 2:** Vite + React + Tailwind island per screen (Runs DAG via
  React Flow first), shell keeps serving the rest. Shared theme tokens already
  match, so islands blend in.
- **Phase 3:** full React shell; static pages become the offline fallback.
