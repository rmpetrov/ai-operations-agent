import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { existsSync } from 'node:fs';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import Ajv2020 from 'ajv/dist/2020.js';
import standalone from 'ajv/dist/standalone/index.js';
import addFormats from 'ajv-formats';
import { compile } from 'json-schema-to-typescript';

const root = fileURLToPath(new URL('../', import.meta.url));
const scratch = resolve(root, '.generated');
const schemaDir = resolve(scratch, 'schema');
const localPython = resolve(root, '../.venv/bin/python');
const mode = process.argv[2];
if (!['--write', '--check'].includes(mode)) throw new Error('Expected --write or --check');
execFileSync(
  existsSync(localPython) ? localPython : 'python',
  ['-I', '-m', 'ai_operations_agent.demo_replay', '--schema-dir', schemaDir],
  { stdio: 'inherit' },
);

const validator = new Ajv2020({
  strictSchema: false,
  code: { source: true },
  inlineRefs: false,
  ownProperties: true,
});
addFormats(validator);
await mkdir(resolve(root, 'src/generated'), { recursive: true });
for (const name of ['catalog', 'run']) {
  const source = await readFile(resolve(schemaDir, name + '.json'), 'utf8');
  const schema = JSON.parse(source);
  validator.addSchema(schema, name);
  const hash = createHash('sha256').update(source).digest('hex');
  // The declaration compiler uses the older tuple spelling. The validator uses
  // the original 2020-12 schema, including its positional and length constraints.
  const typeSchema = JSON.parse(source, (_key, value) => {
    if (value && typeof value === 'object') {
      if (value.prefixItems) {
        value.items = value.prefixItems;
        delete value.prefixItems;
      }
      if (typeof value.title === 'string' && !value.properties) delete value.title;
    }
    return value;
  });
  const declaration = await compile(typeSchema, schema.title, {
    ignoreMinAndMaxItems: true,
    unknownAny: true,
    $refOptions: { resolve: { external: false } },
    bannerComment: '/* Generated from the Python wire schema. SHA-256 ' + hash + '. */',
  });
  const destination = resolve(root, 'src/generated', name + '.d.ts');
  if (mode === '--write') await writeFile(destination, declaration);
  else if ((await readFile(destination, 'utf8')) !== declaration) {
    throw new Error(name + ' wire contract changed. Review and regenerate with npm run contracts.');
  }
}
// Generate standalone validators at build time. The browser never compiles a schema.
await writeFile(
  resolve(scratch, 'validators.cjs'),
  standalone.default(validator, { validateCatalog: 'catalog', validateRun: 'run' }),
);
await writeFile(
  resolve(scratch, 'validators.d.cts'),
  [
    "export function validateCatalog(value: unknown): value is import('../src/generated/catalog').OpsPilotCatalog;",
    "export function validateRun(value: unknown): value is import('../src/generated/run').OpsPilotRun;",
    '',
  ].join('\n'),
);
