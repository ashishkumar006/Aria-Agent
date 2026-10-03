// Comprehensive diagnostic: open the Aria UI and test every interactive element.
const { chromium } = require('playwright');
const path = require('path');

(async () => {
  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  const errors = [];
  const failedRequests = [];
  page.on('pageerror', (e) => errors.push('PAGEERROR: ' + e.message));
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push('CONSOLE.ERROR: ' + m.text());
  });
  page.on('requestfailed', (r) => failedRequests.push(`${r.method()} ${r.url()} - ${r.failure()?.errorText}`));

  const results = [];
  const check = (name, ok, detail = '') => {
    results.push({ name, ok, detail });
    console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? ' — ' + detail : ''}`);
  };

  try {
    console.log('\n=== LOAD PAGE ===');
    await page.goto('http://127.0.0.1:8500/', { waitUntil: 'networkidle', timeout: 20000 });
    await page.waitForTimeout(1000);
    check('Page loads', true);
    check('Title is Aria', (await page.title()).includes('Aria'));
    check('Welcome message visible', !!(await page.$('#messages .msg.assistant')));

    console.log('\n=== SIDEBAR ELEMENTS ===');
    for (const id of ['sidebar', 'newChat', 'sessionSearch', 'sessList', 'costToggle', 'schedToggle', 'memToggle', 'statusDot', 'statusText', 'modelBadge']) {
      check(`#${id} present`, !!(await page.$(`#${id}`)));
    }

    console.log('\n=== TABS ===');
    const tabs = ['chat', 'sessions', 'computer', 'analytics', 'tools', 'config'];
    for (const tab of tabs) {
      const btn = await page.$(`.tab[data-tab="${tab}"]`);
      check(`Tab "${tab}" exists`, !!btn);
    }

    console.log('\n=== TOPBAR ===');
    for (const id of ['convTitle', 'tts', 'voiceSel', 'ttsVoice', 'speedSel', 'ttsSpeed', 'ttsSpeedVal', 'menuBtn']) {
      const exists = !!(await page.$(`#${id}`));
      check(`#${id}`, exists);
    }

    console.log('\n=== CHAT PANE ===');
    for (const id of ['pane-chat', 'messages', 'composer', 'input', 'mic', 'send', 'stop', 'suggestions', 'dag', 'dagRefresh', 'runDetail', 'sttBtn', 'audioFile', 'voiceHint', 'convHint']) {
      check(`#${id}`, !!(await page.$(`#${id}`)));
    }

    console.log('\n=== SIDE PANEL ===');
    for (const id of ['sidePanel', 'sideTitle', 'sideClose', 'costPanel', 'schedPanel', 'memPanel', 'costRows', 'costTotals', 'schedForm', 'schedQuery', 'schedWhen', 'schedRows', 'memRows']) {
      check(`#${id}`, !!(await page.$(`#${id}`)));
    }

    console.log('\n=== OTHER PANES ===');
    for (const id of ['pane-sessions', 'sessTable', 'sessRefresh', 'pane-computer', 'cuCap', 'notifList', 'notifRefresh', 'pane-analytics', 'costTable', 'costRefresh', 'modelBars', 'pane-tools', 'toolsTable', 'toolsRefresh', 'pane-config', 'configPre']) {
      check(`#${id}`, !!(await page.$(`#${id}`)));
    }

    console.log('\n=== INTERACTIONS ===');

    // Test session search
    await page.fill('#sessionSearch', 'capital');
    await page.waitForTimeout(500);
    check('Session search accepts input', (await page.inputValue('#sessionSearch')) === 'capital');
    await page.fill('#sessionSearch', '');

    // Test new chat
    await page.click('#newChat');
    await page.waitForTimeout(300);
    const msgsAfterNew = await page.$$eval('#messages .msg', (els) => els.length);
    check('New chat clears messages (welcome remains)', msgsAfterNew >= 1);

    // Test side panel toggles
    for (const [toggle, panel] of [['costToggle', 'costPanel'], ['schedToggle', 'schedPanel'], ['memToggle', 'memPanel']]) {
      await page.click(`#${toggle}`);
      await page.waitForTimeout(400);
      const open = await page.evaluate(() => document.getElementById('sidePanel').classList.contains('open'));
      const panelVisible = await page.evaluate((p) => !document.getElementById(p).hidden, panel);
      check(`Click #${toggle} opens panel (${panel} visible)`, open && panelVisible);
      await page.click('#sideClose');
      await page.waitForTimeout(300);
      const closed = await page.evaluate(() => !document.getElementById('sidePanel').classList.contains('open'));
      check(`#sideClose closes panel`, closed);
    }

    // Test tab switching
    for (const tab of ['sessions', 'analytics', 'tools', 'config', 'computer', 'chat']) {
      await page.click(`.tab[data-tab="${tab}"]`);
      await page.waitForTimeout(500);
      const active = await page.evaluate((t) => {
        const p = document.getElementById('pane-' + t);
        return p && p.classList.contains('active');
      }, tab);
      check(`Tab "${tab}" activates pane`, active);
    }

    console.log('\n=== DATA LOADING ===');

    // Sessions pane
    await page.click('.tab[data-tab="sessions"]');
    await page.waitForTimeout(2000);
    const sessTableContent = await page.$eval('#sessTable', (el) => el.textContent.trim());
    check('Sessions table has content', sessTableContent.length > 10, sessTableContent.slice(0, 80));
    const sessRowCount = await page.$$eval('#sessTable tbody tr', (rows) => rows.length);
    check('Sessions table has rows', sessRowCount > 0, `${sessRowCount} rows`);

    // Sidebar session list
    const sidebarSessCount = await page.$$eval('#sessList .sess', (els) => els.length);
    check('Sidebar session list populated', sidebarSessCount > 0, `${sidebarSessCount} items`);

    // Analytics pane
    await page.click('.tab[data-tab="analytics"]');
    await page.waitForTimeout(2000);
    const costTableContent = await page.$eval('#costTable', (el) => el.textContent.trim());
    check('Cost table loaded', costTableContent.length > 0, costTableContent.slice(0, 80));

    // Tools pane
    await page.click('.tab[data-tab="tools"]');
    await page.waitForTimeout(2000);
    const toolsContent = await page.$eval('#toolsTable', (el) => el.textContent.trim());
    check('Tools table loaded', toolsContent.length > 10, toolsContent.slice(0, 80));

    // Config pane
    await page.click('.tab[data-tab="config"]');
    await page.waitForTimeout(2000);
    const configContent = await page.$eval('#configPre', (el) => el.textContent.trim());
    check('Config loaded', configContent.length > 100, configContent.slice(0, 80));

    // Computer pane
    await page.click('.tab[data-tab="computer"]');
    await page.waitForTimeout(2000);
    const notifContent = await page.$eval('#notifList', (el) => el.textContent.trim());
    check('Notifications list loaded', notifContent.length > 0, notifContent.slice(0, 80));

    console.log('\n=== CHAT END-TO-END ===');
    await page.click('.tab[data-tab="chat"]');
    await page.waitForTimeout(500);
    await page.fill('#input', 'What is 2+2? Reply with just the number.');
    await page.click('#send');
    await page.waitForFunction(
      () => {
        const bubbles = document.querySelectorAll('#messages .msg.assistant .answer');
        if (!bubbles.length) return false;
        const last = bubbles[bubbles.length - 1];
        return last && last.textContent && last.textContent.length > 0 && !last.querySelector('.typing');
      },
      { timeout: 30000 }
    );
    const answer = await page.evaluate(() => {
      const bubbles = document.querySelectorAll('#messages .msg.assistant .answer');
      return bubbles[bubbles.length - 1].textContent.trim();
    });
    check('Chat answer received', answer.length > 0, `"${answer.slice(0, 100)}"`);

    // Screenshot
    await page.screenshot({ path: path.join(__dirname, 'aria-full.png'), fullPage: true });
    console.log('\nFull screenshot: aria-full.png');

    console.log('\n=== FAILED REQUESTS ===');
    if (failedRequests.length === 0) {
      console.log('  (none)');
    } else {
      failedRequests.forEach((r) => console.log('  ' + r));
    }

    console.log('\n=== ERRORS ===');
    if (errors.length === 0) {
      console.log('  (none)');
    } else {
      errors.forEach((e) => console.log('  ' + e));
    }

    const failed = results.filter((r) => !r.ok);
    console.log(`\n=== SUMMARY: ${results.length - failed.length}/${results.length} passed, ${failed.length} failed, ${errors.length} errors ===`);
  } catch (e) {
    console.log('\nTEST CRASHED:', e.message);
    errors.push(e.message);
  }

  await browser.close();
  process.exit(errors.length > 0 ? 1 : 0);
})();
