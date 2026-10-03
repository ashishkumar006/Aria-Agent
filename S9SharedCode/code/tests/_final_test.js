// Final test: verify the redesigned UI works end-to-end.
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

  const results = [];
  const check = (name, ok, detail = '') => {
    results.push({ name, ok });
    console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? ' — ' + detail : ''}`);
  };

  try {
    await page.goto('http://127.0.0.1:8500/', { waitUntil: 'networkidle', timeout: 20000 });
    await page.waitForTimeout(1500);
    check('Page loads', true);
    check('Title is Aria', (await page.title()).includes('Aria'));
    check('Welcome message visible', !!(await page.$('#messages .msg.assistant')));

    // Sidebar elements
    for (const id of ['sidebar', 'newChat', 'sessionSearch', 'sessList', 'costToggle', 'schedToggle', 'memToggle', 'statusDot']) {
      check(`#${id}`, !!(await page.$(`#${id}`)));
    }

    // Side panel elements
    for (const id of ['sidePanel', 'sideTitle', 'sideClose', 'costPanel', 'schedPanel', 'memPanel']) {
      check(`#${id}`, !!(await page.$(`#${id}`)));
    }

    // Session search
    await page.fill('#sessionSearch', 'capital');
    await page.waitForTimeout(500);
    check('Session search filters list', (await page.inputValue('#sessionSearch')) === 'capital');
    await page.fill('#sessionSearch', '');

    // Side panel toggles
    for (const [toggle, panel] of [['costToggle', 'costPanel'], ['schedToggle', 'schedPanel'], ['memToggle', 'memPanel']]) {
      await page.click(`#${toggle}`);
      await page.waitForTimeout(400);
      const open = await page.evaluate(() => document.getElementById('sidePanel').classList.contains('open'));
      check(`#${toggle} opens panel`, open);
      await page.click('#sideClose');
      await page.waitForTimeout(300);
      const closed = await page.evaluate(() => !document.getElementById('sidePanel').classList.contains('open'));
      check(`#sideClose closes panel`, closed);
    }

    // Sidebar session list loads
    await page.waitForTimeout(2000);
    const sessCount = await page.$$eval('#sessList .sess', (els) => els.length);
    check('Sidebar session list populated', sessCount > 0, `${sessCount} items`);

    // Chat end-to-end
    await page.fill('#input', 'What is 3+5? Just the number.');
    await page.click('#send');
    await page.waitForFunction(
      () => {
        const bubbles = document.querySelectorAll('#messages .msg.assistant .answer');
        if (!bubbles.length) return false;
        const last = bubbles[bubbles.length - 1];
        return last && !last.querySelector('.typing');
      },
      { timeout: 60000 }
    );
    const answer = await page.evaluate(() => {
      const bubbles = document.querySelectorAll('#messages .msg.assistant .answer');
      return bubbles[bubbles.length - 1].textContent.trim();
    });
    check('Chat answer received', answer.length > 0, `"${answer.slice(0, 60)}"`);

    // Screenshot the final state
    await page.screenshot({ path: path.join(__dirname, 'aria-final.png'), fullPage: true });
    console.log('\nFull screenshot: aria-final.png');

    // Screenshot just the sidebar
    const sidebar = await page.$('.sidebar');
    if (sidebar) await sidebar.screenshot({ path: path.join(__dirname, 'aria-sidebar.png') });
    console.log('Sidebar screenshot: aria-sidebar.png');

    const failed = results.filter((r) => !r.ok);
    console.log(`\n=== SUMMARY: ${results.length - failed.length}/${results.length} passed, ${errors.length} errors ===`);
    if (errors.length) errors.forEach((e) => console.log('  ' + e));
  } catch (e) {
    console.log('TEST CRASHED:', e.message);
  }

  await browser.close();
})();
