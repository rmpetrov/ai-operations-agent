import { useEffect, useRef, useState } from 'react';
import type { Catalog, Run, Workflow } from './contracts';
import { loadCatalog, loadRecording } from './loader';
import { EvidenceExplorer, ExecutionTrace, type EvidenceItem } from './RunInspection';
import './workspace.css';

const workflows: Record<Workflow, string> = {
  triage_only: 'Triage only',
  triage_with_recommendations: 'Triage with recommendations',
};

const metrics = [
  ['active_tickets', 'Active tickets'],
  ['sla_breached_tickets', 'SLA breached'],
  ['sla_at_risk_tickets', 'SLA at risk'],
  ['unassigned_tickets', 'Unassigned'],
  ['online_agents', 'Online agents'],
] as const;

const signalLabels: Record<string, string> = {
  SLA_BREACHED: 'SLA breached',
  SLA_AT_RISK: 'SLA at risk',
  UNASSIGNED_BACKLOG: 'Unassigned backlog',
  NO_AGENTS_ONLINE: 'No agents online',
};

type LoadState = { phase: 'idle' | 'loading' } | { phase: 'failed' } | { phase: 'loaded'; run: Run };

const dateFormat = new Intl.DateTimeFormat('en-GB', {
  day: '2-digit',
  month: 'short',
  year: 'numeric',
  timeZone: 'UTC',
});
const timeFormat = new Intl.DateTimeFormat('en-GB', {
  hour: '2-digit',
  minute: '2-digit',
  timeZone: 'UTC',
  hourCycle: 'h23',
});

function SnapshotTime({ time }: { time: string }) {
  const parsed = new Date(time);
  return (
    <time dateTime={time}>
      {dateFormat.format(parsed)} · {timeFormat.format(parsed)} UTC
    </time>
  );
}

function Navigation({ answered, recorded }: { answered: boolean; recorded: boolean }) {
  return (
    <aside className="rail">
      <a className="brand" href="#workspace" aria-label="OpsPilot workspace">
        <span className="brand-mark" aria-hidden="true">
          <i />
          <i />
          <i />
        </span>
        OpsPilot
      </a>
      <nav aria-label="Workspace navigation">
        <a href="#workspace">
          <span className="nav-glyph glyph-grid" aria-hidden="true" />
          Workspace
        </a>
        {answered && (
          <a href="#evidence">
            <span className="nav-glyph glyph-file" aria-hidden="true" />
            Evidence explorer
          </a>
        )}
        {recorded && (
          <a href="#trace">
            <span className="nav-glyph glyph-trace" aria-hidden="true" />
            Execution trace
          </a>
        )}
      </nav>
      <div className="mode">
        <span className="mode-mark" aria-hidden="true" />
        <div>
          <strong>Recorded replay</strong>
          <small>Scripted offline runtime</small>
        </div>
      </div>
    </aside>
  );
}

function OperationalView({ run, inspect }: { run: Run; inspect: (item: EvidenceItem) => void }) {
  const { response, dashboard } = run;
  const parent = response.sources.find((source) => source.tool_name === 'get_queue_triage');
  const detail = response.sources.find((source) => source.tool_name === 'get_queue_recommendations');
  if (!parent) return null;
  const { summary, queues } = parent.snapshot;
  const target = queues.find((row) => row.assessment.queue_id === summary.detail_target_queue_id);
  const recommendations = detail?.snapshot.recommendations;
  const recEvidence = response.evidence.filter(
    (item) =>
      item.reference.kind === 'recommendation' &&
      item.reference.source_id === detail?.source_id &&
      item.queue_id === summary.detail_target_queue_id,
  );
  return (
    <>
      <section className="overview" aria-labelledby="overview-title">
        <div className="section-line">
          <h2 id="overview-title">Queue overview</h2>
          <span className="section-note">
            {summary.available_queue_count} available queues
            {summary.unavailable_queue_count > 0 && ` · ${summary.unavailable_queue_count} unavailable`}
          </span>
        </div>
        {dashboard && (
          <dl className="kpis">
            {metrics.map(([key, label]) => (
              <div key={key}>
                <dt>{label}</dt>
                <dd>{dashboard[key]}</dd>
              </div>
            ))}
          </dl>
        )}
        {summary.outcome === 'no_available_queues' && <p>No authorized queues available to assess.</p>}
      </section>

      <div className="queue-layout">
        <section className="panel rank-panel" aria-labelledby="ranking-title">
          <div className="panel-head">
            <h2 id="ranking-title">Ranked queues</h2>
          </div>
          <div className="table-scroll">
            <table aria-labelledby="ranking-title">
              <thead>
                <tr>
                  <th scope="col">Rank</th>
                  <th scope="col">Queue</th>
                  <th scope="col">Severity</th>
                  <th scope="col">Active</th>
                  <th scope="col">Signals</th>
                </tr>
              </thead>
              <tbody>
                {queues.map((row) => {
                  const a = row.assessment;
                  const item = response.evidence.find(
                    (e) =>
                      e.reference.kind === 'triage_queue' &&
                      e.reference.source_id === parent.source_id &&
                      e.queue_id === a.queue_id,
                  );
                  return (
                    <tr key={a.queue_id}>
                      <td className="rank">
                        {row.position}
                        {row.operational_tie && <small> Tied</small>}
                      </td>
                      <th scope="row">
                        {item ? (
                          <button className="queue-link" onClick={() => inspect(item)}>
                            {a.queue_name}
                          </button>
                        ) : (
                          a.queue_name
                        )}
                        {row.data_warnings.map((w) => (
                          <small key={w} className="data-warning">
                            {w.replaceAll('_', ' ')}
                          </small>
                        ))}
                      </th>
                      <td>
                        <span className={`severity ${a.overall_severity}`}>{a.overall_severity}</span>
                      </td>
                      <td>{a.metrics.active_tickets}</td>
                      <td>{a.signals.length}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </section>
        <section className="priority" aria-labelledby="priority-title">
          <h2 id="priority-title">Priority queue</h2>
          {target ? (
            <>
              <p className="priority-name">{target.assessment.queue_name}</p>
              <p className="priority-sub">Server-selected for review</p>
              <div className="signal-tags">
                {target.assessment.signals.map((signal) => (
                  <span key={signal.code} title={signal.reason}>
                    {signalLabels[signal.code] ?? signal.code}
                  </span>
                ))}
              </div>
            </>
          ) : (
            <p>
              {summary.outcome === 'no_triggered_rules'
                ? 'No configured rules triggered.'
                : 'No eligible queue.'}
            </p>
          )}
        </section>
      </div>

      <section className="panel recommendations" aria-labelledby="recommendations-title">
        <div className="panel-head">
          <h2 id="recommendations-title">Recommendations</h2>
          <span className="review-flags">
            {response.advisory_only && 'Advisory only'}
            {response.requires_human_review && ' · Human review required'}
          </span>
        </div>
        {recommendations?.length ? (
          <ol className="actions">
            {recommendations.map((rec, index) => {
              // Each pointer is resolved relative to the released linked detail source.
              const item = recEvidence.find((e) => e.reference.pointer === `/recommendations/${index}`);
              return (
                <li key={rec.code}>
                  <span className={`severity ${rec.priority}`}>{rec.priority}</span>
                  <div className="action-copy">
                    <h3>{rec.suggested_action}</h3>
                    <p>{rec.explanation}</p>
                  </div>
                  {item && (
                    <button className="text-button" onClick={() => inspect(item)}>
                      View evidence
                    </button>
                  )}
                </li>
              );
            })}
          </ol>
        ) : (
          <p className="panel-note">
            {run.workflow_id === 'triage_only'
              ? 'Recommendations were not requested.'
              : 'No eligible queue for recommendations.'}
          </p>
        )}
      </section>
    </>
  );
}

export default function App() {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [catalogError, setCatalogError] = useState(false);
  const [catalogAttempt, setCatalogAttempt] = useState(0);
  const [workflow, setWorkflow] = useState<Workflow>('triage_with_recommendations');
  const [state, setState] = useState<LoadState>({ phase: 'idle' });
  const [selectedEvidence, setSelectedEvidence] = useState<EvidenceItem | null>(null);
  const evidenceHeading = useRef<HTMLHeadingElement>(null);
  const currentLoad = useRef<AbortController | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setCatalogError(false);
    loadCatalog(controller.signal)
      .then((value) => {
        if (!controller.signal.aborted) setCatalog(value);
      })
      .catch(() => {
        if (!controller.signal.aborted) setCatalogError(true);
      });
    return () => {
      controller.abort();
      currentLoad.current?.abort();
    };
  }, [catalogAttempt]);

  function changeWorkflow(value: Workflow) {
    currentLoad.current?.abort();
    setWorkflow(value);
    setState({ phase: 'idle' });
    setSelectedEvidence(null);
  }

  async function open() {
    if (!catalog) return;
    currentLoad.current?.abort();
    const controller = new AbortController();
    currentLoad.current = controller;
    setSelectedEvidence(null);
    setState({ phase: 'loading' });
    try {
      const run = await loadRecording(catalog, workflow, controller.signal);
      if (!controller.signal.aborted) setState({ phase: 'loaded', run });
    } catch {
      if (!controller.signal.aborted) setState({ phase: 'failed' });
    }
  }

  function inspect(item: EvidenceItem) {
    setSelectedEvidence(item);
    evidenceHeading.current?.focus();
  }

  const run = state.phase === 'loaded' ? state.run : null;
  const answered = run?.response.status === 'answered';
  const status =
    state.phase === 'loading'
      ? 'Loading recorded run…'
      : run
        ? `Recording loaded. Runtime outcome: ${run.response.status}.`
        : '';
  return (
    <div className="app-shell">
      <a href="#workspace" className="skip-link">
        Skip to workspace
      </a>
      <Navigation answered={answered} recorded={Boolean(run)} />
      <main id="workspace" tabIndex={-1}>
        <header className="workspace-head">
          <span className="section-kicker">Operations workspace</span>
          <h1>{catalog?.title ?? 'Scenario replay'}</h1>
          {catalog && <p>{catalog.description}</p>}
        </header>
        {!catalog ? (
          <section className="panel notice">
            {catalogError ? (
              <>
                <p role="alert">The replay catalog could not be loaded.</p>
                <button onClick={() => setCatalogAttempt((n) => n + 1)}>Reload catalog</button>
              </>
            ) : (
              <p role="status">Loading replay catalog…</p>
            )}
          </section>
        ) : (
          <>
            <section className="recording-controls" aria-label="Recording controls">
              <div className="workflow-control">
                <label htmlFor="workflow">Workflow</label>
                <select
                  id="workflow"
                  value={workflow}
                  onChange={(e) => changeWorkflow(e.target.value as Workflow)}
                >
                  {catalog.workflows.map((entry) => (
                    <option key={entry.workflow_id} value={entry.workflow_id}>
                      {workflows[entry.workflow_id]}
                    </option>
                  ))}
                </select>
              </div>
              <button
                type="button"
                className="open-button"
                disabled={state.phase === 'loading'}
                onClick={open}
              >
                <span className="open-glyph" aria-hidden="true" />
                Open recorded run
              </button>
              <dl className="snapshot">
                <div>
                  <dt>Snapshot time</dt>
                  <dd>
                    <SnapshotTime time={catalog.evaluated_at} />
                  </dd>
                </div>
                <div>
                  <dt>Authorized scope</dt>
                  <dd>{catalog.scope_label}</dd>
                </div>
              </dl>
            </section>
            <div className="sr-only" role="status" aria-live="polite">
              {status}
            </div>
            {state.phase === 'failed' && (
              <section className="panel notice" role="alert">
                <h2>Recording unavailable</h2>
                <p>This recording could not be loaded or verified. Open the recorded run to try again.</p>
              </section>
            )}
            {state.phase === 'loading' && (
              <section className="panel notice">
                <p>Loading the recorded snapshot…</p>
              </section>
            )}
            {state.phase === 'idle' && (
              <section className="panel notice">
                <h2>No recording open</h2>
                <p>
                  Open the selected recording to inspect its captured queue rankings, actions and evidence.
                </p>
              </section>
            )}
            {run && (
              <div key={run.response.run_id} className="run">
                {answered ? (
                  <>
                    <OperationalView run={run} inspect={inspect} />
                    <EvidenceExplorer
                      run={run}
                      selected={selectedEvidence}
                      onSelect={setSelectedEvidence}
                      heading={evidenceHeading}
                    />
                  </>
                ) : (
                  <section className="panel notice" role="alert">
                    <h2>{run.response.explanation[0]?.text ?? 'Triage could not be completed.'}</h2>
                    <p>
                      {run.response.error?.message} <code>{run.response.error?.code}</code>
                    </p>
                  </section>
                )}
                <ExecutionTrace
                  run={run}
                  assetDigest={
                    catalog.workflows.find((entry) => entry.workflow_id === run.workflow_id)?.sha256
                  }
                />
              </div>
            )}
          </>
        )}
      </main>
    </div>
  );
}
