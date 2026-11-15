import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, appendFile, rm, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { createInterface } from 'node:readline';

const source = await readFile(new URL('./feed.js', import.meta.url), 'utf8');
const { pollManifest } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const common = { schema_version: 1, scenario_comparison_id: 'scenario', task: 'supplygym', simulator_version: 'v1', scorer_version: 'v1' };
const week = (strategy, n) => ({ ...common, run_id: strategy, strategy, event: 'week', week: n, end_day: n * 7, weekly_operating_surplus: n, cumulative_operating_surplus: n });
const final = strategy => ({ ...common, run_id: strategy, strategy, event: 'final', completed: true });
const lines = records => records.map(record => JSON.stringify(record) + '\n').join('');

function waitForFeed(url, predicate) {
  return new Promise((resolve, reject) => {
    let stop;
    const timeout = setTimeout(() => { stop(); reject(new Error('feed poll timed out')); }, 5000);
    stop = pollManifest(url, groups => {
      if (predicate(groups)) { clearTimeout(timeout); stop(); resolve(groups); }
    }, error => { clearTimeout(timeout); stop(); reject(error); }, 30);
  });
}

test('server and feed replay appended public events without exposing private files', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'supplygym-live-'));
  const live = join(dir, 'live', 'seed-42', 'events.jsonl');
  const baseline = join(dir, 'baselines', 'seed-42', 'ss', 'events.jsonl');
  let server;
  try {
    await mkdir(join(dir, 'live', 'seed-42'), { recursive: true });
    await mkdir(join(dir, 'baselines', 'seed-42', 'ss'), { recursive: true });
    await writeFile(join(dir, 'spectator-manifest.json'), JSON.stringify([{ seed: 42, scenario_comparison_id: 'scenario', events: { opencode: 'live/seed-42/events.jsonl', ss: 'baselines/seed-42/ss/events.jsonl' } }]));
    await writeFile(live, lines([week('opencode', 1)]));
    await writeFile(baseline, lines([week('ss', 1), week('ss', 2), final('ss')]));
    await writeFile(join(dir, 'private.json'), 'secret');
    await writeFile(join(dir, 'live', 'seed-42', 'private.json'), 'secret');
    server = spawn('python', [new URL('./serve.py', import.meta.url).pathname.replace(/^\/(\w:)/, '$1'), '--run-dir', dir, '--port', '0']);
    const reader = createInterface({ input: server.stdout });
    const [announcement] = await Promise.race([once(reader, 'line'), once(server, 'exit').then(() => { throw Error('server exited'); })]);
    const origin = announcement.match(/http:\/\/127\.0\.0\.1:\d+/)?.[0];
    assert.ok(origin, announcement);
    const url = `${origin}/run/spectator-manifest.json`;
    const getRun = (groups, strategy) => groups[0].runs.find(run => run.strategy === strategy);
    await new Promise((resolve, reject) => {
      let appended = false;
      let stop;
      const timeout = setTimeout(() => { stop(); reject(new Error('live poll timed out')); }, 5000);
      const fail = error => { clearTimeout(timeout); stop(); reject(error); };
      stop = pollManifest(url, groups => {
        try {
          const run = getRun(groups, 'opencode');
          if (!appended && run?.latestWeek === 1) {
            assert.equal(getRun(groups, 'ss').latestWeek, 2);
            assert.equal(run.final, null);
            appended = true;
            appendFile(live, lines([week('opencode', 2), final('opencode')])).catch(fail);
          } else if (run?.final) {
            assert.deepEqual(run.weeks.map(record => record.week), [1, 2]);
            clearTimeout(timeout); stop(); resolve();
          }
        } catch (error) { fail(error); }
      }, fail, 30);
    });
    const replay = await waitForFeed(url, groups => getRun(groups, 'opencode')?.final);
    assert.deepEqual(getRun(replay, 'opencode').weeks.map(record => record.week), [1, 2]);
    assert.equal(getRun(replay, 'opencode').final.completed, true);
    for (const path of ['/run/private.json', '/run/live/seed-42/private.json', '/run/live/seed-42/../private.json', '/run/%2e%2e/private.json', '/private.json']) {
      assert.equal((await fetch(origin + path)).status, 404, path);
    }
  } finally {
    if (server && server.exitCode === null) { server.kill(); await once(server, 'exit'); }
    await rm(dir, { recursive: true, force: true });
  }
});
