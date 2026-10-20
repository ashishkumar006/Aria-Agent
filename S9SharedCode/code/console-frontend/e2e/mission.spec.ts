import { test, expect } from '@playwright/test';

test.describe('Mission Section (Event Feed)', () => {
  let errors: string[];
  let networkErrors: string[];

  test.beforeEach(async ({ page }) => {
    errors = [];
    networkErrors = [];
    
    page.on('console', (m) => {
      if (m.type() === 'error') errors.push(m.text().slice(0, 300));
    });
    page.on('pageerror', (e) => errors.push(String(e).slice(0, 300)));
    page.on('response', (resp) => {
      if (!resp.ok() && resp.url().includes('/api/')) {
        networkErrors.push(`${resp.status()} ${resp.url()}`);
      }
    });

    await page.goto('/mission', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(3000);
  });

  test.afterEach(() => {
    const criticalErrors = errors.filter(e => 
      !e.includes('favicon') && 
      !e.includes('DevTools') &&
      !e.includes('websocket') &&
      !e.includes('WebSocket')
    );
    expect(criticalErrors, `critical console errors: ${criticalErrors.join(' | ')}`).toEqual([]);
    expect(networkErrors, `network errors: ${networkErrors.join(' | ')}`).toEqual([]);
  });

  test('page loads: event feed visible with live indicator', async ({ page }) => {
    // Check for key markers on the Mission page
    await expect(page.getByText('EVENT FEED', { exact: false }).first()).toBeVisible({ timeout: 15000 });
    await expect(page.getByText('Sources', { exact: false }).first()).toBeVisible({ timeout: 15000 });
    
    // Live indicator should be present
    await expect(page.getByText('● live', { exact: false }).first()).toBeVisible({ timeout: 15000 });
    
    // Pause button should be present
    await expect(page.getByRole('button', { name: 'pause' })).toBeVisible({ timeout: 15000 });
    
    // Source filter buttons
    await expect(page.getByRole('button', { name: 'all' })).toBeVisible({ timeout: 15000 });
    await expect(page.getByRole('button', { name: 'runs' })).toBeVisible({ timeout: 15000 });
    await expect(page.getByRole('button', { name: 'scheduler' })).toBeVisible({ timeout: 15000 });
    await expect(page.getByRole('button', { name: 'tools' })).toBeVisible({ timeout: 15000 });
    await expect(page.getByRole('button', { name: 'server' })).toBeVisible({ timeout: 15000 });
    await expect(page.getByRole('button', { name: 'errors' })).toBeVisible({ timeout: 15000 });
    
    // Filter input
    await expect(page.getByPlaceholder('Filter…')).toBeVisible({ timeout: 15000 });
  });

  test('source filter buttons work', async ({ page }) => {
    // Click on different source filters
    const filters = ['runs', 'scheduler', 'tools', 'server', 'errors', 'all'];
    
    for (const filter of filters) {
      const btn = page.getByRole('button', { name: filter });
      if (await btn.count() > 0) {
        await btn.click();
        await page.waitForTimeout(500);
        // Should not error
      }
    }
  });

  test('filter input works', async ({ page }) => {
    const filterInput = page.getByPlaceholder('Filter…');
    await filterInput.fill('scheduler');
    await page.waitForTimeout(1000);
    
    // Should filter the feed
    await expect(page.getByText('scheduler', { exact: false }).first()).toBeVisible({ timeout: 10000 });
    
    // Clear filter
    await filterInput.clear();
    await page.waitForTimeout(500);
  });

  test('pause button toggles feed', async ({ page }) => {
    const pauseBtn = page.getByRole('button', { name: 'pause' });
    await expect(pauseBtn).toBeVisible({ timeout: 15000 });
    
    // Click pause
    await pauseBtn.click();
    await page.waitForTimeout(1000);
    
    // Button should change to indicate paused state (might show "resume" or similar)
    // The button text might change or get an active state
    await expect(pauseBtn).toBeVisible({ timeout: 5000 });
    
    // Click again to resume
    await pauseBtn.click();
    await page.waitForTimeout(1000);
  });

  test('event feed shows entries with timestamps', async ({ page }) => {
    // Check that event feed has entries
    const feedEntries = page.locator('text=/\\d{2}:\\d{2}:\\d{2}/').first();
    await expect(feedEntries).toBeVisible({ timeout: 15000 });
    
    // Check for different event types
    const eventTypes = ['scheduler', 'server', 'runs', 'tools'];
    for (const type of eventTypes) {
      const typeText = page.getByText(type, { exact: false }).first();
      // At least some should be visible
      if (await typeText.count() > 0) {
        await expect(typeText).toBeVisible({ timeout: 5000 });
        break;
      }
    }
  });

  test('counters show feed statistics', async ({ page }) => {
    // Check for counters
    await expect(page.getByText(/RUN LINES/)).toBeVisible({ timeout: 15000 });
    await expect(page.getByText(/SCHED LINES/)).toBeVisible({ timeout: 15000 });
    await expect(page.getByText(/SERVER LINES/)).toBeVisible({ timeout: 15000 });
    await expect(page.getByText(/ERRORS/)).toBeVisible({ timeout: 15000 });
    
    // Counters should have numbers
    const runLines = page.getByText(/RUN LINES\s*\d+/);
    await expect(runLines).toBeVisible({ timeout: 15000 });
  });

  test('poll indicator shows refresh status', async ({ page }) => {
    // Check for poll indicator
    await expect(page.getByText(/poll \d+s/)).toBeVisible({ timeout: 15000 });
    await expect(page.getByText('guarded')).toBeVisible({ timeout: 15000 });
  });

  test('no console errors during normal operation', async ({ page }) => {
    // Wait a bit for any async operations
    await page.waitForTimeout(3000);
    
    // Filter out known non-critical errors
    const criticalErrors = errors.filter(e => 
      !e.includes('favicon') && 
      !e.includes('DevTools') &&
      !e.includes('websocket') &&
      !e.includes('WebSocket')
    );
    
    expect(criticalErrors, `critical console errors: ${criticalErrors.join(' | ')}`).toEqual([]);
  });
});

test('api health fast', async ({ request }) => {
  const t0 = Date.now();
  const r = await request.get('/api/health');
  expect(r.ok()).toBeTruthy();
  expect(Date.now() - t0).toBeLessThan(3000);
});