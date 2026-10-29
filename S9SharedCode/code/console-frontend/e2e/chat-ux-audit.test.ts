import { test } from '@playwright/test';

const BASE = 'http://localhost:8500';

test.describe('Chat UX Audit', () => {
  test.beforeEach(async ({ page }) => {
    page.on('crash', () => console.log('!!! BROWSER CRASH !!!'));
    page.on('console', msg => {
      if (msg.type() === 'error' || msg.type() === 'warning') {
        console.log(`[console:${msg.type()}] ${msg.text().slice(0, 500)}`);
      }
    });
    page.on('pageerror', err => console.log('[pageerror]', String(err).slice(0, 500)));
    page.on('requestfailed', req => console.log('[requestfailed]', req.url(), req.failure()?.errorText));

    await page.goto(BASE + '/console', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(2000);
  });

  async function waitForChatDone(page: any) {
    const done = page.locator('text=/done in|stopped|could not save|chat failed/i');
    try {
      await done.first().waitFor({ state: 'visible', timeout: 25000 });
    } catch {
      console.log('  (no done/stopped meta found within 25s)');
    }
  }

  // ── Scenario 1: Normal chat ──
  test('1. Normal chat message and response stream', async ({ page }) => {
    console.log('\n=== S1: Normal chat ===');
    const composer = page.locator('textarea[aria-label="Message Aria"]').first();
    await composer.fill('Hello Aria, can you give me a one sentence greeting?');
    await page.keyboard.press('Enter');

    await page.waitForTimeout(2000);
    const s1Bubbles = await page.locator('.msg-in').count();
    console.log('S1 bubbles:', s1Bubbles);

    await waitForChatDone(page);

    const allText = await page.locator('.msg-in').last().textContent();
    console.log('S1 last bubble:', allText?.slice(0, 300));
    console.log('S1 DONE');
  });

  // ── Scenario 2: Empty message ──
  test('2. Empty message send', async ({ page }) => {
    console.log('\n=== S2: Empty message ===');
    const composer = page.locator('textarea[aria-label="Message Aria"]').first();
    const sendBtn = page.getByRole('button', { name: /send message/i }).first();

    await composer.fill('   ');
    await sendBtn.click();
    await page.waitForTimeout(1000);
    const hint = await page.locator('.text-amber-300, .text-red-300').first().textContent().catch(() => 'N/A');
    const bubbles = await page.locator('.msg-in').count();
    console.log('S2 whitespace hint:', hint);
    console.log('S2 bubbles:', bubbles);

    await composer.fill('');
    await sendBtn.click();
    await page.waitForTimeout(500);
    const emptyBubbles = await page.locator('.msg-in').count();
    console.log('S2 empty bubbles:', emptyBubbles);
    console.log('S2 DONE');
  });

  // ── Scenario 3: Very long message ──
  test('3. Very long message (5500 chars)', async ({ page }) => {
    console.log('\n=== S3: Long message ===');
    const composer = page.locator('textarea[aria-label="Message Aria"]').first();
    const sendBtn = page.getByRole('button', { name: /send message/i }).first();
    const msg = 'This is a very long message. '.repeat(160);
    console.log('S3 length:', msg.length);

    await composer.fill(msg);
    await page.waitForTimeout(500);
    const counter = await page.locator('#chat-len').first().textContent().catch(() => 'N/A');
    console.log('S3 counter:', counter);

    const bubblesBefore = await page.locator('.msg-in').count();
    console.log('S3 bubbles before send:', bubblesBefore);

    await sendBtn.click({ timeout: 5000 }).catch(e => console.log('S3 click:', e.message.slice(0, 100)));
    await page.waitForTimeout(500);

    // Check hint via evaluate to avoid locator timeout
    const hint = await page.evaluate(() => {
      const red = document.querySelector('.text-red-300');
      const amber = document.querySelector('.text-amber-300');
      return (red || amber || document.querySelector('.text-zinc-400'))?.textContent?.slice(0, 200) || 'not found';
    });
    console.log('S3 hint after send:', hint);

    const bubblesAfter = await page.locator('.msg-in').count();
    console.log('S3 bubbles after send:', bubblesAfter);

    const taValue = await composer.inputValue();
    console.log('S3 textarea still has text:', taValue.length > 0, '(len:', taValue.length + ')');
    console.log('S3 DONE');
  });

  // ── Scenario 4: Double-click send while streaming ──
  test('4. Double-click send while streaming', async ({ page }) => {
    console.log('\n=== S4: Double-click while streaming ===');
    const composer = page.locator('textarea[aria-label="Message Aria"]').first();
    const sendBtn = page.getByRole('button', { name: /send (message|queue)/i }).first();

    await composer.fill('Write a long paragraph about the benefits of meditation, please make it detailed.');
    await sendBtn.click();
    await page.waitForTimeout(2500);

    const btnText = await sendBtn.textContent();
    console.log('S4 button text during streaming:', btnText?.trim());

    await sendBtn.dblclick();
    await page.waitForTimeout(1000);

    const queueText = await page.evaluate(() => {
      const els = Array.from(document.querySelectorAll('*'));
      const found = els.find(e => (e.textContent || '').includes('queued'));
      return found ? found.textContent?.trim().slice(0, 200) : 'not found';
    });
    console.log('S4 queue text:', queueText);

    const userBubbles = await page.locator('.msg-in').filter({ has: page.locator('text=You') }).count();
    console.log('S4 user bubbles:', userBubbles);

    await waitForChatDone(page);
    console.log('S4 DONE');
  });

  // ── Scenario 5: Refresh mid-response ──
  test('5. Refresh page mid-response', async ({ page }) => {
    console.log('\n=== S5: Refresh mid-response ===');
    const composer = page.locator('textarea[aria-label="Message Aria"]').first();

    await composer.fill('Write a long detailed story about a space explorer discovering a new planet');
    await page.keyboard.press('Enter');
    await page.waitForTimeout(3000);

    const beforeRefresh = await page.locator('.msg-in').count();
    console.log('S5 bubbles before refresh:', beforeRefresh);

    await page.reload({ waitUntil: 'networkidle', timeout: 30000 });
    await page.waitForTimeout(2000);

    const afterRefresh = await page.locator('.msg-in').count();
    console.log('S5 bubbles after refresh:', afterRefresh);

    const pending = await page.locator('text=/working/i').count();
    const stillRunning = await page.locator('text=/still running/i').count();
    console.log('S5 pending:', pending, 'still running:', stillRunning);

    await waitForChatDone(page);
    await page.waitForTimeout(5000);
    const finalBubbles = await page.locator('.msg-in').count();
    console.log('S5 final bubbles:', finalBubbles);

    const finalText = await page.locator('.msg-in').last().textContent().catch(() => 'N/A');
    console.log('S5 final bubble:', finalText?.slice(0, 300));
    console.log('S5 DONE');
  });

  // ── Scenario 6: New chat while streaming ──
  test('6. New chat while a response is streaming', async ({ page }) => {
    console.log('\n=== S6: New chat while streaming ===');
    const composer = page.locator('textarea[aria-label="Message Aria"]').first();
    const newChatBtn = page.getByRole('button', { name: /start new chat/i }).first();

    await composer.fill('Tell me a long story about a dragon who learns to cook');
    await page.keyboard.press('Enter');
    await page.waitForTimeout(2000);

    const before = await page.locator('.msg-in').count();
    console.log('S6 bubbles before new chat:', before);

    await newChatBtn.click();
    await page.waitForTimeout(3000);

    const after = await page.locator('.msg-in').count();
    console.log('S6 bubbles after new chat:', after);

    const notice = await page.locator('.msg-in').filter({ hasText: /new chat|continues/i }).first().textContent().catch(() => 'not found');
    console.log('S6 notice:', notice?.slice(0, 300));

    await page.waitForTimeout(8000);
    console.log('S6 DONE');
  });

  // ── Scenario 7: Rapid fire messages ──
  test('7. Rapid fire messages', async ({ page }) => {
    console.log('\n=== S7: Rapid fire ===');
    const composer = page.locator('textarea[aria-label="Message Aria"]').first();

    for (let i = 1; i <= 3; i++) {
      await composer.fill(`Quick message number ${i} - what is ${i}+${i}?`);
      await page.keyboard.press('Enter');
      await page.waitForTimeout(100);
    }

    const userBubbles = await page.locator('.msg-in').filter({ has: page.locator('text=You') }).count();
    console.log('S7 user bubbles:', userBubbles);

    await page.waitForTimeout(2000);
    const queueText = await page.evaluate(() => {
      const els = Array.from(document.querySelectorAll('*'));
      const found = els.find(e => {
        const t = (e.textContent || '').toLowerCase();
        return t.includes('queued') && t.includes('clear') === false && t.length < 60;
      });
      return found ? found.textContent?.trim().slice(0, 200) : 'not found';
    });
    console.log('S7 queue text:', queueText);

    await waitForChatDone(page);
    await page.waitForTimeout(12000);
    const total = await page.locator('.msg-in').count();
    console.log('S7 total bubbles:', total);
    console.log('S7 DONE');
  });

  // ── Scenario 8: Ctrl+K command palette ──
  test('8. Ctrl+K command palette', async ({ page }) => {
    console.log('\n=== S8: Ctrl+K ===');
    await page.keyboard.press('Control+k');
    await page.waitForTimeout(1000);

    const paletteVisible = await page.locator('.fixed.z-\\[300\\]').isVisible();
    console.log('S8 palette visible:', paletteVisible);

    const inputVisible = await page.locator('input[placeholder*="Jump to"]').isVisible();
    console.log('S8 input visible:', inputVisible);

    if (inputVisible) {
      await page.locator('input[placeholder*="Jump to"]').fill('Runs');
      await page.waitForTimeout(500);
      const items = await page.locator('[cmdk-item]').count();
      console.log('S8 items after "Runs":', items);
      const labels: string[] = [];
      for (let i = 0; i < Math.min(items, 10); i++) {
        const t = await page.locator('[cmdk-item]').nth(i).textContent();
        if (t) labels.push(t.trim());
      }
      console.log('S8 labels:', JSON.stringify(labels));
    }

    await page.keyboard.press('Escape');
    await page.waitForTimeout(500);
    const afterEscape = await page.locator('.fixed.z-\\[300\\]').isVisible();
    console.log('S8 after Escape:', afterEscape);

    await page.keyboard.press('Control+k');
    await page.waitForTimeout(500);
    const reopen = await page.locator('.fixed.z-\\[300\\]').isVisible();
    console.log('S8 reopen:', reopen);
    console.log('S8 DONE');
  });

  // ── Scenario 9: Console errors ──
  test('9. Console error monitoring', async ({ page }) => {
    console.log('\n=== S9: Console errors ===');
    const errors: string[] = [];
    const logs: string[] = [];
    page.on('console', msg => {
      logs.push(`[${msg.type()}] ${msg.text().slice(0, 300)}`);
      if (msg.type() === 'error' || msg.type() === 'warning') errors.push(`[${msg.type()}] ${msg.text().slice(0, 300)}`);
    });
    page.on('pageerror', err => errors.push('[pageerror] ' + String(err).slice(0, 300)));
    page.on('requestfailed', req => errors.push(`[rfailed] ${req.url()} ${req.failure()?.errorText}`));

    await page.goto(BASE + '/console', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(2000);

    const composer = page.locator('textarea[aria-label="Message Aria"]').first();
    await composer.fill('What is 2+2?');
    await page.keyboard.press('Enter');

    await waitForChatDone(page);
    await page.waitForTimeout(5000);

    const failedReqs = logs.filter(l => l.includes('404') || l.includes('500') || l.includes('403') || l.includes('401'));
    console.log('S9 logs count:', logs.length);
    console.log('S9 logs:', JSON.stringify(logs));
    console.log('S9 errors:', errors.length === 0 ? 'NONE' : errors);
    console.log('S9 failed reqs:', failedReqs);
    console.log('S9 DONE');
  });

  // ── Scenario 10: Navigation during chat ──
  test('10. Navigate between views during a chat', async ({ page }) => {
    console.log('\n=== S10: Navigate during chat ===');
    const composer = page.locator('textarea[aria-label="Message Aria"]').first();

    await composer.fill('Write a detailed essay about quantum computing and its implications for cryptography');
    await page.keyboard.press('Enter');
    await page.waitForTimeout(2000);

    console.log('S10 bubbles before nav:', await page.locator('.msg-in').count());

    await page.getByRole('link', { name: 'Runs' }).click();
    await page.waitForTimeout(2000);
    // Check if we're on the Runs page by checking the URL
    console.log('S10 URL after Runs click:', page.url());
    // Check for any visible content on Runs page
    const runsContent = await page.evaluate(() => {
      const main = document.querySelector('main');
      return main ? main.textContent?.slice(0, 200) : 'no main';
    });
    console.log('S10 Runs page content:', runsContent);

    await page.getByRole('link', { name: 'Memory' }).click();
    await page.waitForTimeout(2000);
    console.log('S10 URL after Memory click:', page.url());
    const memContent = await page.evaluate(() => {
      const main = document.querySelector('main');
      return main ? main.textContent?.slice(0, 200) : 'no main';
    });
    console.log('S10 Memory page content:', memContent);

    // Navigate back to Chat via rail link
    await page.getByRole('link', { name: 'Chat' }).click();
    await page.waitForTimeout(3000);

    const afterNav = await page.locator('.msg-in').count();
    console.log('S10 bubbles after nav back:', afterNav);
    const pendingAfter = await page.locator('text=/working/i').count();
    console.log('S10 pending after nav:', pendingAfter);

    await waitForChatDone(page);
    await page.waitForTimeout(5000);
    console.log('S10 final bubbles:', await page.locator('.msg-in').count());
    console.log('S10 DONE');
  });
});
