import { defineConfig } from '@playwright/test';
import { readFileSync, existsSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));

/* The agent requires a per-launch token on every `/api/*` call (see
   `code/auth.py`). Tests are a separate client from the browser, so they need
   the same token the console gets from the `<meta name="aria-token">` tag in
   the shell - and the agent writes it to `state/agent.token` for exactly this
   kind of local tooling.

   Read once at config load. If the agent is not running, or was started with
   ARIA_AUTH=off, there is no file and no header: tests then run against an
   unauthenticated agent, which is the pre-auth behaviour and still valid. */
function launchToken(): string {
  const override = process.env.ARIA_TOKEN;
  if (override) return override;
  const stateDir = process.env.S9_STATE_DIR
    ? resolve(process.env.S9_STATE_DIR)
    : resolve(here, '..', 'state');
  const file = resolve(stateDir, 'agent.token');
  try {
    if (!existsSync(file)) return '';
    return readFileSync(file, 'utf-8').trim();
  } catch {
    return '';
  }
}

const token = launchToken();

export default defineConfig({
  testDir: './e2e',
  timeout: 120_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  reporter: [['list']],
  use: {
    baseURL: 'http://localhost:8500',
    extraHTTPHeaders: token ? { 'X-Aria-Token': token } : {},
    trace: 'off',
  },
});
