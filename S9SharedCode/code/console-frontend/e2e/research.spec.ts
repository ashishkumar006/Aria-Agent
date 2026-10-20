import { test, expect } from '@playwright/test';

const BASE = 'http://localhost:8500';

test.describe('Research Section', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto(BASE + '/research', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(2000);
  });

  test('page loads without skeleton or errors', async ({ page }) => {
    const errors: string[] = [];
    page.on('console', (m) => {
      if (m.type() === 'error') errors.push(m.text().slice(0, 200));
    });
    page.on('pageerror', (e) => errors.push(String(e).slice(0, 200)));

    // Check for skeleton loaders - should not be present after load
    await expect(page.locator('.animate-pulse').first()).toHaveCount(0, { timeout: 15000 });
    
    // Verify key markers are visible
    await expect(page.getByText('Topics', { exact: false }).first()).toBeVisible({ timeout: 15000 });
    await expect(page.getByText('Research this topic', { exact: false }).first()).toBeVisible({ timeout: 15000 });
    await expect(page.getByText('The pipeline appears here', { exact: false }).first()).toBeVisible({ timeout: 15000 });
    
    expect(errors, `console errors: ${errors.join(' | ')}`).toEqual([]);
  });

  test('UI elements: query input, model picker, send button', async ({ page }) => {
    // Query input
    const queryInput = page.getByPlaceholder('Research this topic');
    await expect(queryInput).toBeVisible();
    await expect(queryInput).toBeEnabled();

    // Model picker
    const modelPicker = page.getByRole('combobox', { name: /model/i }).or(page.locator('[aria-label*="model" i]').first());
    await expect(modelPicker).toBeVisible();

    // Send button
    const sendButton = page.getByRole('button', { name: /send|start research/i }).first();
    await expect(sendButton).toBeVisible();
    await expect(sendButton).toBeEnabled();
  });

  test('submit simple research query and verify streaming response', async ({ page }) => {
    const queryInput = page.getByPlaceholder('Research this topic');
    const sendButton = page.getByRole('button', { name: /send|start research/i }).first();

    await queryInput.fill('What is the capital of France?');
    await sendButton.click();

    // Wait for DAG nodes to appear (streaming starts)
    const dagContainer = page.locator('[data-testid="dag"], .dag, [class*="dag"]').first().or(
      page.locator('text=/nodes?|pipeline|graph/i').first()
    );
    
    // Check for streaming indicators - logs appearing
    await expect(page.getByText(/thinking|researching|searching|planning/i).first()).toBeVisible({ timeout: 30000 });

    // Wait for final answer to render
    await expect(page.getByText(/Paris/i).first()).toBeVisible({ timeout: 120000 });

    // Verify DAG has nodes
    const dagNodes = page.locator('[class*="node"], [data-testid*="node"], .react-flow__node').first();
    await expect(dagNodes).toBeVisible({ timeout: 30000 });
  });

  test('empty query submission shows validation', async ({ page }) => {
    const sendButton = page.getByRole('button', { name: /send|start research/i }).first();
    
    // Try to submit without entering query
    await sendButton.click();
    
    // Should show validation error or disable button
    const errorMsg = page.getByText(/enter a topic|required|empty/i).first();
    await expect(errorMsg).toBeVisible({ timeout: 5000 });
  });

  test('very long query handling', async ({ page }) => {
    const queryInput = page.getByPlaceholder('Research this topic');
    const sendButton = page.getByRole('button', { name: /send|start research/i }).first();

    // Create a very long query (e.g., 5000 chars)
    const longQuery = 'What is '.repeat(500) + 'the capital of France?';
    await queryInput.fill(longQuery);
    
    // Should either truncate, show warning, or accept
    await sendButton.click();
    
    // Verify it doesn't crash - either processes or shows appropriate message
    await expect(page.locator('text=/error|too long|limit/i').first().or(page.getByText(/Paris/i))).toBeVisible({ timeout: 60000 });
  });

  test('cancel mid-stream (click Stop)', async ({ page }) => {
    const queryInput = page.getByPlaceholder('Research this topic');
    const sendButton = page.getByRole('button', { name: /send|start research/i }).first();

    await queryInput.fill('Explain quantum computing in detail with history and applications');
    await sendButton.click();

    // Wait for streaming to start
    await expect(page.getByText(/thinking|researching|searching|planning/i).first()).toBeVisible({ timeout: 15000 });

    // Click Stop button
    const stopButton = page.getByRole('button', { name: /stop|cancel/i }).first();
    await expect(stopButton).toBeVisible({ timeout: 5000 });
    await stopButton.click();

    // Verify streaming stopped - no new nodes/logs appearing
    await page.waitForTimeout(3000);
    
    // Should show partial results or cancellation message
    await expect(page.getByText(/stopped|cancelled|partial/i).first().or(page.getByText(/quantum/i))).toBeVisible({ timeout: 10000 });
  });

  test('follow-up question after completion', async ({ page }) => {
    const queryInput = page.getByPlaceholder('Research this topic');
    const sendButton = page.getByRole('button', { name: /send|start research/i }).first();

    // First query
    await queryInput.fill('What is the capital of France?');
    await sendButton.click();
    await expect(page.getByText(/Paris/i).first()).toBeVisible({ timeout: 120000 });

    // Follow-up question
    await queryInput.fill('What is the population of that city?');
    await sendButton.click();
    
    // Should show new streaming response
    await expect(page.getByText(/thinking|researching|searching|planning/i).first()).toBeVisible({ timeout: 15000 });
    await expect(page.getByText(/million|population/i).first()).toBeVisible({ timeout: 120000 });
  });

  test('switch away and back preserves state (keep-alive)', async ({ page }) => {
    const queryInput = page.getByPlaceholder('Research this topic');
    const sendButton = page.getByRole('button', { name: /send|start research/i }).first();

    // Start a query
    await queryInput.fill('What is the capital of France?');
    await sendButton.click();
    await expect(page.getByText(/Paris/i).first()).toBeVisible({ timeout: 120000 });

    // Navigate away
    await page.getByRole('link', { name: 'Runs', exact: true }).click();
    await expect(page.getByText('Select a run', { exact: false }).first()).toBeVisible({ timeout: 15000 });
    await page.waitForTimeout(1000);

    // Navigate back
    await page.getByRole('link', { name: 'Research', exact: true }).click();
    await page.waitForTimeout(1500);

    // State should be preserved - answer still visible
    await expect(page.getByText(/Paris/i).first()).toBeVisible({ timeout: 15000 });
  });

  test('console has no errors during research flow', async ({ page }) => {
    const errors: string[] = [];
    const networkErrors: string[] = [];
    
    page.on('console', (m) => {
      if (m.type() === 'error') errors.push(m.text().slice(0, 300));
    });
    page.on('pageerror', (e) => errors.push(String(e).slice(0, 300)));
    page.on('response', (resp) => {
      if (!resp.ok() && resp.url().includes('/api/')) {
        networkErrors.push(`${resp.status()} ${resp.url()}`);
      }
    });

    const queryInput = page.getByPlaceholder('Research this topic');
    const sendButton = page.getByRole('button', { name: /send|start research/i }).first();

    await queryInput.fill('What is the capital of France?');
    await sendButton.click();
    await expect(page.getByText(/Paris/i).first()).toBeVisible({ timeout: 120000 });
    await page.waitForTimeout(2000);

    // Filter out known non-critical errors
    const criticalErrors = errors.filter(e => 
      !e.includes('favicon') && 
      !e.includes('DevTools') &&
      !e.includes('websocket') &&
      !e.includes('WebSocket')
    );
    
    expect(criticalErrors, `critical console errors: ${criticalErrors.join(' | ')}`).toEqual([]);
    expect(networkErrors, `network errors: ${networkErrors.join(' | ')}`).toEqual([]);
  });

  test('produced files are downloadable', async ({ page }) => {
    const queryInput = page.getByPlaceholder('Research this topic');
    const sendButton = page.getByRole('button', { name: /send|start research/i }).first();

    // Query that might produce files
    await queryInput.fill('Create a short report on France capital as markdown');
    await sendButton.click();
    await expect(page.getByText(/Paris/i).first()).toBeVisible({ timeout: 120000 });
    await page.waitForTimeout(3000);

    // Look for download links/buttons
    const downloadLinks = page.locator('a[download], button[aria-label*="download" i], a:has-text("Download")').first();
    
    if (await downloadLinks.count() > 0) {
      await expect(downloadLinks).toBeVisible({ timeout: 10000 });
      
      // Test download
      const [download] = await Promise.all([
        page.waitForEvent('download', { timeout: 10000 }),
        downloadLinks.click()
      ]);
      
      expect(download.suggestedFilename()).toBeTruthy();
      console.log(`Downloaded: ${download.suggestedFilename()}`);
    } else {
      console.log('No downloadable files produced for this query (expected for simple queries)');
    }
  });

  test('model picker changes model', async ({ page }) => {
    const modelPicker = page.getByRole('combobox', { name: /model/i }).or(page.locator('[aria-label*="model" i]').first());
    
    await modelPicker.click();
    await page.waitForTimeout(500);
    
    // Should show options
    const options = page.getByRole('option');
    const count = await options.count();
    expect(count).toBeGreaterThan(0);
    
    // Select a different model if available
    if (count > 1) {
      await options.nth(1).click();
      await page.waitForTimeout(500);
    }
  });
});

test('api health fast', async ({ request }) => {
  const t0 = Date.now();
  const r = await request.get(BASE + '/api/health');
  expect(r.ok()).toBeTruthy();
  expect(Date.now() - t0).toBeLessThan(3000);
});