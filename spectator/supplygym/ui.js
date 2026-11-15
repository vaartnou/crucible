const $ = id => document.getElementById(id);
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ( {
  '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
}[c]));
const label = v => String(v ?? '').replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
const value = v => v == null ? '—' : typeof v === 'number' ? new Intl.NumberFormat('en-US', {
  maximumFractionDigits:2
}).format(v) : String(v);
const money = n => n != null && Number.isFinite(Number(n)) ? new Intl.NumberFormat('en-US', {
  style:'currency',currency:'USD',maximumFractionDigits:0
}).format(n) : '—';
const colors = {
  ss:'#f3a873',base_stock:'#83c8d0',fixed_lot:'#c5a9e4',fill_adjusted_base_stock:'#e4d17e'
};
const strategyName = (name, primary) => name === primary ? 'AI' : ( {
  ss:'Default (s, S)',base_stock:'Daily base-stock',fixed_lot:'Fixed lot',fill_adjusted_base_stock:'Fill-adjusted base-stock'
}[name] || label(name));
const color = (name, primary) => name === primary ? '#c7f36b' : (colors[name] || '#a5b8a7');
const sourceLabel = state => state.connected ? 'LIVE' : state.source === 'Scripted replay' ? 'SCRIPTED' : 'REPLAY';
const metric = (name, content) => `<div class="detail-item"><span>${esc(name)}</span><strong>${esc(content)}</strong></div>`;
const raw = (title, data) => `<details class="raw-details"><summary>${esc(title)}</summary><pre>${esc(JSON.stringify(data,null,2))}</pre></details>`;
function observationView(observation = {}) {
  const suppliers = Array.isArray(observation.suppliers) ? observation.suppliers : [];
  const pending = suppliers.reduce((sum, s) => sum + (Number(s.pending_units) || 0), 0);
  const lateCount = suppliers.reduce((sum, supplier) => sum + (Number(supplier.late_orders) || 0), 0);
  const next = suppliers.filter(s => s.days_to_next_arrival != null && Number.isFinite(Number(s.days_to_next_arrival))).sort((a,b) => a.days_to_next_arrival-b.days_to_next_arrival)[0];
  const signal = (Array.isArray(observation.signals) ? observation.signals : []).at(-1);
  const late = suppliers.find(s => Number(s.late_orders)>0 || (s.fill_ratio != null && Number(s.fill_ratio)<1));
  let signalText = 'No public warning reported';
  if (signal) {
    const description = signal.text ?? signal.message ?? signal.description ?? signal.type ?? signal.kind;
    signalText = `Day ${value(signal.day)} · ${description ? value(description) : JSON.stringify(signal)}`;
  } else if (late) signalText = `Day ${value(observation.day)} · ${late.id}: ${value(late.late_orders)} late orders, ${value(Number(late.fill_ratio)*100)}% fill`;
  return `<div class="detail-grid">${metric('On hand',observation.onhand == null?'—':`${value(observation.onhand)} units`)}${metric('Pending',suppliers.length?`${value(pending)} units`:'—')}${metric('Next reported arrival',next?`${value(next.days_to_next_arrival)} days · ${next.id}`:'None reported')}${metric('Late orders',suppliers.length ? value(lateCount) : '—')}${metric('Public signal / fill',signalText)}</div>${raw('View public observation',observation)}`;
}

function decisionView(week) {
  const before=week.observation?.controls || {};
  const after = week.applied_controls || {};
  const keys=new Set([...Object.keys(before),...Object.keys(after)]);
  const changes=[...keys].filter(k=>JSON.stringify(before[k])!==JSON.stringify(after[k]));
  const format = (v, key) => v == null ? '—' : key === 'expedite_fraction' && typeof v === 'number' ? `${value(v * 100)}%` : typeof v === 'object' ? JSON.stringify(v) : value(v);
  const changeHtml=changes.length?changes.map(k=>`<div class="control-change"><span>${esc(label(k))}</span><strong>${esc(format(before[k],k))} → ${esc(format(after[k],k))}</strong></div>`).join(''):'<p class="empty-copy">No applied control changes reported.</p>';
  const result=week.weekly_result?.last_week || {};
  return `<h3>APPLIED CONTROL CHANGES</h3>${changeHtml}<h3>WEEKLY OUTCOME</h3><div class="detail-grid">${metric('Unmet demand',result.lost_units == null?'—':`${value(result.lost_units)} units`)}${metric('Expedite cost',money(result.expedite_cost))}${metric('Weekly operating surplus',money(week.weekly_operating_surplus))}</div>${raw('View requested controls, applied controls and result',{requested_controls:week.requested_controls,applied_controls:week.applied_controls,weekly_result:week.weekly_result})}`;
}

function drawChart(runs,index,primary,source,onSeek) {
  const svg=$('chart');
  const visible=runs.map(run=>( {
    ...run,color:color(run.strategy,primary),points:(run.weeks||[]).filter(w=>Number(w.week)<=index && Number.isFinite(Number(w.cumulative_operating_surplus)))
  }));
  const points=visible.flatMap(run=>run.points);
  $('chart-empty').hidden=points.length>0;
  $('legend').innerHTML=runs.map(run=> {
    const point=(run.weeks||[]).find(w=>Number(w.week)===index);
      return `<div class="legend-item ${run.strategy===primary?'primary':''}"><span class="swatch" style="--color:${color(run.strategy,primary)}"></span>${esc(strategyName(run.strategy,primary))}${run.strategy===primary?`<small>${esc(source)}</small>`:''}<b>${point?money(point.cumulative_operating_surplus):'—'}</b></div>`;
  }).join('');
  if(!points.length) {
    svg.innerHTML='';
    return;
  } const left=82,right=980,top=18,bottom=282,vals=points.map(p=>Number(p.cumulative_operating_surplus));
  let min=Math.min(0,...vals),max=Math.max(0,...vals);
  if(min===max) {
    min-=1;
    max+=1;
  }const pad=(max-min)*.08;
  min-=pad;
  max+=pad;
  const x=w=>left+w/53*(right-left),y=v=>bottom-(v-min)/(max-min)*(bottom-top);
  let markup='';
  for(let i=0;i<=4;i++) {
    const v=min+(max-min)*i/4, yy=y(v);
    markup+=`<line class="grid" x1="${left}" y1="${yy}" x2="${right}" y2="${yy}"/><text x="${left-12}" y="${yy+4}" text-anchor="end">${esc(money(v))}</text>`;
  } if(min<0&&max>0)markup+=`<line class="zero" x1="${left}" y1="${y(0)}" x2="${right}" y2="${y(0)}"/>`;
  for(const week of [0,13,26,39,53])markup+=`<text x="${x(week)}" y="310" text-anchor="middle">${week}</text>`;
  markup+=`<text x="${right}" y="329" text-anchor="end">COMPLETED WEEK</text>`;
  for(const run of visible) {
    const sorted=[...run.points].sort((a,b)=>a.week-b.week);
    if(!sorted.length)continue;
    const d=sorted.map((p,i)=>`${i?'L':'M'}${x(Number(p.week)).toFixed(2)},${y(Number(p.cumulative_operating_surplus)).toFixed(2)}`).join(' ');
    markup+=`<path class="line ${run.strategy===primary?'buyer-line':''}" stroke="${run.color}" d="${d}"/>`;
    const last=sorted.at(-1);
    markup+=`<circle cx="${x(Number(last.week))}" cy="${y(Number(last.cumulative_operating_surplus))}" r="4" fill="${run.color}"/>`;
  } if(index>0)markup+=`<line class="cursor" x1="${x(index)}" x2="${x(index)}" y1="${top}" y2="${bottom}"/>`;
  svg.innerHTML=markup;
  svg.onclick=e=> {
    const rect=svg.getBoundingClientRect(),sx=(e.clientX-rect.left)/rect.width*1000;
    onSeek(Math.max(0,Math.min(index,Math.round((sx-left)/(right-left)*53))));
  };
}

export function createUI(callbacks) {
  let current= {},revealed=false,lastKey='';
  $('files').onchange=e=> {
    callbacks.onFiles?.(e.target.files);
    e.target.value='';
  };
  $('folder').onchange=e=> {
    callbacks.onFiles?.(e.target.files);
    e.target.value='';
  };
  $('demo').onclick=()=>callbacks.onDemo?.();
  $('connect-form').onsubmit=e=> {
    e.preventDefault();
    callbacks.onConnect?.($('url').value||'/run/spectator-manifest.json');
  };
  $('disconnect').onclick=()=>callbacks.onDisconnect?.();
  $('group').onchange=e=>callbacks.onGroup?.(e.target.value);
  $('seek').oninput=e=>callbacks.onSeek?.(Number(e.target.value));
  $('previous').onclick=()=>callbacks.onSeek?.(Math.max(0,current.index-1));
  $('next').onclick=()=>callbacks.onSeek?.(Math.min(current.maxIndex,current.index+1));
  $('latest').onclick=()=>callbacks.onSeek?.(current.maxIndex);
  $('play').onclick=()=>current.playing?callbacks.onPause?.():callbacks.onPlay?.();
  $('reveal').onclick=()=> {
    revealed=!revealed;
    render(current);
  };
  function render(state) {
    current=state;
    const index=Number(state.index)||0,max=Number(state.maxIndex)||0,runs=state.runs||[],primary=state.primary;
    const key=`${state.groupId}:${index}`;
    if(key!==lastKey) {
      revealed=false;
      lastKey=key;
    } $('status').textContent=state.status||'Awaiting data';
    $('status-detail').textContent=state.statusDetail||'Load a recorded run or connect to a live manifest.';
    $('source').textContent=state.source||'NO SOURCE';
    $('status-dot').classList.toggle('live',!!state.connected);
    $('hero-week').textContent=index||'—';
    $('selected-week').textContent=index;
    $('latest-week').textContent=`LATEST ${max}`;
    $('seek').max=max;
    $('seek').value=index;
    $('seek').disabled=!max;
    $('play').textContent=state.playing?'Ⅱ':'▶';
    $('play').setAttribute('aria-label',state.playing?'Pause':'Play');
    $('play').disabled=!max;
    $('previous').disabled=index<=0;
    $('next').disabled=index>=max;
    $('latest').disabled=index>=max;
    const groups=state.groups||[];
    $('group').innerHTML=groups.length?groups.map(g=>`<option value="${esc(g.id)}">${esc(g.label)}</option>`).join(''):'<option value="">No scenario loaded</option>';
    $('group').value=state.groupId||'';
    $('group').disabled=groups.length<2;
    drawChart(runs,index,primary,sourceLabel(state),n=>callbacks.onSeek?.(n));
    const run=runs.find(r=>r.strategy===primary)||runs[0],week=run?.weeks?.find(w=>Number(w.week)===index);
    $('decision-week').textContent=index?`WEEK ${index}`:'WEEK —';
    $('observation').innerHTML=week?observationView(week.observation):'<div class="empty-copy">Select a completed week to inspect the public observation.</div>';
    $('reveal').disabled=!week;
    $('reveal').innerHTML=revealed?'Hide decision & result <span>↑</span>':'Reveal decision & result <span>↓</span>';
    $('revealed').hidden=!revealed||!week;
    if(week&&revealed)$('revealed').innerHTML=decisionView(week);
    const finals=runs.filter(r=>r.final&&Number(r.final.week)<=index);
    $('results').innerHTML=finals.length?finals.map(r=> {
      const f=r.final,a=f.verified_accounting|| {},score=f.score|| {};
      return `<div class="result-row"><strong>${esc(strategyName(r.strategy,primary))}${r.strategy===primary?` · ${esc(sourceLabel(state))}`:''}</strong><span class="result-value">${money(a.operating_surplus)}</span><div class="result-meta">${f.completed?'COMPLETED':'INCOMPLETE'} · Reward: <b>${score.total==null?'—':esc(value(score.total))}</b> · Theoretical upper bound: <b>${money(a.surplus_upper_bound)}</b></div></div>`;
    }).join(''):'<div class="empty-copy">Verified results will appear here when available.</div>';
  } return {
    render
  };
} document.querySelectorAll('.file-label').forEach(item=>item.addEventListener('keydown',event=> {
  if(event.key==='Enter'||event.key===' ') {
    event.preventDefault();item.querySelector('input').click();
  }
}));
