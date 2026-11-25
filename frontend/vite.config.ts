import { createHash } from 'node:crypto';
import { lstat, readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';
import type { Catalog } from './src/contracts';

const inputs = new URL('./replay-input/', import.meta.url);
async function readInput(name: string, limit: number) {
  const path = fileURLToPath(new URL(name, inputs));
  const stat = await lstat(path);
  if (!stat.isFile() || stat.size > limit) throw new Error('Invalid recording input.');
  return readFile(path);
}

export default defineConfig({
  envDir: false,
  publicDir: false,
  plugins: [
    react(),
    {
      name: 'curated-recordings',
      apply: 'build',
      async generateBundle() {
        const { validateCatalog, validateRun }: typeof import('./.generated/validators.cjs') = createRequire(
          import.meta.url,
        )(fileURLToPath(new URL('./.generated/validators.cjs', import.meta.url)));
        const raw = await readInput('catalog.json', 16384);
        const input: unknown = JSON.parse(raw.toString());
        if (!validateCatalog(input)) throw new Error('Invalid replay catalog.');
        const catalog: Catalog = input;
        const workflows = new Set(catalog.workflows.map((entry) => entry.workflow_id));
        if (workflows.size !== 2) throw new Error('Incomplete replay catalog.');
        for (const entry of catalog.workflows) {
          if (entry.asset_path !== entry.workflow_id + '-' + entry.sha256 + '.json') {
            throw new Error('Invalid catalog asset name.');
          }
          const source = await readInput(entry.asset_path, 262144);
          const run: unknown = JSON.parse(source.toString());
          if (
            createHash('sha256').update(source).digest('hex') !== entry.sha256 ||
            !validateRun(run) ||
            run.workflow_id !== entry.workflow_id ||
            run.scenario_id !== catalog.scenario_id ||
            run.source_commit !== catalog.source_commit ||
            run.working_tree_dirty !== catalog.working_tree_dirty ||
            run.dataset_sha256 !== catalog.dataset_sha256 ||
            run.fixture_version !== catalog.fixture_version ||
            run.evaluated_at !== catalog.evaluated_at
          ) {
            throw new Error('Replay asset does not match the catalog.');
          }
          this.emitFile({ type: 'asset', fileName: 'recordings/' + entry.asset_path, source });
        }
        this.emitFile({ type: 'asset', fileName: 'recordings/catalog.json', source: raw });
      },
    },
  ],
  build: { sourcemap: false, commonjsOptions: { include: [/node_modules/, /\.generated/] } },
  test: { environment: 'jsdom', include: ['tests/**/*.test.{ts,tsx}'] },
});
