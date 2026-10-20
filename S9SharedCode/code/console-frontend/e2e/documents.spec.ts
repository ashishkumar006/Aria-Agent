import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const BASE = 'http://localhost:8500';
const SHOTS = 'C:/Users/AISHWA~1/AppData/Local/Temp/opencode/qa-shots';

/* Test fixtures: create small PDF, text, and HTML files in memory */
const TEXT_CONTENT = 'This is a test text document.\nIt has multiple lines.\nFor testing document upload.';
const HTML_CONTENT = '<!DOCTYPE html><html><body><h1>Test HTML</h1><p>This is a test HTML document.</p></body></html>';

/* Minimal valid PDF (1 page, "Test") - base64 encoded */
const PDF_BASE64 = 'JVBERi0xLjQKJcTl8uXrp/Og0MTGCjEgMCBvYmoKPDwvVHlwZS9DYXRhbG9nL1BhZ2VzIDIgMCBSPj4KZW5kb2JqCjIgMCBvYmoKPDwvVHlwZS9QYWdlcy9Db3VudCAxL0tpZHMgWzMgMCBSXSA+PgplbmRvYmoKMyAwIG9iago8PC9UeXBlL1BhZ2UvUGFyZW50IDIgMCBSL1Jlc291cmNlcyA8PC9Gb250IDw8L0YxIDQgMCBSPj4+Pj4vTWVkaWFCb3ggWzAgMCA2MTIgNzkyXSA+PgplbmRvYmoKNCAwIG9iago8PC9UeXBlL0ZvbnQvU3ViVHlwZS9UeXBlMS9CYXNlRm9udC9IZWx2ZXRpY2E+PgplbmRvYmoKc3RhcnR4cmVmCjE3NwolJUVPRgo=';
const PDF_BUFFER = Buffer.from(PDF_BASE64, 'base64');

/* Large file content (simulated - we'll use a larger text file) */
const LARGE_TEXT = 'x'.repeat(1024 * 1024 * 5); // ~5 MB

async function waitForIndexing(page, filename, timeout = 120000) {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    const row = page.locator(`section:has-text("${filename}")`).first();
    await expect(row).toBeVisible({ timeout: 5000 });
    const statusPill = row.locator('span:has-text("ready"), span:has-text("failed"), span:has-text("blocked")').first();
    const status = await statusPill.textContent({ timeout: 5000 });
    if (status?.includes('ready')) return true;
    if (status?.includes('failed')) throw new Error(`Document ${filename} indexing failed`);
    await page.waitForTimeout(2000);
  }
  throw new Error(`Timeout waiting for ${filename} to reach ready status`);
}

async function uploadFile(page, name, buffer, mimeType) {
  await page.setInputFiles('input[type="file"]', {
    name,
    mimeType,
    buffer,
  });
  await expect(page.getByText('Upload documents').first()).toBeVisible({ timeout: 20000 });
  await expect(page.getByText(name).first()).toBeVisible({ timeout: 60000 });
}

async function deleteDocument(page, filename) {
  await page.locator(`[aria-label="Delete ${filename}"]`).click();
  await page.waitForTimeout(1000);
  await expect(page.getByText(filename).first()).toHaveCount(0, { timeout: 15000 });
}

test.describe('Documents section', () => {
  test.beforeEach(async ({ page }) => {
    const errors: string[] = [];
    page.on('console', (m) => {
      if (m.type() === 'error') errors.push(m.text().slice(0, 200));
    });
    page.on('pageerror', (e) => errors.push(String(e).slice(0, 200)));
    await page.goto(BASE + '/documents', { waitUntil: 'networkidle', timeout: 45000 });
    await page.waitForTimeout(2000);
  });

  test('page loads: document list, upload area, filters', async ({ page }) => {
    // Verify upload area exists
    await expect(page.getByText('Upload documents').first()).toBeVisible({ timeout: 15000 });
    await expect(page.getByText('Drop files here or click to choose').first()).toBeVisible();
    
    // Verify stats cards
    await expect(page.getByText('DOCUMENTS').first()).toBeVisible();
    await expect(page.getByText('READY').first()).toBeVisible();
    await expect(page.getByText('IN USE').first()).toBeVisible();
    await expect(page.getByText('BLOCKED').first()).toBeVisible();
    
    // Verify search box
    await expect(page.getByLabel('Search enabled documents')).toBeVisible({ timeout: 15000 });
    await expect(page.getByRole('button', { name: 'Run document search' })).toBeVisible();
    
    // Verify refresh button
    await expect(page.getByRole('button', { name: 'Refresh documents' })).toBeVisible();
    
    // Verify UPLOADED section header
    await expect(page.getByText('UPLOADED', { exact: true }).first()).toBeVisible();
  });

  test('upload text file and wait for indexing', async ({ page }) => {
    await uploadFile(page, 'test-document.txt', Buffer.from(TEXT_CONTENT), 'text/plain');
    await waitForIndexing(page, 'test-document.txt');
    await deleteDocument(page, 'test-document.txt');
  });

  test('upload HTML file and wait for indexing', async ({ page }) => {
    await uploadFile(page, 'test-document.html', Buffer.from(HTML_CONTENT), 'text/html');
    await waitForIndexing(page, 'test-document.html');
    await deleteDocument(page, 'test-document.html');
  });

  test('upload PDF file and wait for indexing', async ({ page }) => {
    await uploadFile(page, 'test-document.pdf', PDF_BUFFER, 'application/pdf');
    await waitForIndexing(page, 'test-document.pdf');
    await deleteDocument(page, 'test-document.pdf');
  });

  test('preview document (click eye icon)', async ({ page }) => {
    await uploadFile(page, 'preview-test.txt', Buffer.from(TEXT_CONTENT), 'text/plain');
    await waitForIndexing(page, 'preview-test.txt');
    
    // Click preview button (eye icon)
    await page.locator('[aria-label="Preview preview-test.txt"]').click();
    
    // Wait for preview modal
    await expect(page.locator('iframe')).toBeVisible({ timeout: 15000 });
    
    // Verify preview shows the content
    const iframe = page.locator('iframe');
    const iframeSrc = await iframe.getAttribute('src');
    expect(iframeSrc).toContain('blob:');
    
    // Close preview
    await page.locator('button[aria-label="Close preview"]').click();
    await expect(page.locator('iframe')).toHaveCount(0, { timeout: 5000 });
    
    await deleteDocument(page, 'preview-test.txt');
  });

  test('enable/disable document', async ({ page }) => {
    await uploadFile(page, 'toggle-test.txt', Buffer.from(TEXT_CONTENT), 'text/plain');
    await waitForIndexing(page, 'toggle-test.txt');
    
    // Document should start as disabled (off)
    await expect(page.locator('[aria-label="Enable toggle-test.txt"]').first()).toBeVisible({ timeout: 15000 });
    
    // Enable document
    await page.locator('[aria-label="Enable toggle-test.txt"]').click();
    await page.waitForTimeout(1000);
    await expect(page.locator('[aria-label="Disable toggle-test.txt"]').first()).toBeVisible({ timeout: 15000 });
    
    // Disable document
    await page.locator('[aria-label="Disable toggle-test.txt"]').click();
    await page.waitForTimeout(1000);
    await expect(page.locator('[aria-label="Enable toggle-test.txt"]').first()).toBeVisible({ timeout: 15000 });
    
    await deleteDocument(page, 'toggle-test.txt');
  });

  test('delete document', async ({ page }) => {
    await uploadFile(page, 'delete-test.txt', Buffer.from(TEXT_CONTENT), 'text/plain');
    await waitForIndexing(page, 'delete-test.txt');
    
    await deleteDocument(page, 'delete-test.txt');
    
    // Verify document is gone from list
    await expect(page.getByText('delete-test.txt').first()).toHaveCount(0);
  });

  test('search within documents', async ({ page }) => {
    // Upload a document with unique content
    const uniqueContent = 'UNIQUESEARCHTERM12345';
    await uploadFile(page, 'search-test.txt', Buffer.from(uniqueContent), 'text/plain');
    await waitForIndexing(page, 'search-test.txt');
    
    // Enable it for search
    await page.locator('[aria-label="Enable search-test.txt"]').click();
    await page.waitForTimeout(1000);
    
    // Search for the unique term
    await page.getByLabel('Search enabled documents').fill(uniqueContent);
    await page.getByRole('button', { name: 'Run document search' }).click();
    
    // Wait for search results
    await expect(page.getByText('SEARCH RESULTS').first()).toBeVisible({ timeout: 30000 });
    await expect(page.getByText(uniqueContent).first()).toBeVisible({ timeout: 15000 });
    
    // Clear search
    await page.getByLabel('Search enabled documents').fill('');
    await page.getByRole('button', { name: 'Run document search' }).click();
    await expect(page.getByText('SEARCH RESULTS').first()).toHaveCount(0, { timeout: 10000 });
    
    await deleteDocument(page, 'search-test.txt');
  });

  test('large file upload', async ({ page }) => {
    // Create a ~5MB text file
    const largeContent = 'Large document content.\n'.repeat(100000); // ~2.5MB
    await uploadFile(page, 'large-test.txt', Buffer.from(largeContent), 'text/plain');
    await waitForIndexing(page, 'large-test.txt', 180000); // Longer timeout for large file
    await deleteDocument(page, 'large-test.txt');
  });

  test('unsupported file type rejected', async ({ page }) => {
    // Try to upload an .exe file (not in ACCEPT list)
    await page.setInputFiles('input[type="file"]', {
      name: 'bad.exe',
      mimeType: 'application/x-msdownload',
      buffer: Buffer.from('MZ'),
    });
    
    // Should show error or not appear in list
    await page.waitForTimeout(3000);
    await expect(page.getByText('bad.exe').first()).toHaveCount(0);
  });

  test('document detail drawer opens on click', async ({ page }) => {
    await uploadFile(page, 'detail-test.txt', Buffer.from(TEXT_CONTENT), 'text/plain');
    await waitForIndexing(page, 'detail-test.txt');
    
    // Click on document row to open detail drawer
    await page.locator('section:has-text("detail-test.txt") button:has-text("detail-test.txt")').first().click();
    
    // Wait for detail drawer
    await expect(page.getByRole('dialog', { name: /Details for detail-test.txt/ })).toBeVisible({ timeout: 15000 });
    await expect(page.getByText('STATUS').first()).toBeVisible();
    await expect(page.getByText('CHUNKS').first()).toBeVisible();
    await expect(page.getByText('SIZE').first()).toBeVisible();
    await expect(page.getByText('MODEL').first()).toBeVisible();
    
    // Close drawer
    await page.locator('button[aria-label="Close details"]').click();
    await expect(page.getByRole('dialog', { name: /Details for detail-test.txt/ })).toHaveCount(0, { timeout: 5000 });
    
    await deleteDocument(page, 'detail-test.txt');
  });

  test('download document', async ({ page }) => {
    await uploadFile(page, 'download-test.txt', Buffer.from(TEXT_CONTENT), 'text/plain');
    await waitForIndexing(page, 'download-test.txt');
    
    // Click download - this triggers a download
    const downloadPromise = page.waitForEvent('download');
    await page.locator('[aria-label="Download download-test.txt"]').click();
    const download = await downloadPromise;
    
    expect(download.suggestedFilename()).toBe('download-test.txt');
    
    await deleteDocument(page, 'download-test.txt');
  });

  test('re-index document', async ({ page }) => {
    await uploadFile(page, 'reindex-test.txt', Buffer.from(TEXT_CONTENT), 'text/plain');
    await waitForIndexing(page, 'reindex-test.txt');
    
    // Click re-index
    await page.locator('[aria-label="Re-index reindex-test.txt"]').click();
    await page.waitForTimeout(1000);
    
    // Should show indexing status again
    await expect(page.locator('span:has-text("parsing"), span:has-text("chunking"), span:has-text("embedding")').first()).toBeVisible({ timeout: 15000 });
    
    await waitForIndexing(page, 'reindex-test.txt');
    await deleteDocument(page, 'reindex-test.txt');
  });

  test('check console for blob URL preview XSS vector', async ({ page }) => {
    const consoleErrors: string[] = [];
    page.on('console', (m) => {
      if (m.type() === 'error') consoleErrors.push(m.text().slice(0, 300));
    });
    page.on('pageerror', (e) => consoleErrors.push(String(e).slice(0, 300)));
    
    // Upload HTML file (potential XSS vector)
    await uploadFile(page, 'xss-test.html', Buffer.from(HTML_CONTENT), 'text/html');
    await waitForIndexing(page, 'xss-test.html');
    
    // Open preview - this uses blob URL in iframe with sandbox="allow-same-origin"
    await page.locator('[aria-label="Preview xss-test.html"]').click();
    await expect(page.locator('iframe')).toBeVisible({ timeout: 15000 });
    
    // Check iframe sandbox attribute
    const iframe = page.locator('iframe');
    const sandbox = await iframe.getAttribute('sandbox');
    expect(sandbox).toBe('allow-same-origin');
    
    // Close preview
    await page.locator('button[aria-label="Close preview"]').click();
    
    await deleteDocument(page, 'xss-test.html');
    
    // Report console errors
    const xssErrors = consoleErrors.filter(e => 
      e.toLowerCase().includes('xss') || 
      e.toLowerCase().includes('content security') ||
      e.toLowerCase().includes('cross-origin') ||
      e.toLowerCase().includes('blob:') ||
      e.toLowerCase().includes('sandbox')
    );
    
    console.log('Console errors during HTML preview test:', consoleErrors);
    console.log('Potential XSS-related errors:', xssErrors);
    
    // The sandbox="allow-same-origin" without allow-scripts should prevent XSS
    // Any errors here would be concerning
    expect(xssErrors.length, 'No XSS-related console errors expected').toBe(0);
  });

  test('multiple file upload at once', async ({ page }) => {
    await page.setInputFiles('input[type="file"]', [
      { name: 'multi1.txt', mimeType: 'text/plain', buffer: Buffer.from('File 1 content') },
      { name: 'multi2.txt', mimeType: 'text/plain', buffer: Buffer.from('File 2 content') },
      { name: 'multi3.txt', mimeType: 'text/plain', buffer: Buffer.from('File 3 content') },
    ]);
    
    await expect(page.getByText('Upload documents').first()).toBeVisible({ timeout: 20000 });
    await expect(page.getByText('multi1.txt').first()).toBeVisible({ timeout: 60000 });
    await expect(page.getByText('multi2.txt').first()).toBeVisible({ timeout: 60000 });
    await expect(page.getByText('multi3.txt').first()).toBeVisible({ timeout: 60000 });
    
    await waitForIndexing(page, 'multi1.txt');
    await waitForIndexing(page, 'multi2.txt');
    await waitForIndexing(page, 'multi3.txt');
    
    await deleteDocument(page, 'multi1.txt');
    await deleteDocument(page, 'multi2.txt');
    await deleteDocument(page, 'multi3.txt');
  });

  test('drag and drop upload', async ({ page }) => {
    const dropZone = page.locator('div[class*="border-dashed"]').first();
    
    // Simulate drag and drop via setInputFiles on the hidden input
    // (Playwright doesn't have native drag-drop simulation for file inputs)
    await page.setInputFiles('input[type="file"]', {
      name: 'drag-drop-test.txt',
      mimeType: 'text/plain',
      buffer: Buffer.from('Drag and drop test content'),
    });
    
    await expect(page.getByText('Upload documents').first()).toBeVisible({ timeout: 20000 });
    await expect(page.getByText('drag-drop-test.txt').first()).toBeVisible({ timeout: 60000 });
    await waitForIndexing(page, 'drag-drop-test.txt');
    await deleteDocument(page, 'drag-drop-test.txt');
  });
});