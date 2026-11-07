import { test, expect } from '@playwright/test';

/* The studio's checks must run in THIS session's workspace, not the process
   global one. `/api/code/checks` only resolves session roots when the request
   carries ?sid=; without it the agent is fed another project's test output as
   its own diagnostics. Both the `/checks` command and the `#checks` composer
   source go through api.codeChecks, so both are pinned here. */
test.describe('Studio checks are session-scoped', () => {
  test('/checks and #checks send the session sid', async ({ page }) => {
    const checksSids: string[] = [];
    let changesSid = '';
    await page.route('**/api/code/checks*', (route) => {
      checksSids.push(new URL(route.request().url()).searchParams.get('sid') || '');
      return route.fulfill({ json: { ok: true, results: [] } });
    });
    await page.route('**/api/studio/changes*', (route) => {
      changesSid = new URL(route.request().url()).searchParams.get('sid') || '';
      return route.fulfill({ json: { changes: [] } });
    });

    await page.goto('http://127.0.0.1:8500/studio');
    await page.waitForLoadState('networkidle');
    await page.waitForSelector('nav[aria-label="Studio sessions"]');

    // Give the fresh session a folder so the composer is enabled.
    await page.getByRole('button', { name: 'Files', exact: true }).click();
    await page.getByRole('button', { name: 'Create an empty scratch folder for this session' }).click();
    const composer = page.getByRole('textbox', { name: 'Ask the agent for a change' });
    await expect(composer).toBeEnabled({ timeout: 15000 });
    await expect.poll(() => changesSid, { timeout: 15000 }).not.toBe('');

    // /checks command path (AgentPanel.runCommand).
    await composer.fill('/checks');
    await composer.press('Enter');
    await expect.poll(() => checksSids.length, { timeout: 15000 }).toBeGreaterThan(0);

    // #checks composer source path (Composer.runProblemCheck): second popup row.
    await composer.fill('#checks');
    await composer.press('ArrowDown');
    await composer.press('Enter');
    await expect.poll(() => checksSids.length, { timeout: 15000 }).toBeGreaterThan(1);

    expect(checksSids[0]).toBe(changesSid);
    expect(checksSids[1]).toBe(changesSid);
  });
});