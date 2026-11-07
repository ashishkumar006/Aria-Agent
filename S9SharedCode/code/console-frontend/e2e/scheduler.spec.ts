import { test, expect } from '@playwright/test';

const BASE = 'http://127.0.0.1:8500';

/* This spec was written against the vanilla `web/scheduler.html` page: it
   filled `#j-query`, clicked `button.primary`, and looked for
   `button.icon-btn.danger[onclick^="cancelJob"]`. None of that exists in the
   React view, so five of its twelve tests had been failing on every run for a
   long time, and two more ("disable/enable", "delete") were guarded by
   `if (count > 0)` — they passed by selecting nothing and asserting nothing. A
   suite that is red on every run stops being read, which is how a page loses
   all its coverage at once.

   Selectors now come from the accessible names the React view actually
   publishes. Anything this file creates it also removes, in a `finally`-style
   cleanup at the end of each test, because the Scheduler's state is the user's
   real board and a test that leaves jobs behind is a test that fires them. */

const QUERY = 'Job query';
const WHEN = 'Job schedule';
const SCHEDULE_BUTTON = 'Schedule job';

/** The create form's own status line, scoped to the section that holds the
 *  inputs. Unscoped `getByRole('status')` is ambiguous on this page: the
 *  TopBar's health dot is a live region too (`aria-label="agent and gateway
 *  connected"`), so the assertion resolves to two elements and fails even when
 *  the job was created correctly — which is exactly what it did here, with
 *  "scheduled sch-…" sitting right there in the error. */
function statusLine(page: any) {
  return page.locator('section')
    .filter({ has: page.getByLabel(QUERY) })
    .getByRole('status');
}

/** The Next fire column. Addressed by position rather than by a text pattern:
 *  "in 1 minute" in the When chip also matches /in \d+m/, and a row-wide
 *  getByText resolves to two elements. */
function nextFireCell(row: any) {
  return row.locator('td').nth(3);
}

async function createJob(page: any, query: string, when: string) {
  await page.getByLabel(QUERY).fill(query);
  await page.getByLabel(WHEN).fill(when);
  await page.getByRole('button', { name: new RegExp(SCHEDULE_BUTTON, 'i') }).click();
}

/** The row whose first cell is this job's text. The What column truncates with
 *  CSS only, so the DOM text — and the accessible name — is still complete. */
function rowFor(page: any, query: string) {
  return page.getByRole('row').filter({ hasText: query }).first();
}

async function cleanup(page: any, query: string) {
  const row = rowFor(page, query);
  if (await row.count() === 0) return;
  const cancel = row.getByLabel(new RegExp(`^Cancel job`, 'i'));
  if (await cancel.count() > 0) {
    await cancel.first().click();
    await expect(row.getByText('off')).toBeVisible({ timeout: 10000 });
  }
  const del = row.getByLabel(new RegExp(`^Delete job`, 'i'));
  if (await del.count() > 0) {
    page.once('dialog', (d: any) => d.accept());
    await del.first().click();
    await expect(rowFor(page, query)).toHaveCount(0, { timeout: 10000 });
  }
}

test.describe('Scheduler page', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto(BASE + '/scheduler', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(2000);
  });

  test('page loads with schedule list and create form', async ({ page }) => {
    await expect(page.getByText('NEW JOB')).toBeVisible();
    await expect(page.getByText('ALL JOBS')).toBeVisible();
    await expect(page.getByLabel(QUERY)).toBeVisible();
    await expect(page.getByLabel(WHEN)).toBeVisible();
    await expect(page.getByRole('button', { name: new RegExp(SCHEDULE_BUTTON, 'i') }))
      .toBeVisible();

    // The first column is "What" because it holds a reminder's query and a
    // wake's reason; "Kind" says which of the two you are looking at.
    for (const h of [/what/i, /kind/i, /when/i, /next fire/i, /state/i]) {
      await expect(page.getByRole('columnheader', { name: h })).toBeVisible();
    }
  });

  test('create a one-shot schedule (in 1 minute)', async ({ page }) => {
    const query = 'e2e one-shot reminder';
    await createJob(page, query, 'in 1 minute');
    await expect(statusLine(page)).toContainText('scheduled', { timeout: 10000 });
    await expect(rowFor(page, query)).toBeVisible({ timeout: 10000 });
    await expect(nextFireCell(rowFor(page, query)))
      .toContainText(/\d{1,2}\/\d{1,2}\/\d{4}/, { timeout: 10000 });
    await cleanup(page, query);
  });

  test('create a recurring schedule (daily at 14:30)', async ({ page }) => {
    const query = 'e2e daily recurring';
    await createJob(page, query, 'daily@14:30');
    await expect(statusLine(page)).toContainText('scheduled', { timeout: 10000 });
    const row = rowFor(page, query);
    await expect(row).toBeVisible({ timeout: 10000 });
    await expect(row.getByText('daily@14:30')).toBeVisible();
    // A recurring row is a different promise from a one-shot, and the table now
    // says so in its own column rather than leaving it to be inferred.
    await expect(row.getByText('recurring', { exact: true })).toBeVisible();
    await cleanup(page, query);
  });

  test('a wake reads differently from a reminder', async ({ page }) => {
    /* The grouping is the whole point of the page's layout: watches and jobs
       used to interleave in one flat list with the taller wake rows making the
       table look cluttered. Rows are one line each now, and the group headings
       carry the counts. */
    const body = await page.locator('body').innerText();
    const hasGroups = /WATCHES · (ARMED|ENDED)/.test(body)
      || /JOBS · (ARMED|FINISHED)/.test(body);
    expect(hasGroups || /JOBS · ARMED/.test(body)).toBeTruthy();

    const heights = await page.evaluate(() => Array.from(
      document.querySelectorAll('tbody tr td:first-child'))
      .map((c) => Math.round(c.getBoundingClientRect().height)));
    const tall = heights.filter((h) => h > 52);
    expect(tall.length, `rows taller than one line: ${JSON.stringify(heights)}`)
      .toBe(0);
  });

  test('schedules appear in list with correct next fire time', async ({ page }) => {
    await expect(page.getByText('scheduled').first()).toBeVisible({ timeout: 10000 });
    const nextFireCells = page.getByRole('cell')
      .filter({ hasText: /in \d+[mhd]|ran \d+|\d{2}\/\d{2}\/\d{4}/ });
    await expect(nextFireCells.first()).toBeVisible({ timeout: 10000 });
  });

  test('disable then enable a schedule', async ({ page }) => {
    // Was `if (cancelBtn.count() > 0)`: on the React view that count was always
    // zero, so the test asserted nothing and reported green for years.
    const query = 'e2e disable enable';
    await createJob(page, query, 'in 2 hours');
    const row = rowFor(page, query);
    await expect(row).toBeVisible({ timeout: 10000 });

    await row.getByLabel(/^Cancel job/).first().click();
    await expect(row.getByText('off')).toBeVisible({ timeout: 10000 });

    await row.getByLabel(/^Enable job/).first().click();
    await expect(row.getByText('scheduled')).toBeVisible({ timeout: 10000 });
    await cleanup(page, query);
  });

  test('delete a schedule the test created', async ({ page }) => {
    const query = 'e2e delete me';
    await createJob(page, query, 'in 3 hours');
    const row = rowFor(page, query);
    await expect(row).toBeVisible({ timeout: 10000 });
    // Cancel first: the row offers Delete only once the job is off, which is
    // the deliberate shape — a live job should not be one click from vanishing.
    await row.getByLabel(/^Cancel job/).first().click();
    await expect(row.getByText('off')).toBeVisible({ timeout: 10000 });
    const label = await row.locator('[aria-label^="Delete job"]').first()
      .getAttribute('aria-label');
    expect(label).toContain(query.slice(0, 12));
    page.once('dialog', (d) => d.accept());
    await row.locator('[aria-label^="Delete job"]').first().click();
    await expect(rowFor(page, query)).toHaveCount(0, { timeout: 15000 });
  });

  test('invalid cron expression shows error', async ({ page }) => {
    await createJob(page, 'e2e invalid cron', 'invalid cron');
    await expect(statusLine(page)).toContainText('unparseable', { timeout: 10000 });
  });

  test('relative time parsing (in X minutes)', async ({ page }) => {
    const query = 'e2e relative time';
    await createJob(page, query, 'in 5 minutes');
    await expect(statusLine(page)).toContainText('scheduled', { timeout: 10000 });
    await expect(rowFor(page, query).getByText('in 5 minutes')).toBeVisible();
    await cleanup(page, query);
  });

  test('pre-notification lead time', async ({ page }) => {
    const response = await page.request.get(BASE + '/api/schedule');
    expect(response.ok()).toBeTruthy();
    const data = await response.json();
    expect(Array.isArray(data.schedules)).toBeTruthy();
    expect(data.schedules.length).toBeGreaterThan(0);
  });

  test('wait for one-shot to fire and check it creates a run', async ({ page }) => {
    const query = 'e2e fire and run';
    await createJob(page, query, 'in 30 seconds');
    await expect(statusLine(page)).toContainText('scheduled', { timeout: 10000 });
    // The fire is due at +30s, and the run it starts is a full agent DAG that
    // takes minutes. Poll the session list rather than sleeping once and
    // asserting: the old fixed 40s wait was the reason this test never passed.
    const deadline = Date.now() + 300000;
    let seen = false;
    while (Date.now() < deadline && !seen) {
      const r = await page.request.get(BASE + '/api/sessions');
      if (r.ok()) {
        const body = await r.text();
        seen = body.includes('s8-') && body.includes(query);
      }
      if (!seen) await page.waitForTimeout(5000);
    }
    expect(seen, `no run session for "${query}" within 5 minutes`).toBeTruthy();

    /* Asserted at the API, not on /runs. Measured while this test was being
       repaired: the fired job's session (`s8-25682fb8`, query "e2e fire and
       run") was absent from the Runs page's rendered list even though the page
       reported 80 runs — its innerText contained neither the query nor the
       session id. Whether that is virtualisation, a filter, or a sort that
       buries a running job is a Runs-page finding in its own right; it is not
       something this test should paper over by asserting a page that does not
       show it. The Scheduler's own contract — the row fired and a session
       exists — is what is checked here. */
    const rows = await (await page.request.get(BASE + '/api/sessions')).json();
    const mine = (rows.sessions || []).filter((s: any) => s.query === query);
    expect(mine.length, `the fired job created no session named "${query}"`)
      .toBeGreaterThan(0);
    expect(mine[0].session_id).toMatch(/^s8-/);

    await page.goto(BASE + '/scheduler', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(3000);
    await expect(rowFor(page, query).getByText('off')).toBeVisible({ timeout: 15000 });
    await page.goto(BASE + '/scheduler', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(2500);
    // Fired one-shots stay visible as history rather than vanishing, so delete
    // the row the test made instead of leaving it for the next run to find.
    await cleanup(page, query);
  });

  test('console has no errors', async ({ page }) => {
    const errors: string[] = [];
    page.on('console', (msg) => {
      if (msg.type() === 'error') errors.push(msg.text().slice(0, 200));
    });
    page.on('pageerror', (err) => errors.push(String(err).slice(0, 200)));
    await page.goto(BASE + '/scheduler', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(3000);
    expect(errors, `Console errors: ${errors.join(' | ')}`).toEqual([]);
  });
});

test('api health check', async ({ request }) => {
  const response = await request.get(BASE + '/api/health');
  expect(response.ok()).toBeTruthy();
  const data = await response.json();
  expect(data.gateway_up).toBe(true);
});
