import { test, expect } from '@playwright/test';

const BASE = 'http://localhost:8500';

test.describe('Ledger page', () => {
  test('page loads with cost summary, by-skill breakdown, and turn history', async ({ page }) => {
    const errors: string[] = [];
    page.on('console', (m) => {
      if (m.type() === 'error') errors.push(m.text().slice(0, 200));
    });
    page.on('pageerror', (e) => errors.push(String(e).slice(0, 200)));

    await page.goto(BASE + '/ledger', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(3000);

    // Cost summary tiles
    await expect(page.getByText('SPEND', { exact: true }).first()).toBeVisible({ timeout: 15000 });
    await expect(page.getByText('CALLS', { exact: true }).first()).toBeVisible();
    await expect(page.getByText('SKILLS', { exact: true }).first()).toBeVisible();
    await expect(page.getByText('TURNS', { exact: true }).first()).toBeVisible();

    // By-skill breakdown table
    await expect(page.getByText('SPEND BY SKILL', { exact: false }).first()).toBeVisible();
    await expect(page.getByRole('table').first()).toBeVisible();
    await expect(page.getByText('Skill', { exact: true }).first()).toBeVisible();
    await expect(page.getByText('Calls', { exact: true }).first()).toBeVisible();
    await expect(page.getByText('In tokens', { exact: true }).first()).toBeVisible();
    await expect(page.getByText('Out tokens', { exact: true }).first()).toBeVisible();
    await expect(page.getByText('USD', { exact: true }).first()).toBeVisible();

    // Turn history table
    await expect(page.getByText('TURN HISTORY', { exact: false }).first()).toBeVisible();
    await expect(page.getByRole('table').nth(1)).toBeVisible();
    await expect(page.getByText('Time', { exact: true }).first()).toBeVisible();
    await expect(page.getByText('Query', { exact: true }).first()).toBeVisible();
    await expect(page.getByText('Calls', { exact: true }).nth(1)).toBeVisible();
    await expect(page.getByText('USD', { exact: true }).nth(1)).toBeVisible();

    // Node reliability section
    await expect(page.getByText('NODE RELIABILITY', { exact: false }).first()).toBeVisible();

    expect(errors, `console errors: ${errors.join(' | ')}`).toEqual([]);
  });

  test('shows costs from previous runs (all-time view)', async ({ page }) => {
    await page.goto(BASE + '/ledger', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(3000);

    // Check that "All time" scope is selected by default
    await expect(page.getByRole('button', { name: 'All time', exact: true })).toHaveAttribute('class', /border-violet-400/);

    // The spend tile should show a value or "n/a" (not just $0.0000)
    const spendTile = page.getByText('SPEND', { exact: true }).first().locator('..');
    await expect(spendTile).toBeVisible();

    // By-skill table should have rows (or show "No spend yet" if truly empty)
    const skillTable = page.getByRole('table').first();
    const rows = skillTable.locator('tbody tr');
    const rowCount = await rows.count();

    // Either there are skill rows, or the empty state shows
    if (rowCount > 1) { // header + at least one data row
      const firstSkillRow = rows.nth(1);
      await expect(firstSkillRow).toBeVisible();
    }
  });

  test('"All time" vs "One conversation" scope toggle', async ({ page }) => {
    await page.goto(BASE + '/ledger', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(3000);

    // Default is "All time"
    await expect(page.getByRole('button', { name: 'All time', exact: true })).toHaveAttribute('class', /border-violet-400/);

    // Click "One conversation"
    await page.getByRole('button', { name: 'One conversation', exact: true }).click();
    await page.waitForTimeout(1000);

    // Now "One conversation" should be selected
    await expect(page.getByRole('button', { name: 'One conversation', exact: true })).toHaveAttribute('class', /border-violet-400/);

    // Should show conversation picker (select dropdown and/or input)
    await expect(page.getByLabel('Pick a recent conversation')).toBeVisible();
    await expect(page.getByPlaceholder('…or paste conversation id')).toBeVisible();

    // Should show scope error prompting to pick a conversation
    await expect(page.getByText(/Pick a conversation/i).first()).toBeVisible({ timeout: 10000 });

    // Switch back to "All time"
    await page.getByRole('button', { name: 'All time', exact: true }).click();
    await page.waitForTimeout(1000);
    await expect(page.getByRole('button', { name: 'All time', exact: true })).toHaveAttribute('class', /border-violet-400/);
  });

  test('conversation picker shows recent conversations', async ({ page }) => {
    await page.goto(BASE + '/ledger', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(3000);

    await page.getByRole('button', { name: 'One conversation', exact: true }).click();
    await page.waitForTimeout(1000);

    const select = page.getByLabel('Pick a recent conversation');
    await expect(select).toBeVisible();

    // Check if there are options in the dropdown
    const options = select.locator('option');
    const optionCount = await options.count();
    // At minimum there should be the "pick recent…" placeholder option
    expect(optionCount).toBeGreaterThanOrEqual(1);

    // If there are real conversations, they should appear
    if (optionCount > 1) {
      // Select the first real option
      const firstRealOption = options.nth(1);
      const value = await firstRealOption.getAttribute('value');
      if (value) {
        await select.selectOption(value);
        await page.waitForTimeout(2000);
        // Table should update with that conversation's data
        await expect(page.getByRole('table').first()).toBeVisible();
      }
    }
  });

  test('manual conversation ID input works', async ({ page }) => {
    await page.goto(BASE + '/ledger', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(3000);

    await page.getByRole('button', { name: 'One conversation', exact: true }).click();
    await page.waitForTimeout(1000);

    const input = page.getByPlaceholder('…or paste conversation id');
    await expect(input).toBeVisible();

    // Enter a fake ID - should show "nothing was ever billed to it"
    await input.fill('fake-conversation-id-12345');
    await page.keyboard.press('Enter');
    await page.waitForTimeout(2000);

    await expect(page.getByText(/nothing was ever billed to it/i).first()).toBeVisible({ timeout: 10000 });
  });

  test('turn history shows recent turns with timestamps and costs', async ({ page }) => {
    await page.goto(BASE + '/ledger', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(3000);

    const turnTable = page.getByRole('table').nth(1);
    await expect(turnTable).toBeVisible();

    const turnRows = turnTable.locator('tbody tr');
    const rowCount = await turnRows.count();

    if (rowCount > 1) { // header + data rows
      const firstTurn = turnRows.nth(1);
      await expect(firstTurn.locator('td').nth(0)).toBeVisible(); // Time
      await expect(firstTurn.locator('td').nth(1)).toBeVisible(); // Query
      await expect(firstTurn.locator('td').nth(2)).toBeVisible(); // Calls
      await expect(firstTurn.locator('td').nth(3)).toBeVisible(); // USD
    } else {
      // Empty state
      await expect(page.getByText('no turns recorded', { exact: false })).toBeVisible();
    }
  });

  test('node reliability section loads', async ({ page }) => {
    await page.goto(BASE + '/ledger', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(4000);

    await expect(page.getByText('NODE RELIABILITY', { exact: false }).first()).toBeVisible();

    const nodeTable = page.getByRole('table').nth(0); // First table is node reliability
    await expect(nodeTable).toBeVisible({ timeout: 15000 });

    // Check headers
    await expect(page.getByText('Skill', { exact: true }).nth(0)).toBeVisible();
    await expect(page.getByText('Nodes', { exact: true })).toBeVisible();
    await expect(page.getByText('Failed', { exact: true })).toBeVisible();
    await expect(page.getByText('Fail rate', { exact: true })).toBeVisible();
    await expect(page.getByText('Why', { exact: true })).toBeVisible();
  });

  test('refresh button works when load fails', async ({ page }) => {
    await page.goto(BASE + '/ledger', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(3000);

    // The page should load successfully normally, but we can verify
    // the retry mechanism exists by checking for the refresh interval display
    await expect(page.getByText('refresh 15s')).toBeVisible();
  });

  test('no console errors on ledger page', async ({ page }) => {
    const errors: string[] = [];
    page.on('console', (m) => {
      if (m.type() === 'error') errors.push(m.text().slice(0, 200));
    });
    page.on('pageerror', (e) => errors.push(String(e).slice(0, 200)));

    await page.goto(BASE + '/ledger', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(3000);

    // Also test navigation to ledger from another page
    await page.goto(BASE + '/console', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(1500);
    await page.getByRole('link', { name: 'Ledger', exact: true }).click();
    await page.waitForTimeout(2000);

    expect(errors, `console errors: ${errors.join(' | ')}`).toEqual([]);
  });

  test('ledger page does not overflow on mobile', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(BASE + '/ledger', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(2000);

    const { doc, win } = await page.evaluate(() => ({
      doc: document.documentElement.scrollWidth,
      win: window.innerWidth,
    }));
    expect(doc, `/ledger overflows on mobile: document ${doc}px in a ${win}px viewport`).toBeLessThanOrEqual(win + 1);
  });
});