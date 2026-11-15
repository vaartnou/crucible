import React, { useMemo, useState } from 'react';
import { Activity, AlertTriangle, Cpu, GitCommit, Layers, Workflow } from 'lucide-react';
import type {
  CallPathTrace,
  ChangeRecord,
  FogState,
  GodState,
  SerializedStep,
} from '../types';

interface InspectorTabsProps {
  godState: GodState | null;
  fogState: FogState;
  showGodView: boolean;
  steps: SerializedStep[];
  selectedService: string | null;
  onSelectService: (svc: string | null) => void;
  inspectMinute: number | null;
  onChangeInspectMinute: (minute: number | null) => void;
  canInteract?: boolean;
  onQuickTool?: (name: string, args: Record<string, unknown>) => void;
  onStageRevert?: (change: ChangeRecord) => void;
}

export const InspectorTabs: React.FC<InspectorTabsProps> = ({
  godState,
  fogState,
  showGodView,
  steps,
  selectedService,
  onSelectService,
  inspectMinute,
  onChangeInspectMinute,
  canInteract,
  onQuickTool,
  onStageRevert,
}) => {
  const [tab, setTab] = useState<'traces' | 'timeline' | 'pods' | 'changes'>('traces');
  const [selectedBranch, setSelectedBranch] = useState<string | null>(null);

  const usingGod = showGodView && godState !== null;

  // Build fallback Fog-of-War traces from discovered edges when God View is off
  const traces: CallPathTrace[] = useMemo(() => {
    if (usingGod && godState) return godState.traces;
    if (fogState.edges.length === 0) return [];
    const byCaller = new Map<string, typeof fogState.edges>();
    for (const e of fogState.edges) {
      const list = byCaller.get(e.caller) ?? [];
      list.push(e);
      byCaller.set(e.caller, list);
    }
    const roots = byCaller.get('edge-gateway') ?? fogState.edges.slice(0, 6);
    return roots.map((topEdge, idx) => {
      const spans: CallPathTrace['spans'] = [];
      let currEdge: (typeof fogState.edges)[number] | undefined = topEdge;
      let depth = 0;
      let cursor = 0;
      let maxErr = 0;
      const visited = new Set<string>();
      while (currEdge && !visited.has(currEdge.callee)) {
        visited.add(currEdge.callee);
        const durMs = currEdge.latency * 1000;
        const tags: string[] = [];
        if (durMs > currEdge.timeout_ms) tags.push('TIMEOUT');
        if (currEdge.pool_rejection_ratio > 0.01) tags.push('POOL_EXHAUSTED');
        if (currEdge.offered_attempt_rate > currEdge.request_rate * 1.25) tags.push('RETRY_STORM');
        maxErr = Math.max(maxErr, currEdge.error_ratio);
        spans.push({
          span_id: `fog-${idx}-${depth}`,
          parent_id: depth > 0 ? `fog-${idx}-${depth - 1}` : null,
          depth,
          caller: currEdge.caller,
          service: currEdge.callee,
          kind: 'rpc',
          start_ms: cursor,
          duration_ms: durMs,
          effective_wait_ms: Math.min(durMs, currEdge.timeout_ms),
          self_ms: 15,
          timeout_ms: currEdge.timeout_ms,
          error_ratio: currEdge.error_ratio,
          edge_error_ratio: currEdge.error_ratio,
          pod_status: 'Observed',
          sample_message: '',
          request_rate: currEdge.request_rate,
          offered_attempt_rate: currEdge.offered_attempt_rate,
          attempt_rate: currEdge.attempt_rate,
          retries: currEdge.retries,
          pool_in_use: currEdge.pool_in_use,
          pool_size: currEdge.pool_size,
          pool_rejection_ratio: currEdge.pool_rejection_ratio,
          queue_depth: 0,
          memory_mib: 0,
          tags,
        });
        cursor += 12;
        depth += 1;
        const nextList = byCaller.get(currEdge.callee);
        currEdge = nextList?.[0];
      }
      const branch = topEdge.callee.split('-')[0] ?? topEdge.callee;
      return {
        trace_id: `fog-tr-${branch}`,
        branch,
        minute: fogState.current_minute,
        root_service: topEdge.caller,
        leaf_service: spans[spans.length - 1]?.service ?? topEdge.callee,
        total_latency_ms: spans.reduce((m, s) => Math.max(m, s.start_ms + s.duration_ms), 0),
        max_error_ratio: maxErr,
        has_error: maxErr > 0.01,
        bottleneck: maxErr > 0.01 ? `${topEdge.callee} (${(maxErr * 100).toFixed(1)}% err)` : 'ok',
        spans,
      };
    });
  }, [usingGod, godState, fogState]);

  const activeTrace = useMemo(() => {
    if (traces.length === 0) return null;
    if (selectedService) {
      const matchSvc = traces.find((t) => t.spans.some((s) => s.service === selectedService));
      if (matchSvc) return matchSvc;
    }
    if (selectedBranch) {
      const found = traces.find((t) => t.branch === selectedBranch);
      if (found) return found;
    }
    return traces[0];
  }, [traces, selectedBranch, selectedService]);

  const changesList = usingGod && godState ? godState.changes : fogState.changes;
  const alertsList = usingGod && godState ? godState.alerts : fogState.alerts;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0 }}>
      <div className="subtabs">
        <button
          className={`subtab ${tab === 'traces' ? 'active' : ''}`}
          onClick={() => setTab('traces')}
        >
          <Workflow size={13} />
          Call paths ({traces.filter((t) => t.has_error).length}/{traces.length})
        </button>
        <button
          className={`subtab ${tab === 'timeline' ? 'active' : ''}`}
          onClick={() => setTab('timeline')}
        >
          <Activity size={13} />
          Impact
        </button>
        <button
          className={`subtab ${tab === 'pods' ? 'active' : ''}`}
          onClick={() => setTab('pods')}
        >
          <Cpu size={13} />
          {selectedService ? `Service: ${selectedService}` : 'Services'}
        </button>
        <button
          className={`subtab ${tab === 'changes' ? 'active' : ''}`}
          onClick={() => setTab('changes')}
        >
          <GitCommit size={13} />
          Changes &amp; alerts
        </button>

        {usingGod && godState && (
          <div
            style={{
              marginLeft: 'auto',
              display: 'flex',
              alignItems: 'center',
              gap: 6,
              padding: '4px 6px',
            }}
          >
            <span className="mono" style={{ fontSize: 11, color: 'var(--text-2)' }}>
              Scrub m={godState.selected_minute}:
            </span>
            <input
              type="range"
              min={godState.min_minute}
              max={godState.max_minute}
              value={godState.selected_minute}
              onChange={(e) => {
                const v = Number(e.target.value);
                onChangeInspectMinute(v === godState.max_minute ? null : v);
              }}
              style={{ width: 95, accentColor: 'var(--cyan)' }}
            />
            {inspectMinute !== null && (
              <button
                className="btn"
                style={{ padding: '2px 6px', fontSize: 10.5 }}
                onClick={() => onChangeInspectMinute(null)}
              >
                Live (m={godState.max_minute})
              </button>
            )}
          </div>
        )}
      </div>

      <div className="scroll-body">
        {tab === 'traces' && (
          <TracesPanel
            traces={traces}
            activeTrace={activeTrace}
            usingGod={usingGod}
            onSelectBranch={(b) => {
              setSelectedBranch(b);
              onSelectService(null);
            }}
            onSelectService={onSelectService}
            canInteract={canInteract}
            onQuickTool={onQuickTool}
          />
        )}

        {tab === 'timeline' && (
          <TimelinePanel
            godState={usingGod ? godState : null}
            fogState={fogState}
            steps={steps}
            onSelectMinute={(m) => onChangeInspectMinute(m)}
            onSelectService={onSelectService}
          />
        )}

        {tab === 'pods' && (
          <PodsPanel
            godState={usingGod ? godState : null}
            fogState={fogState}
            selectedService={selectedService}
            onSelectService={onSelectService}
            canInteract={canInteract}
            onQuickTool={onQuickTool}
          />
        )}

        {tab === 'changes' && (
          <ChangesAndAlertsPanel
            changes={changesList}
            alerts={alertsList}
            selectedService={selectedService}
            onSelectService={onSelectService}
            canInteract={canInteract}
            onStageRevert={onStageRevert}
          />
        )}
      </div>
    </div>
  );
};

/* ---------------- Call-Path Waterfalls ("Traces") ---------------- */
const TracesPanel: React.FC<{
  traces: CallPathTrace[];
  activeTrace: CallPathTrace | null;
  usingGod: boolean;
  onSelectBranch: (b: string) => void;
  onSelectService: (s: string | null) => void;
  canInteract?: boolean;
  onQuickTool?: (name: string, args: Record<string, unknown>) => void;
}> = ({ traces, activeTrace, usingGod, onSelectBranch, onSelectService, canInteract, onQuickTool }) => {
  if (traces.length === 0) {
    return (
      <div style={{ padding: 20, textAlign: 'center', color: 'var(--text-2)' }}>
        <div style={{ marginBottom: 8 }}>
          No edge call-path telemetry observed yet in Fog-of-War mode.
        </div>
        <div style={{ fontSize: 11.5, marginBottom: 10 }}>
          Note: <code>oncall@1</code> simulates a fluid network without per-request OpenTelemetry
          span IDs; call-path waterfalls are synthesized from caller/callee latency, timeout
          deadlines, retry amplification, and connection pool saturation.
        </div>
        {canInteract && onQuickTool && (
          <button
            className="btn btn-primary"
            onClick={() => onQuickTool('query_edges', { service: '*', window: 1 })}
          >
            Run `query_edges(service=&quot;*&quot;)` (+1m)
          </button>
        )}
      </div>
    );
  }

  const maxAxisMs = Math.max(
    250,
    ...(activeTrace?.spans.map((s) =>
      Math.max(s.start_ms + s.duration_ms, s.start_ms + (s.timeout_ms ?? 0)),
    ) ?? [250]),
  );

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8, flexWrap: 'wrap' }}>
        <div style={{ display: 'flex', gap: 5, flexWrap: 'wrap' }}>
          {traces.map((t) => {
            const active = activeTrace?.trace_id === t.trace_id;
            return (
              <button
                key={t.trace_id}
                className="btn"
                style={{
                  borderColor: active
                    ? 'var(--cyan)'
                    : t.has_error
                      ? 'rgba(239,68,68,0.45)'
                      : undefined,
                  background: active ? 'rgba(56,189,248,0.16)' : undefined,
                  padding: '4px 8px',
                }}
                onClick={() => onSelectBranch(t.branch)}
              >
                <span
                  style={{
                    width: 6,
                    height: 6,
                    borderRadius: '50%',
                    background: t.has_error ? '#ef4444' : '#10b981',
                  }}
                />
                <span className="mono">{t.branch}</span>
                <span className="mono" style={{ color: 'var(--text-2)', fontSize: 10.5 }}>
                  {Math.round(t.total_latency_ms)}ms
                </span>
              </button>
            );
          })}
        </div>
        <span style={{ fontSize: 11, color: 'var(--text-2)' }}>
          {usingGod ? 'Derived from live fluid-network EdgeSamples' : 'Derived from queried edges'}
        </span>
      </div>

      {activeTrace && (
        <div className="card" style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <div>
              <span className="mono" style={{ fontWeight: 600 }}>
                {activeTrace.root_service} → {activeTrace.leaf_service}
              </span>{' '}
              <span className={`badge ${activeTrace.has_error ? 'badge-err' : 'badge-ok'}`}>
                {activeTrace.bottleneck}
              </span>
            </div>
            <span className="mono" style={{ fontSize: 11, color: 'var(--text-2)' }}>
              m={activeTrace.minute} · scale 0–{Math.round(maxAxisMs)}ms
            </span>
          </div>

          {activeTrace.spans.map((sp) => {
            const leftPct = Math.min(96, (sp.start_ms / maxAxisMs) * 100);
            const widthPct = Math.max(1.5, Math.min(100 - leftPct, (sp.duration_ms / maxAxisMs) * 100));
            const timeoutPct =
              sp.timeout_ms != null
                ? Math.min(99, ((sp.start_ms + sp.timeout_ms) / maxAxisMs) * 100)
                : null;
            const bad = sp.error_ratio > 0.05 || sp.tags.length > 0;
            const barColor = bad
              ? 'linear-gradient(90deg, rgba(239,68,68,0.75), rgba(245,158,11,0.75))'
              : 'linear-gradient(90deg, rgba(56,189,248,0.65), rgba(16,185,129,0.65))';

            return (
              <div
                key={sp.span_id}
                onClick={() => onSelectService(sp.service)}
                style={{
                  display: 'grid',
                  gridTemplateColumns: '210px minmax(0, 1fr) 175px',
                  gap: 10,
                  alignItems: 'center',
                  padding: '6px 8px',
                  borderRadius: 6,
                  background: 'var(--bg-1)',
                  border: '1px solid var(--border)',
                  cursor: 'pointer',
                }}
              >
                <div style={{ paddingLeft: sp.depth * 14, overflow: 'hidden' }}>
                  <div className="mono" style={{ fontWeight: 600, fontSize: 11.5 }}>
                    {sp.depth > 0 ? '↳ ' : ''}
                    {sp.service}
                  </div>
                  <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap', marginTop: 2 }}>
                    <span className="mono" style={{ fontSize: 10, color: 'var(--text-2)' }}>
                      {sp.kind}
                    </span>
                    {sp.tags.map((t) => (
                      <span key={t} className="badge badge-err" style={{ fontSize: 9.5, padding: '0 4px' }}>
                        {t}
                      </span>
                    ))}
                  </div>
                </div>

                {/* Waterfall timeline bar + timeout deadline line */}
                <div>
                  <div
                    style={{
                      position: 'relative',
                      height: 18,
                      background: '#080b10',
                      borderRadius: 4,
                      border: '1px solid rgba(52, 70, 99, 0.5)',
                      overflow: 'hidden',
                    }}
                  >
                    <div
                      style={{
                        position: 'absolute',
                        left: `${leftPct}%`,
                        width: `${widthPct}%`,
                        top: 2,
                        bottom: 2,
                        borderRadius: 3,
                        background: barColor,
                      }}
                    />
                    {timeoutPct !== null && (
                      <div
                        title={`Edge timeout budget: ${sp.timeout_ms}ms`}
                        style={{
                          position: 'absolute',
                          left: `${timeoutPct}%`,
                          top: 0,
                          bottom: 0,
                          width: 2,
                          background: '#ef4444',
                          boxShadow: '0 0 6px #ef4444',
                        }}
                      />
                    )}
                    <span
                      className="mono"
                      style={{
                        position: 'absolute',
                        left: 6,
                        top: 2,
                        fontSize: 10,
                        color: '#e2e8f0',
                        textShadow: '0 1px 2px #000',
                      }}
                    >
                      {Math.round(sp.duration_ms)}ms
                      {sp.timeout_ms != null ? ` / deadline ${Math.round(sp.timeout_ms)}ms` : ''}
                    </span>
                  </div>
                  {sp.sample_message && sp.sample_message !== 'request completed' && (
                    <div
                      className="mono"
                      style={{ fontSize: 10, color: '#fca5a5', marginTop: 3 }}
                    >
                      log: &quot;{sp.sample_message}&quot;
                    </div>
                  )}
                </div>

                {/* Pool / Retry / Error stats */}
                <div className="mono" style={{ fontSize: 10.5, textAlign: 'right', color: 'var(--text-1)' }}>
                  <div>
                    err:{' '}
                    <b style={{ color: sp.error_ratio > 0.02 ? '#f87171' : '#34d399' }}>
                      {(sp.error_ratio * 100).toFixed(1)}%
                    </b>
                    {sp.pool_size != null && (
                      <>
                        {' · '}pool {Math.round(sp.pool_in_use ?? 0)}/{sp.pool_size}
                      </>
                    )}
                  </div>
                  <div style={{ color: 'var(--text-2)', fontSize: 10 }}>
                    {sp.offered_attempt_rate != null
                      ? `rps ${Math.round(sp.request_rate)} → ${Math.round(sp.offered_attempt_rate)} att`
                      : `rps ${Math.round(sp.request_rate)}`}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
};

/* ---------------- Impact Curve & Cluster Heatmap ---------------- */
const TimelinePanel: React.FC<{
  godState: GodState | null;
  fogState: FogState;
  steps: SerializedStep[];
  onSelectMinute: (m: number) => void;
  onSelectService: (s: string) => void;
}> = ({ godState, fogState, steps, onSelectMinute, onSelectService }) => {
  if (!godState) {
    const seriesKeys = Object.keys(fogState.series);
    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
        <div className="card">
          <div style={{ fontWeight: 600, marginBottom: 6 }}>
            Agent-Queried Metric Series (Fog-of-War)
          </div>
          {seriesKeys.length === 0 ? (
            <div style={{ color: 'var(--text-2)' }}>
              No <code>query_metrics(..., series=true)</code> calls recorded yet, and Simulator God
              View is currently hidden. Toggle <b>Full cluster</b> above to inspect full counterfactual
              loss curves and the 23-service onset heatmap.
            </div>
          ) : (
            seriesKeys.map((metric) => {
              const pts = fogState.series[metric] ?? [];
              return (
                <div key={metric} style={{ marginBottom: 8 }}>
                  <span className="badge badge-info">{metric}</span>{' '}
                  <span className="mono" style={{ fontSize: 11, color: 'var(--text-2)' }}>
                    {pts.length} samples recorded
                  </span>
                </div>
              );
            })
          )}
        </div>
      </div>
    );
  }

  const { minutes, actual_loss, baseline_loss, service_heatmap } = godState.timeline;
  const maxLoss = Math.max(10, ...actual_loss, ...baseline_loss);
  const w = 680;
  const h = 135;
  const padL = 38;
  const padR = 14;
  const padT = 12;
  const padB = 22;
  const plotW = w - padL - padR;
  const plotH = h - padT - padB;

  const xOf = (idx: number) =>
    padL + (minutes.length <= 1 ? 0 : (idx / (minutes.length - 1)) * plotW);
  const yOf = (val: number) => padT + plotH - (val / maxLoss) * plotH;

  const actualPath = actual_loss
    .map((v, i) => `${i === 0 ? 'M' : 'L'} ${xOf(i).toFixed(1)} ${yOf(v).toFixed(1)}`)
    .join(' ');
  const basePath = baseline_loss
    .map((v, i) => `${i === 0 ? 'M' : 'L'} ${xOf(i).toFixed(1)} ${yOf(v).toFixed(1)}`)
    .join(' ');

  const actionMinutes = steps.filter(
    (s) => s.cost_minutes >= 3 || s.call.name === 'resolve',
  );

  const heatmapServices = Object.entries(service_heatmap).sort((a, b) => {
    const maxA = Math.max(...a[1], 0);
    const maxB = Math.max(...b[1], 0);
    return maxB - maxA;
  });

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div className="card">
        <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 6 }}>
          <span style={{ fontWeight: 600 }}>
            Gateway Failed-Request Loss/min (Actual vs. Do-Nothing Counterfactual)
          </span>
          <div style={{ display: 'flex', gap: 12, fontSize: 11 }} className="mono">
            <span style={{ color: '#38bdf8' }}>━ Actual Loss</span>
            <span style={{ color: '#ef4444' }}>┅ Do-Nothing Baseline</span>
            <span style={{ color: '#fbbf24' }}>│ Agent Action</span>
          </div>
        </div>
        <svg width="100%" height={h} viewBox={`0 0 ${w} ${h}`}>
          <line x1={padL} y1={padT + plotH} x2={w - padR} y2={padT + plotH} stroke="#243044" />
          <line x1={padL} y1={padT} x2={padL} y2={padT + plotH} stroke="#243044" />
          <path
            d={basePath}
            fill="none"
            stroke="#ef4444"
            strokeWidth={1.6}
            strokeDasharray="4 3"
            opacity={0.75}
          />
          <path d={actualPath} fill="none" stroke="#38bdf8" strokeWidth={2.2} />

          {actionMinutes.map((st) => {
            const idx = minutes.indexOf(st.minute_after);
            if (idx < 0) return null;
            const x = xOf(idx);
            return (
              <g key={st.step}>
                <line
                  x1={x}
                  y1={padT}
                  x2={x}
                  y2={padT + plotH}
                  stroke="#fbbf24"
                  strokeDasharray="2 2"
                />
                <text x={x + 3} y={padT + 10} fill="#fbbf24" fontSize={8.5} className="mono">
                  #{st.step}
                </text>
              </g>
            );
          })}

          <text x={padL} y={h - 5} fill="#7688a3" fontSize={9.5} className="mono">
            m={minutes[0]}
          </text>
          <text
            x={w - padR}
            y={h - 5}
            textAnchor="end"
            fill="#7688a3"
            fontSize={9.5}
            className="mono"
          >
            m={minutes[minutes.length - 1]}
          </text>
          <text x={4} y={padT + 8} fill="#7688a3" fontSize={9} className="mono">
            {Math.round(maxLoss)}
          </text>
        </svg>
      </div>

      <div className="card">
        <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 6 }}>
          <span style={{ fontWeight: 600 }}>
            Service Error Onset Heatmap (click any cell to time-scrub)
          </span>
          <span style={{ fontSize: 11, color: 'var(--text-2)' }}>
            Sorted by peak error · reveals causal upstream→downstream cascade order
          </span>
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
          {heatmapServices.slice(0, 12).map(([svcName, series]) => (
            <div
              key={svcName}
              style={{
                display: 'grid',
                gridTemplateColumns: '150px minmax(0, 1fr)',
                alignItems: 'center',
                gap: 8,
              }}
            >
              <span
                className="mono"
                onClick={() => onSelectService(svcName)}
                style={{
                  fontSize: 10.5,
                  cursor: 'pointer',
                  color: 'var(--text-1)',
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                }}
              >
                {svcName}
              </span>
              <div
                style={{
                  display: 'grid',
                  gridTemplateColumns: `repeat(${series.length}, minmax(2px, 1fr))`,
                  height: 11,
                  gap: 1,
                }}
              >
                {series.map((err, i) => {
                  const m = minutes[i] ?? 0;
                  const bg =
                    err > 0.25
                      ? '#ef4444'
                      : err > 0.05
                        ? '#f59e0b'
                        : err > 0.01
                          ? '#eab308'
                          : '#16202e';
                  return (
                    <div
                      key={i}
                      title={`${svcName} @ m=${m}: ${(err * 100).toFixed(1)}% error`}
                      onClick={() => onSelectMinute(m)}
                      style={{
                        background: bg,
                        borderRadius: 1,
                        cursor: 'pointer',
                        outline:
                          godState.selected_minute === m ? '1px solid var(--cyan)' : undefined,
                      }}
                    />
                  );
                })}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
};

/* ---------------- Pods & Services Inspector ---------------- */
const PodsPanel: React.FC<{
  godState: GodState | null;
  fogState: FogState;
  selectedService: string | null;
  onSelectService: (s: string | null) => void;
  canInteract?: boolean;
  onQuickTool?: (name: string, args: Record<string, unknown>) => void;
}> = ({ godState, fogState, selectedService, onSelectService, canInteract, onQuickTool }) => {
  if (godState) {
    const filtered = selectedService
      ? godState.services.filter((s) => s.name === selectedService)
      : [...godState.services].sort((a, b) => b.error_ratio - a.error_ratio);

    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
        {selectedService && (
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
              <Layers size={14} color="var(--cyan)" />
              <b className="mono">{selectedService}</b>
              <button className="btn" onClick={() => onSelectService(null)}>
                Show All Services
              </button>
            </div>
            {canInteract && onQuickTool && (
              <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                <button
                  className="btn btn-primary"
                  onClick={() =>
                    onQuickTool('query_metrics', {
                      metric: 'error_ratio',
                      service: selectedService,
                      by: 'pod',
                    })
                  }
                >
                  Pod Errors (+1m)
                </button>
                <button
                  className="btn"
                  onClick={() =>
                    onQuickTool('search_logs', { service: selectedService, level: 'ERROR' })
                  }
                >
                  ERROR Logs (+1m)
                </button>
                <button
                  className="btn"
                  onClick={() =>
                    onQuickTool('run', { command: `config history ${selectedService}` })
                  }
                >
                  Config History (+1m)
                </button>
                <button
                  className="btn"
                  onClick={() =>
                    onQuickTool('run', {
                      command: `kubectl rollout history deploy/${selectedService}`,
                    })
                  }
                >
                  Rollout History (+1m)
                </button>
              </div>
            )}
          </div>
        )}

        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fill, minmax(270px, 1fr))',
            gap: 8,
          }}
        >
          {filtered.slice(0, selectedService ? 1 : 9).flatMap((svc) =>
            svc.pods.map((p) => (
              <div key={p.pod} className="card" style={{ padding: 9 }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
                  <span
                    className="mono"
                    style={{ fontWeight: 600, fontSize: 11.5, cursor: 'pointer' }}
                    onClick={() => onSelectService(svc.name)}
                  >
                    {p.pod}
                  </span>
                  <span
                    className={`badge ${
                      p.state !== 'Running' || p.error_ratio > 0.05
                        ? 'badge-err'
                        : p.error_ratio > 0.01
                          ? 'badge-warn'
                          : 'badge-ok'
                    }`}
                  >
                    {p.state} · {p.version}
                  </span>
                </div>
                <div className="mono" style={{ fontSize: 11, color: 'var(--text-1)' }}>
                  err: {(p.error_ratio * 100).toFixed(1)}% · p99:{' '}
                  {Math.round(p.p99_latency * 1000)}ms · mem: {Math.round(p.memory)}MiB
                  {p.queue_depth > 1 ? ` · Q:${Math.round(p.queue_depth)}` : ''}
                </div>
                {/* Memory bar */}
                <div
                  style={{
                    height: 4,
                    background: '#090d14',
                    borderRadius: 2,
                    marginTop: 5,
                    overflow: 'hidden',
                  }}
                >
                  <div
                    style={{
                      width: `${Math.min(100, (p.memory / 1024) * 100)}%`,
                      height: '100%',
                      background: p.memory > 850 ? '#ef4444' : '#38bdf8',
                    }}
                  />
                </div>
                {p.message && p.message !== 'request completed' && (
                  <div className="mono" style={{ fontSize: 10, color: '#fca5a5', marginTop: 5 }}>
                    {p.message}
                  </div>
                )}
              </div>
            )),
          )}
        </div>
      </div>
    );
  }

  // Fog-of-War pods & selected service quick actions
  const fogPods = selectedService
    ? fogState.pods.filter((p) => p.service === selectedService)
    : fogState.pods;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
      {selectedService && (
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 8 }}>
          <div>
            <b className="mono">{selectedService}</b>{' '}
            <button className="btn" onClick={() => onSelectService(null)}>
              Clear Filter
            </button>
          </div>
          {canInteract && onQuickTool && (
            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
              <button
                className="btn btn-primary"
                onClick={() =>
                  onQuickTool('query_metrics', {
                    metric: 'error_ratio',
                    service: selectedService,
                    by: 'pod',
                  })
                }
              >
                Query Pod Errors (+1m)
              </button>
              <button
                className="btn"
                onClick={() =>
                  onQuickTool('search_logs', { service: selectedService, level: 'ERROR' })
                }
              >
                Search ERROR Logs (+1m)
              </button>
              <button
                className="btn"
                onClick={() =>
                  onQuickTool('run', { command: `config history ${selectedService}` })
                }
              >
                Config History (+1m)
              </button>
              <button
                className="btn"
                onClick={() =>
                  onQuickTool('run', {
                    command: `kubectl rollout history deploy/${selectedService}`,
                  })
                }
              >
                Rollout History (+1m)
              </button>
            </div>
          )}
        </div>
      )}

      {fogPods.length === 0 ? (
        <div style={{ color: 'var(--text-2)', padding: 14 }}>
          No pod-level observations recorded yet{selectedService ? ` for ${selectedService}` : ''}.
          Run <code>kubectl get pods -n shop</code> or{' '}
          <code>query_metrics(..., by=&quot;pod&quot;)</code>.
        </div>
      ) : (
        <table className="data-table mono">
          <thead>
            <tr>
              <th>Pod</th>
              <th>Service</th>
              <th>State</th>
              <th>Version</th>
              <th>Observed Metrics</th>
            </tr>
          </thead>
          <tbody>
            {fogPods.map((p) => (
              <tr key={p.pod}>
                <td>{p.pod}</td>
                <td>{p.service}</td>
                <td>{p.state ?? '—'}</td>
                <td>{p.version ?? '—'}</td>
                <td>
                  {Object.entries(p.metrics)
                    .map(([k, v]) => `${k}=${v.toFixed(3)}`)
                    .join(', ') || '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
};

/* ---------------- Changes & Alerts Panel ---------------- */
const ChangesAndAlertsPanel: React.FC<{
  changes: ChangeRecord[];
  alerts: Array<{
    id: string;
    service: string;
    severity: number;
    error_ratio: number;
    started_at: number;
  }>;
  selectedService: string | null;
  onSelectService: (s: string | null) => void;
  canInteract?: boolean;
  onStageRevert?: (c: ChangeRecord) => void;
}> = ({ changes, alerts, selectedService, onSelectService, canInteract, onStageRevert }) => {
  const filteredChanges = selectedService
    ? changes.filter((c) => c.service === selectedService)
    : changes;

  return (
    <div style={{ display: 'grid', gridTemplateColumns: '270px minmax(0, 1fr)', gap: 12 }}>
      <div className="card">
        <div style={{ fontWeight: 600, marginBottom: 8, display: 'flex', alignItems: 'center', gap: 6 }}>
          <AlertTriangle size={13} color="var(--amber)" /> Active Alerts ({alerts.length})
        </div>
        {alerts.length === 0 ? (
          <div style={{ color: 'var(--text-2)' }}>No alerts fetched yet.</div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            {alerts.map((a) => (
              <div
                key={a.id}
                onClick={() => onSelectService(a.service)}
                style={{
                  padding: '6px 8px',
                  borderRadius: 6,
                  background: 'var(--bg-1)',
                  border: '1px solid var(--border)',
                  cursor: 'pointer',
                }}
              >
                <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                  <span className="mono" style={{ fontWeight: 600, fontSize: 11.5 }}>
                    {a.service}
                  </span>
                  <span
                    className={`badge ${
                      a.severity >= 3
                        ? 'badge-err'
                        : a.severity === 2
                          ? 'badge-warn'
                          : 'badge-info'
                    }`}
                  >
                    SEV {a.severity}
                  </span>
                </div>
                <div className="mono" style={{ fontSize: 10.5, color: 'var(--text-2)', marginTop: 3 }}>
                  err {(a.error_ratio * 100).toFixed(1)}% · onset m={a.started_at}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="card">
        <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
          <span style={{ fontWeight: 600 }}>
            Chronological Production Changes{' '}
            {selectedService ? `(filtered: ${selectedService})` : `(${filteredChanges.length})`}
          </span>
          {selectedService && (
            <button className="btn" style={{ padding: '2px 7px' }} onClick={() => onSelectService(null)}>
              All Services
            </button>
          )}
        </div>
        {filteredChanges.length === 0 ? (
          <div style={{ color: 'var(--text-2)' }}>
            No changes discovered yet. Run <code>changes list</code> or <code>config history</code>.
          </div>
        ) : (
          <table className="data-table mono">
            <thead>
              <tr>
                <th>Min</th>
                <th>ID</th>
                <th>Service</th>
                <th>Kind</th>
                <th>Diff Summary</th>
                <th>Action</th>
              </tr>
            </thead>
            <tbody>
              {[...filteredChanges].reverse().map((c) => (
                <tr key={c.id}>
                  <td>m={c.minute}</td>
                  <td>
                    {c.id}
                    {c.is_root_cause && (
                      <span className="badge badge-err" style={{ marginLeft: 4 }}>
                        CULPRIT
                      </span>
                    )}
                  </td>
                  <td
                    style={{ cursor: 'pointer', color: 'var(--cyan)' }}
                    onClick={() => onSelectService(c.service)}
                  >
                    {c.service}
                    {c.callee ? ` → ${c.callee}` : ''}
                  </td>
                  <td>
                    <span className={`badge ${c.kind === 'config' ? 'badge-warn' : 'badge-violet'}`}>
                      {c.kind}
                    </span>
                  </td>
                  <td>
                    {c.summary}
                    {c.reverted && (
                      <span className="badge badge-ok" style={{ marginLeft: 6 }}>
                        REVERTED
                      </span>
                    )}
                  </td>
                  <td>
                    {canInteract && onStageRevert && !c.reverted && (
                      <button
                        className="btn btn-action"
                        style={{ padding: '2px 7px', fontSize: 10.5 }}
                        onClick={() => onStageRevert(c)}
                      >
                        Stage Revert
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
};
