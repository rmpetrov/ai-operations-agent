import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import App from '../src/App';
import type { Run } from '../src/contracts';
import { loadCatalog, loadRecording } from '../src/loader';
import { catalog, recording } from './recordings';

vi.mock('../src/loader');
beforeEach(() => {
  vi.mocked(loadCatalog).mockResolvedValue(catalog);
  vi.mocked(loadRecording).mockImplementation(async (_, workflow) => recording(workflow));
});
afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});
async function start() {
  render(<App />);
  await screen.findByRole('heading', { name: catalog.title });
  return screen.getByRole('button', { name: 'Open recorded run' });
}

it('keeps operational facts empty until the user opens a recording', async () => {
  await start();
  expect(screen.getByText('No recording open')).toBeTruthy();
  expect(screen.getByText('Scripted offline runtime')).toBeTruthy();
  expect(screen.queryByRole('table')).toBeNull();
  expect(loadRecording).not.toHaveBeenCalled();
});

it('renders scoped totals, order, source associations and the parent of each action', async () => {
  const real = recording('triage_with_recommendations');
  fireEvent.click(await start());
  const rows = within(await screen.findByRole('table', { name: 'Ranked queues' })).getAllByRole('row');
  expect(rows.slice(1).map((row) => within(row).getByRole('rowheader').textContent)).toEqual([
    'Synthetic all',
    'Synthetic calm',
  ]);
  for (const [label, expected] of Object.entries({
    'Active tickets': 3,
    'SLA breached': 1,
    'SLA at risk': 1,
    Unassigned: 2,
    'Online agents': 1,
  })) {
    expect(screen.getAllByText(label, { selector: 'dt' })[0].nextElementSibling?.textContent).toBe(
      String(expected),
    );
  }
  const detail = real.response.sources.find((s) => s.tool_name === 'get_queue_recommendations')!;
  const refs = screen.getAllByRole('button', { name: 'View evidence' });
  expect(refs).toHaveLength(4);
  for (const [index, button] of refs.entries()) {
    fireEvent.click(button);
    expect(JSON.parse(screen.getByLabelText('Resolved evidence value').textContent!)).toEqual(
      detail.snapshot.recommendations[index],
    );
    expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'Evidence explorer' }));
    expect(screen.getByText(detail.source_id)).toBeTruthy();
  }
  fireEvent.click(screen.getByRole('button', { name: 'View parent source' }));
  const parent = JSON.parse(screen.getByLabelText('Resolved evidence value').textContent!);
  expect(parent.assessment.queue_id).toBe(detail.snapshot.assessment.queue_id);
  fireEvent.click(screen.getByRole('button', { name: 'Synthetic calm' }));
  expect(screen.getByLabelText('Resolved evidence value').textContent).toContain('Synthetic calm');
  expect(
    within(screen.getByRole('region', { name: 'Priority queue' })).getByText('Synthetic all'),
  ).toBeTruthy();
  expect(screen.getByRole('status').textContent).toMatch(/answered/);
});

it('clears success and expanded detail before loading and after an asset failure', async () => {
  const open = await start();
  fireEvent.click(open);
  fireEvent.click((await screen.findAllByRole('button', { name: 'View evidence' }))[0]);
  fireEvent.click(screen.getByText('Inspect full source JSON'));
  let fail!: (error: Error) => void;
  vi.mocked(loadRecording).mockImplementationOnce(
    () =>
      new Promise((_, reject) => {
        fail = reject;
      }),
  );
  fireEvent.click(open);
  expect(screen.getByRole('status').textContent).toMatch(/Loading/);
  expect(screen.queryByRole('table')).toBeNull();
  expect(screen.queryByLabelText('Full source JSON')).toBeNull();
  await act(async () => fail(new Error('corrupt recording')));
  expect(screen.getByRole('alert').textContent).toMatch(/Recording unavailable/);
  expect(screen.queryByRole('heading', { name: 'Recommendations' })).toBeNull();
});

it('discards an older completion after a workflow change even when the loader ignores cancellation', async () => {
  let finish!: (run: Run) => void;
  vi.mocked(loadRecording).mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  const open = await start();
  fireEvent.click(open);
  fireEvent.change(screen.getByRole('combobox', { name: 'Workflow' }), { target: { value: 'triage_only' } });
  fireEvent.click(open);
  await screen.findByText('Recommendations were not requested.');
  await act(async () => finish(recording('triage_with_recommendations')));
  expect(screen.getByText('Recommendations were not requested.')).toBeTruthy();
  expect(screen.queryByRole('button', { name: 'View evidence' })).toBeNull();
  expect(screen.getByRole('table')).toBeTruthy();
});
