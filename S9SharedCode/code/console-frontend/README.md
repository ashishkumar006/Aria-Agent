# Console Frontend (`console-frontend/`)

The Aria web console — a React 19 + TypeScript + Vite + Tailwind v4 SPA served by
the agent (`:8500`). It drives the agent's JSON/SSE API; it is not a second
backend. An optional Electron shell (`agent-desktop/`) wraps the same app.

## Scripts
```
npm run dev        # vite dev server
npm run build      # tsc && vite build  → dist/
npm run preview
npm test           # playwright test
npm run test:headed
```

## Layout
| Path | Responsibility |
|---|---|
| `index.html` | Vite entry. |
| `src/main.tsx` | React root. |
| `src/App.tsx` | App shell + router (`react-router-dom`). |
| `src/api.ts` | Typed API client (token header, SSE chat). |
| `src/views/` | One page per console section (Chat, Research, Runs, Memory, Documents, Authoring, Studio, Code, Model Lab, Scheduler, Skills, Apps, Ledger, Architecture, Parts, Computer, Settings). |
| `src/components/` | Shared UI (panes, editors, diff review, terminal, graph). |
| `src/lib/` | Hooks/utilities (e.g. `studioSession.ts`). |
| `src/assets/`, `src/index.css` | Fonts, Tailwind entry, theme tokens (violet accent). |
| `e2e/`, `playwright.config.ts` | End-to-end tests. |
| `public/` | Static assets. |

## Key dependencies
React 19, React Router 7, `@xyflow/react` (DAG graphs), `monaco-editor` (editor),
`@xterm/xterm` (terminal), `three` (3D parts), `pdfjs-dist` (PDF preview),
`@tanstack/react-virtual` (virtual lists), `motion`, `sonner`, `lucide-react`,
`cmdk`.

## Verify
```
npx tsc --noEmit
npm run build
```

## Notes
- Keep the violet accent; theme tokens in `index.css` are shared with the desktop
  shell so islands blend in.
- The console sends the per-launch token from `state/agent.token`.