import { test, expect, Page } from '@playwright/test';

const BASE = 'http://localhost:8500';
const REPORT: string[] = [];

function note(cat: string, msg: string) {
  REPORT.push(`[${cat}] ${msg}`);
}

test.describe('Console UI probe - fast scenarios', () => {
  test('scenario 2: very short query (1-2 words)', async ({ page }) => {
    note('s2', '=== Scenario 2: Very short query ===');
    const consoleErrors: string[] = [];
    page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text().slice(0, 200)); });
    page.on('pageerror', (e) => consoleErrors.push(String(e).slice(0, 200)));
    
    await page.goto(BASE + '/research', { waitUntil: 'networkidle', timeout: 30000 });
    await page.waitForTimeout(2000);
    
    const textarea = page.locator('textarea[aria-label="Research topic"]').first();
    const sendBtn = page.locator('button:has-text("Research")').first();
    const newTopicBtn = page.locator('button:has-text("New topic")').first();
    
    // 1-word query
    await textarea.fill('quantum');
    await page.waitForTimeout(500);
    const disabled1 = await sendBtn.getAttribute('disabled');
    note('s2', `1-word query "quantum": Research button disabled=${disabled1} (null means not disabled)`);
    
    // No client-side validation for short queries - check if it just submits
    if (!disabled1) {
      await sendBtn.click();
      note('s2', 'clicked Research with 1-word query');
      try {
        await expect(page.locator('text=/researching/i')).toBeVisible({ timeout: 10000 });
        note('s2', '1-word query accepted - research started');
      } catch {
        note('s2', '1-word query: no researching indicator within 10s');
      }
      // Stop it quickly
      const stopBtn = page.locator('button:has-text("Stop")').first();
      if (await stopBtn.count() && !(await stopBtn.getAttribute('disabled'))) {
        await stopBtn.click();
        await page.waitForTimeout(1000);
        note('s2', 'stopped 1-word research');
      }
    }
    
    await page.waitForTimeout(3000);
    
    // 2-word query
    await newTopicBtn.click();
    await page.waitForTimeout(500);
    await textarea.fill('quantum computing');
    await page.waitForTimeout(500);
    const disabled2 = await sendBtn.getAttribute('disabled');
    note('s2', `2-word query "quantum computing": Research button disabled=${disabled2}`);
    
    if (consoleErrors.length) note('s2', `console errors: ${JSON.stringify(consoleErrors.slice(0,3))}`);
  });

  test('scenario 3: empty query', async ({ page }) => {
    note('s3', '=== Scenario 3: Empty query ===');
    await page.goto(BASE + '/research', { waitUntil: 'networkidle', timeout: 30000 });
    await page.waitForTimeout(2000);
    
    const textarea = page.locator('textarea[aria-label="Research topic"]').first();
    const sendBtn = page.locator('button:has-text("Research")').first();
    
    await textarea.fill('');
    await page.waitForTimeout(500);
    const disabled = await sendBtn.getAttribute('disabled');
    note('s3', `empty query: Research button disabled=${disabled}`);
    
    await sendBtn.click({ trial: true }).catch(() => {});
    await page.waitForTimeout(500);
    
    // Check the note/status area
    const statusArea = await page.locator('.text-zinc-muted').first().textContent();
    note('s3', `status note text: ${statusArea}`);
    
    // Check if "type a topic first" appears
    const hasValidation = await page.locator('text=/type a topic/i').count();
    note('s3', `validation "type a topic first": ${hasValidation > 0}`);
  });

  test('scenario 5: Runs/Sessions list - zombie sessions', async ({ page }) => {
    note('s5', '=== Scenario 5: Runs list ===');
    await page.goto(BASE + '/runs', { waitUntil: 'networkidle', timeout: 30000 });
    await page.waitForTimeout(3000);
    
    const title = await page.title();
    note('s5', `page title: ${title}`);
    
    // Count runs list items (sidebar buttons with border)
    const runRows = await page.locator('div[class*="border"] > div > div > span').count();
    note('s5', `approx run rows: ${runRows}`);
    
    // Check for zombie indicators
    const noGraphPill = await page.locator('text=no graph').count();
    const noNodesPill = await page.locator('text=no nodes').count();
    const ageUnknown = await page.locator('text=age unknown').count();
    const zeroNodes = await page.locator('text=/0 nodes/').count();
    note('s5', `no graph pills: ${noGraphPill}, no nodes pills: ${noNodesPill}`);
    note('s5', `age unknown: ${ageUnknown}, 0 nodes: ${zeroNodes}`);
    
    // Get the totals bar
    const totalsBar = await page.locator('div.border-white\\/10.bg-white\\/5').first().textContent();
    note('s5', `totals bar: ${totalsBar}`);
    
    // Check for $0.0000
    const zeroDollar = await page.locator('text=/\$0\.0000/').count();
    note('s5', `cells with $0.0000: ${zeroDollar}`);
    
    // Check for empty topics
    const emptyTopic = await page.locator('text=(untitled)').count();
    note('s5', `empty (untitled) topics: ${emptyTopic}`);
    
    // Capture run row texts
    const rowEls = await page.locator('button[class*="border"] > div > div > span').all();
    const rowTexts = [];
    for (let i = 0; i < Math.min(rowEls.length, 20); i++) {
      const t = await rowEls[i].textContent();
      if (t) rowTexts.push(t.replace(/\s+/g, ' ').slice(0, 80));
    }
    note('s5', `run rows: ${JSON.stringify(rowTexts)}`);
  });

  test('scenario 8: Computer Use view', async ({ page }) => {
    note('s8', '=== Scenario 8: Computer Use view ===');
    await page.goto(BASE + '/computer', { waitUntil: 'networkidle', timeout: 30000 });
    await page.waitForTimeout(2000);
    
    const title = await page.title();
    note('s8', `page title: ${title}`);
    
    // Stat cards
    const stats = await page.locator('.instrument-card').all();
    note('s8', `stat cards: ${stats.length}`);
    for (let i = 0; i < stats.length; i++) {
      const t = await stats[i].textContent();
      note('s8', `stat ${i}: ${t?.replace(/\s+/g, ' ')}`);
    }
    
    // Mode pill
    const modeText = await page.locator('text=/live mode|dry-run mode|disabled/i').textContent();
    note('s8', `mode: ${modeText}`);
    
    // Tabs
    const tabs = await page.locator('button', { hasText: /Approvals|Audit trail|Recordings/ }).all();
    note('s8', `tabs: ${tabs.length}`);
    for (const t of tabs) {
      const text = await t.textContent();
      note('s8', `  tab: ${text?.trim()}`);
    }
    
    // Capabilities
    const capText = await page.locator('main').textContent();
    const hasEnabled = capText?.includes('switched OFF') || capText?.includes('dry-run');
    note('s8', `mode info shown: ${hasEnabled}`);
    
    // Check if pending approvals show
    const pendingText = await page.locator('text=/awaiting you|pending/i').first().textContent();
    note('s8', `pending status: ${pendingText}`);
    
    // Test Audit trail tab
    const auditTab = page.locator('button', { hasText: 'Audit trail' });
    if (await auditTab.count()) {
      await auditTab.click();
      await page.waitForTimeout(1500);
      const auditContent = await page.locator('main').textContent();
      const hasEmpty = auditContent?.includes('No gated actions');
      const hasAudit = auditContent?.includes('AUDIT TRAIL');
      note('s8', `audit: empty=${hasEmpty}, header=${hasAudit}`);
    }
    
    // Test Recordings tab
    const recordingsTab = page.locator('button', { hasText: 'Recordings' });
    if (await recordingsTab.count()) {
      await recordingsTab.click();
      await page.waitForTimeout(1500);
      const recContent = await page.locator('main').textContent();
      const hasEmpty = recContent?.includes('No recordings');
      note('s8', `recordings: empty=${hasEmpty}`);
    }
  });

  test('scenario 10: Ledger/Analytics view', async ({ page }) => {
    note('s10', '=== Scenario 10: Ledger view ===');
    await page.goto(BASE + '/ledger', { waitUntil: 'networkidle', timeout: 30000 });
    await page.waitForTimeout(2000);
    
    const title = await page.title();
    note('s10', `page title: ${title}`);
    
    // Stat cards
    const stats = await page.locator('.instrument-card').all();
    note('s10', `stat cards: ${stats.length}`);
    for (let i = 0; i < stats.length; i++) {
      const t = await stats[i].textContent();
      note('s10', `stat ${i}: ${t?.replace(/\s+/g, ' ')}`);
    }
    
    // Check for cost data inconsistencies
    const mainText = await page.locator('main').textContent();
    const hasZeroDollar = mainText?.includes('$0.0000');
    const hasNA = mainText?.includes('n/a');
    const hasUnavailable = mainText?.includes('cost data unavailable');
    note('s10', `shows $0.0000: ${hasZeroDollar}, n/a: ${hasNA}, unavailable: ${hasUnavailable}`);
    
    // Check spend table rows
    const tableRows = await page.locator('tbody tr').count();
    note('s10', `spend table rows: ${tableRows}`);
    for (let i = 0; i < Math.min(tableRows, 5); i++) {
      const t = await page.locator('tbody tr').nth(i).textContent();
      note('s10', `row ${i}: ${t?.replace(/\s+/g, ' ').slice(0, 100)}`);
    }
    
    // Check node health section
    const nodeHealthHeader = await page.locator('text=NODE RELIABILITY').count();
    note('s10', `node reliability section: ${nodeHealthHeader > 0}`);
    
    // Check scope buttons
    const scopeBtns = await page.locator('button', { hasText: /All time|One conversation/ }).all();
    note('s10', `scope buttons: ${scopeBtns.length}`);
    
    // Try /analytics route (legacy - should not exist in SPA)
    await page.goto(BASE + '/analytics', { waitUntil: 'networkidle', timeout: 10000 }).catch(() => {});
    const analyticsTitle = await page.title();
    note('s10', `/analytics route title: ${analyticsTitle}`);
  });

  test.afterAll(() => {
    console.log('\n=== PROBE REPORT (fast scenarios) ===\n');
    for (const line of REPORT) console.log(line);
    console.log(`\nTotal: ${REPORT.length} items`);
  });
});

// Also write report to file
test.afterAll(() => {
  const fs = require('fs');
  fs.writeFileSync('/Users/AISHWA~1/AppData/Local/Temp/kilo/probe_report.md',
    REPORT.join('\n'), 'utf8');
});
