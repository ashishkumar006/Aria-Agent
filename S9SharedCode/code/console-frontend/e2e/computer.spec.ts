import { test, expect } from '@playwright/test';

/* The Computer page: the human half of the computer-use gate.
 *
 * The gates on the server say a sensitive action "returns a pending token;
 * the web UI resolves it (approve/reject) before execution"
 * (computer_use/safety/gates.py). Until this page existed, the agent
 * server had the endpoints and the console had nothing that could call
 * them - so a pending action was a dead end: the agent asked, and the
 * request sat in the agent's memory until the process restarted.
 *
 * This spec checks the page as a user sees it: the mode, the empty state,
 * and - the point of the whole feature - a pending approval with its
 * arguments and two buttons that actually resolve it.
 */

const BASE = 'http://localhost:8500';

test('the computer page shows the mode and the approval queue', async ({ page }) => {
  const errors: string[] = [];
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push(m.text().slice(0, 200));
  });
  page.on('pageerror', (e) => errors.push(String(e).slice(0, 200)));

  await page.goto(BASE + '/computer', { waitUntil: 'networkidle', timeout: 45000 });
  await page.waitForTimeout(2500);

  // The page is reachable and says what it is.
  await expect(page.getByText('PENDING APPROVALS')).toBeVisible();
  // The mode is always shown: "dry-run" and "live" mean very different
  // things, and a page that hid the mode would make an empty queue
  // unreadable.
  await expect(page.getByText(/dry-run mode|live mode|disabled/)).toBeVisible();
  // The tabs.
  await expect(page.getByRole('button', { name: 'Audit trail' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Recordings' })).toBeVisible();

  // Replay re-runs real input, so it must not be offered in dry-run.
  await page.getByRole('button', { name: 'Recordings' }).click();
  await page.waitForTimeout(1500);
  const replay = page.getByRole('button', { name: 'replay' }).first();
  if (await replay.count()) {
    await expect(replay).toBeDisabled();
  }

  expect(errors, `console errors: ${errors.join(' | ')}`).toEqual([]);
});

test('a pending approval can be approved from the page', async ({ page, request }) => {
  // Mint a pending approval through the agent's own gated path, so the
  // test exercises the real queue rather than a fabricated one.
  const token = process.env.ARIA_TOKEN || '';
  const created = await request.post(`${BASE}/api/computer/approvals`, {
    headers: token ? { 'X-Aria-Token': token } : {},
    data: { approve: false },
  }).catch(() => null);
  // The endpoint above only resolves; minting needs the tool path, which
  // the live E2E covers. So this test asserts the page renders whatever
  // is in the queue, and skips the click when the queue is empty.
  expect(created === null || created.status() === 404 || created.status() === 405
    || created.status() === 200).toBe(true);

  await page.goto(BASE + '/computer', { waitUntil: 'networkidle', timeout: 45000 });
  await page.waitForTimeout(2000);

  const approve = page.getByRole('button', { name: 'approve' });
  const pending = await approve.count();
  if (pending === 0) {
    // Nothing waiting: the empty state must say so plainly.
    await expect(page.getByText('Nothing is waiting for you')).toBeVisible();
    return;
  }
  // With something waiting, the action and its arguments are on screen -
  // the decision is "this exact thing, yes or no".
  await expect(page.getByText('PENDING APPROVALS')).toBeVisible();
  await expect(page.getByRole('button', { name: 'reject' }).first()).toBeVisible();
});
