// @vitest-environment node
import { createHash } from 'node:crypto';
import { afterEach, expect, it, vi } from 'vitest';
import { loadCatalog, loadRecording } from '../src/loader';
import { asset, catalog, recording } from './recordings';

const detail = 'triage_with_recommendations';
afterEach(() => vi.unstubAllGlobals());

it('loads only the manifest and its selected content-addressed recording', async () => {
  const fetchMock = vi
    .fn()
    .mockResolvedValueOnce(Response.json(catalog))
    .mockResolvedValueOnce(new Response(asset(detail).toString()));
  vi.stubGlobal('fetch', fetchMock);
  const manifest = await loadCatalog();
  expect(await loadRecording(manifest, detail)).toEqual(recording(detail));
  expect(fetchMock.mock.calls.map((call) => call[0])).toEqual([
    '/recordings/catalog.json',
    '/recordings/' + catalog.workflows[1].asset_path,
  ]);
  expect(fetchMock.mock.calls[1][1]).toMatchObject({
    mode: 'same-origin',
    redirect: 'error',
    credentials: 'omit',
  });
});

it.each(['../unlisted.json', 'https://example.invalid/demo.json', '/outside.json'])(
  'rejects unsafe catalog paths before requesting the artifact',
  async (badPath) => {
    const manifest = structuredClone(catalog);
    manifest.workflows[0].asset_path = badPath;
    const fetchMock = vi.fn().mockResolvedValue(Response.json(manifest));
    vi.stubGlobal('fetch', fetchMock);
    await expect(loadCatalog()).rejects.toThrow();
    await expect(loadRecording(manifest, 'triage_only')).rejects.toThrow();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  },
);

it.each([
  'truncated',
  'missing',
  'oversized',
  'invalid-json',
  'bad-shape',
  'identity',
  'bad-link',
  'bad-pointer',
  'missing-reference',
  'version',
])('rejects %s recordings atomically', async (problem) => {
  const manifest = structuredClone(catalog);
  const run = recording(detail);
  let body: string;
  if (problem === 'oversized') body = ' '.repeat(262145);
  else if (problem === 'invalid-json') body = '{';
  else {
    if (problem === 'bad-shape') Reflect.deleteProperty(run.response, 'evidence');
    if (problem === 'identity') run.workflow_id = 'triage_only';
    if (problem === 'version') Object.assign(run, { schema_version: 'unknown' });
    if (problem === 'bad-pointer') run.response.evidence[0].reference.pointer = '/missing';
    if (problem === 'missing-reference') run.response.evidence.pop();
    if (problem === 'bad-link') Object.assign(run.response.sources[1], { parent_triage_source_id: 'other' });
    body = JSON.stringify(run);
  }
  // Intentionally valid digest for shape/identity cases so these exercise post-hash checks.
  const sha = createHash('sha256').update(body).digest('hex');
  const entry = manifest.workflows[1];
  entry.sha256 = sha;
  entry.asset_path = entry.workflow_id + '-' + sha + '.json';
  const reply = new Response(problem === 'truncated' ? body.slice(1) : body, {
    status: problem === 'missing' ? 404 : 200,
  });
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(reply));
  await expect(loadRecording(manifest, detail)).rejects.toThrow();
});

it('rejects duplicate workflow identities and oversized catalogs', async () => {
  const invalid = { ...catalog, workflows: [catalog.workflows[0], catalog.workflows[0]] };
  vi.stubGlobal(
    'fetch',
    vi
      .fn()
      .mockResolvedValueOnce(Response.json(invalid))
      .mockResolvedValueOnce(new Response(' '.repeat(16385))),
  );
  await expect(loadCatalog()).rejects.toThrow();
  await expect(loadCatalog()).rejects.toThrow();
});
