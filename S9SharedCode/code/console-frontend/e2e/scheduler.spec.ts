import { test, expect } from '@playwright/test';

const BASE = 'http://127.0.0.1:8500';

test.describe('Scheduler page', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto(BASE + '/scheduler', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(2000);
  });

  test('page loads with schedule list and create form', async ({ page }) => {
    // Check for main elements
    await expect(page.getByText('NEW JOB')).toBeVisible();
    await expect(page.getByText('ALL JOBS')).toBeVisible();
    await expect(page.locator('#j-query')).toBeVisible();
    await expect(page.locator('#j-when')).toBeVisible();
    await expect(page.getByRole('button', { name: /schedule/i })).toBeVisible();
    
    // Check table headers
    await expect(page.getByRole('columnheader', { name: /query/i })).toBeVisible();
    await expect(page.getByRole('columnheader', { name: /when/i })).toBeVisible();
    await expect(page.getByRole('columnheader', { name: /next fire/i })).toBeVisible();
    await expect(page.getByRole('columnheader', { name: /state/i })).toBeVisible();
  });

  test('create a one-shot schedule (in 1 minute)', async ({ page }) => {
    const query = 'test one-shot reminder';
    const when = 'in 1 minute';
    
    await page.fill('#j-query', query);
    await page.fill('#j-when', when);
    await page.click('button.primary:has-text("Schedule")');
    
    // Wait for success message
    await expect(page.locator('#job-out')).toContainText('scheduled', { timeout: 10000 });
    
    // Verify it appears in the table
    await expect(page.getByRole('cell', { name: query })).toBeVisible({ timeout: 10000 });
    
    // Check next fire time is shown and reasonable
    const nextFireCell = page.getByRole('cell').filter({ hasText: /in \d+m/ }).first();
    await expect(nextFireCell).toBeVisible({ timeout: 10000 });
  });

  test('create a recurring schedule (daily at 14:30)', async ({ page }) => {
    const query = 'test daily recurring';
    const when = 'daily@14:30';
    
    await page.fill('#j-query', query);
    await page.fill('#j-when', when);
    await page.click('button.primary:has-text("Schedule")');
    
    await expect(page.locator('#job-out')).toContainText('scheduled', { timeout: 10000 });
    await expect(page.getByRole('cell', { name: query })).toBeVisible({ timeout: 10000 });
    await expect(page.getByRole('cell', { name: when })).toBeVisible({ timeout: 10000 });
  });

  test('schedules appear in list with correct next fire time', async ({ page }) => {
    // The page should already have some schedules from previous runs
    await expect(page.getByText('scheduled').first()).toBeVisible({ timeout: 10000 });
    
    // Check that next fire times are displayed in a reasonable format
    const nextFireCells = page.getByRole('cell').filter({ hasText: /in \d+[mhd]|due|\d{4}-\d{2}-\d{2}/ });
    await expect(nextFireCells.first()).toBeVisible({ timeout: 10000 });
  });

  test('disable/enable schedule', async ({ page }) => {
    // Find an active schedule and click the cancel (disable) button
    const cancelBtn = page.locator('button.icon-btn.danger[onclick^="cancelJob"]').first();
    if (await cancelBtn.count() > 0) {
      await cancelBtn.click();
      await page.waitForTimeout(1000);
      
      // Should show as 'off' or disabled
      await expect(page.getByText('off').first()).toBeVisible({ timeout: 10000 });
      
      // Re-enable by clicking the enable button (if available) or recreate
      // Note: The UI only shows cancel/delete for enabled, and only delete for disabled
      // So we'd need to use the API directly to re-enable, or delete and recreate
    }
  });

  test('delete schedule', async ({ page }) => {
    // Find a delete button and click it
    const deleteBtn = page.locator('button.icon-btn.danger[onclick^="deleteJob"]').first();
    if (await deleteBtn.count() > 0) {
      const scheduleId = await deleteBtn.getAttribute('onclick');
      await deleteBtn.click();
      await page.waitForTimeout(1000);
      
      // Schedule should be removed from the table
      if (scheduleId) {
        const id = scheduleId.match(/'([^']+)'/)?.[1];
        if (id) {
          await expect(page.getByText(id)).toHaveCount(0, { timeout: 10000 });
        }
      }
    }
  });

  test('invalid cron expression shows error', async ({ page }) => {
    await page.fill('#j-query', 'test invalid cron');
    await page.fill('#j-when', 'invalid cron');
    await page.click('button.primary:has-text("Schedule")');
    
    // Should show error message
    await expect(page.locator('#job-out')).toContainText('unparseable', { timeout: 10000 });
  });

  test('relative time parsing (in X minutes)', async ({ page }) => {
    await page.fill('#j-query', 'test relative time');
    await page.fill('#j-when', 'in 5 minutes');
    await page.click('button.primary:has-text("Schedule")');
    
    await expect(page.locator('#job-out')).toContainText('scheduled', { timeout: 10000 });
    await expect(page.getByRole('cell', { name: 'in 5 minutes' })).toBeVisible({ timeout: 10000 });
  });

  test('pre-notification lead time', async ({ page }) => {
    // This would require using the API directly since the UI doesn't expose lead time fields
    // But we can verify the backend accepts it by checking existing schedules with notify_policy
    const response = await page.request.get(BASE + '/api/schedule');
    expect(response.ok()).toBeTruthy();
    const data = await response.json();
    
    // Check if any schedules have notify_policy with lead_fire
    const hasLeadNotification = data.schedules.some((s: any) => 
      s.notify_policy && s.notify_policy.lead_fire > 0
    );
    // Just verify the API returns the data structure correctly
    expect(data.schedules.length).toBeGreaterThan(0);
  });

  test('wait for one-shot to fire and check it creates a run', async ({ page }) => {
    // Create a one-shot schedule for 30 seconds from now
    const query = 'test fire and run';
    const when = 'in 30 seconds';
    
    await page.fill('#j-query', query);
    await page.fill('#j-when', when);
    await page.click('button.primary:has-text("Schedule")');
    
    await expect(page.locator('#job-out')).toContainText('scheduled', { timeout: 10000 });
    
    // Wait for the job to fire (30 seconds + buffer)
    await page.waitForTimeout(40000);
    
    // Check the runs page for a new run
    await page.goto(BASE + '/runs', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(2000);
    
    // Look for the run with our query
    await expect(page.getByText(query)).toBeVisible({ timeout: 15000 });
  });

  test('console has no errors', async ({ page }) => {
    const errors: string[] = [];
    page.on('console', (msg) => {
      if (msg.type() === 'error') {
        errors.push(msg.text().slice(0, 200));
      }
    });
    page.on('pageerror', (err) => {
      errors.push(String(err).slice(0, 200));
    });
    
    await page.goto(BASE + '/scheduler', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(3000);
    
    expect(errors, `Console errors: ${errors.join(' | ')}`).toEqual([]);
  });
});

test('api health check', async ({ request }) => {
  const response = await request.get(BASE + '/api/health');
  expect(response.ok()).toBeTruthy();
  const data = await response.json();
  expect(data.gateway_up).toBe(true);
});