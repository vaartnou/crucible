import {observationView} from './ui.js';

const $ = id => document.getElementById(id);
const ids = [...Array.from({length: 10}, (_, i) => `S${String(i + 1).padStart(2, '0')}`), 'A01', 'A02'];

export function createSimulationControls({onManifest}) {
  let run = null, available = false, busy = false, connectedId = '', restoredId = '', localReset = false, actionError = '';
  let pollVersion = 0;
  $('sim-weights').innerHTML = ids.map(id => `<label>${id}<input id="weight-${id}" type="number" min="${Math.exp(-4)}" max="${Math.exp(4)}" step="any" placeholder="Keep current"></label>`).join('');
  const form = $('sim-form');
  const message = text => { $('sim-message').textContent = actionError || text; };
  function controls() {
    const weights = Object.fromEntries(ids.filter(id => $(`weight-${id}`).value !== '').map(id => [id, Number($(`weight-${id}`).value)]));
    return {replenishment: $('sim-replenishment').value, safety_stock_days: Number($('sim-safety').value), order_multiplier: Number($('sim-multiplier').value), expedite_fraction: Number($('sim-expedite').value) / 100, ...(Object.keys(weights).length ? {supplier_weights: weights} : {}), ...($('sim-approve').value ? {approve_alternative: $('sim-approve').value} : {})};
  }
  function restore(next) {
    $('sim-seed').value = next.seed;
    const c = Number(next.week) === 0 && next.draft_controls ? {...next.observation?.controls, ...next.draft_controls} : next.observation?.controls;
    if (!c) return;
    $('sim-replenishment').value = c.replenishment || 'ss';
    $('sim-safety').value = c.safety_stock_days ?? 0;
    $('sim-multiplier').value = c.order_multiplier ?? 1;
    $('sim-expedite').value = Math.round((c.expedite_fraction ?? 0) * 1000000) / 10000;
    for (const id of ids) $(`weight-${id}`).value = c.supplier_weights?.[id] ?? '';
    $('sim-approve').value = c.approve_alternative || '';
  }
  function render() {
    const status = run?.status;
    const ready = status === 'ready';
    $('sim-status').textContent = run ? `${status.toUpperCase()} · WEEK ${run.week ?? 0}` : (available ? 'READY TO START' : 'UNAVAILABLE');
    $('sim-observation').innerHTML = run?.observation ? observationView(run.observation) : 'Create a simulation to see the current decision point before advancing.';
    $('sim-create').disabled = busy || !available || !!run;
    $('sim-step').disabled = busy || !ready;
    $('sim-run').disabled = busy || !ready;
    $('sim-pause').disabled = busy || status !== 'running';
    $('sim-cancel').disabled = busy || !run || !['preparing', 'ready', 'running'].includes(status);
    $('sim-reset').disabled = busy || !available || (run && ['preparing', 'ready', 'running'].includes(status));
    $('sim-seed').disabled = busy || !!run;
    for (const field of form.querySelectorAll('input:not(#sim-seed), select')) field.disabled = busy || status === 'running' || status === 'preparing';
    form.setAttribute('aria-busy', String(busy));
  }
  async function request(path, body) {
    const response = await fetch(path, body === undefined ? {cache: 'no-store'} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    let data;
    try { data = await response.json(); } catch { throw new Error('The simulation server returned an invalid response.'); }
    if (!response.ok) throw new Error(data.error || data.detail || `Simulation request failed (${response.status}).`);
    return data;
  }
  function accept(data) {
    available = !!data.available;
    if (!available) { run = null; message(data.reason || data.error || 'Simulation is unavailable. Start the SupplyGym spectator server to enable controls.'); render(); return; }
    if (localReset && data.run && ['completed', 'cancelled', 'error'].includes(data.run.status)) { render(); return; }
    run = data.run;
    if (run && restoredId !== run.id && run.observation) { restore(run); restoredId = run.id; }
    if (run?.manifest_url && connectedId !== run.id) { connectedId = run.id; onManifest(run.manifest_url); }
    if (run?.status === 'error') message(run.error || 'The simulation encountered an error.');
    else if (run?.status === 'running') message('Running the remaining year with the current standing controls. Simulation pause is separate from playback pause.');
    else if (run?.status === 'preparing') message('Preparing the scenario and comparison strategies…');
    else if (run?.status === 'ready') message('Review the public observation, then apply your controls for the next week.');
    else if (run?.status === 'completed') message('Simulation complete. Explore the results below or start a new scenario.');
    else if (run?.status === 'cancelled') message('Simulation cancelled. Start a new scenario when ready.');
    else message('Choose a seed and controls to begin. The same seed and decisions reproduce the same scenario.');
    render();
  }
  async function action(path, body) {
    if (busy) return;
    busy = true; actionError = ''; pollVersion++; render();
    try { localReset = false; const data = await request(path, body); if (path === '/api/simulation' && data.run) restoredId = data.run.id; accept(data); if (path.endsWith('/step') || path.endsWith('/run')) $('sim-approve').value = ''; }
    catch (error) { actionError = error.message; message(actionError); }
    finally { busy = false; render(); }
  }
  form.onsubmit = event => { event.preventDefault(); if (form.reportValidity()) action('/api/simulation', {seed: Number($('sim-seed').value), controls: controls()}); };
  $('sim-step').onclick = () => { if (form.reportValidity()) action(`/api/simulation/${encodeURIComponent(run.id)}/step`, {controls: controls()}); };
  $('sim-run').onclick = () => { if (form.reportValidity()) action(`/api/simulation/${encodeURIComponent(run.id)}/run`, {controls: controls()}); };
  $('sim-pause').onclick = () => action(`/api/simulation/${encodeURIComponent(run.id)}/pause`, {});
  $('sim-cancel').onclick = () => action(`/api/simulation/${encodeURIComponent(run.id)}/cancel`, {});
  $('sim-reset').onclick = () => { actionError = ''; localReset = true; run = null; restoredId = ''; form.reset(); for (const id of ids) $(`weight-${id}`).value = ''; message('Choose a seed and controls to begin.'); render(); };
  async function poll() {
    if (!busy) {
      try { const version = pollVersion; const data = await request('/api/simulation'); if (version === pollVersion && !busy) accept(data); }
      catch { available = false; message('Simulation server unavailable. Start the SupplyGym spectator server to use interactive controls; recorded feeds remain available below.'); render(); }
    }
    setTimeout(poll, 800);
  }
  render(); poll();
}
