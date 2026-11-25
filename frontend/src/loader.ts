import { validateCatalog, validateRun } from '../.generated/validators.cjs';
import { resolvePointer } from './evidence';
import type { Catalog, Run, Workflow } from './contracts';

const directory = '/recordings/';
const workflows: Workflow[] = ['triage_only', 'triage_with_recommendations'];

function invalid(): never {
  throw new Error('Recording could not be verified.');
}

async function readLocal(name: string, limit: number, signal?: AbortSignal) {
  const res = await fetch(directory + name, {
    signal,
    mode: 'same-origin',
    credentials: 'omit',
    redirect: 'error',
    cache: 'no-store',
  });
  if (!res.ok || !res.body || Number(res.headers.get('content-length')) > limit) invalid();
  const reader = res.body.getReader();
  const bytes = new Uint8Array(limit);
  let size = 0;
  try {
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      if (size + chunk.value.byteLength > limit) {
        await reader.cancel();
        invalid();
      }
      bytes.set(chunk.value, size);
      size += chunk.value.byteLength;
    }
  } finally {
    reader.releaseLock();
  }
  return bytes.slice(0, size);
}

function decode(bytes: Uint8Array): unknown {
  const result: unknown = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
  // Bound inspection of the arbitrary JSON value carried by an evidence item.
  const pending: [unknown, number][] = [[result, 0]];
  while (pending.length) {
    const [value, depth] = pending.pop()!;
    if (depth > 48) invalid();
    if (value && typeof value === 'object') {
      for (const child of Object.values(value)) pending.push([child, depth + 1]);
    }
  }
  return result;
}

function safeEntry(entry: Catalog['workflows'][number]) {
  return (
    workflows.includes(entry.workflow_id) &&
    /^[0-9a-f]{64}$/.test(entry.sha256) &&
    entry.asset_path === entry.workflow_id + '-' + entry.sha256 + '.json'
  );
}

export async function loadCatalog(signal?: AbortSignal): Promise<Catalog> {
  const data = decode(await readLocal('catalog.json', 16 * 1024, signal));
  if (
    !validateCatalog(data) ||
    !data.workflows.every(safeEntry) ||
    !workflows.every((name, index) => data.workflows[index].workflow_id === name)
  )
    invalid();
  return data;
}

function checkRelease(run: Run) {
  const { response, trace, dashboard } = run;
  if (trace.status !== response.status || trace.events.at(-1)?.stage !== 'final_outcome') invalid();
  if (response.status === 'error') {
    if (
      !response.error ||
      response.sources.length ||
      response.evidence.length ||
      dashboard !== null ||
      response.uncertainty.length ||
      response.explanation.length !== 1 ||
      response.explanation[0].kind !== 'inability' ||
      response.explanation[0].references.length
    )
      invalid();
    return;
  }
  const [parent, detail] = response.sources;
  if (
    response.status !== 'answered' ||
    response.error !== null ||
    parent?.tool_name !== 'get_queue_triage' ||
    parent.snapshot.summary.evaluated_at !== run.evaluated_at ||
    (dashboard === null) !== (parent.snapshot.queues.length === 0) ||
    response.sources.some((source) => source.run_id !== response.run_id)
  )
    invalid();
  const target = parent.snapshot.summary.detail_target_queue_id;
  const needsDetail = run.workflow_id === 'triage_with_recommendations' && target !== null;
  if (Boolean(detail) !== needsDetail) invalid();
  if (
    detail &&
    (detail.tool_name !== 'get_queue_recommendations' ||
      detail.parent_triage_source_id !== parent.source_id ||
      detail.source_id === parent.source_id ||
      detail.snapshot.assessment.queue_id !== target)
  )
    invalid();
  // Check that every displayed link can be followed within the released recording.
  // Private provenance and all operational decisions belong to Python.
  const expectedRefs =
    1 +
    parent.snapshot.queues.length +
    (detail?.tool_name === 'get_queue_recommendations' ? 1 + detail.snapshot.recommendations.length : 0);
  const seen = new Set<string>();
  for (const item of response.evidence) {
    const { kind, source_id, pointer } = item.reference;
    const source = response.sources.find((candidate) => candidate.source_id === source_id);
    if (!source || resolvePointer(source.snapshot, pointer) === undefined) invalid();
    if (source.tool_name === 'get_queue_triage') {
      if (kind === 'triage_summary') {
        if (pointer !== '/summary' || item.queue_id !== null) invalid();
      } else {
        const row = /^\/queues\/(0|[1-9]\d*)$/.exec(pointer);
        if (
          kind !== 'triage_queue' ||
          !row ||
          source.snapshot.queues[Number(row[1])]?.assessment.queue_id !== item.queue_id
        )
          invalid();
      }
    } else if (
      item.queue_id !== source.snapshot.assessment.queue_id ||
      !(
        (kind === 'assessment' && pointer === '/assessment') ||
        (kind === 'recommendation' && /^\/recommendations\/(0|[1-9]\d*)$/.test(pointer))
      )
    )
      invalid();
    seen.add(source_id + ':' + pointer);
  }
  if (seen.size !== expectedRefs || seen.size !== response.evidence.length) invalid();
}

export async function loadRecording(
  catalog: Catalog,
  workflow: Workflow,
  signal?: AbortSignal,
): Promise<Run> {
  const entry = catalog.workflows.find((item) => item.workflow_id === workflow);
  if (!entry || !safeEntry(entry)) invalid();
  const bytes = await readLocal(entry.asset_path, 256 * 1024, signal);
  const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', bytes));
  const hex = Array.from(digest, (value) => value.toString(16).padStart(2, '0')).join('');
  if (hex !== entry.sha256) invalid();
  const run = decode(bytes);
  if (
    !validateRun(run) ||
    run.workflow_id !== workflow ||
    run.scenario_id !== catalog.scenario_id ||
    run.fixture_version !== catalog.fixture_version ||
    run.dataset_sha256 !== catalog.dataset_sha256 ||
    run.source_commit !== catalog.source_commit ||
    run.working_tree_dirty !== catalog.working_tree_dirty ||
    run.evaluated_at !== catalog.evaluated_at
  )
    invalid();
  checkRelease(run);
  return run;
}
