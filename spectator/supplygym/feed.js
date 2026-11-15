// PR 37's producer appends JSONL records and flushes each complete line.
// Never consume a partial last line: it may still be in the process of writing.
export function parseFeed(text) {
  const records = [];
  const errors = [];
  const lines = String(text).split('\n');
  lines.pop();
  lines.forEach((line, index) => {
    if (!line.trim()) return;
    try {
      const record = JSON.parse(line);
      if (!record || !['week', 'final'].includes(record.event) ||
          record.schema_version !== 1 || !record.run_id || !record.strategy ||
          !record.scenario_comparison_id || !record.task || !record.simulator_version ||
          !record.scorer_version || (record.event === 'week' &&
          (!Number.isInteger(record.week) || record.week < 1 || record.week > 53 ||
           !Number.isInteger(record.end_day) || record.end_day < 1 || record.end_day > 365 ||
           !Number.isFinite(record.weekly_operating_surplus) ||
           !Number.isFinite(record.cumulative_operating_surplus))) ||
          (record.event === 'final' && typeof record.completed !== 'boolean') ||
          !finiteNumbers(record)) {
        throw new Error('Invalid spectator record');
      }
      records.push(record);
    } catch (error) {
      errors.push(`Line ${index + 1}: ${error.message}`);
    }
  });
  return { records, errors };
}

function finiteNumbers(value) {
  if (typeof value === 'number') return Number.isFinite(value);
  if (value && typeof value === 'object') return Object.values(value).every(finiteNumbers);
  return true;
}

function stable(value) {
  if (Array.isArray(value)) return `[${value.map(stable).join(',')}]`;
  if (value && typeof value === 'object') return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${stable(value[key])}`).join(',')}}`;
  return JSON.stringify(value);
}

function identity(record) {
  return stable([record.scenario_comparison_id, record.task, record.simulator_version, record.scorer_version]);
}

// Preserve distinct versions and run IDs even if their strategy names coincide.
export function groupRuns(records) {
  const groups = new Map();
  for (const record of records) {
    const id = identity(record);
    if (!groups.has(id)) groups.set(id, {
      id, label: record.scenario_comparison_id, scenario_comparison_id: record.scenario_comparison_id,
      task: record.task, simulator_version: record.simulator_version,
      scorer_version: record.scorer_version, primary: 'opencode', runs: []
    });
    const group = groups.get(id);
    let run = group.runs.find(item => item.run_id === record.run_id);
    if (!run) {
      run = { id: record.run_id, run_id: record.run_id, strategy: record.strategy, weeks: [], final: null };
      group.runs.push(run);
    }
    // A conflicting strategy under one run ID cannot be safely attributed.
    if (run.strategy !== record.strategy) continue;
    if (record.event === 'week') {
      const index = run.weeks.findIndex(item => item.week === record.week);
      if (index < 0) run.weeks.push(record); else run.weeks[index] = record;
    } else run.final = record;
  }
  for (const group of groups.values()) {
    const comparisonStrategies = ['ss', 'base_stock', 'fixed_lot', 'fill_adjusted_base_stock'];
    group.primary = group.runs.find(run => run.strategy === 'opencode')?.strategy
      ?? group.runs.find(run => !comparisonStrategies.includes(run.strategy))?.strategy
      ?? group.runs[0].strategy;
    for (const run of group.runs) {
      run.weeks.sort((a, b) => a.week - b.week);
      run.latestWeek = run.weeks.at(-1)?.week || 0;
    }
  }
  return [...groups.values()];
}

function manifestEntries(manifest) {
  if (!Array.isArray(manifest)) throw new Error('Invalid spectator manifest');
  return manifest.flatMap(entry => {
    if (!entry || typeof entry.scenario_comparison_id !== 'string' || !entry.events || typeof entry.events !== 'object') throw new Error('Invalid manifest scenario');
    return Object.entries(entry.events).map(([strategy, path]) => {
      if (typeof path !== 'string' || !path || path.startsWith('/') || path.includes('\\') || path.split('/').some(part => part === '..' || part === '.') || /[?#:]/.test(path)) throw new Error('Invalid manifest event path');
      return { entry, strategy, path };
    });
  });
}

function primary(entry) {
  const names = Object.keys(entry.events);
  return names.find(name => name === 'opencode') ||
    names.find(name => /(^|\/)live\//i.test(entry.events[name])) ||
    names.find(name => !/^(ss|base_stock|fixed_lot|fill_adjusted_base_stock|reference)$/i.test(name)) || names[0] || 'opencode';
}

function assemble(sources, manifest) {
  const records = [];
  for (const source of sources) {
    const parsed = source.parsed ?? parseFeed(source.text);
    if (parsed.errors.length) throw new Error(`${source.path}: ${parsed.errors.join('; ')}`);
    for (const record of parsed.records) {
      if (source.entry && (record.scenario_comparison_id !== source.entry.scenario_comparison_id || record.strategy !== source.strategy)) throw new Error(`${source.path}: record does not match manifest scenario or strategy`);
      records.push(record);
    }
  }
  const groups = groupRuns(records);
  if (!Array.isArray(manifest)) return groups;
  for (const entry of manifest) {
    if (!entry || !entry.scenario_comparison_id || !entry.events) continue;
    const matching = groups.filter(group => group.scenario_comparison_id === entry.scenario_comparison_id);
    // If version identities disagree, keep separate groups rather than pairing them.
    if (!matching.length) {
      const id = `pending:${entry.scenario_comparison_id}`;
      matching.push({ id, scenario_comparison_id: entry.scenario_comparison_id, runs: [] });
      groups.push(matching[0]);
    }
    for (const group of matching) {
      group.label = entry.seed == null ? entry.scenario_comparison_id : `Scenario ${entry.seed}`;
      group.primary = primary(entry);
      // Placeholder makes a not-yet-created live file visible without inventing data.
      if (!group.runs.some(run => run.strategy === group.primary)) {
        group.runs.push({ id: `pending:${group.primary}`, run_id: null, strategy: group.primary, weeks: [], final: null, latestWeek: 0 });
      }
      group.versionMismatch = matching.length > 1;
    }
  }
  return groups;
}

export async function loadFiles(files) {
  const entries = Array.from(files, file => ({ path: file.webkitRelativePath || file.name, file }));
  const manifestFile = entries.find(item => /(^|\/)spectator-manifest\.json$/i.test(item.path));
  if (!manifestFile) {
    // Folder picks may include evaluator artifacts. Without a manifest, inspect
    // only conventional public feed names; individually selected files are explicit.
    const selected = entries.filter(item => item.file.webkitRelativePath
      ? /(^|\/)events\.jsonl$/i.test(item.path) : /\.jsonl$/i.test(item.path));
    return assemble(await Promise.all(selected.map(async item => ({ ...item, text: await item.file.text() }))), null);
  }
  const manifest = JSON.parse(await manifestFile.file.text());
  const declared = manifestEntries(manifest);
  const base = manifestFile.path.slice(0, manifestFile.path.length - 'spectator-manifest.json'.length);
  const sources = await Promise.all(declared.map(async item => {
    const match = entries.find(file => file.path === base + item.path);
    if (!match) {
      if (item.strategy === primary(item.entry)) return null;
      throw new Error(`${item.path}: declared feed not selected`);
    }
    return { ...item, text: await match.file.text() };
  }));
  return assemble(sources.filter(Boolean), manifest);
}

// Poll the manifest and all listed files. Keep the last good contents if a live
// file is temporarily absent or a request fails; a later poll can recover.
export function pollManifest(url, onUpdate, onError = () => {}, interval = 2000) {
  let stopped = false;
  let timer;
  const cache = new Map();
  async function poll() {
    try {
      const response = await fetch(url, { cache: 'no-store' });
      if (!response.ok) throw new Error(`Manifest: HTTP ${response.status}`);
      const manifest = await response.json();
      const entries = manifestEntries(manifest);
      const warnings = [];
      await Promise.all(entries.map(async item => {
        const { path } = item;
        try {
          if (cache.get(path)?.sealed) return;
          const response = await fetch(new URL(path, new URL(url, globalThis.location?.href)).href, { cache: 'no-store' });
          if (!response.ok) throw new Error(`HTTP ${response.status}`);
          const text = await response.text();
          const parsed = parseFeed(text);
          if (parsed.errors.length) throw new Error(parsed.errors.join('; '));
          if (parsed.records.some(record => record.scenario_comparison_id !== item.entry.scenario_comparison_id || record.strategy !== item.strategy)) throw new Error('record does not match manifest scenario or strategy');
          cache.set(path, { parsed, sealed: parsed.records.some(record => record.event === 'final') });
        } catch (error) {
          if (!(item.strategy === primary(item.entry) && error.message === 'HTTP 404' && !cache.has(path))) warnings.push(`${path}: ${error.message}`);
        }
      }));
      if (!stopped) {
        const sources = entries.filter(item => cache.has(item.path)).map(item => ({ ...item, parsed: cache.get(item.path).parsed }));
        onUpdate(assemble(sources, manifest));
        if (warnings.length) onError(new Error(warnings.join('; ')));
      }
    } catch (error) { if (!stopped) onError(error); }
    if (!stopped) timer = setTimeout(poll, interval);
  }
  poll();
  return () => { stopped = true; clearTimeout(timer); };
}
