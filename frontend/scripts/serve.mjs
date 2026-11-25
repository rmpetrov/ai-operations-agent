// Load only the built entry point, its Vite bundles and the two manifest-listed recordings.
import { createServer } from 'node:http';
import { readFile, realpath } from 'node:fs/promises';
import { extname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const host = '127.0.0.1';
const port = 4173;
const root = await realpath(fileURLToPath(new URL('../dist/', import.meta.url)));
const types = {
  '.html': 'text/html',
  '.css': 'text/css',
  '.js': 'text/javascript',
  '.json': 'application/json',
};
const assets = new Map();
async function add(name) {
  const path = resolve(root, name);
  if ((await realpath(path)) !== path) throw new Error('Unexpected symlink in built distribution.');
  const bytes = await readFile(path);
  assets.set('/' + name, { bytes, type: types[extname(name)] });
  return bytes;
}

const html = (await add('index.html')).toString();
assets.set('/', assets.get('/index.html'));
for (const match of html.matchAll(/(?:href|src)="\/(assets\/[\w-]+\.(?:js|css))"/g)) await add(match[1]);
const catalog = JSON.parse(await add('recordings/catalog.json'));
for (const { asset_path: name, sha256, workflow_id } of catalog.workflows) {
  if (
    !['triage_only', 'triage_with_recommendations'].includes(workflow_id) ||
    !/^[0-9a-f]{64}$/.test(sha256) ||
    name !== workflow_id + '-' + sha256 + '.json'
  ) {
    throw new Error('Invalid static asset name.');
  }
  await add('recordings/' + name);
}
createServer((req, res) => {
  const asset = assets.get(req.url);
  const validHost = req.headers.host === host + ':' + port;
  if (!validHost || !['GET', 'HEAD'].includes(req.method) || !asset) {
    res.writeHead(!validHost ? 403 : !asset ? 404 : 405).end();
    return;
  }
  res
    .writeHead(200, {
      'Content-Type': asset.type + '; charset=utf-8',
      'Content-Length': asset.bytes.length,
      'Cache-Control': 'no-store',
      'X-Content-Type-Options': 'nosniff',
      'Content-Security-Policy':
        "default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
    })
    .end(req.method === 'HEAD' ? undefined : asset.bytes);
}).listen(port, host, () => console.log('OpsPilot static replay at http://' + host + ':' + port));
