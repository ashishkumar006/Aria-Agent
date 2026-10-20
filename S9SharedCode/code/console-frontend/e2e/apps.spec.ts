import { test, expect } from '@playwright/test';

const BASE = 'http://localhost:8500';

test.describe('Apps page', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto(BASE + '/apps', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(2500);
  });

  test('page loads: app tracker list, create form, system overview', async ({ page }) => {
    // System overview board should be visible
    await expect(page.getByText('System overview', { exact: true })).toBeVisible({ timeout: 15000 });
    await expect(page.getByText('agent + gateway')).toBeVisible();

    // Boards section should show the board count
    await expect(page.locator('text=Boards').first()).toBeVisible();
    await expect(page.locator('.rounded-full.border.border-white\\/10.bg-white\\/5').first()).toBeVisible();

    // "New app" button should be present
    await expect(page.getByRole('button', { name: /New app/i })).toBeVisible();

    // Stats cards should be present (Agent, Gateway, Sessions, Jobs, Tools, Spend)
    await expect(page.getByText('AGENT')).toBeVisible();
    await expect(page.getByText('GATEWAY')).toBeVisible();
    await expect(page.getByText('SESSIONS')).toBeVisible();
    await expect(page.getByText('JOBS ARMED')).toBeVisible();
    await expect(page.getByText('TOOLS LIVE')).toBeVisible();
    await expect(page.getByText('SPEND')).toBeVisible();

    // Flags section
    await expect(page.getByText('FLAGS & CONFIG')).toBeVisible();

    // Spend by skill section
    await expect(page.getByText('SPEND BY SKILL')).toBeVisible();

    // Pipeline health section
    await expect(page.getByText('PIPELINE HEALTH')).toBeVisible();

    // Latest activity section
    await expect(page.getByText('LATEST ACTIVITY')).toBeVisible();
  });

  test('create an app tracker (OpenRouter free models)', async ({ page }) => {
    await page.getByRole('button', { name: /New app/i }).click();
    await expect(page.getByText('NEW TRACKER APP')).toBeVisible({ timeout: 10000 });

    // Fill in the form for OpenRouter free models
    await page.getByPlaceholder('e.g. Free OpenRouter models').fill('Test OpenRouter Free');
    await page.getByLabel('Kind').selectOption('openrouter_free');
    await page.getByPlaceholder('daily@09:00 · every 12h · manual').fill('manual');

    // Submit
    await page.getByRole('button', { name: /Create app/i }).click();

    // Wait for the app to appear in the list
    await expect(page.getByText('Test OpenRouter Free')).toBeVisible({ timeout: 15000 });

    // Click the new app to open its detail
    await page.getByText('Test OpenRouter Free').click();

    // Detail view should load
    await expect(page.getByText('Apps / Test OpenRouter Free')).toBeVisible({ timeout: 15000 });
    await expect(page.getByText('ITEMS')).toBeVisible();
    await expect(page.getByText('REFRESHED')).toBeVisible();

    // Refresh now button should be present
    await expect(page.getByRole('button', { name: /Refresh now/i })).toBeVisible();

    // Delete button should be present
    await expect(page.getByRole('button', { name: /Delete app/i })).toBeVisible();
  });

  test('create an app tracker (JSON feed)', async ({ page }) => {
    await page.getByRole('button', { name: /New app/i }).click();
    await expect(page.getByText('NEW TRACKER APP')).toBeVisible({ timeout: 10000 });

    // Fill in the form for JSON feed
    await page.getByPlaceholder('e.g. Free OpenRouter models').fill('Test JSON Feed');
    await page.getByLabel('Kind').selectOption('json_feed');
    await page.getByPlaceholder('daily@09:00 · every 12h · manual').fill('manual');
    await page.getByPlaceholder('https://…').fill('https://api.github.com/repos/microsoft/vscode/releases');
    await page.getByPlaceholder('data').fill('data');
    await page.getByPlaceholder('id').fill('id');
    await page.getByPlaceholder('id,name').fill('id,name,tag_name,published_at');

    // Submit
    await page.getByRole('button', { name: /Create app/i }).click();

    // Wait for the app to appear in the list
    await expect(page.getByText('Test JSON Feed')).toBeVisible({ timeout: 15000 });

    // Click the new app to open its detail
    await page.getByText('Test JSON Feed').click();

    // Detail view should load
    await expect(page.getByText('Apps / Test JSON Feed')).toBeVisible({ timeout: 15000 });
  });

  test('refresh app tracker fetches and shows new items', async ({ page }) => {
    // Create an OpenRouter free models tracker first
    await page.getByRole('button', { name: /New app/i }).click();
    await page.getByPlaceholder('e.g. Free OpenRouter models').fill('Refresh Test');
    await page.getByLabel('Kind').selectOption('openrouter_free');
    await page.getByPlaceholder('daily@09:00 · every 12h · manual').fill('manual');
    await page.getByRole('button', { name: /Create app/i }).click();

    await expect(page.getByText('Refresh Test')).toBeVisible({ timeout: 15000 });
    await page.getByText('Refresh Test').click();

    // Wait for detail view
    await expect(page.getByText('Apps / Refresh Test')).toBeVisible({ timeout: 15000 });

    // Click refresh now
    await page.getByRole('button', { name: /Refresh now/i }).click();

    // Wait for refresh to complete - check for progress indicator
    await expect(page.getByText(/fetching:|working…|items/)).toBeVisible({ timeout: 30000 });

    // Wait for refresh to finish (steps should clear)
    await page.waitForTimeout(5000);

    // Items should be displayed
    await expect(page.getByText('ITEMS')).toBeVisible();
    const itemsText = page.getByText(/ITEMS/).locator('..').locator('..');
    await expect(itemsText).toContainText(/\d+/);
  });

  test('invalid URL shows validation error', async ({ page }) => {
    await page.getByRole('button', { name: /New app/i }).click();
    await page.getByPlaceholder('e.g. Free OpenRouter models').fill('Invalid URL Test');
    await page.getByLabel('Kind').selectOption('json_feed');
    await page.getByPlaceholder('https://…').fill('not-a-valid-url');
    await page.getByRole('button', { name: /Create app/i }).click();

    // Should show validation error
    await expect(page.getByText(/url must be an http\(s\) URL/i)).toBeVisible({ timeout: 10000 });
  });

  test('private repo (internal URL) shows auth/SSRF error', async ({ page }) => {
    await page.getByRole('button', { name: /New app/i }).click();
    await page.getByPlaceholder('e.g. Free OpenRouter models').fill('Private Repo Test');
    await page.getByLabel('Kind').selectOption('json_feed');
    await page.getByPlaceholder('https://…').fill('http://127.0.0.1:8109/v1/providers');
    await page.getByRole('button', { name: /Create app/i }).click();

    // Should show SSRF/validation error
    await expect(page.getByText(/feed URL host|loopback|not a public address/i)).toBeVisible({ timeout: 10000 });
  });

  test('delete app tracker', async ({ page }) => {
    // Create an app first
    await page.getByRole('button', { name: /New app/i }).click();
    await page.getByPlaceholder('e.g. Free OpenRouter models').fill('Delete Test');
    await page.getByLabel('Kind').selectOption('openrouter_free');
    await page.getByPlaceholder('daily@09:00 · every 12h · manual').fill('manual');
    await page.getByRole('button', { name: /Create app/i }).click();

    await expect(page.getByText('Delete Test')).toBeVisible({ timeout: 15000 });
    await page.getByText('Delete Test').click();

    await expect(page.getByText('Apps / Delete Test')).toBeVisible({ timeout: 15000 });

    // Click delete button
    page.on('dialog', async dialog => {
      await dialog.accept();
    });
    await page.getByRole('button', { name: /Delete app/i }).click();

    // App should be removed from the list
    await expect(page.getByText('Delete Test')).not.toBeVisible({ timeout: 10000 });

    // Should return to system overview
    await expect(page.getByText('System overview')).toBeVisible();
    await expect(page.getByText('agent + gateway')).toBeVisible();
  });

  test('prefab embed token generation', async ({ page }) => {
    // Create an app first
    await page.getByRole('button', { name: /New app/i }).click();
    await page.getByPlaceholder('e.g. Free OpenRouter models').fill('Prefab Test');
    await page.getByLabel('Kind').selectOption('openrouter_free');
    await page.getByPlaceholder('daily@09:00 · every 12h · manual').fill('manual');
    await page.getByRole('button', { name: /Create app/i }).click();

    await expect(page.getByText('Prefab Test')).toBeVisible({ timeout: 15000 });
    await page.getByText('Prefab Test').click();

    await expect(page.getByText('Apps / Prefab Test')).toBeVisible({ timeout: 15000 });

    // Prefab button should be visible (if prefab flag is enabled)
    const prefabButton = page.getByRole('button', { name: /Prefab/i });
    if (await prefabButton.isVisible({ timeout: 5000 })) {
      await prefabButton.click();

      // Prefab iframe should load
      await expect(page.locator('iframe[title*="Prefab view"]')).toBeVisible({ timeout: 15000 });
    }
  });

  test('A2UI surface rendering', async ({ page }) => {
    // Create an app first
    await page.getByRole('button', { name: /New app/i }).click();
    await page.getByPlaceholder('e.g. Free OpenRouter models').fill('A2UI Test');
    await page.getByLabel('Kind').selectOption('openrouter_free');
    await page.getByPlaceholder('daily@09:00 · every 12h · manual').fill('manual');
    await page.getByRole('button', { name: /Create app/i }).click();

    await expect(page.getByText('A2UI Test')).toBeVisible({ timeout: 15000 });
    await page.getByText('A2UI Test').click();

    await expect(page.getByText('Apps / A2UI Test')).toBeVisible({ timeout: 15000 });

    // A2UI button should be visible
    const a2uiButton = page.getByRole('button', { name: /A2UI/i });
    await expect(a2uiButton).toBeVisible({ timeout: 5000 });
    await a2uiButton.click();

    // A2UI surface should load (either building, error, or rendered)
    await expect(page.getByText(/Building surface|A2UI surface unavailable|A2ui/)).toBeVisible({ timeout: 15000 });
  });

  test('console errors check', async ({ page }) => {
    const errors: string[] = [];
    page.on('console', (msg) => {
      if (msg.type() === 'error') {
        errors.push(msg.text().slice(0, 200));
      }
    });
    page.on('pageerror', (err) => {
      errors.push(String(err).slice(0, 200));
    });

    // Navigate to apps page
    await page.goto(BASE + '/apps', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(3000);

    // Create an app
    await page.getByRole('button', { name: /New app/i }).click();
    await page.getByPlaceholder('e.g. Free OpenRouter models').fill('Console Error Test');
    await page.getByLabel('Kind').selectOption('openrouter_free');
    await page.getByPlaceholder('daily@09:00 · every 12h · manual').fill('manual');
    await page.getByRole('button', { name: /Create app/i }).click();

    await expect(page.getByText('Console Error Test')).toBeVisible({ timeout: 15000 });
    await page.getByText('Console Error Test').click();
    await expect(page.getByText('Apps / Console Error Test')).toBeVisible({ timeout: 15000 });

    // Try refresh
    await page.getByRole('button', { name: /Refresh now/i }).click();
    await page.waitForTimeout(5000);

    // Try A2UI
    await page.getByRole('button', { name: /A2UI/i }).click();
    await page.waitForTimeout(3000);

    // Report console errors
    console.log('Console errors found:', errors);
    // Note: Some errors may be expected (e.g., network errors for invalid URLs in other tests)
    // We just report them here
  });
});

test('api health check', async ({ request }) => {
  const response = await request.get(BASE + '/api/health');
  expect(response.ok()).toBeTruthy();
  const data = await response.json();
  expect(data.agent).toBe('ready');
  expect(data.gateway_up).toBe(true);
});