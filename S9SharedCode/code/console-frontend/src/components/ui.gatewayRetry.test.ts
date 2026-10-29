import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

/**
 * "Retry" on the gateway-down banner must actually re-check.
 *
 * The banner appears from `/api/health`. That endpoint keeps a short cache so
 * a page loading eight panels does not probe the gateway eight times, and the
 * cache could still say "up" for a few seconds after the gateway died. The
 * Retry button called the ordinary poll, so it was answered from that cache:
 * the banner vanished while the gateway was still dead and every paid action
 * kept failing. `?fresh=1` forces a probe on the server.
 */
const UI = readFileSync(resolve(__dirname, 'ui.tsx'), 'utf-8');
const API = readFileSync(resolve(__dirname, '..', 'api.ts'), 'utf-8');

describe('gateway banner Retry is a real check', () => {
  it('forces a probe on the server', () => {
    expect(API).toMatch(/health: \(fresh = false\)/);
    expect(API).toMatch(/\?fresh=1/);
  });

  it('passes the force flag through the provider', () => {
    expect(UI).toMatch(/const tick = useCallback\(async \(fresh = false\) => \{/);
    expect(UI).toMatch(/await api\.health\(fresh\)/);
  });

  it('awaits the check and keeps the button busy until it lands', () => {
    expect(UI).toMatch(/await h\.refresh\(true\)/);
    // No fixed-duration "busy" timer: the result decides.
    expect(UI).not.toMatch(/setTimeout\(\(\) => setBusy\(false\), 900\)/);
    expect(UI).toMatch(/finally \{ setBusy\(false\); \}/);
  });
});
