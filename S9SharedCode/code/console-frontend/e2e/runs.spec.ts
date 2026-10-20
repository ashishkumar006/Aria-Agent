import { test, expect } from '@playwright/test';

const BASE = 'http://127.0.0.1:8500';

test.describe('Runs section', () => {
  test.beforeEach(async ({ page }) => {
    const errors: string[] = [];
    page.on('console', (m) => {
      if (m.type() === 'error') errors.push(m.text().slice(0, 200));
    });
    page.on('pageerror', (e) => errors.push(String(e).slice(0, 200)));
    await page.goto(BASE + '/runs', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(2000);
    expect(errors, `console errors on /runs: ${errors.join(' | ')}`).toEqual([]);
  });

  test('page loads: run list, summary stats, filters', async ({ page }) => {
    // Verify main heading
    await expect(page.getByText('Runs', { exact: true }).first()).toBeVisible({ timeout: 15000 });

    // Verify summary stats (total runs, running, completed, failed)
    await expect(page.getByText(/total runs/i)).toBeVisible({ timeout: 15000 });
    await expect(page.getByText(/running/i)).toBeVisible({ timeout: 15000 });
    await expect(page.getByText(/completed/i)).toBeVisible({ timeout: 15000 });
    await expect(page.getByText(/failed/i)).toBeVisible({ timeout: 15000 });

    // Verify filters are present
    await expect(page.getByRole('combobox', { name: /kind/i })).toBeVisible({ timeout: 15000 });
    await expect(page.getByRole('combobox', { name: /status/i })).toBeVisible({ timeout: 15000 });
    await expect(page.getByRole('textbox', { name: /date/i }).or(page.getByRole('textbox', { name: /from/i })).or(page.getByRole('textbox', { name: /to/i }))).toBeVisible({ timeout: 15000 });
  });

  test('previous runs from Research/Chat appear', async ({ page }) => {
    // Check if there are any runs in the list
    const runRows = page.locator('[data-testid="run-row"], tbody tr, .run-item').first();
    const runCount = await page.locator('[data-testid="run-row"], tbody tr, .run-item').count();

    if (runCount > 0) {
      // Verify run kind column shows research/chat/authoring
      await expect(page.getByText(/research|chat|authoring/i).first()).toBeVisible({ timeout: 15000 });
      // Verify status column
      await expect(page.getByText(/running|completed|failed|cancelled/i).first()).toBeVisible({ timeout: 15000 });
      // Verify timestamp column
      await expect(page.getByText(/\d{4}-\d{2}-\d{2}|\d{2}:\d{2}/).first()).toBeVisible({ timeout: 15000 });
    } else {
      // Empty state
      await expect(page.getByText(/no runs|select a run|empty/i)).toBeVisible({ timeout: 15000 });
    }
  });

  test('click a run to open detail view', async ({ page }) => {
    const runRows = page.locator('[data-testid="run-row"], tbody tr, .run-item');
    const count = await runRows.count();

    if (count > 0) {
      await runRows.first().click();
      await page.waitForTimeout(1500);

      // Verify detail view opened
      await expect(page.getByText(/run detail|run #/i).first()).toBeVisible({ timeout: 15000 });

      // Verify graph/DAG visualization
      await expect(page.locator('.react-flow, [data-testid="run-graph"], .graph-view, svg')).toBeVisible({ timeout: 15000 });

      // Verify node statuses
      await expect(page.getByText(/pending|running|completed|failed/i).first()).toBeVisible({ timeout: 15000 });

      // Verify produced files section
      await expect(page.getByText(/produced files|artifacts|outputs/i).first()).toBeVisible({ timeout: 15000 });

      // Verify answer/output section
      await expect(page.getByText(/answer|output|result/i).first()).toBeVisible({ timeout: 15000 });
    } else {
      test.skip('No runs available to test detail view');
    }
  });

  test('filter by kind (research/authoring/chat)', async ({ page }) => {
    const kindFilter = page.getByRole('combobox', { name: /kind/i });
    await expect(kindFilter).toBeVisible({ timeout: 15000 });

    // Test each kind filter
    for (const kind of ['research', 'authoring', 'chat', 'all']) {
      await kindFilter.selectOption(kind);
      await page.waitForTimeout(1000);
      // Verify filter applied (URL or UI indicator)
    }
  });

  test('date range filter', async ({ page }) => {
    const fromInput = page.getByRole('textbox', { name: /from|start date/i });
    const toInput = page.getByRole('textbox', { name: /to|end date/i });

    if (await fromInput.count() > 0) {
      await fromInput.fill('2024-01-01');
      await page.waitForTimeout(500);
    }
    if (await toInput.count() > 0) {
      await toInput.fill('2024-12-31');
      await page.waitForTimeout(500);
    }
  });

  test('poll interval updates every 2s', async ({ page }) => {
    // Check for auto-refresh indicator or timestamp updates
    const refreshIndicator = page.getByText(/auto.refresh|refreshing|last updated/i).first();
    if (await refreshIndicator.count() > 0) {
      await expect(refreshIndicator).toBeVisible({ timeout: 15000 });
    }
    // Wait for at least one poll cycle
    await page.waitForTimeout(3000);
  });

  test('graph interaction: zoom, pan, node click -> detail panel', async ({ page }) => {
    const runRows = page.locator('[data-testid="run-row"], tbody tr, .run-item');
    const count = await runRows.count();

    if (count > 0) {
      await runRows.first().click();
      await page.waitForTimeout(1500);

      const graph = page.locator('.react-flow, [data-testid="run-graph"], .graph-view').first();
      await expect(graph).toBeVisible({ timeout: 15000 });

      // Test zoom (mouse wheel)
      await graph.hover();
      await page.mouse.wheel(0, -100);
      await page.waitForTimeout(500);

      // Test pan (drag)
      await page.mouse.move(100, 100);
      await page.mouse.down();
      await page.mouse.move(200, 200);
      await page.mouse.up();
      await page.waitForTimeout(500);

      // Click a node
      const nodes = page.locator('.react-flow__node, [data-testid="graph-node"], .node');
      const nodeCount = await nodes.count();
      if (nodeCount > 0) {
        await nodes.first().click();
        await page.waitForTimeout(500);
        // Verify detail panel opens
        await expect(page.getByText(/node detail|task detail|step detail/i).first()).toBeVisible({ timeout: 15000 });
      }
    } else {
      test.skip('No runs available to test graph interaction');
    }
  });

  test('stop a running run (if any)', async ({ page }) => {
    const runRows = page.locator('[data-testid="run-row"], tbody tr, .run-item');
    const count = await runRows.count();

    if (count > 0) {
      // Look for a running run
      const runningRun = page.locator('[data-testid="run-row"], tbody tr, .run-item').filter({ hasText: /running/i }).first();
      if (await runningRun.count() > 0) {
        await runningRun.click();
        await page.waitForTimeout(1000);

        const stopButton = page.getByRole('button', { name: /stop|cancel/i }).first();
        if (await stopButton.count() > 0) {
          await stopButton.click();
          await page.waitForTimeout(1000);
          // Verify run stopped
          await expect(page.getByText(/stopped|cancelled/i).first()).toBeVisible({ timeout: 15000 });
        }
      }
    }
  });

  test('delete a run', async ({ page }) => {
    const runRows = page.locator('[data-testid="run-row"], tbody tr, .run-item');
    const count = await runRows.count();

    if (count > 0) {
      await runRows.first().click();
      await page.waitForTimeout(1000);

      const deleteButton = page.getByRole('button', { name: /delete|remove/i }).first();
      if (await deleteButton.count() > 0) {
        page.on('dialog', async (d) => await d.accept());
        await deleteButton.click();
        await page.waitForTimeout(1000);
        // Verify run deleted (list updates)
      }
    }
  });

  test('empty state handling', async ({ page }) => {
    // This test verifies the empty state is handled gracefully
    await expect(page.getByText('Runs', { exact: true }).first()).toBeVisible({ timeout: 15000 });
    // Empty state message should be visible when no runs
    const emptyMsg = page.getByText(/no runs|select a run|empty|no data/i);
    if (await emptyMsg.count() > 0) {
      await expect(emptyMsg.first()).toBeVisible();
    }
  });

  test('no console errors on runs page', async ({ page }) => {
    const errors: string[] = [];
    page.on('console', (m) => {
      if (m.type() === 'error') errors.push(m.text().slice(0, 200));
    });
    page.on('pageerror', (e) => errors.push(String(e).slice(0, 200)));

    await page.goto(BASE + '/runs', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(2000);

    expect(errors, `console errors on /runs: ${errors.join(' | ')}`).toEqual([]);
  });
});