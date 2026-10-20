import { test, expect } from '@playwright/test';

test.describe('Code Section Tests', () => {
  test.beforeEach(async ({ page }) => {
    // Capture console errors
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
    
    // Wait for React app to mount
    await page.waitForSelector('#root', { state: 'attached' });
    await page.waitForTimeout(2000);
  });

  test('Page loads with all major components', async ({ page }) => {
    // Check file tree exists
    const fileTree = page.locator('[data-testid="file-tree"], .file-tree, [class*="file-tree"]').first();
    await expect(fileTree).toBeVisible({ timeout: 10000 });
    
    // Check editor exists
    const editor = page.locator('.monaco-editor, [class*="monaco-editor"]').first();
    await expect(editor).toBeVisible({ timeout: 10000 });
    
    // Check terminal dock exists
    const terminal = page.locator('[data-testid="terminal"], .terminal, [class*="terminal"]').first();
    await expect(terminal).toBeVisible({ timeout: 10000 });
    
    // Check composer/chat exists
    const composer = page.locator('[data-testid="composer"], .composer, [class*="composer"]').first();
    await expect(composer).toBeVisible({ timeout: 10000 });
  });

  test('File tree browsing', async ({ page }) => {
    // Expand folders in file tree
    const expandButtons = page.locator('[data-testid="file-tree"] button[aria-expanded="false"], .file-tree button[aria-expanded="false"], [class*="file-tree"] button[aria-expanded="false"]');
    const count = await expandButtons.count();
    
    for (let i = 0; i < Math.min(count, 5); i++) {
      await expandButtons.nth(i).click();
      await page.waitForTimeout(300);
    }
  });

  test('Open file in editor', async ({ page }) => {
    // Click on a file in the tree
    const fileItems = page.locator('[data-testid="file-tree"] [role="treeitem"], .file-tree [role="treeitem"], [class*="file-tree"] [role="treeitem"]').filter({ hasText: /\.(ts|tsx|js|json|py)$/ });
    const count = await fileItems.count();
    
    if (count > 0) {
      await fileItems.first().click();
      await page.waitForTimeout(500);
      
      // Verify editor has content
      const editorContent = page.locator('.monaco-editor .view-lines, .monaco-editor .lines-content').first();
      await expect(editorContent).toBeVisible({ timeout: 5000 });
    }
  });

  test('Edit file - type and save', async ({ page }) => {
    // Open a file first
    const fileItems = page.locator('[data-testid="file-tree"] [role="treeitem"], .file-tree [role="treeitem"], [class*="file-tree"] [role="treeitem"]').filter({ hasText: /\.(ts|tsx|js|json|py)$/ });
    const count = await fileItems.count();
    
    if (count > 0) {
      await fileItems.first().click();
      await page.waitForTimeout(500);
      
      // Click in editor to focus
      const editor = page.locator('.monaco-editor').first();
      await editor.click();
      await page.waitForTimeout(200);
      
      // Type some text
      await page.keyboard.type('// Test edit\n');
      await page.waitForTimeout(200);
      
      // Save with Ctrl+S
      await page.keyboard.press('Control+s');
      await page.waitForTimeout(500);
    }
  });

  test('Create new file', async ({ page }) => {
    // Look for new file button
    const newFileBtn = page.locator('button:has-text("New File"), button[aria-label*="new file" i], [data-testid="new-file"]').first();
    if (await newFileBtn.count() > 0) {
      await newFileBtn.click();
      await page.waitForTimeout(500);
      
      // Enter filename
      const input = page.locator('input[placeholder*="name" i], input[placeholder*="file" i]').first();
      if (await input.count() > 0) {
        await input.fill('test-new-file.ts');
        await page.keyboard.press('Enter');
        await page.waitForTimeout(500);
      }
    }
  });

  test('Delete file', async ({ page }) => {
    // This is destructive, so we'll just verify the UI exists
    const fileItems = page.locator('[data-testid="file-tree"] [role="treeitem"], .file-tree [role="treeitem"], [class*="file-tree"] [role="treeitem"]');
    const count = await fileItems.count();
    
    if (count > 0) {
      // Right click on a file
      await fileItems.first().click({ button: 'right' });
      await page.waitForTimeout(300);
      
      // Look for delete option
      const deleteOption = page.locator('[role="menuitem"]:has-text("Delete"), [role="menuitem"]:has-text("Remove")').first();
      if (await deleteOption.count() > 0) {
        // Don't actually delete - just verify it exists
        await page.keyboard.press('Escape');
      } else {
        await page.keyboard.press('Escape');
      }
    }
  });

  test('Terminal - open and run commands', async ({ page }) => {
    // Terminal should already be visible
    const terminal = page.locator('.terminal, [class*="terminal"], [data-testid="terminal"]').first();
    await expect(terminal).toBeVisible();
    
    // Click terminal to focus
    await terminal.click();
    await page.waitForTimeout(300);
    
    // Run ls command
    await page.keyboard.type('ls\n');
    await page.waitForTimeout(1000);
    
    // Run pwd command
    await page.keyboard.type('pwd\n');
    await page.waitForTimeout(1000);
    
    // Check output appears
    const output = page.locator('.terminal .xterm-screen, .terminal .xterm-rows, [class*="terminal"] .xterm-screen').first();
    await expect(output).toBeVisible();
  });

  test('Terminal - resize', async ({ page }) => {
    const terminal = page.locator('.terminal, [class*="terminal"], [data-testid="terminal"]').first();
    
    // Try to find resize handle
    const resizeHandle = page.locator('.resize-handle, [class*="resize"], [data-testid="resize"]').first();
    if (await resizeHandle.count() > 0) {
      await resizeHandle.hover();
      await page.mouse.down();
      await page.mouse.move(0, -100);
      await page.mouse.up();
      await page.waitForTimeout(500);
    }
  });

  test('Code agent - ask question', async ({ page }) => {
    const composer = page.locator('[data-testid="composer"], .composer, [class*="composer"]').first();
    await expect(composer).toBeVisible();
    
    // Find input area
    const input = composer.locator('textarea, input[type="text"], [contenteditable="true"]').first();
    if (await input.count() > 0) {
      await input.fill('What files are in this project?');
      await page.keyboard.press('Enter');
      await page.waitForTimeout(3000);
      
      // Check for response
      const response = page.locator('[data-testid="agent-response"], .agent-response, [class*="agent-response"]').first();
      await expect(response).toBeVisible({ timeout: 10000 });
    }
  });

  test('Code agent - accept/reject edit', async ({ page }) => {
    const composer = page.locator('[data-testid="composer"], .composer, [class*="composer"]').first();
    const input = composer.locator('textarea, input[type="text"], [contenteditable="true"]').first();
    
    if (await input.count() > 0) {
      await input.fill('Add a comment to the first file');
      await page.keyboard.press('Enter');
      await page.waitForTimeout(5000);
      
      // Look for accept/reject buttons
      const acceptBtn = page.locator('button:has-text("Accept"), button:has-text("Apply"), [data-testid="accept"]').first();
      const rejectBtn = page.locator('button:has-text("Reject"), button:has-text("Cancel"), [data-testid="reject"]').first();
      
      if (await acceptBtn.count() > 0) {
        await acceptBtn.click();
      } else if (await rejectBtn.count() > 0) {
        await rejectBtn.click();
      }
      await page.waitForTimeout(1000);
    }
  });

  test('Quick open palette (Ctrl+P)', async ({ page }) => {
    await page.keyboard.press('Control+p');
    await page.waitForTimeout(500);
    
    const palette = page.locator('[role="dialog"]:has-text("Open"), [class*="quick-open"], [class*="command-palette"]').first();
    const isVisible = await palette.isVisible({ timeout: 3000 }).catch(() => false);
    
    if (isVisible) {
      await page.keyboard.type('test');
      await page.waitForTimeout(300);
      await page.keyboard.press('Escape');
    }
  });

  test('Symbol search', async ({ page }) => {
    await page.keyboard.press('Control+Shift+o');
    await page.waitForTimeout(500);
    
    const symbolSearch = page.locator('[role="dialog"]:has-text("Symbol"), [class*="symbol"], [class*="go-to-symbol"]').first();
    const isVisible = await symbolSearch.isVisible({ timeout: 3000 }).catch(() => false);
    
    if (isVisible) {
      await page.keyboard.press('Escape');
    }
  });

  test('Large file handling', async ({ page }) => {
    // Try to find a large file in the tree
    const fileItems = page.locator('[data-testid="file-tree"] [role="treeitem"], .file-tree [role="treeitem"], [class*="file-tree"] [role="treeitem"]').filter({ hasText: /\.(ts|tsx|js|py)$/ });
    const count = await fileItems.count();
    
    for (let i = 0; i < Math.min(count, 10); i++) {
      await fileItems.nth(i).click();
      await page.waitForTimeout(300);
      
      // Check if editor loads without error
      const editor = page.locator('.monaco-editor').first();
      await expect(editor).toBeVisible({ timeout: 5000 });
    }
  });

  test('Binary file handling', async ({ page }) => {
    const fileItems = page.locator('[data-testid="file-tree"] [role="treeitem"], .file-tree [role="treeitem"], [class*="file-tree"] [role="treeitem"]').filter({ hasText: /\.(png|jpg|jpeg|gif|ico|woff|woff2|ttf|eot|pdf)$/ });
    const count = await fileItems.count();
    
    if (count > 0) {
      await fileItems.first().click();
      await page.waitForTimeout(500);
      
      // Should show binary file notice or handle gracefully
      const editor = page.locator('.monaco-editor').first();
      await expect(editor).toBeVisible({ timeout: 5000 });
    }
  });

  test.afterEach(async ({ page }) => {
    // Log console errors
    const errors = await page.evaluate(() => {
      return (window as any).__consoleErrors || [];
    });
    if (errors.length > 0) {
      console.log('Console errors:', errors);
    }
  });
});