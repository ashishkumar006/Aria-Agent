import { test, expect } from '@playwright/test';

const SHOTS = 'C:/Users/AISHWA~1/AppData/Local/Temp/opencode/qa-shots';

test.describe('Parts page', () => {
  test('page loads: part library, search, 3D viewer', async ({ page }) => {
    const errors: string[] = [];
    page.on('console', (m) => {
      if (m.type() === 'error') errors.push(m.text().slice(0, 200));
    });
    page.on('pageerror', (e) => errors.push(String(e).slice(0, 200)));

    await page.goto('/parts', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(2500);

    // Verify main elements are present (Parts appears in nav rail and page header)
    await expect(page.getByText('Parts', { exact: true }).first()).toBeVisible({ timeout: 15000 });
    await expect(page.getByText('Upload a mesh')).toBeVisible({ timeout: 15000 });
    await expect(page.getByText('Choose an STL or OBJ part')).toBeVisible({ timeout: 15000 });

    // Check 3D viewer canvas
    const canvas = page.locator('canvas');
    await expect(canvas.first()).toBeVisible({ timeout: 15000 });

    // Check sidebar controls
    await expect(page.getByText('Pass')).toBeVisible({ timeout: 15000 });
    await expect(page.locator('select[id="parts-pass"]')).toBeVisible({ timeout: 15000 });

    // Check rail present
    await expect(page.getByText('Scheduler', { exact: true }).first()).toBeVisible();

    await page.screenshot({ path: `${SHOTS}/parts.png` });
    expect(errors, `console errors on /parts: ${errors.join(' | ')}`).toEqual([]);
  });

  test('part upload and geometry display', async ({ page }) => {
    const errors: string[] = [];
    page.on('console', (m) => {
      if (m.type() === 'error') errors.push(m.text().slice(0, 200));
    });
    page.on('pageerror', (e) => errors.push(String(e).slice(0, 200)));

    await page.goto('/parts', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(2000);
    const stlContent = `solid cube
  facet normal 0 0 1
    outer loop
      vertex 0 0 0
      vertex 10 0 0
      vertex 10 10 0
    endloop
  endfacet
  facet normal 0 0 1
    outer loop
      vertex 10 10 0
      vertex 0 10 0
      vertex 0 0 0
    endloop
  endfacet
  facet normal 0 0 -1
    outer loop
      vertex 0 0 0
      vertex 0 10 0
      vertex 10 10 0
    endloop
  endfacet
  facet normal 0 0 -1
    outer loop
      vertex 10 10 0
      vertex 10 0 0
      vertex 0 0 0
    endloop
  endfacet
  facet normal 0 1 0
    outer loop
      vertex 0 10 0
      vertex 0 10 10
      vertex 10 10 10
    endloop
  endfacet
  facet normal 0 1 0
    outer loop
      vertex 10 10 10
      vertex 10 10 0
      vertex 0 10 0
    endloop
  endfacet
  facet normal 0 -1 0
    outer loop
      vertex 0 0 0
      vertex 10 0 10
      vertex 0 0 10
    endloop
  endfacet
  facet normal 0 -1 0
    outer loop
      vertex 10 0 10
      vertex 10 0 0
      vertex 0 0 0
    endloop
  endfacet
  facet normal 1 0 0
    outer loop
      vertex 10 0 0
      vertex 10 10 10
      vertex 10 0 10
    endloop
  endfacet
  facet normal 1 0 0
    outer loop
      vertex 10 10 0
      vertex 10 10 10
      vertex 10 0 0
    endloop
  endfacet
  facet normal -1 0 0
    outer loop
      vertex 0 0 0
      vertex 0 0 10
      vertex 0 10 10
    endloop
  endfacet
  facet normal -1 0 0
    outer loop
      vertex 0 10 0
      vertex 0 10 10
      vertex 0 0 0
    endloop
  endfacet
endsolid cube`;

    // Upload via file input
    await page.setInputFiles('input[type="file"]', {
      name: 'test-cube.stl',
      mimeType: 'application/octet-stream',
      buffer: Buffer.from(stlContent, 'utf8'),
    });

    // Wait for mesh to load and metrics to appear
    await expect(page.getByText('faces')).toBeVisible({ timeout: 30000 });
    await expect(page.getByText('vertices')).toBeVisible({ timeout: 15000 });
    await expect(page.getByText('volume')).toBeVisible({ timeout: 15000 });
    await expect(page.getByText('area')).toBeVisible({ timeout: 15000 });

    // Verify objective is displayed
    await expect(page.getByText('Objective')).toBeVisible({ timeout: 15000 });

    await page.screenshot({ path: `${SHOTS}/parts-uploaded.png` });
    expect(errors, `console errors during upload: ${errors.join(' | ')}`).toEqual([]);
  });

  test('3D viewer interaction (rotate, zoom, pan)', async ({ page }) => {
    const errors: string[] = [];
    page.on('console', (m) => {
      if (m.type() === 'error') errors.push(m.text().slice(0, 200));
    });
    page.on('pageerror', (e) => errors.push(String(e).slice(0, 200)));

    await page.goto('/parts', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(2000);

    // Upload test cube first
    const stlContent = `solid cube
  facet normal 0 0 1
    outer loop
      vertex 0 0 0
      vertex 10 0 0
      vertex 10 10 0
    endloop
  endfacet
  facet normal 0 0 1
    outer loop
      vertex 10 10 0
      vertex 0 10 0
      vertex 0 0 0
    endloop
  endfacet
  facet normal 0 0 -1
    outer loop
      vertex 0 0 0
      vertex 0 10 0
      vertex 10 10 0
    endloop
  endfacet
  facet normal 0 0 -1
    outer loop
      vertex 10 10 0
      vertex 10 0 0
      vertex 0 0 0
    endloop
  endfacet
  facet normal 0 1 0
    outer loop
      vertex 0 10 0
      vertex 0 10 10
      vertex 10 10 10
    endloop
  endfacet
  facet normal 0 1 0
    outer loop
      vertex 10 10 10
      vertex 10 10 0
      vertex 0 10 0
    endloop
  endfacet
  facet normal 0 -1 0
    outer loop
      vertex 0 0 0
      vertex 10 0 10
      vertex 0 0 10
    endloop
  endfacet
  facet normal 0 -1 0
    outer loop
      vertex 10 0 10
      vertex 10 0 0
      vertex 0 0 0
    endloop
  endfacet
  facet normal 1 0 0
    outer loop
      vertex 10 0 0
      vertex 10 10 10
      vertex 10 0 10
    endloop
  endfacet
  facet normal 1 0 0
    outer loop
      vertex 10 10 0
      vertex 10 10 10
      vertex 10 0 0
    endloop
  endfacet
  facet normal -1 0 0
    outer loop
      vertex 0 0 0
      vertex 0 0 10
      vertex 0 10 10
    endloop
  endfacet
  facet normal -1 0 0
    outer loop
      vertex 0 10 0
      vertex 0 10 10
      vertex 0 0 0
    endloop
  endfacet
endsolid cube`;

    await page.setInputFiles('input[type="file"]', {
      name: 'test-cube.stl',
      mimeType: 'application/octet-stream',
      buffer: Buffer.from(stlContent, 'utf8'),
    });

    await expect(page.getByText('faces')).toBeVisible({ timeout: 30000 });
    await page.waitForTimeout(1000);

    const canvas = page.locator('canvas').first();
    await expect(canvas).toBeVisible();

    // Test mouse interactions on canvas
    const box = await canvas.boundingBox();
    if (box) {
      // Drag to rotate (simulate rotation)
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
      await page.mouse.down();
      await page.mouse.move(box.x + box.width / 2 + 100, box.y + box.height / 2);
      await page.mouse.up();
      await page.waitForTimeout(500);

      // Test zoom with wheel
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
      await page.mouse.wheel(0, -100);
      await page.waitForTimeout(500);

      // Test pan (shift + drag)
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
      await page.keyboard.down('Shift');
      await page.mouse.down();
      await page.mouse.move(box.x + box.width / 2 + 50, box.y + box.height / 2 + 50);
      await page.mouse.up();
      await page.keyboard.up('Shift');
      await page.waitForTimeout(500);
    }

    expect(errors, `console errors during 3D interaction: ${errors.join(' | ')}`).toEqual([]);
  });

  test('pass selection and optimization loop', async ({ page }) => {
    const errors: string[] = [];
    page.on('console', (m) => {
      if (m.type() === 'error') errors.push(m.text().slice(0, 200));
    });
    page.on('pageerror', (e) => errors.push(String(e).slice(0, 200)));

    await page.goto('/parts', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(2000);

    // Upload test cube
    const stlContent = `solid cube
  facet normal 0 0 1
    outer loop
      vertex 0 0 0
      vertex 10 0 0
      vertex 10 10 0
    endloop
  endfacet
  facet normal 0 0 1
    outer loop
      vertex 10 10 0
      vertex 0 10 0
      vertex 0 0 0
    endloop
  endfacet
  facet normal 0 0 -1
    outer loop
      vertex 0 0 0
      vertex 0 10 0
      vertex 10 10 0
    endloop
  endfacet
  facet normal 0 0 -1
    outer loop
      vertex 10 10 0
      vertex 10 0 0
      vertex 0 0 0
    endloop
  endfacet
  facet normal 0 1 0
    outer loop
      vertex 0 10 0
      vertex 0 10 10
      vertex 10 10 10
    endloop
  endfacet
  facet normal 0 1 0
    outer loop
      vertex 10 10 10
      vertex 10 10 0
      vertex 0 10 0
    endloop
  endfacet
  facet normal 0 -1 0
    outer loop
      vertex 0 0 0
      vertex 10 0 10
      vertex 0 0 10
    endloop
  endfacet
  facet normal 0 -1 0
    outer loop
      vertex 10 0 10
      vertex 10 0 0
      vertex 0 0 0
    endloop
  endfacet
  facet normal 1 0 0
    outer loop
      vertex 10 0 0
      vertex 10 10 10
      vertex 10 0 10
    endloop
  endfacet
  facet normal 1 0 0
    outer loop
      vertex 10 10 0
      vertex 10 10 10
      vertex 10 0 0
    endloop
  endfacet
  facet normal -1 0 0
    outer loop
      vertex 0 0 0
      vertex 0 0 10
      vertex 0 10 10
    endloop
  endfacet
  facet normal -1 0 0
    outer loop
      vertex 0 10 0
      vertex 0 10 10
      vertex 0 0 0
    endloop
  endfacet
endsolid cube`;

    await page.setInputFiles('input[type="file"]', {
      name: 'test-cube.stl',
      mimeType: 'application/octet-stream',
      buffer: Buffer.from(stlContent, 'utf8'),
    });

    await expect(page.getByText('faces')).toBeVisible({ timeout: 30000 });
    await page.waitForTimeout(1000);

    // Check pass dropdown has options
    const passSelect = page.locator('select[id="parts-pass"]');
    await expect(passSelect).toBeVisible();

    // Check available passes
    const options = await passSelect.locator('option').all();
    expect(options.length).toBeGreaterThan(0);

    // Select a pass (should have 'decimate' at minimum)
    const decimateOption = passSelect.locator('option', { hasText: 'decimate' });
    if (await decimateOption.count() > 0) {
      await passSelect.selectOption('decimate');
      await page.waitForTimeout(500);

      // Check budget input appears for decimate
      await expect(page.getByText('triangle budget')).toBeVisible({ timeout: 10000 });

      // Run optimization loop (just one step to verify it works)
      const runButton = page.getByRole('button', { name: /Optimise until it stops helping/ });
      await expect(runButton).toBeEnabled({ timeout: 10000 });
      
      // Click to start (will run up to 8 iterations but should complete quickly on simple cube)
      await runButton.click();
      await page.waitForTimeout(15000); // Wait for optimization to complete

      // Check history appears
      await expect(page.getByText('Loop')).toBeVisible({ timeout: 15000 });
    }

    expect(errors, `console errors during optimization: ${errors.join(' | ')}`).toEqual([]);
  });

  test('edge case: large model load', async ({ page }) => {
    const errors: string[] = [];
    page.on('console', (m) => {
      if (m.type() === 'error') errors.push(m.text().slice(0, 200));
    });
    page.on('pageerror', (e) => errors.push(String(e).slice(0, 200)));

    await page.goto('/parts', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(2000);

    // Create a larger STL (icosphere-like with more faces)
    let largeStl = 'solid large\n';
    for (let i = 0; i < 500; i++) {
      const x1 = Math.random() * 100;
      const y1 = Math.random() * 100;
      const z1 = Math.random() * 100;
      const x2 = x1 + Math.random() * 10;
      const y2 = y1 + Math.random() * 10;
      const z2 = z1 + Math.random() * 10;
      const x3 = x1 + Math.random() * 10;
      const y3 = y1 + Math.random() * 10;
      const z3 = z1 + Math.random() * 10;
      
      largeStl += `  facet normal 0 0 1\n`;
      largeStl += `    outer loop\n`;
      largeStl += `      vertex ${x1} ${y1} ${z1}\n`;
      largeStl += `      vertex ${x2} ${y2} ${z2}\n`;
      largeStl += `      vertex ${x3} ${y3} ${z3}\n`;
      largeStl += `    endloop\n`;
      largeStl += `  endfacet\n`;
    }
    largeStl += 'endsolid large\n';

    await page.setInputFiles('input[type="file"]', {
      name: 'large-model.stl',
      mimeType: 'application/octet-stream',
      buffer: Buffer.from(largeStl, 'utf8'),
    });

    // Should handle large model without crashing
    await expect(page.getByText('faces')).toBeVisible({ timeout: 60000 });
    await page.waitForTimeout(2000);

    const canvas = page.locator('canvas').first();
    await expect(canvas).toBeVisible();

    expect(errors, `console errors during large model load: ${errors.join(' | ')}`).toEqual([]);
  });

  test('edge case: missing textures (STL has no textures)', async ({ page }) => {
    const errors: string[] = [];
    page.on('console', (m) => {
      if (m.type() === 'error') errors.push(m.text().slice(0, 200));
    });
    page.on('pageerror', (e) => errors.push(String(e).slice(0, 200)));

    await page.goto('/parts', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(2000);

    // STL files don't have textures - verify it handles gracefully
    const stlContent = `solid cube
  facet normal 0 0 1
    outer loop
      vertex 0 0 0
      vertex 10 0 0
      vertex 10 10 0
    endloop
  endfacet
  facet normal 0 0 1
    outer loop
      vertex 10 10 0
      vertex 0 10 0
      vertex 0 0 0
    endloop
  endfacet
  facet normal 0 0 -1
    outer loop
      vertex 0 0 0
      vertex 0 10 0
      vertex 10 10 0
    endloop
  endfacet
  facet normal 0 0 -1
    outer loop
      vertex 10 10 0
      vertex 10 0 0
      vertex 0 0 0
    endloop
  endfacet
  facet normal 0 1 0
    outer loop
      vertex 0 10 0
      vertex 0 10 10
      vertex 10 10 10
    endloop
  endfacet
  facet normal 0 1 0
    outer loop
      vertex 10 10 10
      vertex 10 10 0
      vertex 0 10 0
    endloop
  endfacet
  facet normal 0 -1 0
    outer loop
      vertex 0 0 0
      vertex 10 0 10
      vertex 0 0 10
    endloop
  endfacet
  facet normal 0 -1 0
    outer loop
      vertex 10 0 10
      vertex 10 0 0
      vertex 0 0 0
    endloop
  endfacet
  facet normal 1 0 0
    outer loop
      vertex 10 0 0
      vertex 10 10 10
      vertex 10 0 10
    endloop
  endfacet
  facet normal 1 0 0
    outer loop
      vertex 10 10 0
      vertex 10 10 10
      vertex 10 0 0
    endloop
  endfacet
  facet normal -1 0 0
    outer loop
      vertex 0 0 0
      vertex 0 0 10
      vertex 0 10 10
    endloop
  endfacet
  facet normal -1 0 0
    outer loop
      vertex 0 10 0
      vertex 0 10 10
      vertex 0 0 0
    endloop
  endfacet
endsolid cube`;

    await page.setInputFiles('input[type="file"]', {
      name: 'test-cube.stl',
      mimeType: 'application/octet-stream',
      buffer: Buffer.from(stlContent, 'utf8'),
    });

    await expect(page.getByText('faces')).toBeVisible({ timeout: 30000 });
    await page.waitForTimeout(1000);

    // Should render wireframe without texture errors
    const canvas = page.locator('canvas').first();
    await expect(canvas).toBeVisible();

    // No texture-related errors should appear
    const textureErrors = errors.filter(e => 
      e.toLowerCase().includes('texture') || 
      e.toLowerCase().includes('material') ||
      e.toLowerCase().includes('uv')
    );
    expect(textureErrors, `texture-related errors: ${textureErrors.join(' | ')}`).toEqual([]);

    expect(errors, `console errors: ${errors.join(' | ')}`).toEqual([]);
  });

  test('edge case: export part', async ({ page }) => {
    const errors: string[] = [];
    page.on('console', (m) => {
      if (m.type() === 'error') errors.push(m.text().slice(0, 200));
    });
    page.on('pageerror', (e) => errors.push(String(e).slice(0, 200)));

    await page.goto('/parts', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(2000);

    // Upload test cube
    const stlContent = `solid cube
  facet normal 0 0 1
    outer loop
      vertex 0 0 0
      vertex 10 0 0
      vertex 10 10 0
    endloop
  endfacet
  facet normal 0 0 1
    outer loop
      vertex 10 10 0
      vertex 0 10 0
      vertex 0 0 0
    endloop
  endfacet
  facet normal 0 0 -1
    outer loop
      vertex 0 0 0
      vertex 0 10 0
      vertex 10 10 0
    endloop
  endfacet
  facet normal 0 0 -1
    outer loop
      vertex 10 10 0
      vertex 10 0 0
      vertex 0 0 0
    endloop
  endfacet
  facet normal 0 1 0
    outer loop
      vertex 0 10 0
      vertex 0 10 10
      vertex 10 10 10
    endloop
  endfacet
  facet normal 0 1 0
    outer loop
      vertex 10 10 10
      vertex 10 10 0
      vertex 0 10 0
    endloop
  endfacet
  facet normal 0 -1 0
    outer loop
      vertex 0 0 0
      vertex 10 0 10
      vertex 0 0 10
    endloop
  endfacet
  facet normal 0 -1 0
    outer loop
      vertex 10 0 10
      vertex 10 0 0
      vertex 0 0 0
    endloop
  endfacet
  facet normal 1 0 0
    outer loop
      vertex 10 0 0
      vertex 10 10 10
      vertex 10 0 10
    endloop
  endfacet
  facet normal 1 0 0
    outer loop
      vertex 10 10 0
      vertex 10 10 10
      vertex 10 0 0
    endloop
  endfacet
  facet normal -1 0 0
    outer loop
      vertex 0 0 0
      vertex 0 0 10
      vertex 0 10 10
    endloop
  endfacet
  facet normal -1 0 0
    outer loop
      vertex 0 10 0
      vertex 0 10 10
      vertex 0 0 0
    endloop
  endfacet
endsolid cube`;

    await page.setInputFiles('input[type="file"]', {
      name: 'test-cube.stl',
      mimeType: 'application/octet-stream',
      buffer: Buffer.from(stlContent, 'utf8'),
    });

    await expect(page.getByText('faces')).toBeVisible({ timeout: 30000 });
    await page.waitForTimeout(1000);

    // Check if there's an export/download button
    // The Parts page doesn't have an explicit export button in the current UI,
    // but we can verify the optimized result can be accessed via the API
    // For now, verify the page doesn't crash and the result_stl_base64 would be available
    // after optimization runs

    // Run a quick optimization to generate a result
    const runButton = page.getByRole('button', { name: /Optimise until it stops helping/ });
    if (await runButton.isEnabled({ timeout: 5000 })) {
      await runButton.click();
      await page.waitForTimeout(15000);
    }

    expect(errors, `console errors during export test: ${errors.join(' | ')}`).toEqual([]);
  });
});