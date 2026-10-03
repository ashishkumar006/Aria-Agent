// Debug: trace what happens when the sessions tab is clicked.
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const networkLog = [];
  page.on('response', (r) => {
    if (r.url().includes('/api/')) {
      networkLog.push(`${r.status()} ${r.request().method()} ${r.url()}`);
    }
  });
  page.on('pageerror', (e) => console.log('PAGEERROR:', e.message));
  page.on('console', (m) => console.log(`[${m.type()}]`, m.text()));

  await page.goto('http://127.0.0.1:8500/', { waitUntil: 'networkidle' });
  await page.waitForTimeout(1000);

  console.log('--- Network on page load ---');
  networkLog.forEach((l) => console.log('  ' + l));
  networkLog.length = 0;

  console.log('\n--- Clicking sessions tab ---');
  await page.click('.tab[data-tab="sessions"]');
  await page.waitForTimeout(3000);

  console.log('\n--- Network after sessions tab ---');
  networkLog.forEach((l) => console.log('  ' + l));

  const content = await page.$eval('#sessTable', (el) => el.innerHTML.slice(0, 200));
  console.log('\n--- sessTable content (first 200 chars) ---');
  console.log(content);

  console.log('\n--- Clicking tools tab ---');
  networkLog.length = 0;
  await page.click('.tab[data-tab="tools"]');
  await page.waitForTimeout(3000);
  networkLog.forEach((l) => console.log('  ' + l));
  const toolsContent = await page.$eval('#toolsTable', (el) => el.innerHTML.slice(0, 200));
  console.log('toolsTable content:', toolsContent);

  await browser.close();
})();
