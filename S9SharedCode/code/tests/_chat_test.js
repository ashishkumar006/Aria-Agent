// End-to-end test: send a chat message and verify the orchestrator returns an answer.
const { chromium } = require('playwright');
const path = require('path');

(async () => {
  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push('PAGEERROR: ' + e.message));
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push('CONSOLE.ERROR: ' + m.text());
  });

  await page.goto('http://127.0.0.1:8500/', { waitUntil: 'networkidle', timeout: 20000 });
  await page.waitForTimeout(1000);

  console.log('Sending chat message: "What is 2+2? Reply with just the number."');
  await page.fill('#input', 'What is 2+2? Reply with just the number.');
  await page.click('#send');

  // Wait for the answer (typing indicator to disappear).
  const start = Date.now();
  try {
    await page.waitForFunction(
      () => {
        const bubbles = document.querySelectorAll('#messages .msg.assistant .answer');
        if (!bubbles.length) return false;
        const last = bubbles[bubbles.length - 1];
        return last && !last.querySelector('.typing');
      },
      { timeout: 60000 }
    );
  } catch (e) {
    console.log('TIMEOUT waiting for answer after 60s');
  }
  const elapsed = ((Date.now() - start) / 1000).toFixed(1);

  const answer = await page.evaluate(() => {
    const bubbles = document.querySelectorAll('#messages .msg.assistant .answer');
    if (!bubbles.length) return '(no bubbles)';
    return bubbles[bubbles.length - 1].textContent.trim();
  });
  console.log(`Answer (after ${elapsed}s): "${answer}"`);

  // Check the agent reasoning log.
  const reasoning = await page.evaluate(() => {
    const bodies = document.querySelectorAll('.thinking-body');
    if (!bodies.length) return '(no reasoning)';
    return bodies[bodies.length - 1].textContent.trim().slice(-500);
  });
  console.log(`Reasoning (last 500 chars):\n${reasoning}`);

  console.log('\nErrors:', errors.length === 0 ? '(none)' : errors.join('\n'));

  await page.screenshot({ path: path.join(__dirname, 'aria-chat-e2e.png'), fullPage: true });
  console.log('Screenshot: aria-chat-e2e.png');

  await browser.close();
  process.exit(answer === '(no answer)' || answer === '(no bubbles)' ? 1 : 0);
})();
