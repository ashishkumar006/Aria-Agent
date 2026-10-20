import { test, expect } from '@playwright/test';

const BASE = 'http://localhost:8500';

test.describe('Settings page', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto(BASE + '/settings', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(2500);
  });

  test('page loads with all setting categories visible', async ({ page }) => {
    const errors: string[] = [];
    page.on('console', (m) => {
      if (m.type() === 'error') errors.push(m.text().slice(0, 200));
    });
    page.on('pageerror', (e) => errors.push(String(e).slice(0, 200)));

    // Environment section
    await expect(page.getByText('Environment', { exact: true }).first()).toBeVisible({ timeout: 15000 });
    
    // Check all environment rows
    const envRows = ['agent', 'gateway', 'mcp tools', 'sessions', 'schedules', 'templates'];
    for (const row of envRows) {
      await expect(page.getByText(row, { exact: false }).first()).toBeVisible({ timeout: 15000 });
    }

    // API Token section
    await expect(page.getByText('API TOKEN', { exact: false }).first()).toBeVisible({ timeout: 15000 });
    await expect(page.getByRole('textbox', { name: 'API bearer token' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Save' })).toBeVisible();

    // READ-ONLY CONFIG section
    await expect(page.getByText('READ-ONLY CONFIG', { exact: false }).first()).toBeVisible({ timeout: 15000 });
    await expect(page.getByPlaceholder('filter config…')).toBeVisible();

    // Config dump area
    await expect(page.locator('pre, [class*="font-mono"]').first()).toBeVisible({ timeout: 15000 });

    // Gateway links
    await expect(page.getByRole('link', { name: 'overview' })).toBeVisible();
    await expect(page.getByRole('link', { name: 'system' })).toBeVisible();

    // Check for console errors (filter out expected 403s from auth-gated endpoints)
    const filteredErrors = errors.filter(e => !e.includes('403 (Forbidden)') && !e.includes('Failed to load resource'));
    expect(filteredErrors, `console errors: ${filteredErrors.join(' | ')}`).toEqual([]);
  });

  test('API token can be saved and cleared', async ({ page }) => {
    const errors: string[] = [];
    page.on('console', (m) => {
      if (m.type() === 'error') errors.push(m.text().slice(0, 200));
    });
    page.on('pageerror', (e) => errors.push(String(e).slice(0, 200)));

    const tokenInput = page.getByRole('textbox', { name: 'API bearer token' });
    const saveBtn = page.getByRole('button', { name: 'Save' });
    const clearBtn = page.getByRole('button', { name: 'Clear' });

    // Save a test token
    await tokenInput.fill('test-bearer-token-12345');
    await saveBtn.click();
    
    // Wait for save confirmation (may show 403 in console but UI should still show saved)
    await expect(page.getByText('saved — reloading status…')).toBeVisible({ timeout: 10000 });
    await page.waitForTimeout(3000);

    // Clear the token
    await clearBtn.click();
    await page.waitForTimeout(1500);

    // Verify input is empty
    await expect(tokenInput).toHaveValue('');

    // Filter out expected 403s from auth-gated endpoints
    const filteredErrors = errors.filter(e => !e.includes('403 (Forbidden)') && !e.includes('Failed to load resource'));
    expect(filteredErrors, `console errors: ${filteredErrors.join(' | ')}`).toEqual([]);
  });

  test('API token input accepts special characters without crashing', async ({ page }) => {
    const errors: string[] = [];
    page.on('console', (m) => {
      if (m.type() === 'error') errors.push(m.text().slice(0, 200));
    });
    page.on('pageerror', (e) => errors.push(String(e).slice(0, 200)));

    const tokenInput = page.getByRole('textbox', { name: 'API bearer token' });
    const saveBtn = page.getByRole('button', { name: 'Save' });

    // Test with special characters
    await tokenInput.fill('token with spaces & symbols!@#$%^&*()');
    await saveBtn.click();
    await page.waitForTimeout(2000);

    // Should not crash - the token is just stored locally
    await expect(page.getByText('saved — reloading status…')).toBeVisible({ timeout: 10000 });

    // Filter out expected 403s from auth-gated endpoints
    const filteredErrors = errors.filter(e => !e.includes('403 (Forbidden)') && !e.includes('Failed to load resource'));
    expect(filteredErrors, `console errors: ${filteredErrors.join(' | ')}`).toEqual([]);
  });

  test('config filter works correctly', async ({ page }) => {
    const errors: string[] = [];
    page.on('console', (m) => {
      if (m.type() === 'error') errors.push(m.text().slice(0, 200));
    });
    page.on('pageerror', (e) => errors.push(String(e).slice(0, 200)));

    const filterInput = page.getByPlaceholder('filter config…');

    // Wait for config to load
    await expect(page.locator('pre, [class*="font-mono"]').first()).toBeVisible({ timeout: 15000 });
    await page.waitForTimeout(1000);

    // Filter for a common config term
    await filterInput.fill('model');
    await page.waitForTimeout(500);
    
    // Check hit count appears
    await expect(page.getByText(/hit/, { exact: false })).toBeVisible({ timeout: 5000 });

    // Clear filter
    await filterInput.clear();
    await page.waitForTimeout(500);
    await expect(page.getByText(/hit/, { exact: false })).toHaveCount(0);

    const filteredErrors = errors.filter(e => !e.includes('403 (Forbidden)') && !e.includes('Failed to load resource'));
    expect(filteredErrors, `console errors: ${filteredErrors.join(' | ')}`).toEqual([]);
  });

  test('config dump shows YAML content with syntax highlighting', async ({ page }) => {
    const errors: string[] = [];
    page.on('console', (m) => {
      if (m.type() === 'error') errors.push(m.text().slice(0, 200));
    });
    page.on('pageerror', (e) => errors.push(String(e).slice(0, 200)));

    // Wait for config to load - wait for the "loading..." to disappear
    await expect(page.locator('pre, [class*="font-mono"]').first()).toBeVisible({ timeout: 15000 });
    await page.waitForTimeout(2000);

    // Wait for actual config content (not "loading...")
    await expect(page.locator('text=loading…')).toHaveCount(0, { timeout: 15000 });

    // Check that config has content (not empty)
    const configContent = await page.locator('[class*="font-mono"]').first().textContent();
    expect(configContent?.length).toBeGreaterThan(100);

    // Check for YAML-like structure (keys with colons)
    expect(configContent).toMatch(/:\s/);

    const filteredErrors = errors.filter(e => !e.includes('403 (Forbidden)') && !e.includes('Failed to load resource'));
    expect(filteredErrors, `console errors: ${filteredErrors.join(' | ')}`).toEqual([]);
  });

  test('environment status pills show correct states', async ({ page }) => {
    const errors: string[] = [];
    page.on('console', (m) => {
      if (m.type() === 'error') errors.push(m.text().slice(0, 200));
    });
    page.on('pageerror', (e) => errors.push(String(e).slice(0, 200)));

    // Wait for environment rows to load
    await expect(page.getByText('agent', { exact: false }).first()).toBeVisible({ timeout: 15000 });
    await page.waitForTimeout(1000);

    // Check that each row has a status indicator (up/down text)
    // The Pill component renders spans with 'up' or 'down' text
    const upCount = await page.getByText('up', { exact: true }).count();
    const downCount = await page.getByText('down', { exact: true }).count();
    const totalStatus = upCount + downCount;
    expect(totalStatus).toBeGreaterThanOrEqual(6); // agent, gateway, mcp tools, sessions, schedules, templates

    // Verify each row has a note
    const notes = page.locator('text=/unreachable|reachable|registered|in first|jobs|saved|unreadable/');
    const noteCount = await notes.count();
    expect(noteCount).toBeGreaterThanOrEqual(6);

    const filteredErrors = errors.filter(e => !e.includes('403 (Forbidden)') && !e.includes('Failed to load resource'));
    expect(filteredErrors, `console errors: ${filteredErrors.join(' | ')}`).toEqual([]);
  });

  test('persistence across reload - API token', async ({ page }) => {
    const errors: string[] = [];
    page.on('console', (m) => {
      if (m.type() === 'error') errors.push(m.text().slice(0, 200));
    });
    page.on('pageerror', (e) => errors.push(String(e).slice(0, 200)));

    const tokenInput = page.getByRole('textbox', { name: 'API bearer token' });
    const saveBtn = page.getByRole('button', { name: 'Save' });

    // Set a token
    const testToken = 'persistent-test-token-' + Date.now();
    await tokenInput.fill(testToken);
    await saveBtn.click();
    await expect(page.getByText('saved — reloading status…')).toBeVisible({ timeout: 10000 });
    await page.waitForTimeout(3000);

    // Reload the page
    await page.reload({ waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(2500);

    // Token should persist in localStorage - clear button should appear
    await expect(page.getByRole('button', { name: 'Clear' })).toBeVisible({ timeout: 15000 });

    const filteredErrors = errors.filter(e => !e.includes('403 (Forbidden)') && !e.includes('Failed to load resource'));
    expect(filteredErrors, `console errors: ${filteredErrors.join(' | ')}`).toEqual([]);
  });

  test('reset to defaults - clear token restores empty state', async ({ page }) => {
    const errors: string[] = [];
    page.on('console', (m) => {
      if (m.type() === 'error') errors.push(m.text().slice(0, 200));
    });
    page.on('pageerror', (e) => errors.push(String(e).slice(0, 200)));

    const tokenInput = page.getByRole('textbox', { name: 'API bearer token' });
    const saveBtn = page.getByRole('button', { name: 'Save' });
    const clearBtn = page.getByRole('button', { name: 'Clear' });

    // Set a token first
    await tokenInput.fill('token-to-clear');
    await saveBtn.click();
    await expect(page.getByText('saved — reloading status…')).toBeVisible({ timeout: 10000 });
    await page.waitForTimeout(2000);

    // Clear it
    await clearBtn.click();
    await page.waitForTimeout(1500);

    // Verify cleared state - input empty, clear button gone
    await expect(tokenInput).toHaveValue('');
    await expect(clearBtn).toHaveCount(0);

    const filteredErrors = errors.filter(e => !e.includes('403 (Forbidden)') && !e.includes('Failed to load resource'));
    expect(filteredErrors, `console errors: ${filteredErrors.join(' | ')}`).toEqual([]);
  });

  test('no console errors on settings page', async ({ page }) => {
    const errors: string[] = [];
    page.on('console', (m) => {
      if (m.type() === 'error') errors.push(m.text().slice(0, 200));
    });
    page.on('pageerror', (e) => errors.push(String(e).slice(0, 200)));

    await page.goto(BASE + '/settings', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(3000);

    // All environment rows loaded
    await expect(page.getByText('agent', { exact: false }).first()).toBeVisible({ timeout: 15000 });
    await expect(page.getByText('gateway', { exact: false }).first()).toBeVisible({ timeout: 15000 });
    await expect(page.getByText('mcp tools', { exact: false }).first()).toBeVisible({ timeout: 15000 });

    // Config loaded
    await expect(page.locator('[class*="font-mono"]').first()).toBeVisible({ timeout: 15000 });

    const filteredErrors = errors.filter(e => !e.includes('403 (Forbidden)') && !e.includes('Failed to load resource'));
    expect(filteredErrors, `console errors on /settings: ${filteredErrors.join(' | ')}`).toEqual([]);
  });

  test('gateway links have correct href attributes', async ({ page }) => {
    const errors: string[] = [];
    page.on('console', (m) => {
      if (m.type() === 'error') errors.push(m.text().slice(0, 200));
    });
    page.on('pageerror', (e) => errors.push(String(e).slice(0, 200)));

    // Check overview link
    const overviewLink = page.getByRole('link', { name: 'overview' });
    await expect(overviewLink).toBeVisible();
    
    const overviewHref = await overviewLink.getAttribute('href');
    expect(overviewHref).toContain('/static/system.html');

    // Check system link
    const systemLink = page.getByRole('link', { name: 'system' });
    await expect(systemLink).toBeVisible();
    
    const systemHref = await systemLink.getAttribute('href');
    expect(systemHref).toContain('/static/system.html');

    const filteredErrors = errors.filter(e => !e.includes('403 (Forbidden)') && !e.includes('Failed to load resource'));
    expect(filteredErrors, `console errors: ${filteredErrors.join(' | ')}`).toEqual([]);
  });
});