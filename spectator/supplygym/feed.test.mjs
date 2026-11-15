import { readFile } from 'node:fs/promises';
import assert from 'node:assert/strict';
import { test } from 'node:test';

// The browser loads this as an ES module without requiring a package.json.
const source = await readFile(new URL('./feed.js', import.meta.url), 'utf8');
const { parseFeed, groupRuns, loadFiles, pollManifest } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const base = { schema_version: 1, run_id: 'r', strategy: 'scripted', scenario_comparison_id: 'scenario', task: 'supplygym', simulator_version: { hash: 'a' }, scorer_version: 'v' };
const week = { ...base, event: 'week', week: 1, end_day: 7, weekly_operating_surplus: -3, cumulative_operating_surplus: -3 };
const final = { ...base, event: 'final', completed: false };
const line = event => `${JSON.stringify(event)}\n`;
const manifest = [{ seed: 42, scenario_comparison_id: 'scenario', events: { scripted: 'live/events.jsonl', ss: 'baselines/ss/events.jsonl' } }];
const file = (path, content) => ({ name: path.split('/').at(-1), webkitRelativePath: path, text: async () => content });

test('complete records, deduplication, terminal, and version identity', () => {
  assert.equal(parseFeed(line(week) + '{').records.length, 1);
  assert.equal(parseFeed(line({ ...week, cumulative_operating_surplus: null })).errors.length, 1);
  assert.equal(parseFeed(line(week).replace('-3', '1e400')).errors.length, 1);
  const groups = groupRuns([week, week, final, { ...week, run_id: 'b', scorer_version: 'different' }]);
  assert.equal(groups.length, 2);
  assert.equal(groups[0].runs[0].weeks.length, 1);
  assert.equal(groups[0].runs[0].final.completed, false);
});

test('folder reads only declared public files and checks identity', async () => {
  const baseline = { ...week, run_id: 'b', strategy: 'ss' };
  const files = [file('out/spectator-manifest.json', JSON.stringify(manifest)), file('out/baselines/ss/events.jsonl', line(baseline)), { name: 'private.jsonl', webkitRelativePath: 'out/private.jsonl', text: () => { throw Error('private file read'); } }];
  const groups = await loadFiles(files);
  assert.equal(groups[0].primary, 'scripted');
  assert.equal(groups[0].runs.find(run => run.strategy === 'scripted').weeks.length, 0);
  await assert.rejects(loadFiles([...files, file('out/live/events.jsonl', line({ ...week, scenario_comparison_id: 'wrong' }))]), /does not match/);
  await assert.rejects(loadFiles([...files, file('out/live/events.jsonl', line({ ...week, strategy: 'wrong' }))]), /does not match/);
});

test('poll permits initial live 404, reports other failures, and caches final feeds', async () => {
  const originalFetch = globalThis.fetch;
  const calls = {};
  const errors = [];
  const baseline = { ...final, run_id: 'b', strategy: 'ss' };
  globalThis.fetch = async url => {
    const key = String(url);
    calls[key] = (calls[key] || 0) + 1;
    if (key.endsWith('manifest.json')) return { ok: true, json: async () => manifest };
    if (key.endsWith('baselines/ss/events.jsonl')) return { ok: true, text: async () => line(baseline) };
    if (calls[key] === 1) return { ok: false, status: 404 };
    return { ok: false, status: 500 };
  };
  try {
    await new Promise((resolve, reject) => {
      let updates = 0;
      const stop = pollManifest('https://example.test/manifest.json', () => {
        if (++updates === 2) { stop(); resolve(); }
      }, error => errors.push(error.message), 5);
      setTimeout(() => { stop(); reject(Error('poll timeout')); }, 1000).unref();
    });
    assert.equal(calls['https://example.test/baselines/ss/events.jsonl'], 1);
    assert.deepEqual(errors, ['live/events.jsonl: HTTP 500']);
  } finally { globalThis.fetch = originalFetch; }
});
