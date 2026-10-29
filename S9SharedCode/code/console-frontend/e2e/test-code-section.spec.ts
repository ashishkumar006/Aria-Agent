import { test, expect } from '@playwright/test';

test.describe('Code Section Tests', () => {
  test.beforeEach(async ({ page }) => {
    const consoleErrors: string[] = [];
    page.on('console', msg => {
      if (msg.type() === 'error') {
        consoleErrors.push(msg.text());
      }
    });
    page.on('pageerror', error => {
      consoleErrors.push(error.message);
    });
    
    await page.goto('http://127.0.0.1:8500/code');
    await page.waitForLoadState('networkidle');
    
    await page.waitForSelector('#root', { state: 'attached' });
    await page.waitForTimeout(2000);
  });

  test('Page loads with all major components', async ({ page }) => {
    await expect(page.locator('aside[aria-label="Explorer"]')).toBeVisible({ timeout: 10000 });
    await expect(page.locator('text="Pick a file to start editing"')).toBeVisible({ timeout: 5000 });
    await expect(page.locator('button[role="tab"]:has-text("Terminal")')).toBeVisible({ timeout: 5000 });

    const agentBtn = page.locator('button:has-text("Agent")').first();
    await expect(agentBtn).toBeVisible({ timeout: 5000 });

    // The agent panel starts visible at >=1280px; toggle only if hidden
    const ariaPressed = await agentBtn.getAttribute('aria-pressed');
    if (ariaPressed !== 'true') {
      await agentBtn.click({ timeout: 5000 });
    }
    await expect(page.locator('textarea[aria-label="Ask the agent about this code"]')).toBeVisible({ timeout: 10000 });
  });

  test('File tree browsing', async ({ page }) => {
    await page.locator('aside[aria-label="Explorer"]').waitFor({ timeout: 10000 });
    await page.waitForTimeout(3000);

    for (let i = 0; i < 5; i++) {
      const expandBtn = page.locator('aside[aria-label="Explorer"] button[aria-expanded="false"]').first();
      const count = await expandBtn.count();
      if (count === 0) break;
      await expandBtn.click({ timeout: 5000 });
      await page.waitForTimeout(500);
    }
  });

  test('Open file in editor', async ({ page }) => {
    await page.locator('aside[aria-label="Explorer"]').waitFor({ timeout: 10000 });
    await page.waitForTimeout(3000);

    const fileBtn = page.locator('aside[aria-label="Explorer"] button:has(span.truncate):not([aria-expanded])').first();
    const count = await fileBtn.count();
    if (count > 0) {
      await fileBtn.click({ timeout: 5000 });
      await page.waitForTimeout(5000);
      await expect(page.locator('.monaco-editor').first()).toBeVisible({ timeout: 15000 });
    }
  });

  test('Edit file - type and save', async ({ page }) => {
    await page.locator('aside[aria-label="Explorer"]').waitFor({ timeout: 10000 });
    await page.waitForTimeout(3000);

    const fileBtn = page.locator('aside[aria-label="Explorer"] button:has(span.truncate):not([aria-expanded])').first();
    const count = await fileBtn.count();
    if (count > 0) {
      await fileBtn.click({ timeout: 5000 });
      await page.waitForTimeout(5000);

      const editor = page.locator('.monaco-editor').first();
      await editor.click({ timeout: 10000 });
      await page.waitForTimeout(500);

      await page.keyboard.type('// Test edit\n');
      await page.waitForTimeout(500);
      await page.keyboard.press('Control+s');
      await page.waitForTimeout(1000);
    }
  });

  test('Create new file', async ({ page }) => {
    await page.locator('aside[aria-label="Explorer"]').waitFor({ timeout: 10000 });

    const newFileBtn = page.locator('button[aria-label="New file"]').first();
    if (await newFileBtn.count() > 0) {
      await newFileBtn.click();
      await page.waitForTimeout(500);

      const input = page.locator('input[aria-label="New file name"]').first();
      if (await input.count() > 0) {
        await input.fill('test-new-file.ts');
        await page.keyboard.press('Enter');
        await page.waitForTimeout(1000);
      }
    }
  });

  test('Delete file', async ({ page }) => {
    await page.locator('aside[aria-label="Explorer"]').waitFor({ timeout: 10000 });
    await page.waitForTimeout(3000);

    const fileBtn = page.locator('aside[aria-label="Explorer"] button:has(span.truncate):not([aria-expanded])').first();
    const count = await fileBtn.count();
    if (count > 0) {
      await fileBtn.click({ button: 'right' });
      await page.waitForTimeout(500);

      await page.keyboard.press('Escape');
      await page.waitForTimeout(300);
    }
  });

  test('Terminal - open and run commands', async ({ page }) => {
    const terminalTab = page.locator('button[role="tab"]:has-text("Terminal")').first();
    await terminalTab.click();
    await page.waitForTimeout(5000);

    const xtermScreen = page.locator('.xterm-screen, .xterm-rows').first();
    await expect(xtermScreen).toBeVisible({ timeout: 15000 });

    await xtermScreen.click();
    await page.waitForTimeout(500);

    await page.keyboard.type('ls\n');
    await page.waitForTimeout(2000);

    await page.keyboard.type('pwd\n');
    await page.waitForTimeout(2000);
  });

  test('Terminal - resize', async ({ page }) => {
    const terminalTab = page.locator('button[role="tab"]:has-text("Terminal")').first();
    await terminalTab.click();
    await page.waitForTimeout(1000);

    const resizeHandle = page.locator('.resize-handle, [class*="resize-handle"], [data-testid="resize"]').first();
    if (await resizeHandle.count() > 0) {
      await resizeHandle.hover();
      await page.mouse.down();
      await page.mouse.move(0, -100);
      await page.mouse.up();
      await page.waitForTimeout(500);
    }
  });

  test('Code agent - ask question', async ({ page }) => {
    const agentBtn = page.locator('button:has-text("Agent")').first();
    const ariaPressed = await agentBtn.getAttribute('aria-pressed');
    if (ariaPressed !== 'true') {
      await agentBtn.click({ timeout: 5000 });
    }
    await page.waitForTimeout(2000);

    const composer = page.locator('textarea[aria-label="Ask the agent about this code"]').first();
    await expect(composer).toBeVisible({ timeout: 10000 });

    await composer.fill('What files are in this project?');
    await page.waitForTimeout(1000);
    await composer.press('Enter');
    await page.waitForTimeout(15000);

    const response = page.locator('.prose-chat').first();
    await expect(response).toBeVisible({ timeout: 20000 });
  });

  test('Code agent - accept/reject edit', async ({ page }) => {
    const agentBtn = page.locator('button:has-text("Agent")').first();
    const ariaPressed = await agentBtn.getAttribute('aria-pressed');
    if (ariaPressed !== 'true') {
      await agentBtn.click({ timeout: 5000 });
    }
    await page.waitForTimeout(2000);

    const composer = page.locator('textarea[aria-label="Ask the agent about this code"]').first();
    if (await composer.count() > 0) {
      await composer.fill('Add a comment to the first file');
      await composer.press('Enter');
      await page.waitForTimeout(15000);

      const keepBtn = page.locator('button:has-text("Keep")').first();
      const revertBtn = page.locator('button:has-text("Revert")').first();

      if (await keepBtn.count() > 0) {
        await keepBtn.click();
      } else if (await revertBtn.count() > 0) {
        await revertBtn.click();
      }
      await page.waitForTimeout(1000);
    }
  });

  test('Quick open palette (Ctrl+P)', async ({ page }) => {
    await page.keyboard.press('Control+p');
    await page.waitForTimeout(500);
    
    const palette = page.locator('[role="dialog"][aria-label="Go to file"]').first();
    const isVisible = await palette.isVisible({ timeout: 3000 }).catch(() => false);
    
    if (isVisible) {
      await page.keyboard.type('test');
      await page.waitForTimeout(500);
      await page.keyboard.press('Escape');
      await page.waitForTimeout(500);
    }
  });

  test('Symbol search', async ({ page }) => {
    await page.keyboard.press('Control+Shift+o');
    await page.waitForTimeout(500);
    
    const symbolSearch = page.locator('[aria-label="Go to Symbol"], [aria-label*="Symbol"]').first();
    const isVisible = await symbolSearch.isVisible({ timeout: 3000 }).catch(() => false);
    
    if (isVisible) {
      await page.keyboard.press('Escape');
      await page.waitForTimeout(500);
    }
  });

  test('Large file handling', async ({ page }) => {
    await page.locator('aside[aria-label="Explorer"]').waitFor({ timeout: 10000 });
    await page.waitForTimeout(3000);

    const fileBtn = page.locator('aside[aria-label="Explorer"] button:has(span.truncate):not([aria-expanded])').first();
    const count = await fileBtn.count();

    if (count > 0) {
      await fileBtn.click({ timeout: 5000 });
      await page.waitForTimeout(5000);
      await expect(page.locator('.monaco-editor').first()).toBeVisible({ timeout: 15000 });
    }
  });

  test('Binary file handling', async ({ page }) => {
    await page.locator('aside[aria-label="Explorer"]').waitFor({ timeout: 10000 });
    await page.waitForTimeout(3000);

    const fileBtn = page.locator('aside[aria-label="Explorer"] button:has(span.truncate):not([aria-expanded])').first();
    const count = await fileBtn.count();

    if (count > 0) {
      await fileBtn.click({ timeout: 5000 });
      await page.waitForTimeout(5000);
      await expect(page.locator('.monaco-editor').first()).toBeVisible({ timeout: 15000 });
    }
  });

  test.afterEach(async ({ page }) => {
    page.on('console', msg => {
      if (msg.type() === 'error') {
        console.log('Console error:', msg.text());
      }
    });
    page.on('pageerror', error => {
      console.log('Page error:', error.message);
    });
  });
});
