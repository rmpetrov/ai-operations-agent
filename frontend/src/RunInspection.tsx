import { resolvePointer } from './evidence';
import type { RefObject } from 'react';
import type { Run } from './contracts';

export type EvidenceItem = Run['response']['evidence'][number];
type TraceEvent = Run['trace']['events'][number];

const stages: Record<TraceEvent['stage'], string> = {
  model_attempt: 'Scripted adapter attempt',
  tool_proposed: 'Tool proposed',
  tool_accepted: 'Tool request accepted',
  tool_rejected: 'Tool request rejected',
  triage_snapshot_dispatch: 'Snapshot dispatch attempted',
  triage_snapshot_completed: 'Snapshot returned',
  parent_capture: 'Triage evidence capture',
  snapshot_detail_dispatch: 'Snapshot-derived detail dispatch attempted',
  linked_capture: 'Linked detail capture',
  server_owned_composition: 'Server-owned composition',
  workflow_completeness: 'Workflow completeness check',
  public_validation: 'Public contract validation',
  private_validation: 'Private provenance validation',
  final_outcome: 'Final outcome',
};
const countLabels: Record<keyof Run['trace']['operation_counts'], string> = {
  adapter_attempts: 'Scripted adapter attempts',
  proposals: 'Tools proposed',
  accepted: 'Requests accepted',
  rejected: 'Requests rejected',
  snapshot_dispatch_attempts: 'Snapshot dispatch attempts',
  completed_snapshots: 'Completed snapshots',
  detail_dispatch_attempts: 'Detail dispatch attempts',
  logical_dispatch_attempts: 'Logical dispatch attempts',
};

function sourceLabel(tool: string) {
  return tool === 'get_queue_triage' ? 'Parent triage source' : 'Linked detail source';
}

export function EvidenceExplorer({
  run,
  selected,
  onSelect,
  heading,
}: {
  run: Run;
  selected: EvidenceItem | null;
  onSelect: (item: EvidenceItem) => void;
  heading: RefObject<HTMLHeadingElement | null>;
}) {
  const { response } = run;
  const item = selected ?? response.evidence[0];
  if (!item) return null;
  const source = response.sources.find((s) => s.source_id === item.reference.source_id);
  if (!source) return null;
  const parentId = source.tool_name === 'get_queue_recommendations' ? source.parent_triage_source_id : null;
  const parent = response.evidence.find(
    (e) =>
      e.reference.kind === 'triage_queue' &&
      e.reference.source_id === parentId &&
      e.queue_id === item.queue_id,
  );
  const value = resolvePointer(source.snapshot, item.reference.pointer);
  return (
    <section id="evidence" className="panel inspection" aria-labelledby="evidence-title">
      <div className="panel-head">
        <h2 id="evidence-title" ref={heading} tabIndex={-1}>
          Evidence explorer
        </h2>
        <span className="section-note">
          {response.sources.length} {response.sources.length === 1 ? 'source' : 'sources'} ·{' '}
          {response.evidence.length} references
        </span>
      </div>
      <div className="explorer-body">
        <div className="reference-select">
          <label htmlFor="evidence-reference">Evidence reference</label>
          <select
            id="evidence-reference"
            value={response.evidence.indexOf(item)}
            onChange={(e) => {
              const next = response.evidence[Number(e.target.value)];
              if (next) onSelect(next);
            }}
          >
            {response.sources.map((s) => (
              <optgroup key={s.source_id} label={sourceLabel(s.tool_name)}>
                {response.evidence.map(
                  (e, index) =>
                    e.reference.source_id === s.source_id && (
                      <option key={e.reference.pointer} value={index}>
                        {e.reference.pointer} ·{' '}
                        {e.queue_id === null ? 'Scope summary' : `Queue ${e.queue_id}`}
                      </option>
                    ),
                )}
              </optgroup>
            ))}
          </select>
          <dl className="evidence-meta">
            <div>
              <dt>Source</dt>
              <dd>{sourceLabel(source.tool_name)}</dd>
            </div>
            <div>
              <dt>Source ID</dt>
              <dd className="mono">{source.source_id}</dd>
            </div>
            <div>
              <dt>Association</dt>
              <dd>
                {item.queue_id === null
                  ? 'Scope-level summary'
                  : `Queue-level evidence · Queue ${item.queue_id}`}
              </dd>
            </div>
            {parentId && (
              <div>
                <dt>Parent source ID</dt>
                <dd className="mono">{parentId}</dd>
              </div>
            )}
          </dl>
          {parent && (
            <button className="text-button" onClick={() => onSelect(parent)}>
              View parent source
            </button>
          )}
        </div>
        <div className="code-inspector">
          <div className="code-heading">
            <span>Resolved JSON</span>
            <code>{item.reference.pointer}</code>
          </div>
          {value === undefined ? (
            <p role="alert">The evidence pointer could not be resolved.</p>
          ) : (
            <pre tabIndex={0} aria-label="Resolved evidence value">
              {JSON.stringify(value, null, 2)}
            </pre>
          )}
        </div>
      </div>
      <details className="source-disclosure" key={source.source_id}>
        <summary>Inspect full source JSON</summary>
        <pre tabIndex={0} aria-label="Full source JSON">
          {JSON.stringify(source, null, 2)}
        </pre>
      </details>
    </section>
  );
}

export function ExecutionTrace({ run, assetDigest }: { run: Run; assetDigest: string | undefined }) {
  const { trace, response } = run;
  return (
    <section id="trace" className="panel trace" aria-labelledby="trace-title">
      <div className="panel-head">
        <h2 id="trace-title">Execution trace</h2>
        <span className="section-note">{trace.events.length} recorded events</span>
      </div>
      <div className="trace-body">
        <dl className="trace-counts">
          {Object.entries(trace.operation_counts).map(([key, value]) => (
            <div key={key}>
              <dt>{countLabels[key as keyof typeof countLabels]}</dt>
              <dd>{value}</dd>
            </div>
          ))}
        </dl>
        <p className="trace-note">
          Counts come from recorded runtime events. Physical SQL, connection checkouts and provider latency
          were unmeasured.
        </p>
        <details>
          <summary>Inspect recorded events</summary>
          <ol className="events">
            {trace.events.map((event, i) => (
              <li key={i}>
                <span>{stages[event.stage]}</span>
                {event.tool && <code>{event.tool}</code>}
                {event.stage === 'final_outcome' && <strong>{trace.status}</strong>}
              </li>
            ))}
          </ol>
        </details>
        <details>
          <summary>Recording provenance</summary>
          <dl className="evidence-meta">
            <div>
              <dt>Run ID</dt>
              <dd className="mono">{response.run_id}</dd>
            </div>
            <div>
              <dt>Source revision</dt>
              <dd className="mono">{run.source_commit}</dd>
            </div>
            <div>
              <dt>Working tree at capture</dt>
              <dd>{run.working_tree_dirty ? 'Included uncommitted changes' : 'Clean'}</dd>
            </div>
            <div>
              <dt>Fixture version</dt>
              <dd className="mono">{run.fixture_version}</dd>
            </div>
            <div>
              <dt>Dataset SHA-256</dt>
              <dd className="mono">{run.dataset_sha256}</dd>
            </div>
            <div>
              <dt>Recording SHA-256</dt>
              <dd className="mono">{assetDigest}</dd>
            </div>
            <div>
              <dt>Measurement source</dt>
              <dd className="mono">{trace.measurement_source}</dd>
            </div>
          </dl>
        </details>
      </div>
    </section>
  );
}
