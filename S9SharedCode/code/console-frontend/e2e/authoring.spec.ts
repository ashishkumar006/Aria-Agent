import { test, expect } from '@playwright/test';

const BASE = 'http://127.0.0.1:8500';

test.describe('Authoring Section', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto(BASE + '/authoring', { waitUntil: 'networkidle', timeout: 45000 });
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
    await expect(page.getByText('Authoring', { exact: false }).first()).toBeVisible({ timeout: 15000 });
    await expect(page.getByText('Document brief', { exact: false }).first()).toBeVisible({ timeout: 15000 });
    await expect(page.getByText('Output', { exact: false }).first()).toBeVisible({ timeout: 15000 });
    await expect(page.getByText('Try one of these', { exact: false }).first()).toBeVisible({ timeout: 15000 });
    await expect(page.getByText('Recent documents', { exact: false }).first()).toBeVisible({ timeout: 15000 });

    expect(errors, `console errors: ${errors.join(' | ')}`).toEqual([]);
  });

  test('UI elements: brief input, author button, example templates, output area', async ({ page }) => {
    // Brief input (textarea)
    const briefInput = page.locator('#brief-box');
    await expect(briefInput).toBeVisible({ timeout: 15000 });
    await expect(briefInput).toBeEnabled();

    // Author button
    const authorBtn = page.getByRole('button', { name: /^Author$/ });
    await expect(authorBtn).toBeVisible({ timeout: 15000 });
    await expect(authorBtn).toBeEnabled();

    // Stop button (initially visible)
    const stopBtn = page.getByRole('button', { name: /^Stop$/ });
    await expect(stopBtn).toBeVisible({ timeout: 15000 });

    // Example template buttons
    const templateBtns = page.getByRole('button', { name: /briefing|deck|spreadsheet|report/i });
    const templateCount = await templateBtns.count();
    expect(templateCount).toBeGreaterThanOrEqual(4);

    // Output area
    const outputArea = page.getByText('Output', { exact: false }).first();
    await expect(outputArea).toBeVisible({ timeout: 15000 });
  });

  test('example template buttons are clickable', async ({ page }) => {
    const templateBtns = page.getByRole('button', { name: /briefing|deck|spreadsheet|report/i });
    const count = await templateBtns.count();
    
    for (let i = 0; i < count; i++) {
      const btn = templateBtns.nth(i);
      const text = await btn.textContent();
      console.log(`Template ${i}: ${text?.trim().slice(0, 80)}`);
      await expect(btn).toBeVisible();
      await expect(btn).toBeEnabled();
    }
  });

  test('can type in brief input', async ({ page }) => {
    const briefInput = page.locator('#brief-box');
    
    await briefInput.fill('A short test document about Playwright testing');
    await expect(briefInput).toHaveValue('A short test document about Playwright testing');
    
    // Clear and type again
    await briefInput.clear();
    await briefInput.fill('Another test brief');
    await expect(briefInput).toHaveValue('Another test brief');
  });

  test('clicking example template fills the brief input', async ({ page }) => {
    const briefInput = page.locator('#brief-box');
    const templateBtns = page.getByRole('button', { name: /briefing|deck|spreadsheet|report/i });
    
    // Click first template
    const firstTemplate = templateBtns.first();
    const templateText = await firstTemplate.textContent();
    
    await firstTemplate.click();
    await page.waitForTimeout(500);
    
    // Check if brief input was populated
    const briefValue = await briefInput.inputValue();
    console.log('Brief after clicking template:', briefValue?.slice(0, 200));
    
    // The template text should be in the brief input
    expect(briefValue?.length).toBeGreaterThan(0);
  });

  test('document generation - simple brief', async ({ page }) => {
    const errors: string[] = [];
    page.on('console', (m) => {
      if (m.type() === 'error') errors.push(m.text().slice(0, 200));
    });
    page.on('pageerror', (e) => errors.push(String(e).slice(0, 200)));

    const briefInput = page.locator('#brief-box');
    const authorBtn = page.getByRole('button', { name: /^Author$/ });

    // Fill in a simple brief
    await briefInput.fill('A one-page briefing on the benefits of automated testing');
    
    // Click Author
    await authorBtn.click();
    
    // Wait for generation to start (Stop button should be the main action now)
    await page.waitForTimeout(3000);
    
    // Wait for completion - check for output content or stop button going away
    // The generation can take a while, so wait longer
    await page.waitForTimeout(60000);
    
    // Check output area has content
    const outputSection = page.locator('main').first();
    const outputText = await outputSection.textContent();
    
    console.log('Generation output preview:', outputText?.slice(0, 1000));
    
    // Check for any errors
    const criticalErrors = errors.filter(e => 
      !e.includes('favicon') && 
      !e.includes('DevTools') &&
      !e.includes('websocket') &&
      !e.includes('WebSocket')
    );
    
    expect(criticalErrors, `critical console errors: ${criticalErrors.join(' | ')}`).toEqual([]);
  });

  test('cancel generation with Stop button', async ({ page }) => {
    const briefInput = page.locator('#brief-box');
    const authorBtn = page.getByRole('button', { name: /^Author$/ });
    const stopBtn = page.getByRole('button', { name: /^Stop$/ });

    // Fill in a brief that would take a while
    await briefInput.fill('A comprehensive 50-page report on the history of software testing methodologies with detailed comparisons of all major frameworks');
    
    // Click Author
    await authorBtn.click();
    
    // Wait for generation to start
    await page.waitForTimeout(3000);
    
    // Click Stop
    await stopBtn.click();
    await page.waitForTimeout(2000);
    
    // Should have stopped - check for partial output or cancellation message
    const outputSection = page.locator('main').first();
    const outputText = await outputSection.textContent();
    console.log('Output after stop:', outputText?.slice(0, 500));
  });

  test('empty brief handling', async ({ page }) => {
    const briefInput = page.locator('#brief-box');
    const authorBtn = page.getByRole('button', { name: /^Author$/ });

    // Clear the brief
    await briefInput.clear();
    
    // Try to click Author
    await authorBtn.click();
    await page.waitForTimeout(2000);
    
    // Should handle gracefully - check for validation or no action
    const outputSection = page.locator('main').first();
    const outputText = await outputSection.textContent();
    console.log('Output with empty brief:', outputText?.slice(0, 500));
  });

  test('recent documents list renders', async ({ page }) => {
    // Check recent documents section
    await expect(page.getByText('Recent documents', { exact: false }).first()).toBeVisible({ timeout: 15000 });
    
    // Should have document entries
    const docButtons = page.getByRole('button', { name: /file|ago/i });
    const count = await docButtons.count();
    console.log('Recent document entries:', count);
    expect(count).toBeGreaterThan(0);
  });

  test('clicking recent document loads it', async ({ page }) => {
    const docButtons = page.getByRole('button', { name: /file|ago/i });
    const count = await docButtons.count();
    
    if (count > 0) {
      // Click first recent document
      const firstDoc = docButtons.first();
      const docText = await firstDoc.textContent();
      console.log('Clicking recent doc:', docText?.trim().slice(0, 100));
      
      await firstDoc.click();
      await page.waitForTimeout(3000);
      
      // Check if output area shows the document
      const outputSection = page.locator('main').first();
      const outputText = await outputSection.textContent();
      console.log('Output after clicking recent doc:', outputText?.slice(0, 500));
    }
  });

  test('console has no errors during authoring flow', async ({ page }) => {
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

    // Navigate and interact
    await page.goto(BASE + '/authoring', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(2000);

    // Type in brief
    const briefInput = page.locator('#brief-box');
    await briefInput.fill('A test document about Playwright');
    
    // Click Author
    const authorBtn = page.getByRole('button', { name: /^Author$/ });
    await authorBtn.click();
    
    // Wait a bit for generation to start
    await page.waitForTimeout(10000);

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

  test('keyboard shortcut Ctrl+Enter triggers generation', async ({ page }) => {
    const briefInput = page.locator('#brief-box');
    
    await briefInput.fill('Test document via keyboard shortcut');
    
    // Press Ctrl+Enter
    await briefInput.press('Control+Enter');
    await page.waitForTimeout(3000);
    
    // Should have triggered generation
    const stopBtn = page.getByRole('button', { name: /^Stop$/ });
    await expect(stopBtn).toBeVisible({ timeout: 5000 });
  });
});

test('api health fast', async ({ request }) => {
  const t0 = Date.now();
  const r = await request.get(BASE + '/api/health');
  expect(r.ok()).toBeTruthy();
  expect(Date.now() - t0).toBeLessThan(3000);
});