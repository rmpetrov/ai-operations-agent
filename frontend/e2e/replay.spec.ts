import { expect, test } from '@playwright/test';
import { mkdir } from 'node:fs/promises';
import { catalog, recording } from '../tests/recordings';

test('inspects both recorded workflows using only static assets', async ({ page }, info) => {
  const requests: { path: string; method: string; origin: string }[] = [];
  page.on('request', (request) => {
    const url = new URL(request.url());
    requests.push({ path: url.pathname, method: request.method(), origin: url.origin });
  });
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto('/');
  const open = page.getByRole('button', { name: 'Open recorded run' });
  await expect(open).toBeVisible();
  await expect(page.getByRole('table')).toHaveCount(0);
  await page.keyboard.press('Tab');
  await expect(page.getByText('Skip to workspace')).toBeFocused();
  await page.keyboard.press('Enter');
  await page.keyboard.press('Tab');
  await expect(page.getByRole('combobox', { name: 'Workflow' })).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(open).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(page.getByText('2 available queues')).toBeVisible();
  const kpis = page.getByRole('region', { name: 'Queue overview' });
  for (const [label, value] of Object.entries({
    'Active tickets': '3',
    'SLA breached': '1',
    'SLA at risk': '1',
    Unassigned: '2',
    'Online agents': '1',
  }))
    await expect(kpis.locator('dt').filter({ hasText: label }).locator('..').locator('dd')).toHaveText(value);
  const rows = page.getByRole('table', { name: 'Ranked queues' }).getByRole('rowheader');
  await expect(rows).toHaveText(['Synthetic all', 'Synthetic calm']);
  await expect(page.getByRole('region', { name: 'Priority queue' })).toContainText('Synthetic all');
  const run = recording('triage_with_recommendations');
  const detail = run.response.sources.find((s) => s.tool_name === 'get_queue_recommendations')!;
  await expect(
    page.getByRole('region', { name: 'Recommendations' }).getByRole('heading', { level: 3 }),
  ).toHaveText(detail.snapshot.recommendations.map((rec) => rec.suggested_action));
  const evidence = page.getByRole('button', { name: 'View evidence' });
  for (const [index, rec] of detail.snapshot.recommendations.entries()) {
    await evidence.nth(index).click();
    await expect(page.getByRole('heading', { name: 'Evidence explorer' })).toBeFocused();
    expect(JSON.parse(await page.getByLabel('Resolved evidence value').innerText())).toEqual(rec);
  }
  await page.getByRole('button', { name: 'View parent source' }).click();
  await expect(page.getByLabel('Resolved evidence value')).toContainText('Synthetic all');
  await expect(page.getByText(detail.parent_triage_source_id, { exact: true })).toBeVisible();
  await page.getByText('Inspect recorded events').click();
  await expect(page.getByText('Private provenance validation')).toBeVisible();
  await page.getByText('Inspect recorded events').click();
  await page.getByLabel('Evidence reference').selectOption('0');
  await page.getByRole('heading', { name: catalog.title }).scrollIntoViewIfNeeded();
  await page.evaluate(() => window.scrollTo(0, 0));
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await mkdir('review', { recursive: true });
  await page.screenshot({ path: 'review/opspilot-' + info.project.name + '.png', fullPage: true });

  await page.getByRole('combobox', { name: 'Workflow' }).selectOption('triage_only');
  await expect(page.getByRole('table')).toHaveCount(0);
  await open.click();
  await expect(page.getByText('Recommendations were not requested.')).toBeVisible();
  await expect(evidence).toHaveCount(0);
  await expect(rows).toHaveText(['Synthetic all', 'Synthetic calm']);
  expect(errors).toEqual([]);
  const assets = catalog.workflows.map((entry) => '/recordings/' + entry.asset_path);
  expect(requests.filter((req) => req.path.startsWith('/recordings/')).map((req) => req.path)).toEqual([
    '/recordings/catalog.json',
    assets[1],
    assets[0],
  ]);
  expect(
    requests.every(
      (req) =>
        req.method === 'GET' &&
        req.origin === 'http://127.0.0.1:4173' &&
        (['/', '/recordings/catalog.json', ...assets].includes(req.path) ||
          /^\/assets\/[\w-]+\.(js|css)$/.test(req.path)),
    ),
  ).toBe(true);
});

test('clears a previous answer if the selected asset is corrupt or missing', async ({ page }) => {
  await page.goto('/');
  const open = page.getByRole('button', { name: 'Open recorded run' });
  await open.click();
  await expect(page.getByRole('table')).toBeVisible();
  const path = '/recordings/' + catalog.workflows[1].asset_path;
  for (const status of [200, 404]) {
    await page.route(path, (route) => route.fulfill({ status, contentType: 'application/json', body: '{}' }));
    await open.click();
    await expect(page.getByRole('alert')).toContainText('Recording unavailable');
    await expect(page.getByRole('table')).toHaveCount(0);
    await expect(page.getByRole('heading', { name: 'Evidence explorer' })).toHaveCount(0);
    await page.unroute(path);
  }
});

test('serves only curated distribution files from the loopback origin', async ({ request }) => {
  const reply = await request.get('/');
  expect(reply.status()).toBe(200);
  expect(reply.headers()['content-security-policy']).toContain("connect-src 'self'");
  for (const path of [
    '/.env',
    '/.git/HEAD',
    '/src/main.tsx',
    '/evaluation-results/report.json',
    '/recordings/triage_v1.json',
    '/api/v1/tickets',
    '/docs',
  ]) {
    expect((await request.get(path)).status()).toBe(404);
  }
  expect((await request.post('/recordings/catalog.json')).status()).toBe(405);
  expect((await request.get('/', { headers: { Host: 'other.invalid' } })).status()).toBe(403);
});
