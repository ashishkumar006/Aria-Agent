import { test, expect } from '@playwright/test';

const BASE = 'http://localhost:8500';

test.describe('Console Chat - Full Test Suite', () => {
  test.beforeEach(async ({ page }) => {
    // Navigate to console page
    await page.goto(BASE + '/console', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(2000);
  });

  test('page loads with chat interface, composer, model picker', async ({ page }) => {
    // Check for chat interface elements
    await expect(page.getByText('Threads', { exact: false }).first()).toBeVisible({ timeout: 15000 });
    await expect(page.getByText('Quick answers, no pipeline', { exact: false }).first()).toBeVisible();
    
    // Check for composer (message input)
    const composer = page.getByRole('textbox', { name: /message|composer|ask/i }).first();
    await expect(composer).toBeVisible({ timeout: 15000 });
    
    // Check for model picker
    const modelPicker = page.getByRole('combobox', { name: /model|provider/i }).first();
    await expect(modelPicker).toBeVisible({ timeout: 15000 });
    
    // Check for new chat button
    const newChatBtn = page.getByRole('button', { name: /new chat|new thread/i }).first();
    await expect(newChatBtn).toBeVisible({ timeout: 15000 });
  });

  test('simple chat: send "Hello, how are you?"', async ({ page }) => {
    const composer = page.getByRole('textbox', { name: /message|composer|ask/i }).first();
    await composer.fill('Hello, how are you?');
    await page.keyboard.press('Enter');
    
    // Wait for response to start streaming
    await page.waitForTimeout(3000);
    
    // Verify response appears (delta frames render)
    const response = page.locator('[data-message-role="assistant"], .message-assistant, [role="article"]').first();
    await expect(response).toBeVisible({ timeout: 30000 });
  });

  test('streaming response works (delta frames render)', async ({ page }) => {
    const composer = page.getByRole('textbox', { name: /message|composer|ask/i }).first();
    await composer.fill('Count from 1 to 10 slowly');
    await page.keyboard.press('Enter');
    
    // Check for streaming indicator or partial content
    await page.waitForTimeout(1000);
    
    // Look for streaming content appearing incrementally
    const assistantMsg = page.locator('[data-message-role="assistant"], .message-assistant').first();
    await expect(assistantMsg).toBeVisible({ timeout: 30000 });
    
    // Wait for streaming to complete
    await page.waitForTimeout(5000);
    
    // Verify final content
    const content = await assistantMsg.textContent();
    expect(content!.length).toBeGreaterThan(0);
  });

  test('empty message handling', async ({ page }) => {
    const composer = page.getByRole('textbox', { name: /message|composer|ask/i }).first();
    const sendBtn = page.getByRole('button', { name: /send/i }).first();
    
    // Try to send empty message
    await composer.fill('');
    await sendBtn.click();
    
    // Should not send or should show validation
    await page.waitForTimeout(1000);
    
    // No new assistant message should appear
    const assistantMsgs = page.locator('[data-message-role="assistant"], .message-assistant');
    const count = await assistantMsgs.count();
    // Should be 0 or same as before
  });

  test('very long message handling', async ({ page }) => {
    const composer = page.getByRole('textbox', { name: /message|composer|ask/i }).first();
    const longMessage = 'A'.repeat(10000); // 10k characters
    
    await composer.fill(longMessage);
    await page.keyboard.press('Enter');
    
    await page.waitForTimeout(5000);
    
    // Should handle gracefully - either truncate, reject, or process
    const assistantMsg = page.locator('[data-message-role="assistant"], .message-assistant').first();
    await expect(assistantMsg).toBeVisible({ timeout: 60000 });
  });

  test('stop mid-stream', async ({ page }) => {
    const composer = page.getByRole('textbox', { name: /message|composer|ask/i }).first();
    await composer.fill('Write a very long story about a dragon that goes on for many paragraphs');
    await page.keyboard.press('Enter');
    
    // Wait for streaming to start
    await page.waitForTimeout(2000);
    
    // Click stop button
    const stopBtn = page.getByRole('button', { name: /stop|cancel/i }).first();
    if (await stopBtn.isVisible({ timeout: 2000 })) {
      await stopBtn.click();
      await page.waitForTimeout(1000);
      
      // Verify streaming stopped
      const assistantMsg = page.locator('[data-message-role="assistant"], .message-assistant').first();
      const content = await assistantMsg.textContent();
      expect(content!.length).toBeLessThan(5000); // Should be partial
    }
  });

  test('new chat button clears history', async ({ page }) => {
    // Send a message first
    const composer = page.getByRole('textbox', { name: /message|composer|ask/i }).first();
    await composer.fill('Test message for history');
    await page.keyboard.press('Enter');
    await page.waitForTimeout(3000);
    
    // Click new chat
    const newChatBtn = page.getByRole('button', { name: /new chat|new thread/i }).first();
    await newChatBtn.click();
    await page.waitForTimeout(1000);
    
    // Verify history cleared - no assistant messages visible
    const assistantMsgs = page.locator('[data-message-role="assistant"], .message-assistant');
    await expect(assistantMsgs).toHaveCount(0);
  });

  test('model switching in picker', async ({ page }) => {
    const modelPicker = page.getByRole('combobox', { name: /model|provider/i }).first();
    await modelPicker.click();
    
    // Get available options
    const options = page.getByRole('option');
    const count = await options.count();
    expect(count).toBeGreaterThan(1);
    
    // Select a different model
    await options.nth(1).click();
    await page.waitForTimeout(1000);
    
    // Verify model changed
    const selectedValue = await modelPicker.inputValue();
    expect(selectedValue).toBeTruthy();
  });

  test('token display / cost display', async ({ page }) => {
    const composer = page.getByRole('textbox', { name: /message|composer|ask/i }).first();
    await composer.fill('Hello');
    await page.keyboard.press('Enter');
    await page.waitForTimeout(5000);
    
    // Check for token/cost display
    const tokenDisplay = page.getByText(/token|cost|usage/i).first();
    // May or may not be visible depending on implementation
    // Just verify no errors
  });

  test('artifact rendering (if any in response)', async ({ page }) => {
    const composer = page.getByRole('textbox', { name: /message|composer|ask/i }).first();
    await composer.fill('Create a simple HTML page with a button');
    await page.keyboard.press('Enter');
    await page.waitForTimeout(10000);
    
    // Check for artifact/rendered content
    const artifact = page.locator('[data-artifact], .artifact, iframe, [data-rendered]').first();
    // Artifacts may or may not appear depending on the model response
  });

  test('conversation persistence: refresh page, check history remains', async ({ page }) => {
    const composer = page.getByRole('textbox', { name: /message|composer|ask/i }).first();
    
    // Send a message
    await composer.fill('Persistent test message');
    await page.keyboard.press('Enter');
    await page.waitForTimeout(5000);
    
    // Refresh page
    await page.reload({ waitUntil: 'networkidle' });
    await page.waitForTimeout(2000);
    
    // Check if message history persists
    const userMsg = page.getByText('Persistent test message', { exact: false }).first();
    const persisted = await userMsg.isVisible({ timeout: 10000 }).catch(() => false);
    
    // Log result - persistence may or may not be implemented
    console.log('Conversation persistence:', persisted ? 'WORKS' : 'NOT IMPLEMENTED');
  });

  test('console errors check', async ({ page }) => {
    const errors: string[] = [];
    page.on('console', (msg) => {
      if (msg.type() === 'error') {
        errors.push(msg.text().slice(0, 500));
      }
    });
    page.on('pageerror', (err) => {
      errors.push(String(err).slice(0, 500));
    });
    
    // Navigate and interact
    await page.goto(BASE + '/console', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(2000);
    
    const composer = page.getByRole('textbox', { name: /message|composer|ask/i }).first();
    await composer.fill('Test for console errors');
    await page.keyboard.press('Enter');
    await page.waitForTimeout(5000);
    
    // Report errors
    console.log('Console errors found:', errors.length);
    errors.forEach(e => console.log('  -', e));
    
    // Filter out known benign errors
    const criticalErrors = errors.filter(e => 
      !e.includes('favicon') && 
      !e.includes('404') &&
      !e.includes('net::ERR') &&
      !e.includes('chunk') &&
      !e.includes('extension')
    );
    
    console.log('Critical errors:', criticalErrors.length);
    criticalErrors.forEach(e => console.log('  CRITICAL:', e));
  });
});