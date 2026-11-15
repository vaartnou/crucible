import React, { useMemo, useState } from 'react';
import { Eye, EyeOff, KeyRound, Maximize2, Radar, ZoomIn, ZoomOut } from 'lucide-react';
import type { EdgeRecord, FogService, FogState, GodService, GodState } from '../types';

interface ClusterGraphProps {
  godState: GodState | null;
  fogState: FogState;
  showGodView: boolean;
  onToggleGodView: () => void;
  revealSecret: boolean;
  onToggleRevealSecret: () => void;
  selectedService: string | null;
  onSelectService: (name: string | null) => void;
  activeStepService: string | null;
  canQuickRun?: boolean;
  onQuickTool?: (name: string, args: Record<string, unknown>) => void;
}

interface PositionedNode {
  name: string;
  kind: string;
  x: number;
  y: number;
  errorRatio: number | null;
  p99Latency: number | null;
  requestRate: number | null;
  queueDepth: number | null;
  memory: number | null;
  status: string;
  version: string | null;
  observedAt: number | null;
  podStates: Array<{ state: string; error: number; version: string }>;
  isRootCause: boolean;
}

function healthColor(err: number | null, p99: number | null, status: string): string {
  if (status === 'OOMKilled' || status === 'CrashLoopBackOff') return '#ef4444';
  if (err === null && p99 === null) return '#475569';
  if ((err ?? 0) > 0.12 || (p99 ?? 0) > 2.0) return '#ef4444';
  if ((err ?? 0) > 0.015 || (p99 ?? 0) > 0.8) return '#f59e0b';
  return '#10b981';
}

export const ClusterGraph: React.FC<ClusterGraphProps> = ({
  godState,
  fogState,
  showGodView,
  onToggleGodView,
  revealSecret,
  onToggleRevealSecret,
  selectedService,
  onSelectService,
  activeStepService,
  canQuickRun,
  onQuickTool,
}) => {
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [dragging, setDragging] = useState<{ startX: number; startY: number } | null>(null);

  const usingGod = showGodView && godState !== null;

  const { nodes, edges, canvasWidth, canvasHeight } = useMemo(() => {
    const rawServices: Array<{
      name: string;
      kind: string;
      deps: string[];
      god?: GodService;
      fog?: FogService;
    }> = [];

    if (usingGod && godState) {
      for (const s of godState.services) {
        rawServices.push({
          name: s.name,
          kind: s.kind,
          deps: s.dependencies,
          god: s,
        });
      }
    } else {
      const fogPodsBySvc = new Map<string, typeof fogState.pods>();
      for (const p of fogState.pods) {
        const list = fogPodsBySvc.get(p.service) ?? [];
        list.push(p);
        fogPodsBySvc.set(p.service, list);
      }
      // Infer edges from fogState.edges if topology wasn't fetched yet
      const inferredDeps = new Map<string, Set<string>>();
      for (const e of fogState.edges) {
        if (!inferredDeps.has(e.caller)) inferredDeps.set(e.caller, new Set());
        inferredDeps.get(e.caller)!.add(e.callee);
      }
      for (const s of fogState.services) {
        const deps = s.dependencies ?? Array.from(inferredDeps.get(s.name) ?? []);
        const kindGuess =
          s.kind ??
          (s.name === 'edge-gateway' || s.name.includes('-api-')
            ? 'http'
            : s.name.includes('-worker-')
              ? 'worker'
              : s.name.includes('-db-')
                ? 'db'
                : s.name.includes('cache')
                  ? 'cache'
                  : 'unknown');
        rawServices.push({
          name: s.name,
          kind: kindGuess,
          deps,
          fog: s,
        });
      }
    }

    if (rawServices.length === 0) {
      return { nodes: [] as PositionedNode[], edges: [] as EdgeRecord[], canvasWidth: 960, canvasHeight: 420 };
    }

    const byName = new Map(rawServices.map((s) => [s.name, s]));
    // Assign columns by branch prefix (e.g. `payments-api-42` -> `payments`)
    const apis = rawServices
      .filter((s) => s.name !== 'edge-gateway' && s.kind === 'http')
      .sort((a, b) => a.name.localeCompare(b.name));
    const caches = rawServices.filter((s) => s.kind === 'cache');
    const totalCols = Math.max(1, apis.length + caches.length);
    const colSpacing = totalCols > 5 ? 158 : 185;
    const width = Math.max(880, totalCols * colSpacing + 120);
    const height = 430;

    const coords = new Map<string, { x: number; y: number }>();
    const startX = (width - (totalCols - 1) * colSpacing) / 2;

    if (byName.has('edge-gateway')) {
      coords.set('edge-gateway', { x: width / 2, y: 52 });
    }

    let colIdx = 0;
    for (const c of caches) {
      coords.set(c.name, { x: startX + colIdx * colSpacing, y: 158 });
      colIdx += 1;
    }

    for (const apiSvc of apis) {
      const x = startX + colIdx * colSpacing;
      coords.set(apiSvc.name, { x, y: 158 });
      const workerName = apiSvc.deps.find((d) => byName.has(d));
      if (workerName) {
        coords.set(workerName, { x, y: 268 });
        const workerObj = byName.get(workerName);
        const dbName = workerObj?.deps.find((d) => byName.has(d));
        if (dbName) {
          coords.set(dbName, { x, y: 374 });
        }
      }
      colIdx += 1;
    }

    // Place any remaining unplaced discovered services cleanly
    const unplaced = rawServices.filter((s) => !coords.has(s.name));
    unplaced.forEach((s, idx) => {
      const rowY = s.kind === 'db' ? 374 : s.kind === 'worker' ? 268 : 158;
      const spreadX = 100 + ((idx * 165) % Math.max(300, width - 200));
      coords.set(s.name, { x: spreadX, y: rowY });
    });

    const culprit = usingGod && revealSecret ? godState?.secret?.culprit : null;
    const fogPodsMap = new Map<string, typeof fogState.pods>();
    for (const p of fogState.pods) {
      const arr = fogPodsMap.get(p.service) ?? [];
      arr.push(p);
      fogPodsMap.set(p.service, arr);
    }

    const builtNodes: PositionedNode[] = rawServices.map((s) => {
      const pos = coords.get(s.name) ?? { x: width / 2, y: 200 };
      if (s.god) {
        return {
          name: s.name,
          kind: s.kind,
          x: pos.x,
          y: pos.y,
          errorRatio: s.god.error_ratio,
          p99Latency: s.god.p99_latency,
          requestRate: s.god.request_rate,
          queueDepth: s.god.queue_depth,
          memory: s.god.memory,
          status: s.god.status,
          version: s.god.version,
          observedAt: godState?.selected_minute ?? null,
          podStates: s.god.pods.map((p) => ({
            state: p.state,
            error: p.error_ratio,
            version: p.version,
          })),
          isRootCause: culprit === s.name,
        };
      }
      const fm = s.fog?.metrics ?? {};
      const fpods = fogPodsMap.get(s.name) ?? [];
      const worstState = fpods.some((p) => p.state === 'OOMKilled')
        ? 'OOMKilled'
        : fpods.some((p) => p.state === 'CrashLoopBackOff')
          ? 'CrashLoopBackOff'
          : 'Running';
      return {
        name: s.name,
        kind: s.kind,
        x: pos.x,
        y: pos.y,
        errorRatio: fm.error_ratio ?? null,
        p99Latency: fm.p99_latency ?? null,
        requestRate: fm.request_rate ?? null,
        queueDepth: fm.queue_depth ?? null,
        memory: fm.memory ?? null,
        status: worstState,
        version: fpods[0]?.version ?? null,
        observedAt: s.fog?.observed_at ?? null,
        podStates: fpods.map((p) => ({
          state: p.state ?? 'Running',
          error: p.metrics.error_ratio ?? 0,
          version: p.version ?? '',
        })),
        isRootCause: false,
      };
    });

    // Build edge records (telemetry edges + structural fallback edges)
    const edgeMap = new Map<string, EdgeRecord>();
    const sourceEdges = usingGod && godState ? godState.edges : fogState.edges;
    for (const e of sourceEdges) {
      edgeMap.set(`${e.caller}->${e.callee}`, e);
    }
    for (const s of rawServices) {
      for (const dep of s.deps) {
        const key = `${s.name}->${dep}`;
        if (!edgeMap.has(key) && coords.has(dep)) {
          edgeMap.set(key, {
            caller: s.name,
            callee: dep,
            request_rate: 0,
            attempt_rate: 0,
            error_ratio: 0,
            latency: 0,
            concurrency: 0,
            pool_in_use: 0,
            timeout_ms: 2000,
            pool_size: 128,
            retries: 0,
            offered_attempt_rate: 0,
            pool_rejection_ratio: 0,
          });
        }
      }
    }

    return {
      nodes: builtNodes,
      edges: Array.from(edgeMap.values()),
      canvasWidth: width,
      canvasHeight: height,
    };
  }, [usingGod, godState, fogState, revealSecret]);

  const nodeByName = useMemo(() => new Map(nodes.map((n) => [n.name, n])), [nodes]);

  return (
    <div
      style={{ width: '100%', height: '100%', position: 'relative', userSelect: 'none' }}
      onMouseDown={(e) => {
        if ((e.target as HTMLElement).closest('button')) return;
        setDragging({ startX: e.clientX - pan.x, startY: e.clientY - pan.y });
      }}
      onMouseMove={(e) => {
        if (!dragging) return;
        setPan({ x: e.clientX - dragging.startX, y: e.clientY - dragging.startY });
      }}
      onMouseUp={() => setDragging(null)}
      onMouseLeave={() => setDragging(null)}
    >
      {/* Overlay Header Controls */}
      <div
        style={{
          position: 'absolute',
          top: 10,
          left: 12,
          right: 12,
          zIndex: 10,
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          pointerEvents: 'none',
        }}
      >
        <div style={{ display: 'flex', gap: 8, alignItems: 'center', pointerEvents: 'auto' }}>
          <span
            className={`badge ${usingGod ? 'badge-violet' : 'badge-info'}`}
            style={{ padding: '3px 8px', fontSize: 11 }}
          >
            {usingGod
              ? `SIMULATOR GOD VIEW @ m=${godState?.selected_minute ?? 5}`
              : `AGENT FOG-OF-WAR (${nodes.length} services discovered)`}
          </span>
          {godState?.stampede_until != null && godState.selected_minute < godState.stampede_until && (
            <span className="badge badge-err">
              CACHE STAMPEDE ACTIVE (until m={godState.stampede_until})
            </span>
          )}
          {usingGod && revealSecret && godState?.secret && (
            <span className="badge badge-err">
              ROOT CAUSE: {godState.secret.culprit} / {godState.secret.fault} ({godState.secret.change_id})
            </span>
          )}
        </div>

        <div style={{ display: 'flex', gap: 6, alignItems: 'center', pointerEvents: 'auto' }}>
          <button
            className={`btn ${showGodView ? 'btn-primary' : ''}`}
            onClick={onToggleGodView}
            title="Toggle between fair Agent Fog-of-War view and live Simulator God View"
          >
            {showGodView ? <Eye size={13} /> : <EyeOff size={13} />}
            {showGodView ? 'God View: ON' : 'Fog-of-War'}
          </button>
          {showGodView && (
            <button
              className={`btn ${revealSecret ? 'btn-danger' : ''}`}
              onClick={onToggleRevealSecret}
              title="Reveal hidden culprit service, fault kind, and triggering change ID"
            >
              <KeyRound size={13} />
              {revealSecret ? 'Hide Spoiler' : 'Reveal Cause'}
            </button>
          )}
          <button className="btn" onClick={() => setZoom((z) => Math.min(1.6, z + 0.15))} title="Zoom in">
            <ZoomIn size={13} />
          </button>
          <button className="btn" onClick={() => setZoom((z) => Math.max(0.55, z - 0.15))} title="Zoom out">
            <ZoomOut size={13} />
          </button>
          <button
            className="btn"
            onClick={() => {
              setZoom(1);
              setPan({ x: 0, y: 0 });
            }}
            title="Reset view"
          >
            <Maximize2 size={13} />
          </button>
        </div>
      </div>

      {/* Empty Fog-of-War state */}
      {nodes.length === 0 ? (
        <div
          style={{
            height: '100%',
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            gap: 10,
            color: 'var(--text-1)',
            padding: 24,
            textAlign: 'center',
          }}
        >
          <Radar size={34} color="var(--cyan)" />
          <div style={{ fontWeight: 600, fontSize: 14, color: 'var(--text-0)' }}>
            Cluster Unmapped (Agent Fog-of-War Active)
          </div>
          <div style={{ maxWidth: 460, color: 'var(--text-2)', lineHeight: 1.5 }}>
            Just like the evaluated agent, you start at <b>minute 5</b> knowing nothing about the
            cluster topology or active alerts until you spend tool calls—or toggle{' '}
            <b>God View</b> above to inspect the live fluid network.
          </div>
          {canQuickRun && onQuickTool && (
            <div style={{ display: 'flex', gap: 8, marginTop: 6, flexWrap: 'wrap', justifyContent: 'center' }}>
              <button
                className="btn btn-primary"
                onClick={() => onQuickTool('run', { command: 'kubectl get services' })}
              >
                Run `kubectl get services` (+1m)
              </button>
              <button className="btn" onClick={() => onQuickTool('list_alerts', {})}>
                Run `list_alerts` (+1m)
              </button>
              <button
                className="btn"
                onClick={() =>
                  onQuickTool('query_metrics', {
                    metric: 'error_ratio',
                    service: '*',
                    by: 'service',
                  })
                }
              >
                Query `*` Error Ratio (+1m)
              </button>
            </div>
          )}
        </div>
      ) : (
        <svg
          width="100%"
          height="100%"
          viewBox={`0 0 ${canvasWidth} ${canvasHeight}`}
          style={{ cursor: dragging ? 'grabbing' : 'grab' }}
        >
          <defs>
            <marker
              id="arrow-ok"
              viewBox="0 0 10 10"
              refX="8"
              refY="5"
              markerWidth="5"
              markerHeight="5"
              orient="auto-start-reverse"
            >
              <path d="M 0 1 L 10 5 L 0 9 z" fill="#334155" />
            </marker>
            <marker
              id="arrow-err"
              viewBox="0 0 10 10"
              refX="8"
              refY="5"
              markerWidth="5"
              markerHeight="5"
              orient="auto-start-reverse"
            >
              <path d="M 0 1 L 10 5 L 0 9 z" fill="#ef4444" />
            </marker>
            <marker
              id="arrow-warn"
              viewBox="0 0 10 10"
              refX="8"
              refY="5"
              markerWidth="5"
              markerHeight="5"
              orient="auto-start-reverse"
            >
              <path d="M 0 1 L 10 5 L 0 9 z" fill="#f59e0b" />
            </marker>
          </defs>

          <g
            transform={`translate(${pan.x}, ${pan.y}) scale(${zoom})`}
            style={{ transformOrigin: 'center center' }}
          >
            {/* Edges */}
            {edges.map((edge) => {
              const from = nodeByName.get(edge.caller);
              const to = nodeByName.get(edge.callee);
              if (!from || !to) return null;

              const isTimeout = edge.latency * 1000 > edge.timeout_ms && edge.timeout_ms > 0;
              const isPoolReject = edge.pool_rejection_ratio > 0.01;
              const isRetryStorm =
                edge.request_rate > 0 && edge.offered_attempt_rate > edge.request_rate * 1.3;
              const bad = edge.error_ratio > 0.08 || isTimeout || isPoolReject;
              const warn = !bad && (edge.error_ratio > 0.01 || isRetryStorm);

              const stroke = bad ? '#ef4444' : warn ? '#f59e0b' : '#2d3d56';
              const strokeW = bad ? 2.2 : warn ? 1.8 : Math.min(2.2, 1.1 + edge.attempt_rate / 180);
              const y1 = from.y + 25;
              const y2 = to.y - 27;
              const midX = (from.x + to.x) / 2;
              const midY = (y1 + y2) / 2;
              const d = `M ${from.x} ${y1} C ${from.x} ${midY}, ${to.x} ${midY}, ${to.x} ${y2}`;

              let badgeLabel = '';
              if (isPoolReject) badgeLabel = `POOL -${Math.round(edge.pool_rejection_ratio * 100)}%`;
              else if (isTimeout) badgeLabel = `TIMEOUT >${Math.round(edge.timeout_ms)}ms`;
              else if (isRetryStorm)
                badgeLabel = `RETRY ${(edge.offered_attempt_rate / edge.request_rate).toFixed(1)}x`;

              return (
                <g key={`${edge.caller}->${edge.callee}`}>
                  <path
                    d={d}
                    fill="none"
                    stroke={stroke}
                    strokeWidth={strokeW}
                    strokeOpacity={bad || warn ? 0.92 : 0.65}
                    className={edge.attempt_rate > 0 || usingGod ? 'edge-animated' : undefined}
                    markerEnd={
                      bad ? 'url(#arrow-err)' : warn ? 'url(#arrow-warn)' : 'url(#arrow-ok)'
                    }
                  />
                  {badgeLabel && (
                    <g transform={`translate(${midX}, ${midY})`}>
                      <rect
                        x={-44}
                        y={-9}
                        width={88}
                        height={17}
                        rx={4}
                        fill="#180b0e"
                        stroke={bad ? '#ef4444' : '#f59e0b'}
                        strokeWidth={1}
                      />
                      <text
                        x={0}
                        y={3}
                        textAnchor="middle"
                        fill={bad ? '#fca5a5' : '#fcd34d'}
                        fontSize={9}
                        fontFamily="var(--font-mono)"
                        fontWeight={600}
                      >
                        {badgeLabel}
                      </text>
                    </g>
                  )}
                </g>
              );
            })}

            {/* Service Nodes */}
            {nodes.map((node) => {
              const color = healthColor(node.errorRatio, node.p99Latency, node.status);
              const isSelected = selectedService === node.name;
              const isStepTarget = activeStepService === node.name;
              const nodeW = 142;
              const nodeH = 52;
              const versions = new Set(node.podStates.map((p) => p.version).filter(Boolean));
              const hasVersionSkew = versions.size > 1;

              return (
                <g
                  key={node.name}
                  transform={`translate(${node.x - nodeW / 2}, ${node.y - nodeH / 2})`}
                  style={{ cursor: 'pointer' }}
                  onClick={(e) => {
                    e.stopPropagation();
                    onSelectService(isSelected ? null : node.name);
                  }}
                >
                  {/* Active agent target halo */}
                  {isStepTarget && (
                    <rect
                      x={-5}
                      y={-5}
                      width={nodeW + 10}
                      height={nodeH + 10}
                      rx={10}
                      fill="none"
                      stroke="#38bdf8"
                      strokeWidth={1.8}
                      strokeDasharray="4 3"
                    />
                  )}
                  {/* Root cause spoiler halo */}
                  {node.isRootCause && (
                    <rect
                      x={-4}
                      y={-4}
                      width={nodeW + 8}
                      height={nodeH + 8}
                      rx={10}
                      fill="none"
                      stroke="#ef4444"
                      strokeWidth={2.2}
                    />
                  )}

                  <rect
                    width={nodeW}
                    height={nodeH}
                    rx={7}
                    fill={isSelected ? '#1a2536' : '#111823'}
                    stroke={isSelected ? '#38bdf8' : color}
                    strokeWidth={isSelected ? 2 : 1.4}
                  />

                  {/* Left status accent bar */}
                  <rect x={0} y={0} width={4} height={nodeH} rx={2} fill={color} />

                  {/* Service Name & Kind */}
                  <text
                    x={10}
                    y={16}
                    fill="#eef2f8"
                    fontSize={10.5}
                    fontWeight={600}
                    fontFamily="var(--font-mono)"
                  >
                    {node.name.length > 17 ? node.name.slice(0, 16) + '…' : node.name}
                  </text>

                  {/* Mini pod dots top-right */}
                  <g transform={`translate(${nodeW - 30}, 11)`}>
                    {(node.podStates.length > 0
                      ? node.podStates
                      : [{ state: 'unknown', error: 0, version: '' }]
                    )
                      .slice(0, 3)
                      .map((p, idx) => {
                        const pColor =
                          p.state === 'OOMKilled' || p.state === 'CrashLoopBackOff' || p.error > 0.1
                            ? '#ef4444'
                            : p.error > 0.01
                              ? '#f59e0b'
                              : p.state === 'unknown'
                                ? '#475569'
                                : '#10b981';
                        return <circle key={idx} cx={idx * 8} cy={0} r={3} fill={pColor} />;
                      })}
                  </g>

                  {/* Telemetry bottom line */}
                  <text
                    x={10}
                    y={33}
                    fill={
                      node.errorRatio !== null && node.errorRatio > 0.02
                        ? '#f87171'
                        : '#94a3b8'
                    }
                    fontSize={9.5}
                    fontFamily="var(--font-mono)"
                  >
                    err:{node.errorRatio !== null ? `${(node.errorRatio * 100).toFixed(0)}%` : '—'}{' '}
                    p99:
                    {node.p99Latency !== null
                      ? node.p99Latency >= 1
                        ? `${node.p99Latency.toFixed(1)}s`
                        : `${Math.round(node.p99Latency * 1000)}ms`
                      : '—'}
                  </text>

                  {/* Sub-badges (kind, version skew, queue, or staleness) */}
                  <text x={10} y={45} fill="#64748b" fontSize={8.5} fontFamily="var(--font-mono)">
                    {node.kind.toUpperCase()}
                    {node.version ? ` · ${node.version}` : ''}
                    {hasVersionSkew ? ' [SKEW]' : ''}
                    {node.queueDepth && node.queueDepth > 5
                      ? ` · Q:${Math.round(node.queueDepth)}`
                      : !usingGod && node.observedAt !== null
                        ? ` · @m${node.observedAt}`
                        : ''}
                  </text>
                </g>
              );
            })}
          </g>
        </svg>
      )}
    </div>
  );
};
