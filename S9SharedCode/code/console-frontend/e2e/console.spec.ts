import { test, expect } from '@playwright/test';

const BASE = 'http://localhost:8500';
const SHOTS = 'C:/Users/AISHWA~1/AppData/Local/Temp/opencode/qa-shots';

const PAGES: { route: string; markers: string[] }[] = [
  { route: '/research', markers: ['Topics', 'Research this topic', 'The pipeline appears here'] },
  /* "Conversations" and "Ask anything" never existed in the current Chat
     view; asserting them made this spec permanently red, and the failure
     was then edited away rather than fixed. These are the real markers: the
     sidebar heading and the empty-state intro. ("Message Aria" only exists
     as the composer's placeholder/aria-label, which getByText cannot see, so
     asserting it as text would re-redden this spec.) */
  { route: '/console', markers: ['Threads', 'Quick answers, no pipeline'] },
  { route: '/runs', markers: ['Runs', 'Select a run'] },
  // "Memory types", not "Drawers": the sidebar listed the seven gateway
  // drawers (working/episode/fact/playbook/policy/audit/document), which is
  // the storage cabinet rather than a memory type, and hid preferences
  // because `preference` is stored in the `fact` drawer. It now lists the four
  // kinds the agent works with.
  { route: '/memory', markers: ['Memory types', 'preferences', 'REMEMBER'] },
  { route: '/scheduler', markers: ['NEW JOB', 'ALL JOBS'] },
  { route: '/skills', markers: ['CAPABILITY PACKS', 'ALL TOOLS'] },
  { route: '/apps', markers: ['System overview', 'SPEND BY SKILL'] },
  { route: '/ledger', markers: ['SPEND BY SKILL', 'TURN HISTORY'] },
  { route: '/mission', markers: ['EVENT FEED', 'Sources'] },
  { route: '/settings', markers: ['READ-ONLY CONFIG', 'Environment'] },
  { route: '/code', markers: ['Explorer', 'read-only source', 'Problems'] },
];

for (const p of PAGES) {
  test(`page ${p.route} renders clean`, async ({ page }) => {
    const errors: string[] = [];
    page.on('console', (m) => {
      if (m.type() === 'error') errors.push(m.text().slice(0, 200));
    });
    page.on('pageerror', (e) => errors.push(String(e).slice(0, 200)));
    await page.goto(BASE + p.route, { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(2500);
    for (const m of p.markers) {
      await expect(page.getByText(m, { exact: false }).first()).toBeVisible({ timeout: 15000 });
    }
    // rail present on every page
    await expect(page.getByText('Scheduler', { exact: true }).first()).toBeVisible();
    const name = p.route.replace('/', '') || 'root';
    await page.screenshot({ path: `${SHOTS}/${name}.png` });
    expect(errors, `console errors on ${p.route}: ${errors.join(' | ')}`).toEqual([]);
  });
}

test('client-side nav keeps document alive', async ({ page }) => {
  await page.goto(BASE + '/research', { waitUntil: 'networkidle', timeout: 45000 });
  await page.waitForTimeout(1500);
  await page.getByRole('link', { name: 'Runs' }).click();
  await page.waitForTimeout(1500);
  await expect(page.getByText('Select a run', { exact: false }).first()).toBeVisible({ timeout: 15000 });
});

/* Every section flashed a blank frame before its list arrived. The cause was
   the Suspense fallback: it drew two short bars centred on a full-bleed black
   panel and replaced the whole shell, so any hop to a not-yet-visited route
   looked momentarily empty. Two things must hold now —

   1. when Suspense does fire, the shell skeleton is on screen (nav strip +
      header), never an empty void; and
   2. after the first idle warm-up, client-side navigation resolves from the
      module cache without suspending at all.

   (2) is the one that actually fixes the symptom: sampling the first paint
   of a route would only ever catch the fallback, never the flash the user
   reported between two already-loaded routes. */
test('route navigation never blanks the shell', async ({ page }) => {
  await page.goto(BASE + '/research', { waitUntil: 'networkidle', timeout: 45000 });
  /* let the idle preloader warm every split chunk */
  await page.waitForTimeout(3000);

  const links = ['Runs', 'Memory', 'Documents', 'Research', 'Skills', 'Apps', 'Ledger', 'Code'];
  for (const label of links) {
    await page.getByRole('link', { name: label, exact: true }).click();
    /* Sample every animation frame right after the click: if the shell were
       being torn down by Suspense, at least one sample would find the page
       with no nav links at all. */
    const blank = await page.evaluate(async () => {
      for (let i = 0; i < 12; i++) {
        await new Promise((r) => requestAnimationFrame(r));
        const nav = document.querySelectorAll('aside a[aria-label]').length;
        if (nav < 4) return `only ${nav} nav links after ${i} frames`;
      }
      return null;
    });
    expect(blank, `shell blanked while navigating to ${label}`).toBeNull();
  }
});

/* The test above proves navigation never suspends, which also means it can
   never exercise the fallback. Stall one chunk on purpose so the fallback
 * really renders, and assert it draws the shell (nav column + header row)
   rather than the old centred two-bar void. Without this the fallback could
   regress to a black screen and the suite would stay green, because the
   preloader would keep hiding it. */
/* The test above proves navigation never suspends, which also means it can
   never exercise the fallback. Throttle the network and cold-load a route so
   its chunk really is still in flight, then assert the fallback draws the
   shell (nav skeleton + header) rather than the old centred two-bar void.
   Without this the fallback could regress to a black screen and the suite
   would stay green, because the preloader would keep hiding it.

   Throttling rather than intercepting the chunk: a held request interacts
   badly with `networkidle` (which then blocks on the very request being held)
   and leaves the lazy import pending, so the fallback is never observable. */
test('route fallback keeps the shell visible while a chunk is in flight', async ({ page }) => {
  const cdp = await page.context().newCDPSession(page);
  await cdp.send('Network.enable');
  await cdp.send('Network.emulateNetworkConditions', {
    offline: false,
    latency: 300,
    downloadThroughput: 30 * 1024,
    uploadThroughput: 30 * 1024,
  });
  await page.goto(BASE + '/code', { waitUntil: 'commit', timeout: 60000 });

  /* Note on what is being asserted: a skeleton has no text, so
     `body.innerText` is legitimately empty and says nothing about whether the
     frame looks blank. What distinguishes the shell fallback from the old
     "two bars centred on black" is that it fills the same geometry as the
     real view — a full-height nav column and a full-width content area — so
     those boxes are what gets measured. */
  const shell = await page.evaluate(async () => {
    for (let i = 0; i < 240; i++) {
      await new Promise((r) => requestAnimationFrame(r));
      const aside = document.querySelector('aside');
      const main = document.querySelector('main');
      if (!aside || !main) continue;
      const aBox = aside.getBoundingClientRect();
      const mBox = main.getBoundingClientRect();
      const rows = aside.querySelectorAll('.animate-pulse').length;
      const content = Array.from(main.querySelectorAll('.animate-pulse'));
      const widest = content.reduce((w, el) => Math.max(w, el.getBoundingClientRect().width), 0);
      const contentRows = content.length;
      if (rows > 0 && contentRows > 0) {
        return {
          rows,
          contentRows,
          railH: aBox.height,
          mainW: mBox.width,
          widest,
          vw: window.innerWidth,
          vh: window.innerHeight,
        };
      }
    }
    return null;
  });
  expect(shell, 'fallback never rendered a shell skeleton').not.toBeNull();
  expect(shell!.rows, 'fallback nav column had no skeleton rows').toBeGreaterThan(4);
  expect(shell!.contentRows, 'fallback content column had no skeleton rows').toBeGreaterThan(2);
  /* The rail must span the viewport, not sit as a sliver. */
  expect(shell!.railH, 'fallback rail is not full height').toBeGreaterThan(shell!.vh * 0.5);
  /* …and the content skeleton must be as wide as the real content area, so
     the frame reads as a loading shell rather than an empty page. */
  expect(shell!.widest, 'fallback content skeleton is too narrow')
    .toBeGreaterThan(shell!.vw * 0.4);

  /* …and it still resolves into the real view once the chunk arrives. */
  await expect(page.getByText('Explorer', { exact: true })).toBeVisible({ timeout: 60000 });
});

test('code section edits a local draft and never claims it saved', async ({ page }) => {
  await page.goto(BASE + '/code', { waitUntil: 'networkidle', timeout: 45000 });
  await page.getByRole('button', { name: /^flow\.py$/ }).first().click();
  const ta = page.getByRole('textbox', { name: /Editor for/ });
  await expect(ta).toBeVisible({ timeout: 15000 });
  /* Real content, served by the agent - not a placeholder. */
  await expect(ta).toHaveValue(/import|def |class /);

  await ta.fill('const marker = "aria-code-draft";');
  await expect(page.getByText('browser draft')).toBeVisible();
  /* The status bar must keep stating that nothing reaches disk. */
  await expect(page.getByText(/nothing is written to disk/)).toBeVisible();

  /* …and Ctrl+S must not pretend to save. */
  await ta.click();
  await page.keyboard.press('Control+s');
  await expect(page.getByText(/nothing is written to disk — this is a browser draft/)).toBeVisible();

  /* Revert puts back exactly what the agent serves and clears the dirty mark. */
  await page.keyboard.press('Control+Shift+P');
  await page.getByRole('textbox', { name: 'Command' }).fill('Revert File');
  await page.keyboard.press('Enter');
  await expect(ta).toHaveValue(/import|def |class /);
  await expect(page.getByText('browser draft')).toHaveCount(0);
});

test('code section serves real files from both workspace roots', async ({ page }) => {
  await page.goto(BASE + '/code', { waitUntil: 'networkidle', timeout: 45000 });
  /* Both roots are listed, and each shows its own top-level files. */
  await expect(page.getByTitle('S9SharedCode/code', { exact: true })).toBeVisible({ timeout: 15000 });
  await expect(page.getByTitle('llm_gatewayV9', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: /^agent_server\.py$/ })).toBeVisible();
  /* Hidden files and vendored trees are not offered. */
  await expect(page.getByTitle(/node_modules/)).toHaveCount(0);
  await expect(page.getByTitle(/^\.env/)).toHaveCount(0);
});

test('quick open finds a file that is not open yet', async ({ page }) => {
  await page.goto(BASE + '/code', { waitUntil: 'networkidle', timeout: 45000 });
  await page.getByRole('button', { name: /^flow\.py$/ }).first().click();
  await expect(page.locator('[role="tab"]')).toHaveCount(1);

  await page.keyboard.press('Control+p');
  const box = page.getByRole('textbox', { name: 'Search files by name' });
  await expect(box).toBeVisible({ timeout: 10000 });
  /* The index covers the whole workspace, not just open tabs. */
  await box.fill('chunker');
  await expect(page.getByRole('dialog', { name: 'Go to file' }).getByText('chunker.py').first()).toBeVisible();
  await page.keyboard.press('Enter');

  await expect(page.locator('[role="tab"]')).toHaveCount(2);
  const ta = page.getByRole('textbox', { name: /Editor for/ });
  await expect(ta).toBeVisible();
  await expect(ta).toHaveValue(/Structure-aware chunking/);
});

test('command palette toggles the minimap', async ({ page }) => {
  await page.goto(BASE + '/code', { waitUntil: 'networkidle', timeout: 45000 });
  await page.getByRole('button', { name: /^flow\.py$/ }).first().click();
  await expect(page.getByRole('textbox', { name: /Editor for/ })).toBeVisible({ timeout: 15000 });
  const before = await page.locator('[aria-hidden="true"].border-l').count();

  await page.keyboard.press('Control+Shift+P');
  await page.getByRole('textbox', { name: 'Command' }).fill('Toggle Minimap');
  await page.keyboard.press('Enter');
  await expect(page.getByRole('button', { name: 'Toggle minimap' })).toHaveAttribute('aria-pressed', 'false');
  expect(await page.locator('[aria-hidden="true"].border-l').count()).toBeLessThan(before);
});

test('find reports a real match count for the open buffer', async ({ page }) => {
  await page.goto(BASE + '/code', { waitUntil: 'networkidle', timeout: 45000 });
  await page.getByRole('button', { name: /^flow\.py$/ }).first().click();
  const ta = page.getByRole('textbox', { name: /Editor for/ });
  await expect(ta).toBeVisible({ timeout: 15000 });

  await ta.click();
  await page.keyboard.press('Control+f');
  const find = page.getByRole('textbox', { name: 'Find' });
  await expect(find).toBeVisible();
  await find.fill('def ');
  /* The count must be a positive integer, not "no results" - flow.py is full
     of `def `. A hardcoded 0 here is what a non-functional finder returns. */
  await expect(page.getByText(/^\d+ matches?$/)).toBeVisible({ timeout: 10000 });
  await page.keyboard.press('Escape');
  await expect(find).toHaveCount(0);
});

test('toggle line comment edits the buffer and marks the tab dirty', async ({ page }) => {
  await page.goto(BASE + '/code', { waitUntil: 'networkidle', timeout: 45000 });
  await page.getByRole('button', { name: /^flow\.py$/ }).first().click();
  const ta = page.getByRole('textbox', { name: /Editor for/ });
  await expect(ta).toBeVisible({ timeout: 15000 });
  const before = await ta.inputValue();

  await ta.click();
  await page.keyboard.press('Control+Home');
  await page.keyboard.press('Shift+ArrowDown');
  await page.keyboard.press('Control+/');
  await expect(ta).not.toHaveValue(before);
  expect(await ta.inputValue()).toMatch(/^# /);
  await expect(page.getByText('browser draft')).toBeVisible();

  /* …and toggling again removes it, which proves the comment is symmetric
     rather than a one-way prefix. */
  await page.keyboard.press('Control+/');
  await expect(ta).toHaveValue(before);
});

test('go to line moves the caret and the status bar agrees', async ({ page }) => {
  await page.goto(BASE + '/code', { waitUntil: 'networkidle', timeout: 45000 });
  await page.getByRole('button', { name: /^flow\.py$/ }).first().click();
  await expect(page.getByRole('textbox', { name: /Editor for/ })).toBeVisible({ timeout: 15000 });

  await page.keyboard.press('Control+g');
  const box = page.getByRole('textbox', { name: 'Line number' });
  await expect(box).toBeVisible();
  await box.fill('40');
  await page.keyboard.press('Enter');
  await expect(page.getByText('Ln 40, Col 1')).toBeVisible({ timeout: 10000 });
});

test('code section offers no way to run or write anything', async ({ page }) => {
  await page.goto(BASE + '/code', { waitUntil: 'networkidle', timeout: 45000 });
  await page.getByRole('button', { name: /^flow\.py$/ }).first().click();
  await expect(page.getByRole('textbox', { name: /Editor for/ })).toBeVisible({ timeout: 15000 });

  /* Honest about what it cannot do: real syntax diagnostics, but no type
     information and no way to run anything. */
  await expect(page.getByText(/Type errors, completion and go-to-definition/)).toBeVisible();
  await expect(page.getByText(/Run and agent actions stay disabled/)).toBeVisible();
  /* No save-to-disk or run control anywhere in the section. */
  await expect(page.getByRole('button', { name: /^(Save|Run|Execute|Apply)$/ })).toHaveCount(0);
  /* …and the browser-draft wording is present rather than a bare "Saved". */
  await expect(page.getByText('Saved')).toHaveCount(0);
});

/* ── Phase 1: the highlight overlay must sit exactly on the text ────────────
   The overlay technique (coloured <pre> behind a transparent textarea) only
   works if both layers have identical geometry. It did not: the gutter was
   floated AND the textarea carried a matching margin, so the textarea was
   offset twice while the overlay was offset once and every line looked
   clipped by a character. Nothing caught it except measuring both boxes. */
test('highlight overlay is pixel-aligned with the textarea', async ({ page }) => {
  await page.goto(BASE + '/code', { waitUntil: 'networkidle', timeout: 45000 });
  await page.getByRole('button', { name: /^flow\.py$/ }).first().click();
  await expect(page.getByRole('textbox', { name: /Editor for/ })).toBeVisible({ timeout: 15000 });
  await page.waitForTimeout(600);

  const geo = await page.evaluate(() => {
    const ta = document.querySelector('textarea[aria-label^="Editor for"]')!;
    const pre = document.querySelector('pre')!;
    const a = ta.getBoundingClientRect();
    const b = pre.getBoundingClientRect();
    const cs = getComputedStyle(ta);
    const cp = getComputedStyle(pre);
    return {
      dx: b.left - a.left, dy: b.top - a.top, dw: b.width - a.width,
      pad: [cs.paddingLeft, cp.paddingLeft],
      lh: [cs.lineHeight, cp.lineHeight],
      fs: [cs.fontSize, cp.fontSize],
    };
  });
  expect(Math.abs(geo.dx), 'overlay x offset').toBeLessThan(0.5);
  expect(Math.abs(geo.dy), 'overlay y offset').toBeLessThan(0.5);
  expect(Math.abs(geo.dw), 'overlay width').toBeLessThan(0.5);
  expect(geo.pad[0]).toBe(geo.pad[1]);
  expect(geo.lh[0]).toBe(geo.lh[1]);
  expect(geo.fs[0]).toBe(geo.fs[1]);
});

test('code is syntax highlighted, not flat monospace', async ({ page }) => {
  await page.goto(BASE + '/code', { waitUntil: 'networkidle', timeout: 45000 });
  await page.getByRole('button', { name: /^flow\.py$/ }).first().click();
  await expect(page.getByRole('textbox', { name: /Editor for/ })).toBeVisible({ timeout: 15000 });
  await page.waitForTimeout(700);

  const kinds = await page.evaluate(() => {
    const pre = document.querySelector('pre')!;
    const set = new Set<string>();
    for (const el of pre.querySelectorAll('span')) {
      for (const c of el.className.split(/\s+/)) if (c) set.add(c);
    }
    return [...set];
  });
/* flow.py has keywords, builtins, strings, comments and numbers, so all of
     these token classes must be present. One flat colour means the highlighter
     is not running at all.
     The comment token is `text-zinc-muted`, not `text-zinc-500`: the theme's
     own index.css records that zinc-500 is ~3.9:1 on these panels, which fails
     WCAG AA at 12.5px, and introduces --color-zinc-muted (5.6:1) to replace
     it. Comments are the most-read token class in the editor, so this one
     matters more than the others. */
    for (const cls of ['text-purple-300', 'text-sky-300', 'text-amber-300', 'text-zinc-muted']) {
      expect(kinds, `no ${cls} tokens rendered`).toContain(cls);
    }
    /* Guard the regression directly: the failing-AA tiers must not come back. */
    expect(kinds, 'comment tokens are back on a WCAG-failing colour')
      .not.toContain('text-zinc-500');
});

/* ── Phase 4: real diagnostics ─────────────────────────────────────────────
   The Problems panel used to say "no language server" and show nothing. It now
   asks the server to parse the draft with Python's own ast module, so a real
   syntax error must appear with a real position and disappear when fixed. */
test('a real python syntax error is reported and then cleared', async ({ page }) => {
  await page.goto(BASE + '/code', { waitUntil: 'networkidle', timeout: 45000 });
  await page.getByRole('button', { name: /^flow\.py$/ }).first().click();
  const ta = page.getByRole('textbox', { name: /Editor for/ });
  await expect(ta).toBeVisible({ timeout: 15000 });
  await page.waitForTimeout(800);
  const original = await ta.inputValue();

  /* Clean file: no errors, and it says which parser ran. */
  await expect(page.getByText(/No syntax errors/)).toBeVisible({ timeout: 15000 });

  await ta.click();
  await page.keyboard.press('Control+Home');
  await page.keyboard.type('def broken(:');
  await expect(page.getByText(/invalid syntax/)).toBeVisible({ timeout: 15000 });
  /* A position, not just a message. */
  await expect(page.getByText(/^1:\d+$/)).toBeVisible();

  /* Remove exactly what was typed. Typing `(` also inserts its closing
     partner, so the inserted text is longer than what was typed - and
     over-deleting would eat a quote from the real first line and produce a
     DIFFERENT syntax error, which would make this test pass for the wrong
     reason. */
  const after = await ta.inputValue();
  const inserted = after.length - original.length;
  await ta.click();
  await page.keyboard.press('Control+Home');
  for (let i = 0; i < inserted; i++) await page.keyboard.press('Delete');
  await expect(ta).toHaveValue(original);
  await expect(page.getByText(/invalid syntax/)).toHaveCount(0, { timeout: 15000 });
  await expect(page.getByText(/No syntax errors/)).toBeVisible();
});

/* ── Phase 3: undo/redo ─────────────────────────────────────────────────────
   Structural edits are applied by writing to textarea.value, which native
   Ctrl+Z cannot see. Without an explicit stack, undo restores a stale buffer
   and redo is impossible. */
test('undo and redo restore the buffer exactly', async ({ page }) => {
  await page.goto(BASE + '/code', { waitUntil: 'networkidle', timeout: 45000 });
  await page.getByRole('button', { name: /^flow\.py$/ }).first().click();
  const ta = page.getByRole('textbox', { name: /Editor for/ });
  await expect(ta).toBeVisible({ timeout: 15000 });
  await page.waitForTimeout(800);
  const original = await ta.inputValue();

  await ta.click();
  await page.keyboard.press('Control+Home');
  await page.keyboard.type('MARKER = 1');
  await expect(ta).not.toHaveValue(original);
  await expect(page.getByText('browser draft')).toBeVisible();

  await page.keyboard.press('Control+z');
  await expect(ta).toHaveValue(original, { timeout: 10000 });
  await expect(page.getByText('browser draft')).toHaveCount(0);

  /* Redo must work too - this is what broke when restoring from history
     re-recorded a snapshot and wiped the redo stack. */
  await page.keyboard.press('Control+y');
  await expect(ta).not.toHaveValue(original, { timeout: 10000 });
  await page.keyboard.press('Control+z');
  await expect(ta).toHaveValue(original, { timeout: 10000 });
});

test('undo reverts a structural edit, not just typing', async ({ page }) => {
  await page.goto(BASE + '/code', { waitUntil: 'networkidle', timeout: 45000 });
  await page.getByRole('button', { name: /^flow\.py$/ }).first().click();
  const ta = page.getByRole('textbox', { name: /Editor for/ });
  await expect(ta).toBeVisible({ timeout: 15000 });
  await page.waitForTimeout(800);
  const original = await ta.inputValue();

  await ta.click();
  await page.keyboard.press('Control+Home');
  await page.keyboard.press('Shift+ArrowDown');
  await page.keyboard.press('Control+/');
  await expect(ta).not.toHaveValue(original);
  await page.keyboard.press('Control+z');
  await expect(ta).toHaveValue(original, { timeout: 10000 });
});

test('bracket auto-close and pair delete', async ({ page }) => {
  await page.goto(BASE + '/code', { waitUntil: 'networkidle', timeout: 45000 });
  await page.getByRole('button', { name: /^flow\.py$/ }).first().click();
  const ta = page.getByRole('textbox', { name: /Editor for/ });
  await expect(ta).toBeVisible({ timeout: 15000 });
  await page.waitForTimeout(800);
  const original = await ta.inputValue();

  await ta.click();
  await page.keyboard.press('Control+Home');
  await page.keyboard.type('zz = f(');
  /* The closing paren is inserted and the caret lands between the pair. */
  await expect(ta).toHaveValue(/^zz = f\(\)/);
  const caret = await ta.evaluate((el) => el.selectionStart);
  expect(caret, 'caret should sit between the brackets').toBe(7);

  /* Backspace between an auto-inserted pair removes BOTH, not just the opener. */
  await page.keyboard.press('Backspace');
  await expect(ta).toHaveValue(/^zz = f"""/);

  /* Undo until the served text is back. The exact number of steps is an
     implementation detail (coalesced typing, the auto-close insert and the
     pair delete are three separate history entries), so this asserts the
     property that matters - undo walks all the way back - rather than a count
     that would break the next time the coalescing window changes. */
  for (let i = 0; i < 8; i++) {
    if ((await ta.inputValue()) === original) break;
    await page.keyboard.press('Control+z');
    await page.waitForTimeout(250);
  }
  await expect(ta).toHaveValue(original, { timeout: 10000 });
});

/* ── Phase 2: structure ───────────────────────────────────────────────────── */
test('code folds a region and says so', async ({ page }) => {
  await page.goto(BASE + '/code', { waitUntil: 'networkidle', timeout: 45000 });
  await page.getByRole('button', { name: /^flow\.py$/ }).first().click();
  await expect(page.getByRole('textbox', { name: /Editor for/ })).toBeVisible({ timeout: 15000 });
  await page.waitForTimeout(900);

  /* The fold control has to be reachable: the gutter used to be aria-hidden,
     which put a real button outside the accessibility tree. */
  const chevron = page.getByRole('button', { name: /^Fold line/ }).first();
  await expect(chevron).toBeVisible();
  await chevron.click();
  await expect(page.getByText(/folded/)).toBeVisible();
  await expect(page.getByText(/folded/)).toHaveCount(1);

  /* Unfold again. */
  await page.getByRole('button', { name: /^Fold line/ }).first().click();
  await expect(page.getByText(/folded/)).toHaveCount(0);
});

test('symbol outline lists classes and functions from the buffer', async ({ page }) => {
  await page.goto(BASE + '/code', { waitUntil: 'networkidle', timeout: 45000 });
  await page.getByRole('button', { name: /^flow\.py$/ }).first().click();
  await expect(page.getByRole('textbox', { name: /Editor for/ })).toBeVisible({ timeout: 15000 });
  await page.waitForTimeout(900);

  await page.getByRole('button', { name: 'Toggle Symbol Outline' }).click();
  const outline = page.getByRole('complementary', { name: 'Symbol outline' });
  await expect(outline).toBeVisible();
  /* flow.py defines Graph and Executor classes and several module functions. */
  await expect(outline.getByRole('button', { name: /Graph/ }).first()).toBeVisible();
  await expect(outline.getByRole('button', { name: /Executor/ }).first()).toBeVisible();
  await expect(outline.getByRole('button', { name: /_budget_note/ }).first()).toBeVisible();
});

/* ── Phase 5: workspace search ────────────────────────────────────────────── */
test('workspace search finds a symbol and jumps to its line', async ({ page }) => {
  await page.goto(BASE + '/code', { waitUntil: 'networkidle', timeout: 45000 });
  await page.keyboard.press('Control+Shift+F');
  const box = page.getByRole('textbox', { name: 'Search across the workspace' });
  await expect(box).toBeVisible({ timeout: 10000 });
  await box.fill('class EmbedderError');
  await expect(page.getByText(/matches?$/)).toBeVisible({ timeout: 30000 });

  /* Two characters is refused rather than scanning the tree. */
  await box.fill('cl');
  await expect(page.getByText('type 3+ characters')).toBeVisible();

  await box.fill('DOCUMENT_EMBED_BATCH');
  const hit = page.locator('button', { hasText: 'DOCUMENT_EMBED_BATCH' }).first();
  await expect(hit).toBeVisible({ timeout: 30000 });
  await hit.click();
  await expect(page.getByRole('textbox', { name: /Editor for/ })).toBeVisible({ timeout: 15000 });
  /* The caret lands in the file that was clicked. */
  const ta = page.getByRole('textbox', { name: /Editor for/ });
  await expect(ta).toBeVisible();
});

test('keyboard shortcut sheet opens and closes', async ({ page }) => {
  await page.goto(BASE + '/code', { waitUntil: 'networkidle', timeout: 45000 });
  await page.keyboard.press('Control+,');
  const sheet = page.getByRole('dialog', { name: 'Keyboard shortcuts' });
  await expect(sheet).toBeVisible({ timeout: 10000 });
  /* It has to be honest about the one shortcut that does nothing. */
  await expect(sheet.getByText('Ctrl+S', { exact: true }).first()).toBeVisible();
  await expect(sheet.getByText(/drafts are browser-only/)).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(sheet).toHaveCount(0);
});


test('api health fast', async ({ request }) => {
  const t0 = Date.now();
  const r = await request.get(BASE + '/api/health');
  expect(r.ok()).toBeTruthy();
  expect(Date.now() - t0).toBeLessThan(3000);
});

/* The empty Code page fits on a phone, but the editor does not necessarily:
   the gutter, the textarea and the minimap are three fixed-width pieces in a
   row, and a long line with wrap off sets an explicit `ch` width. Open a real
   file and check again, otherwise the loop above passes on a page that never
   rendered an editor. */
test('code editor does not overflow a phone with a file open', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(BASE + '/code', { waitUntil: 'networkidle', timeout: 45000 });
  await page.getByRole('button', { name: /^flow\.py$/ }).first().click();
  await expect(page.getByRole('textbox', { name: /Editor for/ })).toBeVisible({ timeout: 15000 });
  await page.waitForTimeout(600);
  const { doc, win } = await page.evaluate(() => ({
    doc: document.documentElement.scrollWidth,
    win: window.innerWidth,
  }));
  expect(doc, `/code overflows with a file open: document ${doc}px in a ${win}px viewport`)
    .toBeLessThanOrEqual(win + 1);
});

/* The shell was a fixed 52px rail + fixed 248px list pane in a row, so on a
   390px phone the content column collapsed to ~90px: every page scrolled
   sideways and rendered one letter per line. Nothing in the suite above
   noticed, because a full-page screenshot is as wide as the overflow and
   still "looks fine". Assert the invariant on a narrow viewport instead —
   it is cheap, and it is the only assertion that would have caught it. */
for (const p of PAGES) {
  test(`page ${p.route} does not overflow horizontally on a phone`, async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(BASE + p.route, { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(1500);
    const { doc, win } = await page.evaluate(() => ({
      doc: document.documentElement.scrollWidth,
      win: window.innerWidth,
    }));
    expect(doc, `${p.route} overflows: document ${doc}px wide in a ${win}px viewport`)
      .toBeLessThanOrEqual(win + 1);
  });
}

/* The rail collapses to icons below lg; those icons are the only navigation
   on a phone, so each has to keep a usable tap target and an accessible
   name rather than shrinking to a 16px glyph. */
test('rail icons stay labelled and tappable when collapsed', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(BASE + '/research', { waitUntil: 'networkidle', timeout: 45000 });
  const links = page.locator('nav[aria-label="Primary"] a');
  const n = await links.count();
  expect(n).toBeGreaterThanOrEqual(10);
  for (let i = 0; i < n; i += 1) {
    const el = links.nth(i);
    await expect(el).toHaveAttribute('aria-label', /\w/);
    const box = await el.boundingBox();
    expect(box, `rail item ${i} is not laid out`).not.toBeNull();
    expect(box!.width, `rail item ${i} tap width`).toBeGreaterThanOrEqual(32);
    expect(box!.height, `rail item ${i} tap height`).toBeGreaterThanOrEqual(32);
  }
});

/* The Memory page gained a per-item "forget" control because the only
   deletion path used to be a whole-drawer wipe, which made a single
   mis-captured preference impossible to remove. It deletes real state, so
   this test creates its own row, removes it, and proves the row is gone —
   it must never be run against a user's existing memory. */
test('memory page can forget one row without wiping the rest', async ({ page, request }) => {
  const marker = 'zz-e2e-forget-probe';
  const created = await request.post(BASE + '/api/memory/remember', {
    data: { kind: 'fact', descriptor: marker, keywords: ['zz-probe'] },
  });
  expect(created.ok(), `seed failed: ${created.status()}`).toBeTruthy();
  const id = (await created.json()).id;
  expect(id).toBeTruthy();

  // Read the whole store, not a page of it. The count assertion used to
  // derive `before - 1` from a `limit=200` read, so it silently stopped
  // holding once the store passed 200 rows: both reads returned exactly 200
  // and the delete was reported as a no-op even though it had worked. It
  // failed only because ordinary use grew the store past the limit.
  const before = await (await request.get(BASE + '/api/memory?limit=1000')).json();
  const beforeIds = new Set<string>(before.items.map((i: { id: string }) => i.id));
  // A read that hit the limit is a page, not the store, and every count below
  // would be meaningless. Fail loudly instead of asserting on a truncated list.
  expect(before.items.length, 'memory read was truncated; raise the limit')
    .toBeLessThan(1000);

  page.on('dialog', (d) => void d.accept());
  await page.goto(BASE + '/memory', { waitUntil: 'networkidle', timeout: 45000 });
  const btn = page.locator(`[aria-label="Forget memory ${id}"]`);
  await expect(btn).toBeVisible({ timeout: 15000 });
  await btn.click();

  // Row disappears from the page...
  await expect(btn).toHaveCount(0, { timeout: 15000 });
  // ...and is really gone from the store, without a full wipe.
  const after = await (await request.get(BASE + '/api/memory?limit=1000')).json();
  const afterIds = new Set<string>(after.items.map((i: { id: string }) => i.id));
  expect(afterIds.has(id)).toBeFalsy();
  expect(afterIds.size).toBe(beforeIds.size - 1);
  // Every other row survived - this is the "without wiping the rest" claim.
  expect([...beforeIds].filter((x) => x !== id && !afterIds.has(x))).toEqual([]);
});

/* The 404 must be truthful: deleting an id that is not there is a 404, and
   the UI must surface it rather than reporting a successful delete. */
test('forgetting a missing memory reports not-found', async ({ request }) => {
  const r = await request.delete(BASE + '/api/memory/mem:e2e-not-a-real-id', { failOnStatusCode: false });
  expect(r.status()).toBe(404);
});

/* ── Graceful degradation: the console must never assert a clean bill of
   health it could not actually verify. Each of these views used to render a
   confident all-clear when its read failed: Mission showed "● live" with
   ERRORS 0 in the OK tone, Skills showed "40/40 tools live / WITHHELD none",
   and Apps showed "No flags defined" / "$0.0000" as if those were facts. */
const fail = (pattern: RegExp, status = 500) => async (route: import('@playwright/test').Route) => {
  await route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify({ error: 'zzprobe injected failure' }),
  });
  void pattern;
};

test('mission admits it cannot read the event feed', async ({ page }) => {
  await page.route('**/api/events*', fail(/events/));
  const errors: string[] = [];
  page.on('pageerror', (e) => errors.push(String(e).slice(0, 200)));
  await page.goto(BASE + '/mission', { waitUntil: 'networkidle', timeout: 45000 });
  await page.waitForTimeout(2500);
  // The live pill must not claim liveness.
  await expect(page.getByText('● live', { exact: false }).first()).toHaveCount(0);
  await expect(page.getByText(/feed error/i).first()).toBeVisible({ timeout: 15000 });
  // And the error count must be unknown, not zero.
  await expect(page.getByText(/feed unreachable/i).first()).toBeVisible();
  expect(errors, 'page errors on /mission').toEqual([]);
});

test('skills admits it does not know which tools are withheld', async ({ page }) => {
  await page.route('**/api/config/tools*', fail(/config\/tools/));
  await page.goto(BASE + '/skills', { waitUntil: 'networkidle', timeout: 45000 });
  await page.waitForTimeout(2500);
  // The false all-clear must be gone.
  await expect(page.getByText(/WITHHELD none/i).first()).toHaveCount(0);
  await expect(page.getByText(/guard read failed|guard state unknown/i).first())
    .toBeVisible({ timeout: 15000 });
});

test('a failed flag toggle says it reverted', async ({ page }) => {
  await page.route('**/api/apps/flags*', (route) =>
    route.request().method() === 'POST'
      ? route.fulfill({ status: 500, contentType: 'application/json',
                        body: JSON.stringify({ error: 'zzprobe flag write failed' }) })
      : route.continue());
  await page.goto(BASE + '/apps', { waitUntil: 'networkidle', timeout: 45000 });
  await page.waitForTimeout(2500);
  const sw = page.locator('[role="switch"]').first();
  await expect(sw).toBeVisible({ timeout: 15000 });
  await sw.click();
  await expect(page.getByText(/toggle reverted/i).first())
    .toBeVisible({ timeout: 15000 });
});

test('ledger refuses to show all-time spend for an empty conversation scope',
  async ({ page }) => {
    await page.goto(BASE + '/ledger', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(2000);
    await page.getByText('One conversation', { exact: false }).first().click();
    await page.waitForTimeout(2000);
    // Previously this silently fell back to all-time totals.
    await expect(page.getByText(/Pick a conversation/i).first())
      .toBeVisible({ timeout: 15000 });
  });

test('thread delete asks before deleting', async ({ page }) => {
  await page.goto(BASE + '/console', { waitUntil: 'networkidle', timeout: 45000 });
  await page.waitForTimeout(2500);
  const del = page.locator('button[aria-label^="Delete thread"]').first();
  if (await del.count()) {
    let asked = false;
    page.on('dialog', async (d) => { asked = true; await d.dismiss(); });
    await del.click({ force: true });
    await page.waitForTimeout(1200);
    expect(asked, 'delete must confirm with the user').toBeTruthy();
  }
});

test('unknown routes serve the app shell, not raw JSON', async ({ request }) => {
  const r = await request.get(BASE + '/zzprobe-no-such-route', { failOnStatusCode: false });
  const ct = (r.headers()['content-type'] || '');
  expect(ct, `unknown route returned ${ct}`).toContain('text/html');
});

/* The Documents page: upload, per-document enable/disable, click-through
   detail, and the retrieval-only test-search. Each test creates its own
   document and deletes it afterwards, so nothing depends on pre-existing
   state. */
const DOC_TEXT = [
  '# Operations Runbook',
  '',
  '## Restart procedure',
  '',
  'To restart the ingest service, drain the queue first. The on-call engineer',
  'approves every production restart. Drain completes within four minutes.',
  '',
  '## Capacity planning',
  '',
  'Each ingest worker holds ZZCAPACITYTOKEN in memory. Adding workers is the',
  'only supported way to raise throughput on the ingest tier.',
].join('\n');

async function uploadDoc(page: import('@playwright/test').Page, name: string) {
  page.on('dialog', (d) => void d.accept());
  await page.goto(BASE + '/documents', { waitUntil: 'networkidle', timeout: 45000 });
  await page.setInputFiles('input[type="file"]', {
    name, mimeType: 'text/markdown', buffer: Buffer.from(DOC_TEXT, 'utf8'),
  });
}

test('documents page renders and accepts an upload', async ({ page }) => {
  await uploadDoc(page, 'zz-e2e-runbook.md');
  await expect(page.getByText('Upload documents').first())
    .toBeVisible({ timeout: 20000 });
  await expect(page.getByText('zz-e2e-runbook.md').first())
    .toBeVisible({ timeout: 45000 });
  await page.locator('[aria-label="Delete zz-e2e-runbook.md"]').click();
});

test('documents page exposes an enable control per document', async ({ page }) => {
  await uploadDoc(page, 'zz-e2e-toggle.md');
  await expect(page.getByText('zz-e2e-toggle.md').first())
    .toBeVisible({ timeout: 45000 });
  await expect(page.locator('[aria-label^="Enable zz-e2e-toggle.md"]'))
    .toBeVisible({ timeout: 45000 });
  await page.locator('[aria-label="Delete zz-e2e-toggle.md"]').click();
});

test('documents page has a retrieval-only test-search box', async ({ page }) => {
  await page.goto(BASE + '/documents', { waitUntil: 'networkidle', timeout: 45000 });
  await expect(page.getByLabel('Search enabled documents'))
    .toBeVisible({ timeout: 20000 });
  await expect(page.getByText('Upload documents').first()).toBeVisible();
});
