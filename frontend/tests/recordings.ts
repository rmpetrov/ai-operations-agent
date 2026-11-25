import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import type { Catalog, Run, Workflow } from '../src/contracts';

const path = resolve('replay-input');
export const catalog: Catalog = JSON.parse(readFileSync(resolve(path, 'catalog.json'), 'utf8'));
export function recording(workflow: Workflow): Run {
  const entry = catalog.workflows.find((item) => item.workflow_id === workflow)!;
  return JSON.parse(readFileSync(resolve(path, entry.asset_path), 'utf8'));
}
export function asset(workflow: Workflow) {
  return readFileSync(
    resolve(path, catalog.workflows.find((entry) => entry.workflow_id === workflow)!.asset_path),
  );
}
