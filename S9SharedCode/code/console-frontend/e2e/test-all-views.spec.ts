import { test, expect, Page } from '@playwright/test';

const BASE = 'http://localhost:8500';
const SHOTS = 'C:/Users/AISHWA~1/AppData/Local/Temp/kilo/qa-shots';

let findings: { severity: string; view: string; finding: string; evidence: string }[] = [];

interface Finding {
  severity: 'H' | 'M' | 'L';
  view: string;
  finding: string;
  evidence: string;
}

function addFinding(f: Finding) {
  findings.push(f);
}

test.describe('Comprehensive Console Test - All Views', () => {
  test.beforeEach(async ({ page }) => {
    findings = [];
    page.on('console', (msg) => {
      if (msg.type() === 'error') {
        const text = msg.text().slice(0, 300);
        if (!text.includes('DevTools') && !text.includes('favicon')) {
          addFinding({ severity: 'L', view: 'console-errors', finding: `JS console error`, evidence: text });
        }
      }
    });
    page.on('pageerror', (err) => {
      addFinding({ severity: 'H', view: 'console-errors', finding: `Page error (unhandled JS exception)`, evidence: String(err).slice(0, 300) });
    });
    page.on('response', (resp) => {
      if (resp.url().includes('/api/') && !resp.ok() && resp.status() >= 400 && resp.status() < 500) {
        addFinding({ severity: 'M', view: 'console-errors', finding: `API 4xx error response`, evidence: `${resp.status()} ${resp.url()}` });
      }
    });
  });

  test('1. Code view - Monaco editor, create/edit files', async ({ page }) => {
    await page.goto(BASE + '/code', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(2500);

    // Check page loaded
    await expect(page.getByText('Explorer', { exact: true }).first()).toBeVisible({ timeout: 15000 });
    await expect(page.getByText('Pick a file to start editing', { exact: false }).first()).toBeVisible({ timeout: 10000 });

    // Check Monaco editor presence
    const editorHost = page.locator('div[data-editor-host]');
    const editorCount = await editorHost.count();
    if (editorCount === 0) {
      // Might be in a different state - check for textarea
      const textarea = page.locator('textarea[aria-label^="Editor"]');
      const taCount = await textarea.count();
      if (taCount === 0) {
        addFinding({
          severity: 'H',
          view: 'Code',
          finding: 'Monaco editor not rendered (no data-editor-host or editor textarea found)',
          evidence: 'Editor host count: 0, textarea count: 0',
        });
      } else {
        addFinding({
          severity: 'L',
          view: 'Code',
          finding: 'Editor renders as textarea (possibly Monaco fallback mode)',
          evidence: `Textarea found, editor host not found`,
        });
      }
    } else {
      // Check syntax highlighting
      const highlighter = await page.evaluate(() => {
        const pre = document.querySelector('pre');
        if (!pre) return 'no pre element';
        const spans = pre.querySelectorAll('span');
        const classes = new Set<string>();
        spans.forEach((s) => {
          s.className.split(/\s+/).forEach((c) => {
            if (c && c.startsWith('text-')) classes.add(c);
          });
        });
        return [...classes].join(', ') || 'no syntax classes';
      });
      addFinding({
        severity: 'L',
        view: 'Code',
        finding: 'Editor rendering details',
        evidence: `Highlighted token classes: ${highlighter}`,
      });
    }

    // Open a file from the explorer
    const flowPyBtn = page.getByRole('button', { name: /^flow\.py$/ }).first();
    if (await flowPyBtn.count() > 0) {
      await flowPyBtn.click();
      await page.waitForTimeout(1000);
      const ta = page.getByRole('textbox', { name: /Editor for/ });
      if (await ta.count() > 0) {
        const content = await ta.inputValue();
        if (content.length > 0) {
          addFinding({
            severity: 'L',
            view: 'Code',
            finding: 'File opens in editor with content',
            evidence: `flow.py content length: ${content.length} chars, starts with: ${content.substring(0, 50)}`,
          });
        } else {
          addFinding({
            severity: 'M',
            view: 'Code',
            finding: 'File opened in editor but content is empty',
            evidence: 'Editor textarea has empty value',
          });
        }

        // Test editing
        const original = content;
        await ta.fill('const testEdit = "console-test-marker";');
        await new Promise((r) => setTimeout(r, 500));
        const newValue = await ta.inputValue();
        if (newValue === 'const testEdit = "console-test-marker";') {
          addFinding({
            severity: 'L',
            view: 'Code',
            finding: 'Editor edit works (fill replaces content)',
            evidence: 'Editor value matches expected after fill',
          });
        } else {
          addFinding({
            severity: 'M',
            view: 'Code',
            finding: 'Editor edit did not produce expected content',
            evidence: `Expected: "const testEdit = ..." Got: "${newValue.substring(0, 80)}"`,
          });
        }

        // Check for "browser draft" indicator after edit
        const draftIndicator = await page.getByText('browser draft').count();
        if (draftIndicator > 0) {
          addFinding({
            severity: 'L',
            view: 'Code',
            finding: 'Dirty state indicator present after edit',
            evidence: '"browser draft" text is visible',
          });
        } else {
          addFinding({
            severity: 'M',
            view: 'Code',
            finding: 'No dirty state indicator after editing file',
            evidence: '"browser draft" text not found after edit',
          });
        }

        // Check status bar
        const statusBar = await page.locator('.ide-status, [role="status"]').count();
        const lnCol = await page.getByText(/Ln\s*\d+/).count();
        if (lnCol > 0) {
          addFinding({
            severity: 'L',
            view: 'Code',
            finding: 'Status bar shows line/column position',
            evidence: `Ln/Col indicator found`,
          });
        } else {
          addFinding({
            severity: 'M',
            view: 'Code',
            finding: 'No Ln/Col status bar indicator visible',
            evidence: 'No element matching /Ln\\s*\\d+/ found',
          });
        }

        // Revert the edit
        await page.keyboard.press('Control+Shift+P');
        const cmdBox = page.getByRole('textbox', { name: 'Command' });
        await cmdBox.fill('Revert File');
        await page.keyboard.press('Enter');
        await page.waitForTimeout(1000);
        const reverted = await ta.inputValue();
        if (reverted === original) {
          addFinding({
            severity: 'L',
            view: 'Code',
            finding: 'Revert File command restores original content',
            evidence: 'Content matches original after revert',
          });
        } else {
          addFinding({
            severity: 'M',
            view: 'Code',
            finding: 'Revert File did not restore original content',
            evidence: `Content after revert does not match original. Length diff: ${reverted.length - original.length}`,
          });
        }
      }
    } else {
      addFinding({
        severity: 'M',
        view: 'Code',
        finding: 'Could not find flow.py in the explorer',
        evidence: 'No button with name "flow.py" found in explorer',
      });
    }

    // Test "New file" button
    const newFileBtn = page.getByRole('button', { name: 'New file' });
    if (await newFileBtn.count() > 0) {
      addFinding({
        severity: 'L',
        view: 'Code',
        finding: '"New file" button is present',
        evidence: 'Button with aria-label "New file" found',
      });
      await newFileBtn.click();
      const nameInput = page.getByPlaceholder('notes.md');
      if (await nameInput.count() > 0) {
        await nameInput.fill('test-probe-file.txt');
        await page.getByRole('button', { name: 'Create' }).click();
        await page.waitForTimeout(2000);
        // Check if file appears in explorer
        const fileExists = await page.getByRole('button', { name: 'test-probe-file.txt' }).count();
        if (fileExists > 0) {
          addFinding({
            severity: 'L',
            view: 'Code',
            finding: 'New file creation works',
            evidence: 'test-probe-file.txt appears in explorer after creation',
          });
        } else {
          addFinding({
            severity: 'M',
            view: 'Code',
            finding: 'New file creation did not show file in explorer',
            evidence: 'test-probe-file.txt not found in explorer after creation',
          });
        }
        // Clean up: delete via command palette
        await page.keyboard.press('Control+Shift+P');
        await page.getByRole('textbox', { name: 'Command' }).fill('Open folder');
        await page.keyboard.press('Enter');
      }
    } else {
      addFinding({
        severity: 'M',
        view: 'Code',
        finding: 'No "New file" button in explorer',
        evidence: 'Button with name "New file" not found',
      });
    }

    // Check Problems panel
    await page.getByRole('button', { name: 'Run checks' }).first();
    const problemsPanel = await page.getByText(/No syntax errors|syntax error/i).count();
    addFinding({
      severity: 'L',
      view: 'Code',
      finding: 'Problems panel status',
      evidence: `Elements matching "No syntax errors" or "syntax error": ${problemsPanel}`,
    });

    // Check for command palette
    await page.keyboard.press('Control+,');
    const shortcutSheet = await page.getByRole('dialog', { name: 'Keyboard shortcuts' }).count();
    if (shortcutSheet > 0) {
      addFinding({
        severity: 'L',
        view: 'Code',
        finding: 'Keyboard shortcuts sheet opens with Ctrl+,',
        evidence: 'Dialog with name "Keyboard shortcuts" is visible',
      });
      await page.keyboard.press('Escape');
    }

    await page.screenshot({ path: `${SHOTS}/code.png` });
  });

  test('2. Settings - configuration display', async ({ page }) => {
    await page.goto(BASE + '/settings', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(3000);

    // Check environment status pills
    const envLabels = ['agent', 'gateway', 'mcp tools', 'sessions', 'schedules', 'templates'];
    for (const label of envLabels) {
      const found = await page.getByText(label, { exact: false }).first().count();
      if (found > 0) {
        const pillText = await page.getByText(label).locator('..').locator('..').locator('.bg-white\\/5, .bg-violet-400\\/10, .text-emerald-300, .text-red-300, .text-zinc-400').first().textContent().catch(() => '');
        addFinding({
          severity: 'L',
          view: 'Settings',
          finding: `Environment status "${label}" is displayed`,
          evidence: `Label found in sidebar, pill state text: "${pillText}"`,
        });
      } else {
        addFinding({
          severity: 'M',
          view: 'Settings',
          finding: `Environment status "${label}" NOT displayed`,
          evidence: 'Label not found in sidebar',
        });
      }
    }

    // Check API token section
    const tokenInput = page.getByLabel('API bearer token');
    if (await tokenInput.count() > 0) {
      addFinding({
        severity: 'L',
        view: 'Settings',
        finding: 'API token input field is present',
        evidence: 'Input with aria-label "API bearer token" found',
      });
      const saveBtn = page.getByRole('button', { name: 'Save' });
      if (await saveBtn.count() > 0) {
        addFinding({
          severity: 'L',
          view: 'Settings',
          finding: 'Save button for API token is present',
          evidence: 'Button with name "Save" found',
        });
      }
    } else {
      addFinding({
        severity: 'M',
        view: 'Settings',
        finding: 'API token input field NOT present',
        evidence: 'No input with aria-label "API bearer token"',
      });
    }

    // Check config viewer
    const configHeader = await page.getByText('READ-ONLY CONFIG').count();
    if (configHeader > 0) {
      addFinding({
        severity: 'L',
        view: 'Settings',
        finding: 'Config viewer section is present',
        evidence: 'READ-ONLY CONFIG header found',
      });
    }

    // Check config filter
    const configFilter = page.getByPlaceholder('filter config…');
    if (await configFilter.count() > 0) {
      addFinding({
        severity: 'L',
        view: 'Settings',
        finding: 'Config filter input is present',
        evidence: 'Input with placeholder "filter config…" found',
      });
      // Try filtering
      await configFilter.fill('provider');
      await page.waitForTimeout(500);
      const hits = await page.getByText(/\d+ hit/).count();
      if (hits > 0) {
        const hitText = await page.getByText(/\d+ hit/).first().textContent();
        addFinding({
          severity: 'L',
          view: 'Settings',
          finding: 'Config filter works (provider search)',
          evidence: `Hit counter visible: "${hitText}"`,
        });
      }
      await configFilter.clear();
    } else {
      addFinding({
        severity: 'M',
        view: 'Settings',
        finding: 'Config filter input NOT present',
        evidence: 'No input with placeholder "filter config…"',
      });
    }

    // Check gateway deep links
    const links = await page.locator('a[href*="localhost:8109"], a[href*="127.0.0.1:8109"]').count();
    if (links > 0) {
      addFinding({
        severity: 'L',
        view: 'Settings',
        finding: 'Gateway deep links are present',
        evidence: `${links} link(s) to gateway port 8109 found`,
      });
    }

    // Check if config is empty or has content
    const configText = await page.locator('.font-mono.text-\\[11\\.5px\\]').first().textContent().catch(() => '');
    if (configText && configText.length > 10) {
      addFinding({
        severity: 'L',
        view: 'Settings',
        finding: 'Config content displayed',
        evidence: `Config text length: ${configText.length} chars, sample: "${configText.substring(0, 80)}"`,
      });
    } else if (configText === '(empty)') {
      addFinding({
        severity: 'M',
        view: 'Settings',
        finding: 'Config is empty',
        evidence: '(empty)'
      });
    }

    await page.screenshot({ path: `${SHOTS}/settings.png` });
  });

  test('3. Tools (Skills) - tool availability and display', async ({ page }) => {
    await page.goto(BASE + '/skills', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(3000);

    // Check section header
    const header = await page.getByText('CAPABILITY PACKS').count();
    if (header > 0) {
      addFinding({
        severity: 'L',
        view: 'Tools/Skills',
        finding: 'Capability packs section header visible',
        evidence: 'CAPABILITY PACKS text found',
      });
    }

    // Check stat cards
    const toolsLiveStat = await page.getByText('TOOLS LIVE').count();
    if (toolsLiveStat > 0) {
      const val = await page.getByText('TOOLS LIVE').locator('..').locator('..').textContent();
      addFinding({
        severity: 'L',
        view: 'Tools/Skills',
        finding: 'TOOLS LIVE stat card present',
        evidence: `Stat card text: "${val?.substring(0, 100) || 'not found'}"`,
      });
    }

    const withheldStat = await page.getByText('WITHHELD').count();
    if (withheldStat > 0) {
      const val = await page.getByText('WITHHELD').locator('..').locator('..').textContent();
      addFinding({
        severity: 'L',
        view: 'Tools/Skills',
        finding: 'WITHHELD stat card present',
        evidence: `Stat card text: "${val?.substring(0, 100) || 'not found'}"`,
      });
    }

    // Check pack count
    const packsText = await page.getByText(/packs/).first().textContent().catch(() => '');
    addFinding({
      severity: 'L',
      view: 'Tools/Skills',
      finding: 'Pack count badge',
      evidence: `Text containing "packs": "${packsText || 'not found'}"`,
    });

    // Check ALL TOOLS table
    const toolsTable = await page.getByText('ALL TOOLS').count();
    if (toolsTable > 0) {
      addFinding({
        severity: 'L',
        view: 'Tools/Skills',
        finding: 'ALL TOOLS table section present',
        evidence: 'ALL TOOLS header found',
      });
      // Count table rows
      const tableRows = await page.locator('table tbody tr').count();
      addFinding({
        severity: 'L',
        view: 'Tools/Skills',
        finding: `Tools table has ${tableRows} rows`,
        evidence: `tbody tr count: ${tableRows}`,
      });

      // Check for tool state pills
      const statePills = await page.locator('td').filter({ hasText: /live|withheld|unknown/ }).count();
      addFinding({
        severity: 'L',
        view: 'Tools/Skills',
        finding: `Tool state indicators found`,
        evidence: `${statePills} cells with live/withheld/unknown state text`,
      });
    } else {
      addFinding({
        severity: 'M',
        view: 'Tools/Skills',
        finding: 'ALL TOOLS table NOT present',
        evidence: 'ALL TOOLS header not found',
      });
    }

    // Check for guard error banner
    const guardBanner = await page.locator('role=alert').count();
    if (guardBanner > 0) {
      const alertText = await page.locator('role=alert').first().textContent();
      addFinding({
        severity: 'M',
        view: 'Tools/Skills',
        finding: 'Guard error or toggle error banner present',
        evidence: `Alert text: "${alertText?.substring(0, 200)}"`,
      });
    }

    // Check filter
    const filterInput = page.getByLabel('Filter tools');
    if (await filterInput.count() > 0) {
      addFinding({
        severity: 'L',
        view: 'Tools/Skills',
        finding: 'Tool filter input present',
        evidence: 'Input with aria-label "Filter tools" found',
      });
    }

    // Check for individual tool names
    const toolNames = ['web_search', 'read_file', 'create_file', 'run_checks'];
    for (const name of toolNames) {
      const found = await page.getByText(name, { exact: true }).count();
      if (found > 0) {
        addFinding({
          severity: 'L',
          view: 'Tools/Skills',
          finding: `Tool "${name}" is listed`,
          evidence: `"${name}" text found in the page`,
        });
      }
    }

    await page.screenshot({ path: `${SHOTS}/skills.png` });
  });

  test('4. Apps - stale apps referencing removed providers (OpenRouter)', async ({ page }) => {
    await page.goto(BASE + '/apps', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(3000);

    // Check system overview
    const overview = await page.getByText('System overview').count();
    if (overview > 0) {
      addFinding({
        severity: 'L',
        view: 'Apps',
        finding: 'System overview board visible',
        evidence: 'System overview text found',
      });
    }

    // Check for OpenRouter references
    const openrouterRefs = await page.locator('text=/[Oo]pen[Rr]outer/i').count();
    if (openrouterRefs > 0) {
      addFinding({
        severity: 'M',
        view: 'Apps',
        finding: 'OpenRouter references found in Apps view',
        evidence: `${openrouterRefs} element(s) referencing OpenRouter found`,
      });
      // Get the actual text
      const texts = await page.locator('text=/[Oo]pen[Rr]outer/i').first().textContent();
      addFinding({
        severity: 'M',
        view: 'Apps',
        finding: 'OpenRouter text content',
        evidence: `"${texts?.substring(0, 100)}"`,
      });
    } else {
      addFinding({
        severity: 'L',
        view: 'Apps',
        finding: 'No OpenRouter references found in Apps view',
        evidence: 'No elements referencing OpenRouter found',
      });
    }

    // Check for openrouter_free kind apps
    const openrouterApps = await page.locator('text=/openrouter_free/i').count();
    if (openrouterApps > 0) {
      addFinding({
        severity: 'M',
        view: 'Apps',
        finding: 'Apps with kind "openrouter_free" still exist',
        evidence: `${openrouterApps} element(s) with "openrouter_free" found`,
      });
    }

    // Check for app tracker list
    const newAppBtn = await page.getByRole('button', { name: /New app/i }).count();
    if (newAppBtn > 0) {
      addFinding({
        severity: 'L',
        view: 'Apps',
        finding: '"New app" button present',
        evidence: 'Button with name matching /New app/i found',
      });
    } else {
      addFinding({
        severity: 'M',
        view: 'Apps',
        finding: '"New app" button NOT present',
        evidence: 'No button with name matching /New app/i',
      });
    }

    // Check stats cards
    const stats = ['AGENT', 'GATEWAY', 'SESSIONS', 'JOBS ARMED', 'TOOLS LIVE', 'SPEND'];
    for (const s of stats) {
      const found = await page.getByText(s, { exact: true }).count();
      if (found > 0) {
        addFinding({
          severity: 'L',
          view: 'Apps',
          finding: `Stat card "${s}" present`,
          evidence: `"${s}" text found`,
        });
      } else {
        addFinding({
          severity: 'M',
          view: 'Apps',
          finding: `Stat card "${s}" NOT present`,
          evidence: `"${s}" text not found`,
        });
      }
    }

    // Check flags section
    const flagsSection = await page.getByText('FLAGS & CONFIG').count();
    if (flagsSection > 0) {
      addFinding({
        severity: 'L',
        view: 'Apps',
        finding: 'Flags & Config section present',
        evidence: 'FLAGS & CONFIG header found',
      });
    }

    // Check spend by skill
    const spendSection = await page.getByText('SPEND BY SKILL').count();
    if (spendSection > 0) {
      addFinding({
        severity: 'L',
        view: 'Apps',
        finding: 'Spend by skill section present',
        evidence: 'SPEND BY SKILL header found',
      });
    }

    await page.screenshot({ path: `${SHOTS}/apps.png` });
  });

  test('5. Ledger - cost display', async ({ page }) => {
    await page.goto(BASE + '/ledger', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(3000);

    // Check scope selection
    const allTimeBtn = await page.getByRole('button', { name: 'All time' }).count();
    const oneConvBtn = await page.getByRole('button', { name: 'One conversation' }).count();
    if (allTimeBtn > 0) {
      addFinding({
        severity: 'L',
        view: 'Ledger',
        finding: '"All time" scope button present',
        evidence: 'Button with name "All time" found',
      });
    }
    if (oneConvBtn > 0) {
      addFinding({
        severity: 'L',
        view: 'Ledger',
        finding: '"One conversation" scope button present',
        evidence: 'Button with name "One conversation" found',
      });
    }

    // Check stat cards
    const spendStat = await page.getByText('SPEND').count();
    if (spendStat > 0) {
      // Get the SPEND value
      const spendVal = await page.getByText('SPEND').locator('..').locator('..').locator('..').textContent();
      addFinding({
        severity: 'L',
        view: 'Ledger',
        finding: 'SPEND stat card present',
        evidence: `SPEND card text: "${spendVal?.substring(0, 100) || 'empty'}"`,
      });
    }

    const callsStat = await page.getByText('CALLS').count();
    if (callsStat > 0) {
      const callsVal = await page.getByText('CALLS').locator('..').locator('..').locator('..').textContent();
      addFinding({
        severity: 'L',
        view: 'Ledger',
        finding: 'CALLS stat card present',
        evidence: `CALLS card text: "${callsVal?.substring(0, 100) || 'empty'}"`,
      });
    }

    const skillsStat = await page.getByText('SKILLS').count();
    if (skillsStat > 0) {
      const skillsVal = await page.getByText('SKILLS').locator('..').locator('..').locator('..').textContent();
      addFinding({
        severity: 'L',
        view: 'Ledger',
        finding: 'SKILLS stat card present',
        evidence: `SKILLS card text: "${skillsVal?.substring(0, 100) || 'empty'}"`,
      });
    }

    const turnsStat = await page.getByText('TURNS').count();
    if (turnsStat > 0) {
      const turnsVal = await page.getByText('TURNS').locator('..').locator('..').locator('..').textContent();
      addFinding({
        severity: 'L',
        view: 'Ledger',
        finding: 'TURNS stat card present',
        evidence: `TURNS card text: "${turnsVal?.substring(0, 100) || 'empty'}"`,
      });
    }

    // Check for contradiction: SPEND n/a while tables have data
    await page.waitForTimeout(2000);
    const spendText = await page.locator('text=/SPEND/i').first().locator('..').locator('..').locator('..').textContent();
    const spendBySkillTable = await page.locator('text=/SPEND BY SKILL/i').count();
    if (spendBySkillTable > 0) {
      const tableRows = await page.locator('text=/SPEND BY SKILL/i').locator('..').locator('table tbody tr').count();
      if (tableRows > 0) {
        const tableUsdCells = await page.locator('text=/SPEND BY SKILL/i').locator('..').locator('table tbody tr').locator('td').filter({ hasText: /\$\d/ }).count();
        addFinding({
          severity: 'L',
          view: 'Ledger',
          finding: `Spend by skill table has ${tableRows} rows, ${tableUsdCells} with USD values`,
          evidence: `Table rows: ${tableRows}, USD cells: ${tableUsdCells}, SPEND stat: "${spendText?.substring(0, 50)}"`,
        });
        // Check for contradiction
        if (spendText?.includes('n/a') && tableUsdCells > 0) {
          addFinding({
            severity: 'M',
            view: 'Ledger',
            finding: 'CONTRADICTION: SPEND shows n/a but tables have real USD values',
            evidence: `SPEND stat: "${spendText}" but ${tableUsdCells} USD cells found in tables`,
          });
        }
      }
    }

    // Check turn history table
    const turnHistory = await page.getByText('TURN HISTORY').count();
    if (turnHistory > 0) {
      const thRows = await page.locator('text=/TURN HISTORY/i').locator('..').locator('table tbody tr').count();
      addFinding({
        severity: 'L',
        view: 'Ledger',
        finding: `Turn history table has ${thRows} rows`,
        evidence: `Table rows: ${thRows}`,
      });
    }

    // Check node reliability section
    const nodeReliab = await page.getByText('NODE RELIABILITY').count();
    if (nodeReliab > 0) {
      addFinding({
        severity: 'L',
        view: 'Ledger',
        finding: 'Node reliability section present',
        evidence: 'NODE RELIABILITY header found',
      });
      const nodeText = await page.getByText('NODE RELIABILITY').locator('..').locator('..').textContent();
      addFinding({
        severity: 'L',
        view: 'Ledger',
        finding: 'Node reliability details',
        evidence: `Text: "${nodeText?.substring(0, 200) || 'empty'}"`,
      });
    }

    // Check for "Pick a conversation" when switching to one conversation scope
    await page.getByRole('button', { name: 'One conversation' }).click();
    await page.waitForTimeout(2000);
    const pickConv = await page.getByText(/Pick a conversation/i).count();
    if (pickConv > 0) {
      addFinding({
        severity: 'L',
        view: 'Ledger',
        finding: 'One conversation scope shows "Pick a conversation" prompt',
        evidence: 'Text "Pick a conversation" found',
      });
    } else {
      addFinding({
        severity: 'M',
        view: 'Ledger',
        finding: 'One conversation scope does NOT show "Pick a conversation" prompt',
        evidence: 'Text "Pick a conversation" not found',
      });
    }

    // Switch back to all time
    await page.getByRole('button', { name: 'All time' }).click();
    await page.waitForTimeout(1000);

    await page.screenshot({ path: `${SHOTS}/ledger.png` });
  });

  test('6. Console - chat/feed functionality', async ({ page }) => {
    await page.goto(BASE + '/console', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(3000);

    // Check threads list
    const threadsHeader = await page.getByText('Threads').count();
    if (threadsHeader > 0) {
      addFinding({
        severity: 'L',
        view: 'Console',
        finding: 'Threads sidebar header visible',
        evidence: 'Threads text found',
      });
    }

    // Check composer
    const composer = await page.getByPlaceholder(/Message Aria/i).count();
    if (composer === 0) {
      // Try other composer selectors
      const composerArea = await page.locator('textarea').first().count();
      if (composerArea > 0) {
        addFinding({
          severity: 'L',
          view: 'Console',
          finding: 'Composer textarea found',
          evidence: 'textarea element found on page',
        });
      }
    } else {
      addFinding({
        severity: 'L',
        view: 'Console',
        finding: 'Composer with placeholder found',
        evidence: `Placeholder matches /Message Aria/i`,
      });
    }

    // Check for Send button
    const sendBtn = await page.locator('button[aria-label*="Send"], button[aria-label*="Queue"], button:has-text("Send")').first().count();
    if (sendBtn > 0) {
      const btnText = await page.locator('button').filter({ hasText: /Send|Queue/i }).first().textContent();
      addFinding({
        severity: 'L',
        view: 'Console',
        finding: 'Send/Queue button found',
        evidence: `Button text: "${btnText || 'found but no text'}"`,
      });
    } else {
      addFinding({
        severity: 'M',
        view: 'Console',
        finding: 'Send/Queue button NOT found',
        evidence: 'No button matching Send/Queue found',
      });
    }

    // Check for model picker
    const modelPicker = await page.getByText(/model/i).count();
    if (modelPicker > 0) {
      addFinding({
        severity: 'L',
        view: 'Console',
        finding: 'Model picker element found',
        evidence: `${modelPicker} elements referencing "model"`,
      });
    }

    // Check for tool count badge
    const toolBadge = await page.locator('text=/\\d+\\s*tool/i').first().count();
    if (toolBadge > 0) {
      const badgeText = await page.locator('text=/\\d+\\s*tool/i').first().textContent();
      addFinding({
        severity: 'L',
        view: 'Console',
        finding: 'Tool count badge visible',
        evidence: `Badge text: "${badgeText?.substring(0, 50)}"`,
      });
    }

    // Check for mode toggle (light/dag)
    const modeToggle = await page.locator('button, [role="tab"]').filter({ hasText: /light|dag/i }).count();
    if (modeToggle > 0) {
      addFinding({
        severity: 'L',
        view: 'Console',
        finding: 'Mode toggle (light/dag) found',
        evidence: `${modeToggle} mode toggle elements found`,
      });
    }

    // Check empty state
    const emptyState = await page.getByText(/Quick answers|no pipeline|Ask anything/i).count();
    if (emptyState > 0) {
      const emptyText = await page.locator('text=/Quick answers|no pipeline|Ask anything/i').first().textContent();
      addFinding({
        severity: 'L',
        view: 'Console',
        finding: 'Empty state text found',
        evidence: `Text: "${emptyText?.substring(0, 80)}"`,
      });
    }

    // Check for stale controls when idle
    const startNewChat = await page.getByRole('button', { name: /Start new/i }).count();
    if (startNewChat > 0) {
      addFinding({
        severity: 'L',
        view: 'Console',
        finding: 'Start new chat button present',
        evidence: 'Button with name matching /Start new/i found',
      });
    }

    await page.screenshot({ path: `${SHOTS}/console.png` });
  });

  test('7. Parts - part management', async ({ page }) => {
    await page.goto(BASE + '/parts', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(3000);

    // Check main elements
    const uploadText = await page.getByText('Upload a mesh').count();
    if (uploadText > 0) {
      addFinding({
        severity: 'L',
        view: 'Parts',
        finding: 'Upload prompt visible',
        evidence: 'Upload a mesh text found',
      });
    }

    const chooseText = await page.getByText('Choose an STL or OBJ part').count();
    if (chooseText > 0) {
      addFinding({
        severity: 'L',
        view: 'Parts',
        finding: 'File selection prompt visible',
        evidence: 'Choose an STL or OBJ part text found',
      });
    }

    // Check canvas
    const canvas = page.locator('canvas').first();
    const canvasVisible = await canvas.isVisible();
    if (canvasVisible) {
      addFinding({
        severity: 'L',
        view: 'Parts',
        finding: '3D viewer canvas is present',
        evidence: 'canvas element found and visible',
      });
    } else {
      addFinding({
        severity: 'M',
        view: 'Parts',
        finding: '3D viewer canvas NOT visible',
        evidence: 'canvas element not visible or not found',
      });
    }

    // Check pass selector
    const passSelect = page.locator('select[id="parts-pass"]');
    if (await passSelect.count() > 0) {
      const options = await passSelect.locator('option').count();
      addFinding({
        severity: 'L',
        view: 'Parts',
        finding: `Pass selector present with ${options} options`,
        evidence: `select#parts-pass has ${options} options`,
      });
      const optionTexts = await passSelect.locator('option').allTextContents();
      addFinding({
        severity: 'L',
        view: 'Parts',
        finding: `Pass options: ${optionTexts.join(', ')}`,
        evidence: optionTexts.join(', '),
      });
    } else {
      addFinding({
        severity: 'M',
        view: 'Parts',
        finding: 'Pass selector NOT present',
        evidence: 'select#parts-pass not found',
      });
    }

    // Check file input
    const fileInput = page.locator('input[type="file"]');
    if (await fileInput.count() > 0) {
      addFinding({
        severity: 'L',
        view: 'Parts',
        finding: 'File input for mesh upload present',
        evidence: 'input[type="file"] found',
      });
    }

    // Check objective display
    const objective = await page.getByText('Objective').count();
    if (objective > 0) {
      addFinding({
        severity: 'L',
        view: 'Parts',
        finding: 'Objective display present',
        evidence: 'Objective text found',
      });
    }

    // Check optimization button
    const optButton = await page.getByRole('button', { name: /Optimise/i }).count();
    if (optButton > 0) {
      addFinding({
        severity: 'L',
        view: 'Parts',
        finding: 'Optimization button present',
        evidence: `Found ${optButton} button(s) matching /Optimise/i`,
      });
    }

    await page.screenshot({ path: `${SHOTS}/parts.png` });
  });

  test('8. Mission - mission/event feed display', async ({ page }) => {
    await page.goto(BASE + '/mission', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(3000);

    // Check TopBar crumb - the route is /mission but crumb may say "Console"
    const topbarText = await page.locator('header').first().textContent().catch(() => '');
    addFinding({
      severity: 'L',
      view: 'Mission',
      finding: 'TopBar crumb text',
      evidence: `Header text: "${topbarText?.substring(0, 100) || 'not found'}"`,
    });

    // Check event feed header
    const feedHeader = await page.getByText('EVENT FEED').count();
    if (feedHeader > 0) {
      addFinding({
        severity: 'L',
        view: 'Mission',
        finding: 'EVENT FEED header visible',
        evidence: 'EVENT FEED text found',
      });
    }

    // Check Sources section
    const sourcesHeader = await page.getByText('Sources').count();
    if (sourcesHeader > 0) {
      addFinding({
        severity: 'L',
        view: 'Mission',
        finding: 'Sources sidebar header visible',
        evidence: 'Sources text found',
      });
    }

    // Check source filter buttons
    const sourceButtons: { name: string; label: string }[] = [
      { name: 'all', label: 'all' },
      { name: 'runs', label: 'runs' },
      { name: 'scheduler', label: 'scheduler' },
      { name: 'tools', label: 'tools' },
      { name: 'server', label: 'server' },
      { name: 'errors', label: 'errors' },
    ];
    for (const sb of sourceButtons) {
      const count = await page.getByRole('button', { name: sb.name }).count();
      if (count > 0) {
        addFinding({
          severity: 'L',
          view: 'Mission',
          finding: `Source filter "${sb.label}" button present`,
          evidence: `Button found`,
        });
      } else {
        addFinding({
          severity: 'M',
          view: 'Mission',
          finding: `Source filter "${sb.label}" button NOT present`,
          evidence: `Button with name "${sb.name}" not found`,
        });
      }
    }

    // Check live indicator
    const liveIndicators = ['● live', '○ paused', 'feed error'];
    for (const ind of liveIndicators) {
      const count = await page.locator(`text=/${ind}/`).count();
      if (count > 0) {
        addFinding({
          severity: 'L',
          view: 'Mission',
          finding: `Live indicator "${ind}" found`,
          evidence: `Found`,
        });
      }
    }

    // Check pause button
    const pauseBtn = await page.getByRole('button', { name: /pause|resume/i }).count();
    if (pauseBtn > 0) {
      addFinding({
        severity: 'L',
        view: 'Mission',
        finding: 'Pause/resume button present',
        evidence: `${pauseBtn} pause/resume button(s) found`,
      });
    }

    // Check counters
    const counterLabels = ['RUN LINES', 'SCHED LINES', 'SERVER LINES', 'ERRORS'];
    for (const label of counterLabels) {
      const count = await page.getByText(label).count();
      if (count > 0) {
        const text = await page.getByText(label).first().locator('..').locator('..').textContent();
        addFinding({
          severity: 'L',
          view: 'Mission',
          finding: `Counter "${label}" present`,
          evidence: `Text: "${text?.substring(0, 100) || 'found'}`,
        });
      } else {
        addFinding({
          severity: 'M',
          view: 'Mission',
          finding: `Counter "${label}" NOT present`,
          evidence: `"${label}" text not found`,
        });
      }
    }

    // Check poll indicator
    const pollText = await page.locator('text=/poll\\s*\\d+s/i').count();
    if (pollText > 0) {
      addFinding({
        severity: 'L',
        view: 'Mission',
        finding: 'Poll indicator present',
        evidence: `Found poll indicator text`,
      });
    } else {
      const guarded = await page.locator('text=/guarded/i').count();
      addFinding({
        severity: 'L',
        view: 'Mission',
        finding: 'Poll/guarded indicator status',
        evidence: `poll text: ${pollText}, guarded text: ${guarded}`,
      });
    }

    // Check filter input
    const filterInput = await page.getByPlaceholder('Filter…').count();
    if (filterInput > 0) {
      addFinding({
        severity: 'L',
        view: 'Mission',
        finding: 'Filter input present',
        evidence: 'Input with placeholder "Filter…" found',
      });
    }

    // Check for event feed entries
    await page.waitForTimeout(2000);
    const feedEntries = await page.locator('text=/\\d{2}:\\d{2}:\\d{2}/').count();
    if (feedEntries > 0) {
      addFinding({
        severity: 'L',
        view: 'Mission',
        finding: `Event feed has ${feedEntries} entries with timestamps`,
        evidence: `Found ${feedEntries} elements matching timestamp pattern`,
      });
    } else {
      addFinding({
        severity: 'M',
        view: 'Mission',
        finding: 'Event feed has no entries with timestamps',
        evidence: 'No elements matching \\d{2}:\\d{2}:\\d{2} found',
      });
    }

    // Check for "No events" empty state
    const emptyState = await page.locator('text=/No events/i').count();
    if (emptyState > 0) {
      addFinding({
        severity: 'L',
        view: 'Mission',
        finding: 'Event feed shows empty state message',
        evidence: 'Text matching "No events" found',
      });
    }

    await page.screenshot({ path: `${SHOTS}/mission.png` });
  });

  test('9. Responsive behavior - small window', async ({ page }) => {
    const PAGES = [
      { route: '/code', name: 'Code' },
      { route: '/settings', name: 'Settings' },
      { route: '/skills', name: 'Skills' },
      { route: '/apps', name: 'Apps' },
      { route: '/ledger', name: 'Ledger' },
      { route: '/console', name: 'Console' },
      { route: '/parts', name: 'Parts' },
      { route: '/mission', name: 'Mission' },
    ];

    for (const p of PAGES) {
      await page.setViewportSize({ width: 390, height: 844 });
      await page.goto(BASE + p.route, { waitUntil: 'networkidle', timeout: 45000 });
      await page.waitForTimeout(2000);

      const { doc, win } = await page.evaluate(() => ({
        doc: document.documentElement.scrollWidth,
        win: window.innerWidth,
      }));

      if (doc > win + 1) {
        addFinding({
          severity: 'H',
          view: 'Responsive',
          finding: `Page ${p.route} OVERFLOWS horizontally on 390px viewport`,
          evidence: `document scrollWidth: ${doc}px, viewport width: ${win}px (overflow: ${doc - win}px)`,
        });
      } else {
        addFinding({
          severity: 'L',
          view: 'Responsive',
          finding: `Page ${p.route} fits within 390px viewport`,
          evidence: `document scrollWidth: ${doc}px <= viewport width: ${win}px`,
        });
      }

      // Check rail icons
      const railLinks = page.locator('nav[aria-label="Primary"] a');
      const linkCount = await railLinks.count();
      if (linkCount > 0) {
        let allLabelled = true;
        let allTappable = true;
        for (let i = 0; i < Math.min(linkCount, 15); i++) {
          const el = railLinks.nth(i);
          const label = await el.getAttribute('aria-label');
          if (!label || label.trim().length === 0) allLabelled = false;
          const box = await el.boundingBox();
          if (!box || box.width < 32 || box.height < 32) allTappable = false;
        }
        if (!allLabelled) {
          addFinding({
            severity: 'M',
            view: 'Responsive',
            finding: `Some rail icons on ${p.route} lack accessible labels`,
            evidence: `${linkCount} rail links found, not all have aria-label`,
          });
        }
        if (!allTappable) {
          addFinding({
            severity: 'M',
            view: 'Responsive',
            finding: `Some rail icons on ${p.route} are not tappable (< 32px)`,
            evidence: `${linkCount} rail links found, some have bounding box < 32px`,
          });
        }
      }

      await page.screenshot({ path: `${SHOTS}/responsive-${p.name.toLowerCase()}.png` });
    }

    // Reset viewport
    await page.setViewportSize({ width: 1280, height: 720 });
  });

  test('10. Console errors across all views', async ({ page }) => {
    const pages = [
      '/code', '/settings', '/skills', '/apps', '/ledger', '/console', '/parts', '/mission',
    ];

    for (const route of pages) {
      const routeErrors: string[] = [];
      const routePageErrors: string[] = [];
      page.on('console', (msg) => {
        if (msg.type() === 'error') {
          const text = msg.text().slice(0, 300);
          if (!text.includes('DevTools') && !text.includes('favicon') && !text.includes('websocket') && !text.includes('WebSocket')) {
            routeErrors.push(text);
          }
        }
      });
      page.on('pageerror', (err) => {
        routePageErrors.push(String(err).slice(0, 300));
      });

      await page.goto(BASE + route, { waitUntil: 'networkidle', timeout: 45000 });
      await page.waitForTimeout(4000);

      // Check for network errors
      const networkErrors: string[] = [];
      page.on('response', (resp) => {
        if (resp.url().includes('/api/') && !resp.ok()) {
          networkErrors.push(`${resp.status()} ${resp.url()}`);
        }
      });

      // Trigger some interactions to surface errors
      if (route === '/code') {
        const flowPy = page.getByRole('button', { name: /^flow\.py$/ }).first();
        if (await flowPy.count() > 0) await flowPy.click();
      } else if (route === '/apps') {
        const newApp = page.getByRole('button', { name: /New app/i });
        if (await newApp.count() > 0) {
          await newApp.click();
          await page.waitForTimeout(1000);
          page.getByRole('button', { name: /Create app/i }).first();
        }
      }

      await page.waitForTimeout(2000);

      if (routeErrors.length > 0) {
        addFinding({
          severity: 'M',
          view: route,
          finding: `JS console errors detected on ${route}`,
          evidence: `Errors: ${routeErrors.join(' | ')}`,
        });
      }

      if (routePageErrors.length > 0) {
        addFinding({
          severity: 'H',
          view: route,
          finding: `Unhandled JS exceptions on ${route}`,
          evidence: `Errors: ${routePageErrors.join(' | ')}`,
        });
      }

      // Check for API errors
      // The event handler collects them; let's check the page context
      const apiErrors = await page.evaluate(() => {
        // Check if any visible error messages are present
        const errorElements = Array.from(document.querySelectorAll('[role="alert"], .text-red-200, .text-red-300'));
        return errorElements
          .filter((el) => el.textContent && el.textContent.trim().length > 0)
          .map((el) => el.textContent!.trim().substring(0, 100));
      });

      if (apiErrors.length > 0 && routeErrors.length === 0 && routePageErrors.length === 0) {
        addFinding({
          severity: 'L',
          view: route,
          finding: `Visible error/alert elements on ${route}`,
          evidence: `Alert texts: ${apiErrors.join(' | ')}`,
        });
      }
    }

    // Summary
    const totalFindings = findings.length;
    const errorCount = findings.filter((f) => f.severity === 'H').length;
    const warningCount = findings.filter((f) => f.severity === 'M').length;
    const infoCount = findings.filter((f) => f.severity === 'L').length;

    addFinding({
      severity: 'L',
      view: 'Summary',
      finding: `Test complete: ${totalFindings} findings (${errorCount} H, ${warningCount} M, ${infoCount} L)`,
      evidence: `Total findings: ${totalFindings}`,
    });
  });
});
