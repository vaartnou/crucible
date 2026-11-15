import React, { useEffect, useState } from 'react';
import { CheckCircle2, Clock, Code2, Play, Terminal, Wrench } from 'lucide-react';
import type {
  ChangeRecord,
  CommandTemplate,
  FogState,
  GodState,
  SerializedStep,
  ToolInfo,
} from '../types';

interface McpConsoleProps {
  multipleReports?: boolean;
  tools: ToolInfo[];
  commandTemplates: CommandTemplate[];
  faultKinds: string[];
  remediations: string[];
  knownServices: string[];
  knownChanges: ChangeRecord[];
  selectedService: string | null;
  stagedRevert: ChangeRecord | null;
  onClearStagedRevert: () => void;
  disabled: boolean;
  busy: boolean;
  onExecuteTool: (name: string, args: Record<string, unknown>) => Promise<void>;
  lastStep: SerializedStep | null;
  fogState: FogState;
  godState: GodState | null;
}

const METRIC_NAMES = [
  'error_ratio',
  'p99_latency',
  'request_rate',
  'memory',
  'pool_in_use',
  'queue_depth',
];

export const McpConsole: React.FC<McpConsoleProps> = ({
  multipleReports = false,
  tools,
  commandTemplates,
  faultKinds,
  remediations,
  knownServices,
  knownChanges,
  selectedService,
  stagedRevert,
  onClearStagedRevert,
  disabled,
  busy,
  onExecuteTool,
  lastStep,
}) => {
  const [activeTool, setActiveTool] = useState<string>('run');
  const [rawJsonMode, setRawJsonMode] = useState(false);
  const [rawArgsText, setRawArgsText] = useState('{\n  "command": "kubectl get services"\n}');

  // Guided state per tool
  const [runCommand, setRunCommand] = useState('kubectl get services');
  const [cmdService, setCmdService] = useState('');
  const [cmdRevision, setCmdRevision] = useState('');

  const [metricName, setMetricName] = useState('error_ratio');
  const [metricSvc, setMetricSvc] = useState('*');
  const [metricBy, setMetricBy] = useState<'service' | 'pod'>('service');
  const [metricWindow, setMetricWindow] = useState(5);
  const [metricSeries, setMetricSeries] = useState(false);

  const [edgeSvc, setEdgeSvc] = useState('*');
  const [edgeWindow, setEdgeWindow] = useState(1);
  const [edgeSeries, setEdgeSeries] = useState(false);

  const [logSvc, setLogSvc] = useState('');
  const [logQuery, setLogQuery] = useState('');
  const [logLevel, setLogLevel] = useState<'' | 'INFO' | 'ERROR'>('ERROR');
  const [logSince, setLogSince] = useState(10);
  const [logPage, setLogPage] = useState(0);

  const [waitMinutes, setWaitMinutes] = useState(30);

  const [repService, setRepService] = useState('');
  const [repFault, setRepFault] = useState('');
  const [repChangeId, setRepChangeId] = useState('');
  const [repRemediation, setRepRemediation] = useState('');
  const [savedReports, setSavedReports] = useState<Record<string, string>[]>([]);
  const [showRawResult, setShowRawResult] = useState(false);

  // Sync clicked service into active fields
  useEffect(() => {
    if (selectedService) {
      setCmdService(selectedService);
      if (!logSvc) setLogSvc(selectedService);
      if (!repService) setRepService(selectedService);
    }
  }, [selectedService, logSvc, repService]);

  // Handle "Stage Revert" from changes table
  useEffect(() => {
    if (!stagedRevert) return;
    setActiveTool('run');
    setCmdService(stagedRevert.service);
    setCmdRevision(stagedRevert.id);
    if (stagedRevert.kind === 'config') {
      setRunCommand(`config revert ${stagedRevert.service} ${stagedRevert.id}`);
      setRepRemediation('config_revert');
    } else {
      setRunCommand(
        `kubectl rollout undo deploy/${stagedRevert.service} --to-revision=${stagedRevert.id}`,
      );
      setRepRemediation('rollback');
    }
    setRepService(stagedRevert.service);
    setRepChangeId(stagedRevert.id);
    onClearStagedRevert();
  }, [stagedRevert, onClearStagedRevert]);

  const buildGuidedArgs = (): Record<string, unknown> => {
    switch (activeTool) {
      case 'run':
        return { command: runCommand };
      case 'query_metrics':
        return {
          metric: metricName,
          service: metricSvc || '*',
          by: metricBy,
          window: metricWindow,
          series: metricSeries,
        };
      case 'query_edges':
        return {
          service: edgeSvc || '*',
          window: edgeWindow,
          series: edgeSeries,
        };
      case 'search_logs':
        return {
          service: logSvc || selectedService || knownServices[0] || 'edge-gateway',
          query: logQuery,
          level: logLevel,
          since: logSince,
          page: logPage,
        };
      case 'list_alerts':
        return {};
      case 'wait':
        return { minutes: waitMinutes };
      case 'resolve':
        if (multipleReports) return { reports: savedReports };
        return {
          report: {
            service: repService,
            fault_kind: repFault,
            change_id: repChangeId,
            remediation: repRemediation,
          },
        };
      default:
        return {};
    }
  };

  useEffect(() => {
    if (!rawJsonMode) {
      setRawArgsText(JSON.stringify(buildGuidedArgs(), null, 2));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    activeTool,
    runCommand,
    metricName,
    metricSvc,
    metricBy,
    metricWindow,
    metricSeries,
    edgeSvc,
    edgeWindow,
    edgeSeries,
    logSvc,
    logQuery,
    logLevel,
    logSince,
    logPage,
    waitMinutes,
    repService,
    repFault,
    repChangeId,
    repRemediation,
    rawJsonMode,
  ]);

  const handleSubmit = async () => {
    let args: Record<string, unknown>;
    if (rawJsonMode) {
      try {
        args = JSON.parse(rawArgsText) as Record<string, unknown>;
      } catch (err) {
        alert(`Invalid JSON arguments: ${String(err)}`);
        return;
      }
    } else {
      args = buildGuidedArgs();
    }
    await onExecuteTool(activeTool, args);
  };

  const applyTemplate = (tpl: CommandTemplate) => {
    const svc = cmdService || selectedService || knownServices[0] || 'edge-gateway';
    const matchingChanges = knownChanges.filter((c) => c.service === svc && !c.reverted);
    const rev = cmdRevision || matchingChanges[matchingChanges.length - 1]?.id || 'chg-000000';
    const built = tpl.template.replace('{service}', svc).replace('{revision}', rev);
    setRunCommand(built);
  };

  const toolSpec = tools.find((t) => t.name === activeTool);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
      {/* Tool Selector Pills */}
      <div className="card" style={{ padding: 9 }}>
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            marginBottom: 8,
          }}
        >
          <span style={{ fontWeight: 600, display: 'flex', alignItems: 'center', gap: 6 }}>
            <Terminal size={14} color="var(--cyan)" /> MCP Tool Console
          </span>
          <button
            className={`btn ${rawJsonMode ? 'btn-primary' : ''}`}
            style={{ padding: '2px 7px', fontSize: 11 }}
            onClick={() => setRawJsonMode((v) => !v)}
          >
            <Code2 size={12} /> {rawJsonMode ? 'Guided UI' : 'Raw MCP JSON'}
          </button>
        </div>

        <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap', marginBottom: 8 }}>
          {tools.map((t) => {
            const active = t.name === activeTool;
            const costBadge =
              t.name === 'run'
                ? '+1/+3m'
                : t.name === 'wait'
                  ? '+Nm'
                  : t.name === 'resolve'
                    ? 'END'
                    : '+1m';
            return (
              <button
                key={t.name}
                className={`btn ${active ? 'btn-primary' : ''}`}
                style={{ padding: '4px 8px', fontSize: 11.5 }}
                onClick={() => setActiveTool(t.name)}
              >
                <span className="mono">{t.name}</span>
                <span style={{ fontSize: 9.5, opacity: 0.7 }} className="mono">
                  {costBadge}
                </span>
              </button>
            );
          })}
        </div>

        {toolSpec && (
          <div style={{ fontSize: 11.5, color: 'var(--text-2)', marginBottom: 8 }}>
            {toolSpec.description}
          </div>
        )}

        {rawJsonMode ? (
          <textarea
            className="input mono"
            rows={6}
            style={{ width: '100%', resize: 'vertical' }}
            value={rawArgsText}
            onChange={(e) => setRawArgsText(e.target.value)}
          />
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {activeTool === 'run' && (
              <>
                <div style={{ display: 'flex', gap: 6 }}>
                  <select
                    className="select mono"
                    style={{ flex: 1 }}
                    value={cmdService}
                    onChange={(e) => setCmdService(e.target.value)}
                  >
                    <option value="">Target service (for templates)…</option>
                    {knownServices.map((s) => (
                      <option key={s} value={s}>
                        {s}
                      </option>
                    ))}
                  </select>
                  <select
                    className="select mono"
                    style={{ width: 145 }}
                    value={cmdRevision}
                    onChange={(e) => setCmdRevision(e.target.value)}
                  >
                    <option value="">Change ID…</option>
                    {knownChanges
                      .filter((c) => !cmdService || c.service === cmdService)
                      .map((c) => (
                        <option key={c.id} value={c.id}>
                          {c.id} (m={c.minute} {c.kind})
                        </option>
                      ))}
                  </select>
                </div>

                <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
                  {commandTemplates.map((tpl) => (
                    <button
                      key={tpl.id}
                      className={`btn ${tpl.kind === 'action' ? 'btn-action' : ''}`}
                      style={{ padding: '3px 7px', fontSize: 10.5 }}
                      onClick={() => applyTemplate(tpl)}
                    >
                      {tpl.label}{' '}
                      <span className="mono" style={{ opacity: 0.75 }}>
                        +{tpl.cost}m
                      </span>
                    </button>
                  ))}
                </div>

                <input
                  className="input mono"
                  value={runCommand}
                  onChange={(e) => setRunCommand(e.target.value)}
                  placeholder="kubectl get services"
                />
              </>
            )}

            {activeTool === 'query_metrics' && (
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6 }}>
                <label style={{ display: 'flex', flexDirection: 'column', gap: 3, fontSize: 11 }}>
                  Metric
                  <select
                    className="select mono"
                    value={metricName}
                    onChange={(e) => setMetricName(e.target.value)}
                  >
                    {METRIC_NAMES.map((m) => (
                      <option key={m} value={m}>
                        {m}
                      </option>
                    ))}
                  </select>
                </label>
                <label style={{ display: 'flex', flexDirection: 'column', gap: 3, fontSize: 11 }}>
                  Service (`*` or name)
                  <select
                    className="select mono"
                    value={metricSvc}
                    onChange={(e) => setMetricSvc(e.target.value)}
                  >
                    <option value="*">* (All Services)</option>
                    {knownServices.map((s) => (
                      <option key={s} value={s}>
                        {s}
                      </option>
                    ))}
                  </select>
                </label>
                <label style={{ display: 'flex', flexDirection: 'column', gap: 3, fontSize: 11 }}>
                  Group By
                  <select
                    className="select mono"
                    value={metricBy}
                    onChange={(e) => setMetricBy(e.target.value as 'service' | 'pod')}
                  >
                    <option value="service">service</option>
                    <option value="pod">pod</option>
                  </select>
                </label>
                <label style={{ display: 'flex', flexDirection: 'column', gap: 3, fontSize: 11 }}>
                  Window (minutes)
                  <input
                    type="number"
                    min={1}
                    max={150}
                    className="input mono"
                    value={metricWindow}
                    onChange={(e) => setMetricWindow(Number(e.target.value))}
                  />
                </label>
                <label
                  style={{
                    gridColumn: '1 / -1',
                    display: 'flex',
                    alignItems: 'center',
                    gap: 6,
                    fontSize: 11.5,
                  }}
                >
                  <input
                    type="checkbox"
                    checked={metricSeries}
                    onChange={(e) => setMetricSeries(e.target.checked)}
                  />
                  Return per-minute time series (<code>series=true</code>)
                </label>
              </div>
            )}

            {activeTool === 'query_edges' && (
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6 }}>
                <label style={{ display: 'flex', flexDirection: 'column', gap: 3, fontSize: 11 }}>
                  Caller Service
                  <select
                    className="select mono"
                    value={edgeSvc}
                    onChange={(e) => setEdgeSvc(e.target.value)}
                  >
                    <option value="*">* (All Callers)</option>
                    {knownServices.map((s) => (
                      <option key={s} value={s}>
                        {s}
                      </option>
                    ))}
                  </select>
                </label>
                <label style={{ display: 'flex', flexDirection: 'column', gap: 3, fontSize: 11 }}>
                  Window (minutes)
                  <input
                    type="number"
                    min={1}
                    max={150}
                    className="input mono"
                    value={edgeWindow}
                    onChange={(e) => setEdgeWindow(Number(e.target.value))}
                  />
                </label>
                <label
                  style={{
                    gridColumn: '1 / -1',
                    display: 'flex',
                    alignItems: 'center',
                    gap: 6,
                    fontSize: 11.5,
                  }}
                >
                  <input
                    type="checkbox"
                    checked={edgeSeries}
                    onChange={(e) => setEdgeSeries(e.target.checked)}
                  />
                  Return per-minute edge samples (<code>series=true</code>)
                </label>
              </div>
            )}

            {activeTool === 'search_logs' && (
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6 }}>
                <label style={{ display: 'flex', flexDirection: 'column', gap: 3, fontSize: 11 }}>
                  Service
                  <input
                    className="input mono"
                    list="known-services-list"
                    value={logSvc}
                    onChange={(e) => setLogSvc(e.target.value)}
                    placeholder={selectedService ?? 'edge-gateway'}
                  />
                  <datalist id="known-services-list">
                    {knownServices.map((s) => (
                      <option key={s} value={s} />
                    ))}
                  </datalist>
                </label>
                <label style={{ display: 'flex', flexDirection: 'column', gap: 3, fontSize: 11 }}>
                  Severity Level
                  <select
                    className="select mono"
                    value={logLevel}
                    onChange={(e) => setLogLevel(e.target.value as '' | 'INFO' | 'ERROR')}
                  >
                    <option value="">All Levels</option>
                    <option value="ERROR">ERROR</option>
                    <option value="INFO">INFO</option>
                  </select>
                </label>
                <label style={{ display: 'flex', flexDirection: 'column', gap: 3, fontSize: 11 }}>
                  Text Filter
                  <input
                    className="input mono"
                    value={logQuery}
                    onChange={(e) => setLogQuery(e.target.value)}
                    placeholder="timeout / pool / OOM…"
                  />
                </label>
                <div style={{ display: 'flex', gap: 6 }}>
                  <label style={{ display: 'flex', flexDirection: 'column', gap: 3, fontSize: 11, flex: 1 }}>
                    Since (min)
                    <input
                      type="number"
                      min={1}
                      max={150}
                      className="input mono"
                      value={logSince}
                      onChange={(e) => setLogSince(Number(e.target.value))}
                    />
                  </label>
                  <label style={{ display: 'flex', flexDirection: 'column', gap: 3, fontSize: 11, width: 64 }}>
                    Page
                    <input
                      type="number"
                      min={0}
                      max={100}
                      className="input mono"
                      value={logPage}
                      onChange={(e) => setLogPage(Number(e.target.value))}
                    />
                  </label>
                </div>
              </div>
            )}

            {activeTool === 'list_alerts' && (
              <div style={{ fontSize: 12, color: 'var(--text-1)' }}>
                Fetches firing alerts over the trailing 5 simulated minutes (sorted by severity).
              </div>
            )}

            {activeTool === 'wait' && (
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                {[5, 10, 15, 30].map((m) => (
                  <button
                    key={m}
                    className={`btn ${waitMinutes === m ? 'btn-primary' : ''}`}
                    onClick={() => setWaitMinutes(m)}
                  >
                    <Clock size={12} /> +{m}m
                  </button>
                ))}
                <input
                  type="number"
                  min={1}
                  max={30}
                  className="input mono"
                  style={{ width: 70 }}
                  value={waitMinutes}
                  onChange={(e) => setWaitMinutes(Number(e.target.value))}
                />
              </div>
            )}

            {activeTool === 'resolve' && (
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6 }}>
                {multipleReports && (
                  <div style={{ gridColumn: '1 / -1', fontSize: 12 }}>
                    <p>Add each diagnosed cause, then close after verifying recovery.</p>
                    {savedReports.map((report, index) => (
                      <div key={index}>
                        {report.service} · {report.fault_kind} · {report.change_id}{' '}
                        <button className="btn" type="button" onClick={() => setSavedReports(
                          (reports) => reports.filter((_, i) => i !== index),
                        )}>Remove</button>
                      </div>
                    ))}
                    <button className="btn" type="button"
                      disabled={!repService || !repFault || !repChangeId || !repRemediation || savedReports.length >= 8}
                      onClick={() => {
                        setSavedReports((reports) => [...reports, {
                          service: repService, fault_kind: repFault,
                          change_id: repChangeId, remediation: repRemediation,
                        }]);
                        setRepService(''); setRepFault(''); setRepChangeId(''); setRepRemediation('');
                      }}>Add diagnosis</button>
                  </div>
                )}
                <label style={{ display: 'flex', flexDirection: 'column', gap: 3, fontSize: 11 }}>
                  Culprit Service
                  <input
                    className="input mono"
                    list="known-services-list"
                    value={repService}
                    onChange={(e) => setRepService(e.target.value)}
                    placeholder="payments-api-42"
                  />
                </label>
                <label style={{ display: 'flex', flexDirection: 'column', gap: 3, fontSize: 11 }}>
                  Fault Kind
                  <select
                    className="select mono"
                    value={repFault}
                    onChange={(e) => setRepFault(e.target.value)}
                  >
                    <option value="">Select fault_kind…</option>
                    {faultKinds.map((fk) => (
                      <option key={fk} value={fk}>
                        {fk}
                      </option>
                    ))}
                  </select>
                </label>
                <label style={{ display: 'flex', flexDirection: 'column', gap: 3, fontSize: 11 }}>
                  Triggering Change ID
                  <input
                    className="input mono"
                    list="known-changes-list"
                    value={repChangeId}
                    onChange={(e) => setRepChangeId(e.target.value)}
                    placeholder="chg-123456"
                  />
                  <datalist id="known-changes-list">
                    {knownChanges.map((c) => (
                      <option
                        key={c.id}
                        value={c.id}
                        label={`${c.service} m=${c.minute} (${c.summary ?? 'diff not discovered'})`}
                      />
                    ))}
                  </datalist>
                </label>
                <label style={{ display: 'flex', flexDirection: 'column', gap: 3, fontSize: 11 }}>
                  Remediation
                  <select
                    className="select mono"
                    value={repRemediation}
                    onChange={(e) => setRepRemediation(e.target.value)}
                  >
                    <option value="">Select remediation…</option>
                    {remediations.map((rm) => (
                      <option key={rm} value={rm}>
                        {rm}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
            )}
          </div>
        )}

        <button
          className={`btn ${
            activeTool === 'resolve'
              ? 'btn-emerald'
              : activeTool === 'run' &&
                  (runCommand.includes('undo') ||
                    runCommand.includes('revert') ||
                    runCommand.includes('restart'))
                ? 'btn-action'
                : 'btn-primary'
          }`}
          style={{ width: '100%', marginTop: 10, padding: '7px 12px', fontWeight: 600 }}
          disabled={disabled || busy}
          onClick={() => void handleSubmit()}
        >
          {activeTool === 'resolve' ? (
            <>
              <CheckCircle2 size={14} /> Submit Diagnosis &amp; Run 30-Min Recovery Probe
            </>
          ) : activeTool === 'run' &&
            (runCommand.includes('undo') ||
              runCommand.includes('revert') ||
              runCommand.includes('restart')) ? (
            <>
              <Wrench size={14} /> Execute Cluster Mutation (+3m)
            </>
          ) : (
            <>
              <Play size={13} /> Invoke MCP `{activeTool}`
            </>
          )}
        </button>
      </div>

      {/* Latest Tool Output Viewer */}
      {lastStep && (
        <div className="card" style={{ padding: 9 }}>
          <div
            style={{
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
              marginBottom: 6,
            }}
          >
            <span className="mono" style={{ fontSize: 11.5, fontWeight: 600 }}>
              Latest Observation (#{lastStep.step} `{lastStep.call.name}` @ m={lastStep.observed_at} →{' '}
              m={lastStep.minute_after})
            </span>
            <button
              className="btn"
              style={{ padding: '1px 6px', fontSize: 10.5 }}
              onClick={() => setShowRawResult((v) => !v)}
            >
              {showRawResult ? 'Formatted' : 'Raw JSON'}
            </button>
          </div>
          {!lastStep.result.ok ? (
            <div className="badge badge-err" style={{ display: 'block', padding: 8 }}>
              {lastStep.result.error}
            </div>
          ) : showRawResult ? (
            <pre className="code-block" style={{ maxHeight: 220 }}>
              {JSON.stringify(lastStep.result, null, 2)}
            </pre>
          ) : (
            <FormattedToolOutput step={lastStep} />
          )}
        </div>
      )}
    </div>
  );
};

const FormattedToolOutput: React.FC<{ step: SerializedStep }> = ({ step }) => {
  const content = step.result.content;
  const data =
    content && typeof content === 'object' && 'data' in content
      ? (content as { data: unknown }).data
      : content;

  if (typeof data === 'string') {
    return (
      <pre className="code-block" style={{ maxHeight: 210 }}>
        {data}
      </pre>
    );
  }

  if (Array.isArray(data)) {
    if (data.length === 0) {
      return <div style={{ color: 'var(--text-2)', fontSize: 11.5 }}>Empty list `[]`.</div>;
    }
    const first = data[0] as Record<string, unknown>;
    const cols = Object.keys(first).slice(0, 6);
    return (
      <div style={{ maxHeight: 215, overflow: 'auto' }}>
        <table className="data-table mono" style={{ fontSize: 11 }}>
          <thead>
            <tr>
              {cols.map((c) => (
                <th key={c}>{c}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {data.slice(0, 30).map((row, idx) => {
              const r = row as Record<string, unknown>;
              return (
                <tr key={idx}>
                  {cols.map((c) => {
                    const v = r[c];
                    const txt =
                      typeof v === 'number'
                        ? Number.isInteger(v)
                          ? String(v)
                          : v.toFixed(4)
                        : typeof v === 'object' && v !== null
                          ? JSON.stringify(v)
                          : String(v ?? '');
                    return <td key={c}>{txt}</td>;
                  })}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    );
  }

  return (
    <pre className="code-block" style={{ maxHeight: 200 }}>
      {JSON.stringify(data, null, 2)}
    </pre>
  );
};
