import React, { useEffect, useRef, useState } from 'react';
import { Play, Box } from 'lucide-react';
import { api } from '../api';
import type { HarborContainerInfo, HarborJobsState } from '../types';

interface Props {
  containers: HarborContainerInfo[];
  dockerAvailable: boolean;
  dockerError: string | null;
  selectedContainerId: string | null;
  onSelectContainer: (id: string) => void;
  onOpenRuns: (path: string) => Promise<void>;
}

export const HarborWatchPanel: React.FC<Props> = ({ containers, dockerAvailable, dockerError, selectedContainerId, onSelectContainer, onOpenRuns }) => {
  const [state, setState] = useState<HarborJobsState | null>(null);
  const [exportId, setExportId] = useState('');
  const [model, setModel] = useState('google/gemini-3.8-flash');
  const [concurrency, setConcurrency] = useState(2);
  const [busy, setBusy] = useState(false);
  const starting = useRef(false);
  const [error, setError] = useState<string | null>(null);
  const [showSetup, setShowSetup] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      try {
        const next = await api.getHarborJobs();
        if (!cancelled) {
          setState(next);
          setExportId(current => current || next.exports[0]?.id || '');
          if (next.allowed_models?.length) {
            setModel(current => next.allowed_models!.includes(current) ? current : next.allowed_models![0]);
          }
          setConcurrency(current => Math.min(current, next.max_concurrency ?? 4));
        }
      } catch (err) {
        if (!cancelled) setError(String(err));
      }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 2500);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, []);

  const activeJob = state?.jobs.find(job => job.status === 'running');
  const launch = async () => {
    if (starting.current || activeJob) return;
    starting.current = true;
    setBusy(true);
    setError(null);
    try {
      await api.launchHarbor({ export: exportId, model, concurrency });
      setShowSetup(false);
      setState(await api.getHarborJobs());
    } catch (err) {
      setError(String(err));
    } finally {
      starting.current = false;
      setBusy(false);
    }
  };

  return <>
    <div className="run-summary">
      <div><span className="eyebrow">Evaluation</span><strong>Harbor + OpenCode</strong><span>Docker · results saved to disk</span></div>
      {!!state?.jobs.length && <button className="btn" onClick={() => setShowSetup(v => !v)}>{showSetup ? 'Hide setup' : 'New evaluation'}</button>}
    </div>
    {error && <div className="run-error" role="alert">{error}</div>}
    {(!state?.jobs.length || showSetup) && <div className="card run-setup">
      <label className="field">Export
        <select className="select" value={exportId} onChange={e => setExportId(e.target.value)}>
          {!state?.exports.length && <option value="">No prepared exports</option>}
          {state?.exports.map(item => <option key={item.id} value={item.id}>{item.name} · {item.tasks} trials</option>)}
        </select>
      </label>
      <label className="field">Model
        {state?.allowed_models?.length ? <select className="select mono" value={model} onChange={e => setModel(e.target.value)}>
          {state.allowed_models.map(item => <option key={item} value={item}>{item}</option>)}
        </select> : <input className="input mono" value={model} onChange={e => setModel(e.target.value)} placeholder="google/gemini-3.8-flash" />}
      </label>
      <details className="advanced-settings"><summary>Run settings</summary>
        <label>Concurrent trials
          <input className="input" type="number" min={1} max={state?.max_concurrency ?? 4} value={concurrency} onChange={e => setConcurrency(Number(e.target.value))} />
        </label>
        <p>Uses OpenCode 1.19.5. API credentials come from the server environment.</p>
      </details>
      {!state?.exports.length && state && <p className="muted">Prepare an oncall Harbor export in <code>{state.exports_dir}</code> first.</p>}
      {state && !state.harbor_available && <p className="muted">Install Harbor on the server to launch evaluations.</p>}
      {!dockerAvailable && <p className="muted">The evaluation worker cannot reach Docker.{dockerError ? ` ${dockerError}` : ''}</p>}
      <button className="btn btn-primary" disabled={busy || !!activeJob || !state?.harbor_available || !dockerAvailable || !exportId || !model.trim() || !Number.isInteger(concurrency) || concurrency < 1 || concurrency > (state?.max_concurrency ?? 4)} onClick={() => void launch()}>
        <Play size={14} /> {busy ? 'Starting Harbor…' : activeJob ? 'Evaluation in progress' : 'Start evaluation'}
      </button>
    </div>}
    {state && <div className="storage-note">Saved runs <code>{state.jobs_dir}</code></div>}
    {!!containers.length && <section className="harbor-section"><h2>Live trials</h2>
      {containers.map(container => <button key={container.container_id} className={`trial-choice ${selectedContainerId === container.container_id ? 'selected' : ''}`} onClick={() => onSelectContainer(container.container_id)}>
        <Box size={14} /><span><strong>{container.compose_project}</strong><small>Seed {container.seed} · {container.steps} calls</small></span>
      </button>)}
    </section>}
    {!!state?.jobs.length && <section className="harbor-section"><h2>Recent jobs</h2>
      {state.jobs.slice(0, 3).map(job => <div className="job-row" key={job.id}>
        <div><strong>{job.model || job.agent}</strong><span>{job.status === 'unknown' ? 'Status unavailable after restart' : job.status} · {job.completed_trials}{job.total_trials != null ? ` / ${job.total_trials}` : ''} trials complete</span><small>{job.id}</small></div>
        {job.completed_trials > 0 && <button className="btn" onClick={() => void onOpenRuns(job.path).catch(err => setError(String(err)))}>View trials</button>}
        {job.status === 'failed' && <small className="muted">See job logs in <code>{job.path}</code></small>}
      </div>)}
    </section>}
    {!containers.length && <p className="muted">Live trial visualizations appear when Harbor starts the simulator. Completed transcripts are available in Runs.</p>}
  </>;
};
