import React, { useState } from 'react';
import { ChevronDown, ChevronRight, MessageSquare } from 'lucide-react';
import type { SerializedStep } from '../types';

interface ActionTimelineProps {
  steps: SerializedStep[];
  selectedStep: number | null;
  onSelectStep?: (stepIndex: number) => void;
  onFocusService?: (service: string) => void;
  agentLogs?: string | null;
}

function summarizeCall(step: SerializedStep): {
  label: string;
  kindBadge: { text: string; cls: string };
  targetService: string | null;
} {
  const { name, args } = step.call;
  if (name === 'run') {
    const cmd = String(args.command ?? '');
    const isMut =
      cmd.includes('undo') || cmd.includes('revert') || cmd.includes('restart');
    const parts = cmd.split(/\s+/);
    let svc: string | null = null;
    for (const p of parts) {
      if (p.startsWith('deploy/')) svc = p.replace('deploy/', '');
      else if (p.includes('-api-') || p.includes('-worker-') || p.includes('-db-') || p === 'session-cache' || p === 'edge-gateway') {
        svc = p;
      }
    }
    return {
      label: cmd,
      kindBadge: isMut
        ? { text: `ACTION +${step.cost_minutes}m`, cls: 'badge-warn' }
        : { text: `READ +${step.cost_minutes}m`, cls: 'badge-info' },
      targetService: svc,
    };
  }
  if (name === 'query_metrics') {
    const svc = String(args.service ?? '*');
    return {
      label: `query_metrics(${String(args.metric)}, svc=${svc}, by=${String(args.by ?? 'service')}${args.series ? ', series' : ''})`,
      kindBadge: { text: `METRIC +${step.cost_minutes}m`, cls: 'badge-info' },
      targetService: svc !== '*' ? svc : null,
    };
  }
  if (name === 'query_edges') {
    const svc = String(args.service ?? '*');
    return {
      label: `query_edges(svc=${svc}${args.end_minute != null ? `, end=${String(args.end_minute)}` : ''})`,
      kindBadge: { text: `EDGES +${step.cost_minutes}m`, cls: 'badge-info' },
      targetService: svc !== '*' ? svc : null,
    };
  }
  if (name === 'search_logs') {
    const svc = String(args.service ?? '');
    return {
      label: `search_logs(${svc}${args.level ? `, ${String(args.level)}` : ''}${args.query ? `, "${String(args.query)}"` : ''})`,
      kindBadge: { text: `LOGS +${step.cost_minutes}m`, cls: 'badge-info' },
      targetService: svc || null,
    };
  }
  if (name === 'list_alerts') {
    return {
      label: 'list_alerts()',
      kindBadge: { text: `ALERTS +${step.cost_minutes}m`, cls: 'badge-info' },
      targetService: null,
    };
  }
  if (name === 'wait') {
    return {
      label: `wait(${String(args.minutes)}m)`,
      kindBadge: { text: `CLOCK +${step.cost_minutes}m`, cls: 'badge-violet' },
      targetService: null,
    };
  }
  if (name === 'resolve') {
    const rep = (args.report ?? {}) as Record<string, unknown>;
    const svc = String(rep.service ?? '');
    return {
      label: `resolve(${svc || '?'}, ${String(rep.fault_kind ?? '?')}, ${String(rep.change_id ?? '?')})`,
      kindBadge: { text: 'RESOLVE', cls: 'badge-ok' },
      targetService: svc || null,
    };
  }
  return {
    label: `${name}(${JSON.stringify(args)})`,
    kindBadge: { text: name.toUpperCase(), cls: 'badge-info' },
    targetService: null,
  };
}

export function extractTargetService(step: SerializedStep | undefined): string | null {
  if (!step) return null;
  return summarizeCall(step).targetService;
}

export const ActionTimeline: React.FC<ActionTimelineProps> = ({
  steps,
  selectedStep,
  onSelectStep,
  onFocusService,
  agentLogs,
}) => {
  const [expanded, setExpanded] = useState<Record<number, boolean>>({});
  const [showContainerLogs, setShowContainerLogs] = useState(false);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      {agentLogs && (
        <div className="card" style={{ padding: 8 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <span style={{ fontWeight: 600, fontSize: 11.5 }}>
              Harbor Agent Container `/logs/agent` Tail
            </span>
            <button
              className="btn"
              style={{ padding: '2px 7px', fontSize: 10.5 }}
              onClick={() => setShowContainerLogs((v) => !v)}
            >
              {showContainerLogs ? 'Hide' : 'Show Stdout/Logs'}
            </button>
          </div>
          {showContainerLogs && (
            <pre className="code-block" style={{ marginTop: 6, maxHeight: 170 }}>
              {agentLogs}
            </pre>
          )}
        </div>
      )}

      {steps.length === 0 ? (
        <div style={{ padding: 18, textAlign: 'center', color: 'var(--text-2)' }}>
          No MCP tool calls recorded yet.
        </div>
      ) : (
        steps.map((st) => {
          const info = summarizeCall(st);
          const isExp = Boolean(expanded[st.step]);
          const isScrubActive = selectedStep === st.step + 1;

          return (
            <div
              key={st.step}
              className="card"
              style={{
                padding: '8px 10px',
                borderColor: !st.result.ok
                  ? 'rgba(239,68,68,0.5)'
                  : isScrubActive
                    ? 'var(--cyan)'
                    : undefined,
              }}
            >
              {st.thought && (
                <div
                  style={{
                    display: 'flex',
                    gap: 6,
                    padding: '5px 8px',
                    marginBottom: 6,
                    borderRadius: 5,
                    background: 'rgba(167, 139, 250, 0.1)',
                    border: '1px solid rgba(167, 139, 250, 0.28)',
                    fontSize: 11.5,
                    color: '#ddd6fe',
                  }}
                >
                  <MessageSquare size={13} style={{ flexShrink: 0, marginTop: 2 }} />
                  <div>{st.thought}</div>
                </div>
              )}

              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 6,
                  cursor: 'pointer',
                }}
                onClick={() => {
                  setExpanded((prev) => ({ ...prev, [st.step]: !prev[st.step] }));
                  if (info.targetService && onFocusService) onFocusService(info.targetService);
                }}
              >
                {isExp ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
                <span className="mono" style={{ fontSize: 11, color: 'var(--text-2)' }}>
                  #{st.step + 1}
                </span>
                <span className={`badge ${st.result.ok ? info.kindBadge.cls : 'badge-err'}`}>
                  {st.result.ok ? info.kindBadge.text : 'ERROR'}
                </span>
                <span
                  className="mono"
                  style={{
                    fontSize: 11,
                    flex: 1,
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                  }}
                >
                  {info.label}
                </span>
                <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-2)' }}>
                  m{st.observed_at}→m{st.minute_after}
                </span>
                {onSelectStep && (
                  <button
                    className="btn"
                    style={{ padding: '1px 5px', fontSize: 10 }}
                    onClick={(e) => {
                      e.stopPropagation();
                      onSelectStep(st.step + 1);
                    }}
                    title="Scrub replay to right after this call"
                  >
                    @Step
                  </button>
                )}
              </div>

              {isExp && (
                <div style={{ marginTop: 8, display: 'flex', flexDirection: 'column', gap: 6 }}>
                  <pre className="code-block" style={{ maxHeight: 180 }}>
                    {JSON.stringify({ call: st.call, result: st.result }, null, 2)}
                  </pre>
                </div>
              )}
            </div>
          );
        })
      )}
    </div>
  );
};
