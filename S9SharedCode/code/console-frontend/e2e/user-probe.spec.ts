import { test, expect, Page } from '@playwright/test';

const BASE = 'http://localhost:8500';
const REPORT: string[] = [];

function note(cat: string, severity: string, msg: string) {
  REPORT.push(`[${cat}][${severity}] ${msg}`);
}

test.describe('User QA probe - all console views', () => {
  test.afterAll(() => {
    console.log('\n=== USER QA PROBE REPORT ===\n');
    for (const line of REPORT) console.log(line);
    console.log(`\nTotal: ${REPORT.length} items`);
  });

  test('1. Navigate to Code view - create a new file', async ({ page }) => {
    await page.goto(BASE + '/code', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(2000);

    // Check the explorer loads
    const explorer = page.locator('aside[aria-label="Explorer"]');
    const hasExplorer = await explorer.isVisible({ timeout: 5000 }).catch(() => false);
    note('code', 'INFO', `Explorer visible: ${hasExplorer}`);

    // Check for workspace name/title
    const title = await page.title();
    note('code', 'INFO', `Page title: ${title}`);

    // Try to find "New file" button
    const newFileBtn = page.locator('button[aria-label="New file"]').first();
    const hasNewFileBtn = await newFileBtn.isVisible({ timeout: 5000 }).catch(() => false);
    note('code', 'INFO', `New file button visible: ${hasNewFileBtn}`);

    if (hasNewFileBtn) {
      await newFileBtn.click({ timeout: 5000 });
      await page.waitForTimeout(500);

      const input = page.locator('input[aria-label="New file name"]').first();
      const hasInput = await input.isVisible({ timeout: 3000 }).catch(() => false);
      note('code', 'INFO', `New file name input visible: ${hasInput}`);

      if (hasInput) {
        await input.fill('qa-probe-test.ts');
        await page.keyboard.press('Enter');
        await page.waitForTimeout(1500);

        // Check if file opened in editor
        const editor = page.locator('[aria-label^="Editor for"]').first();
        const hasEditor = await editor.isVisible({ timeout: 5000 }).catch(() => false);
        note('code', 'INFO', `Editor opened for new file: ${hasEditor}`);
      }
    }

    // Check console errors
    const errors: string[] = [];
    page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text().slice(0, 200)); });
    page.on('pageerror', (e) => errors.push(String(e).slice(0, 200)));
    note('code', 'INFO', `Console errors: ${errors.length ? errors.slice(0, 3).join(' | ') : 'none'}`);
  });

  test('2. Monaco editor - syntax highlighting, file operations', async ({ page }) => {
    await page.goto(BASE + '/code', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(3000);

    // Find a file to open
    const fileBtn = page.locator('aside[aria-label="Explorer"] button:has(span.truncate):not([aria-expanded])').first();
    const fileCount = await fileBtn.count();
    note('monaco', 'INFO', `Available files in explorer: ${fileCount}`);

    if (fileCount > 0) {
      const fileName = await fileBtn.getAttribute('aria-label') || await fileBtn.textContent();
      note('monaco', 'INFO', `First file: ${fileName?.slice(0, 80)}`);

      await fileBtn.click({ timeout: 5000 });
      await page.waitForTimeout(3000);

      // Check editor loaded
      const editorHost = page.locator('[data-editor-host]').first();
      const hasEditor = await editorHost.isVisible({ timeout: 10000 }).catch(() => false);
      note('monaco', hasEditor ? 'INFO' : 'HIGH', `Monaco editor loaded: ${hasEditor}`);

      if (hasEditor) {
        // Check for syntax highlighting (tokenizer spans)
        const highlights = await page.evaluate(() => {
          const pre = document.querySelector('pre');
          if (!pre) return { found: false };
          const spans = pre.querySelectorAll('span[class*="text-"]');
          const classes = new Set<string>();
          spans.forEach(s => s.className.split(/\s+/).forEach(c => { if (c.startsWith('text-')) classes.add(c); }));
          return { found: true, tokenClasses: [...classes].slice(0, 10) };
        });
        note('monaco', 'INFO', `Syntax highlighting tokens: ${JSON.stringify(highlights)}`);

        // Check for tab in view
        const tabs = await page.locator('[role="tab"]').count();
        note('monaco', 'INFO', `Open tabs: ${tabs}`);

        // Check status bar
        const statusBar = page.locator('.text-[10px]').first();
        const hasStatusBar = await statusBar.isVisible({ timeout: 3000 }).catch(() => false);
        note('monaco', 'INFO', `Status bar visible: ${hasStatusBar}`);

        // Check minimap (if present)
        const minimap = page.locator('.minimap, [class*="minimap"]').first();
        const hasMinimap = await minimap.isVisible({ timeout: 2000 }).catch(() => false);
        note('monaco', 'INFO', `Minimap visible: ${hasMinimap}`);

        // Check command palette access
        await page.keyboard.press('Control+Shift+P');
        await page.waitForTimeout(500);
        const cmdPalette = page.locator('[aria-label="Command"]').first();
        const hasPalette = await cmdPalette.isVisible({ timeout: 3000 }).catch(() => false);
        note('monaco', 'INFO', `Command palette (Ctrl+Shift+P): ${hasPalette}`);
        if (hasPalette) await page.keyboard.press('Escape');

        // Test find in file
        await page.keyboard.press('Control+f');
        await page.waitForTimeout(500);
        const findBox = page.locator('[aria-label="Find"]').first();
        const hasFind = await findBox.isVisible({ timeout: 3000 }).catch(() => false);
        note('monaco', 'INFO', `Find in file (Ctrl+F): ${hasFind}`);
        if (hasFind) await page.keyboard.press('Escape');
      }
    }

    const errors: string[] = [];
    page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text().slice(0, 200)); });
    page.on('pageerror', (e) => errors.push(String(e).slice(0, 200)));
    note('monaco', 'INFO', `Console errors: ${errors.length ? errors.slice(0, 3).join(' | ') : 'none'}`);
  });

  test('3. Settings view - configuration issues', async ({ page }) => {
    await page.goto(BASE + '/settings', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(2000);

    // Check environment sections
    const envSections = ['agent', 'gateway', 'mcp tools', 'sessions', 'schedules', 'templates'];
    for (const s of envSections) {
      const visible = await page.getByText(s, { exact: false }).first().isVisible({ timeout: 3000 }).catch(() => false);
      note('settings', 'INFO', `Environment section "${s}": ${visible}`);
    }

    // Check API token section
    const tokenLabel = await page.getByText('API TOKEN', { exact: false }).first().isVisible({ timeout: 5000 }).catch(() => false);
    note('settings', 'INFO', `API TOKEN section visible: ${tokenLabel}`);

    const tokenInput = page.getByRole('textbox', { name: 'API bearer token' });
    const hasTokenInput = await tokenInput.isVisible({ timeout: 5000 }).catch(() => false);
    note('settings', 'INFO', `Token input visible: ${hasTokenInput}`);

    const saveBtn = page.getByRole('button', { name: 'Save' });
    const hasSaveBtn = await saveBtn.isVisible({ timeout: 5000 }).catch(() => false);
    note('settings', 'INFO', `Save button visible: ${hasSaveBtn}`);

    // Check config dump
    const configBox = page.locator('[class*="font-mono"]').first();
    const hasConfig = await configBox.isVisible({ timeout: 5000 }).catch(() => false);
    note('settings', 'INFO', `Config dump visible: ${hasConfig}`);

    // Check config content length
    const configText = await configBox.textContent();
    note('settings', configText && configText.length > 100 ? 'INFO' : 'MEDIUM',
      `Config dump content length: ${configText?.length || 0} chars`);

    // Check config filter
    const filterInput = page.getByPlaceholder('filter config…');
    const hasFilter = await filterInput.isVisible({ timeout: 5000 }).catch(() => false);
    note('settings', 'INFO', `Config filter visible: ${hasFilter}`);

    // Check gateway links
    const overviewLink = page.getByRole('link', { name: 'overview' });
    const systemLink = page.getByRole('link', { name: 'system' });
    const hasOverview = await overviewLink.isVisible({ timeout: 3000 }).catch(() => false);
    const hasSystem = await systemLink.isVisible({ timeout: 3000 }).catch(() => false);
    note('settings', 'INFO', `Gateway overview link visible: ${hasOverview}, href: ${await overviewLink.getAttribute('href') || 'none'}`);
    note('settings', 'INFO', `Gateway system link visible: ${hasSystem}, href: ${await systemLink.getAttribute('href') || 'none'}`);

    // Check templates count
    const tplPill = await page.locator('text=templates').locator('..').locator('text=/up|down/').textContent();
    note('settings', 'INFO', `Templates status: ${tplPill}`);

    // Save a test token and verify it persists
    const testToken = 'qa-probe-token-' + Date.now();
    await tokenInput.fill(testToken);
    await saveBtn.click();
    await page.waitForTimeout(2000);

    await page.reload({ waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(2000);
    const hasClearBtn = await page.getByRole('button', { name: 'Clear' }).isVisible({ timeout: 10000 }).catch(() => false);
    note('settings', hasClearBtn ? 'INFO' : 'MEDIUM', `Token persisted after reload (Clear button visible): ${hasClearBtn}`);

    // Clean up - clear the token
    if (hasClearBtn) {
      await page.getByRole('button', { name: 'Clear' }).click();
      await page.waitForTimeout(1000);
    }

    const errors: string[] = [];
    page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text().slice(0, 200)); });
    page.on('pageerror', (e) => errors.push(String(e).slice(0, 200)));
    note('settings', 'INFO', `Console errors: ${errors.length ? errors.slice(0, 3).join(' | ') : 'none'}`);
  });

  test('4. Templates view - check for template functionality', async ({ page }) => {
    // First check if there's a navigable Templates route
    await page.goto(BASE + '/templates', { waitUntil: 'networkidle', timeout: 10000 }).catch(() => {});
    const title = await page.title();
    note('templates', 'INFO', `Route /templates - page title: ${title}`);

    // Check if templates appears in the rail navigation
    const railTemplate = page.getByRole('link', { name: 'Templates' }).first();
    const hasRailTemplate = await railTemplate.isVisible({ timeout: 3000 }).catch(() => false);
    note('templates', hasRailTemplate ? 'INFO' : 'LOW', `Templates in rail navigation: ${hasRailTemplate}`);

    // Check command palette for templates
    await page.keyboard.press('Control+K');
    await page.waitForTimeout(500);
    const palette = page.getByRole('dialog').first();
    const hasPalette = await palette.isVisible({ timeout: 3000 }).catch(() => false);
    note('templates', 'INFO', `Command palette openable: ${hasPalette}`);
    if (hasPalette) {
      await page.keyboard.type('template');
      await page.waitForTimeout(500);
      // Look for template-related commands
      const cmdItems = await page.locator('[role="option"]').all();
      const cmdTexts = [];
      for (let i = 0; i < Math.min(cmdItems.length, 20); i++) {
        const t = await cmdItems[i].textContent();
        if (t?.toLowerCase().includes('template')) cmdTexts.push(t?.trim());
      }
      note('templates', 'INFO', `Template-related commands in palette: ${cmdTexts.length ? cmdTexts.join(', ') : 'none found'}`);
      await page.keyboard.press('Escape');
    }

    // Check the API endpoint directly
    const token = (await page.evaluate(() => document.querySelector('meta[name="aria-token"]')?.getAttribute('content') || ''));
    note('templates', 'INFO', `Has launch token from meta: ${token ? 'yes' : 'no'}`);
  });

  test('5. Skills view - verify skill listings', async ({ page }) => {
    await page.goto(BASE + '/skills', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(3000);

    const title = await page.title();
    note('skills', 'INFO', `Page title: ${title}`);

    // Check for capability packs header
    const hasHeader = await page.getByText('CAPABILITY PACKS').first().isVisible({ timeout: 10000 }).catch(() => false);
    note('skills', hasHeader ? 'INFO' : 'MEDIUM', `CAPABILITY PACKS header: ${hasHeader}`);

    // Check for ALL TOOLS section
    const hasTools = await page.getByText('ALL TOOLS').first().isVisible({ timeout: 10000 }).catch(() => false);
    note('skills', hasTools ? 'INFO' : 'MEDIUM', `ALL TOOLS section: ${hasTools}`);

    // Count skill cards/packs
    const packs = await page.locator('.instrument-card').count();
    note('skills', 'INFO', `Instrument cards (packs/skill categories): ${packs}`);

    // Count individual tools
    const toolItems = await page.locator('button[title], [aria-label^="Toggle"]').count();
    note('skills', 'INFO', `Tool toggle controls: ${toolItems}`);

    // Check tool guard status
    const guardText = await page.locator('text=/WITHHELD|guard|disabled/guard|enabled/i').first().textContent();
    note('skills', 'INFO', `Tool guard status: ${guardText?.trim() || 'not found'}`);

    const errors: string[] = [];
    page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text().slice(0, 200)); });
    page.on('pageerror', (e) => errors.push(String(e).slice(0, 200)));
    note('skills', 'INFO', `Console errors: ${errors.length ? errors.slice(0, 3).join(' | ') : 'none'}`);
  });

  test('6. Tools view - verify tool availability', async ({ page }) => {
    // Tools are managed from the Skills view
    await page.goto(BASE + '/skills', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(2000);

    // Check if there's a separate "Tools" label in the UI
    const toolsHeader = await page.getByRole('link', { name: 'Tools' }).isVisible({ timeout: 3000 }).catch(() => false);
    note('tools', 'INFO', `Tools as rail item: ${toolsHeader}`);

    // Check the API endpoint
    const toolList = await page.evaluate(async () => {
      const t = localStorage.getItem('aria_token') || '';
      const h: Record<string, string> = {};
      const launch = document.querySelector('meta[name="aria-token"]')?.getAttribute('content') || '';
      if (launch) h['X-Aria-Token'] = launch;
      try {
        const r = await fetch('/api/tools', { headers: h });
        if (!r.ok) return { error: `${r.status} ${r.statusText}` };
        const d = await r.json();
        return { count: d.tools?.length || 0, names: d.tools?.slice(0, 10).map((t: { name: string }) => t.name) || [] };
      } catch (e) {
        return { error: String(e) };
      }
    });
    note('tools', 'INFO', `API /api/tools result: ${JSON.stringify(toolList)}`);

    // Check tools guard endpoint
    const guardResult = await page.evaluate(async () => {
      const launch = document.querySelector('meta[name="aria-token"]')?.getAttribute('content') || '';
      const h: Record<string, string> = {};
      if (launch) h['X-Aria-Token'] = launch;
      try {
        const r = await fetch('/api/config/tools', { headers: h });
        if (!r.ok) return { error: `${r.status} ${r.statusText}` };
        return await r.json();
      } catch (e) {
        return { error: String(e) };
      }
    });
    note('tools', 'INFO', `API /api/config/tools result: ${JSON.stringify(guardResult)}`);
  });

  test('7. Theme toggle - light/dark mode', async ({ page }) => {
    await page.goto(BASE + '/settings', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(1000);

    // Check for any theme toggle in the UI
    const themeToggle = page.locator('button[title*="theme"], button[title*="dark"], button[title*="light"], button[aria-label*="theme"], button[aria-label*="dark"], button[aria-label*="light"]').first();
    const hasToggle = await themeToggle.isVisible({ timeout: 2000 }).catch(() => false);
    note('theme', 'MEDIUM', `Theme toggle button found: ${hasToggle}`);

    // Check the HTML class
    const htmlClass = await page.evaluate(() => document.documentElement.className);
    note('theme', 'MEDIUM', `HTML class attribute: "${htmlClass}"`);

    // Check for any sun/moon toggle
    const sunMoon = page.locator('svg[class*="sun"], svg[class*="moon"], [aria-label*="sun"], [aria-label*="moon"]').first();
    const hasSunMoon = await sunMoon.isVisible({ timeout: 2000 }).catch(() => false);
    note('theme', 'MEDIUM', `Sun/moon icon found: ${hasSunMoon}`);

    // Check body class
    const bodyClass = await page.evaluate(() => document.body.className);
    note('theme', 'INFO', `Body class: "${bodyClass}"`);

    // Check for system preference media query support
    const themeVar = await page.evaluate(() => {
      const styles = getComputedStyle(document.documentElement);
      return {
        bgColor: styles.backgroundColor,
        color: styles.color,
        hasDarkClass: document.documentElement.classList.contains('dark'),
      };
    });
    note('theme', 'INFO', `Computed theme: ${JSON.stringify(themeVar)}`);
  });

  test('8. Broken links and missing assets', async ({ page }) => {
    // Navigate to a page and check for 404s in the network
    const broken: string[] = [];
    page.on('response', (r) => {
      if (r.status() === 404 && r.url().includes('localhost:8500')) {
        broken.push(`${r.url()} -> ${r.status()}`);
      }
    });

    await page.goto(BASE + '/settings', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(2000);

    // Check favicon
    const favicon = page.locator('link[rel="icon"], link[rel="shortcut icon"]').first();
    const hasFavicon = await favicon.count();
    note('assets', 'INFO', `Favicon links: ${hasFavicon}`);

    // Check for SVG icons
    const svgIcons = await page.locator('svg').count();
    note('assets', 'INFO', `SVG elements: ${svgIcons}`);

    // Check manifest
    const manifest = page.locator('link[rel="manifest"]').first();
    const hasManifest = await manifest.count();
    note('assets', 'INFO', `Web manifest: ${hasManifest}`);

    // Try navigating to unknown routes (should serve app shell)
    await page.goto(BASE + '/nonexistent-route', { waitUntil: 'networkidle', timeout: 10000 }).catch(() => {});
    const ct = await page.evaluate(() => document.contentType);
    note('assets', broken.length ? 'LOW' : 'INFO', `Unknown route /nonexistent-route serves: ${ct}, broken links: ${broken.length ? broken.slice(0, 5).join('; ') : 'none'}`);

    // Check image loading
    const imgs = await page.locator('img').all();
    let brokenImgs = 0;
    for (const img of imgs) {
      const src = await img.getAttribute('src');
      if (src && src.startsWith('http')) {
        const status = await page.evaluate(async (s) => {
          try {
            const r = await fetch(s, { method: 'HEAD' });
            return r.status;
          } catch { return 0; }
        }, src);
        if (status >= 400) brokenImgs++;
      }
    }
    note('assets', brokenImgs > 0 ? 'MEDIUM' : 'INFO', `Broken images: ${brokenImgs} out of ${imgs.length}`);
  });

  test('9. Responsive design - small window', async ({ page }) => {
    // Test at phone width
    await page.setViewportSize({ width: 390, height: 844 });

    const routes = ['/research', '/code', '/skills', '/settings'];
    for (const route of routes) {
      await page.goto(BASE + route, { waitUntil: 'networkidle', timeout: 45000 });
      await page.waitForTimeout(2000);

      const { doc, win } = await page.evaluate(() => ({
        doc: document.documentElement.scrollWidth,
        win: window.innerWidth,
      }));
      const overflows = doc > win + 1;
      note('responsive', overflows ? 'MEDIUM' : 'INFO',
        `${route}: viewport ${win}px, document ${doc}px, horizontal overflow: ${overflows}`);
    }

    // Reset viewport
    await page.setViewportSize({ width: 1280, height: 720 });

    // Check rail on mobile
    await page.goto(BASE + '/research', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(1000);

    const navLinks = page.locator('nav[aria-label="Primary"] a');
    const linkCount = await navLinks.count();
    note('responsive', 'INFO', `Mobile nav links count: ${linkCount}`);

    // Check tap targets
    let smallTapTargets = 0;
    for (let i = 0; i < linkCount; i++) {
      const box = await navLinks.nth(i).boundingBox();
      if (box && (box.width < 32 || box.height < 32)) smallTapTargets++;
    }
    note('responsive', smallTapTargets > 0 ? 'MEDIUM' : 'INFO',
      `Small tap targets (<32px): ${smallTapTargets} of ${linkCount}`);
  });

  test('10. Computer Use view functionality', async ({ page }) => {
    await page.goto(BASE + '/computer', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(2000);

    const title = await page.title();
    note('computer', 'INFO', `Page title: ${title}`);

    // Check stat cards
    const stats = await page.locator('.instrument-card').all();
    note('computer', 'INFO', `Stat cards found: ${stats.length}`);
    for (let i = 0; i < stats.length; i++) {
      const t = await stats[i].textContent();
      note('computer', 'INFO', `  stat ${i}: ${t?.replace(/\s+/g, ' ').slice(0, 80)}`);
    }

    // Check mode indicator
    const modeText = await page.locator('text=/live mode|dry-run mode|disabled/i').first().textContent();
    note('computer', 'INFO', `Mode indicator: ${modeText?.trim() || 'not found'}`);

    // Check tabs
    const tabLabels = ['Approvals', 'Audit trail', 'Recordings'];
    for (const label of tabLabels) {
      const tab = page.locator(`button:has-text("${label}")`).first();
      const hasTab = await tab.isVisible({ timeout: 3000 }).catch(() => false);
      note('computer', hasTab ? 'INFO' : 'MEDIUM', `Tab "${label}": ${hasTab}`);
    }

    // Check approvals section
    const approvalsSection = await page.locator('main').textContent();
    const hasApprovalsText = approvalsSection?.includes('approval') || approvalsSection?.includes('pending');
    note('computer', 'INFO', `Approvals/pending text in view: ${hasApprovalsText}`);

    // Test Audit trail tab
    const auditTab = page.locator('button', { hasText: 'Audit trail' });
    if (await auditTab.count()) {
      await auditTab.click();
      await page.waitForTimeout(1500);
      const auditContent = await page.locator('main').textContent();
      const hasAuditHeader = auditContent?.includes('AUDIT TRIAL');
      const hasEmptyMsg = auditContent?.includes('No gated actions');
      note('computer', 'INFO', `Audit trail: header=${hasAuditHeader}, empty_msg=${hasEmptyMsg}`);
    }

    // Test Recordings tab
    const recordingsTab = page.locator('button', { hasText: 'Recordings' });
    if (await recordingsTab.count()) {
      await recordingsTab.click();
      await page.waitForTimeout(1500);
      const recContent = await page.locator('main').textContent();
      const hasEmpty = recContent?.includes('No recordings');
      note('computer', 'INFO', `Recordings: empty_msg=${hasEmpty}`);
    }

    const errors: string[] = [];
    page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text().slice(0, 200)); });
    page.on('pageerror', (e) => errors.push(String(e).slice(0, 200)));
    note('computer', 'INFO', `Console errors: ${errors.length ? errors.slice(0, 3).join(' | ') : 'none'}`);
  });
});
