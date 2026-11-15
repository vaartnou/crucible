import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Activity,
  BookOpen,
  Bot,
  Clock,
  Film,
  Flame,
  Gamepad2,
  GitCompare,
  Pause,
  Play,
  RefreshCw,
  RotateCcw,
} from 'lucide-react';
import { api } from './api';
import { ActionTimeline, extractTargetService } from './components/ActionTimeline';
import { ClusterGraph } from './components/ClusterGraph';
import { CompareDrawer } from './components/CompareDrawer';
import { InspectorTabs } from './components/InspectorTabs';
import { HarborWatchPanel } from './components/HarborWatchPanel';
import { McpConsole } from './components/McpConsole';
import type {
  BaselineComparePayload,
  ChangeRecord,
  FogState,
  HarborContainerInfo,
  ReplayPayload,
  SavedRunItem,
  WorkbenchMeta,
  WorkbenchSession,
} from './types';

const EMPTY_FOG: FogState = {
  current_minute: 5,
  topology_known: false,
  services: [],
  pods: [],
  edges: [],
  alerts: [],
  changes: [],
  events: [],
  logs: [],
  series: {},
  actions_taken: [],
};

export const App: React.FC = () => {
  const [meta, setMeta] = useState<WorkbenchMeta | null>(null);
  const [modeTab, setModeTab] = useState<'play' | 'watch' | 'replay'>(() => new URLSearchParams(window.location.search).has('trial') ? 'replay' : 'watch');
  const [errorBanner, setErrorBanner] = useState<string | null>(null);
  const [showInstructions, setShowInstructions] = useState(false);

  // Shared visual toggles
  const [playGodView, setPlayGodView] = useState(false);
  const [watchGodView, setWatchGodView] = useState(true);
  const [revealSecret, setRevealSecret] = useState(false);
  const [selectedService, setSelectedService] = useState<string | null>(null);
  const [inspectMinute, setInspectMinute] = useState<number | null>(null);

  // Human Play state
  const [playSeed, setPlaySeed] = useState(0);
  const [playDifficulty, setPlayDifficulty] = useState(0);
  const [playSession, setPlaySession] = useState<WorkbenchSession | null>(null);
  const [playRightTab, setPlayRightTab] = useState<'mcp' | 'log'>('mcp');
  const [playBusy, setPlayBusy] = useState(false);
  const [stagedRevert, setStagedRevert] = useState<ChangeRecord | null>(null);

  // Live Harbor container state
  const [harborStatus, setHarborStatus] = useState<{
    docker_available: boolean;
    error: string | null;
    containers: HarborContainerInfo[];
  }>({ docker_available: true, error: null, containers: [] });
  const [selectedContainerId, setSelectedContainerId] = useState<string | null>(() => new URLSearchParams(window.location.search).get('container'));
  const [harborPayload, setHarborPayload] = useState<ReplayPayload | null>(null);

  // Replay & Saved Runs state
  const [savedRuns, setSavedRuns] = useState<SavedRunItem[]>([]);
  const [runFilter, setRunFilter] = useState('');
  const [selectedRunPath, setSelectedRunPath] = useState<string | null>(() => new URLSearchParams(window.location.search).get('trial'));
  const [replayData, setReplayData] = useState<ReplayPayload | null>(null);
  const [scrubStep, setScrubStep] = useState<number | null>(null);
  const [replayPlaying, setReplayPlaying] = useState(false);

  // Side-by-side Compare Drawer
  const [compareOpen, setCompareOpen] = useState(false);
  const [comparePolicy, setComparePolicy] = useState('graph_walk');
  const [compareBaseline, setCompareBaseline] = useState<BaselineComparePayload | null>(null);

  // Restore a bookmarked run without creating another episode.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const m = await api.getMeta();
        if (cancelled) return;
        setMeta(m);
        const params = new URLSearchParams(window.location.search);
        if (params.get('trial')) {
          setSelectedRunPath(params.get('trial'));
          setModeTab('replay');
        } else if (params.get('container')) {
          setSelectedContainerId(params.get('container'));
        }
      } catch (err) {
        if (!cancelled) setErrorBanner(String(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (modeTab === 'play' && !playSession && !playBusy) {
      void startNewPlayEpisode(playSeed, playDifficulty);
    }
  }, [modeTab]);

  useEffect(() => {
    const url = new URL(window.location.href);
    url.searchParams.delete('run');
    if (modeTab === 'replay' && selectedRunPath) url.searchParams.set('trial', selectedRunPath);
    else url.searchParams.delete('trial');
    if (modeTab === 'watch' && selectedContainerId) url.searchParams.set('container', selectedContainerId);
    else url.searchParams.delete('container');
    window.history.replaceState(null, '', url);
  }, [modeTab, selectedRunPath, selectedContainerId]);

  // Re-fetch active play session when God View / Reveal Secret / Inspect Minute changes
  useEffect(() => {
    if (!playSession || modeTab !== 'play') return;
    let cancelled = false;
    api
      .getSession(playSession.session_id, {
        includeGod: playGodView,
        revealSecret,
        inspectMinute,
      })
      .then((s) => {
        if (!cancelled) setPlaySession(s);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playGodView, revealSecret, inspectMinute, modeTab]);

  // Poll Harbor containers when in Harbor watch mode
  useEffect(() => {
    if (modeTab !== 'watch') return;
    let cancelled = false;
    const pollList = async () => {
      try {
        const res = await api.listHarborContainers();
        if (cancelled) return;
        setHarborStatus(res);
        if (selectedContainerId && !res.containers.some(c => c.container_id === selectedContainerId)) {
          setSelectedContainerId(null);
          setHarborPayload(null);
        } else if (!selectedContainerId && res.containers.length > 0) {
          setSelectedContainerId(res.containers[0]!.container_id);
        }
      } catch (err) {
        if (!cancelled) {
          setHarborStatus({ docker_available: false, error: String(err), containers: [] });
        }
      }
    };
    void pollList();
    const timer = window.setInterval(() => void pollList(), 2000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [modeTab, selectedContainerId]);

  // Tail selected Harbor container
  useEffect(() => {
    if (modeTab !== 'watch' || !selectedContainerId) return;
    let cancelled = false;
    const pollContainer = async () => {
      try {
        const data = await api.tailHarborContainer(selectedContainerId, {
          inspectMinute,
          revealSecret,
        });
        if (!cancelled) setHarborPayload(data);
      } catch {
        // The container list will clear an exited trial; its saved files appear in Runs.
      }
    };
    void pollContainer();
    const timer = window.setInterval(() => void pollContainer(), 900);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [modeTab, selectedContainerId, inspectMinute, revealSecret]);

  // Load saved runs list in Replay tab (and auto-refresh for live `.jsonl.tmp` runs)
  const refreshRuns = useCallback(async () => {
    try {
      const list = await api.listRuns();
      setSavedRuns(list);
      if (!selectedRunPath && list.length > 0) {
        setSelectedRunPath(list[0]!.path);
      }
    } catch (err) {
      setErrorBanner(String(err));
    }
  }, [selectedRunPath]);

  useEffect(() => {
    if (modeTab !== 'replay') return;
    void refreshRuns();
    const timer = window.setInterval(() => void refreshRuns(), 2500);
    return () => window.clearInterval(timer);
  }, [modeTab, refreshRuns]);

  // Load / scrub selected replay run (and auto-tail if `.jsonl.tmp` is live!)
  useEffect(() => {
    if (modeTab !== 'replay' || !selectedRunPath) return;
    let cancelled = false;
    const loadReplay = async () => {
      try {
        const rep = await api.replayRun(selectedRunPath, {
          upToStep: scrubStep,
          inspectMinute,
          revealSecret,
        });
        if (!cancelled) setReplayData(rep);
      } catch (err) {
        if (!cancelled) setErrorBanner(String(err));
      }
    };
    void loadReplay();
    const isLiveRun = savedRuns.find((r) => r.path === selectedRunPath)?.live;
    if (!isLiveRun) return;
    const timer = window.setInterval(() => void loadReplay(), 1000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [modeTab, selectedRunPath, scrubStep, inspectMinute, revealSecret, savedRuns]);

  // Animated step playback in Replay mode
  useEffect(() => {
    if (!replayPlaying || !replayData) return;
    const total = replayData.total_steps;
    const timer = window.setInterval(() => {
      setScrubStep((prev) => {
        const curr = prev ?? 0;
        if (curr >= total) {
          setReplayPlaying(false);
          return total;
        }
        return curr + 1;
      });
    }, 600);
    return () => window.clearInterval(timer);
  }, [replayPlaying, replayData]);

  // Handlers
  const startNewPlayEpisode = async (seedOverride?: number, diffOverride?: number) => {
    setPlayBusy(true);
    setErrorBanner(null);
    setInspectMinute(null);
    setSelectedService(null);
    try {
      const s = await api.createSession(
        {
          task_id: 'oncall@1',
          seed: seedOverride ?? playSeed,
          difficulty: diffOverride ?? playDifficulty,
          mode: 'human',
        },
        playGodView,
        revealSecret,
      );
      setPlaySession(s);
    } catch (err) {
      setErrorBanner(String(err));
    } finally {
      setPlayBusy(false);
    }
  };

  const executePlayTool = async (name: string, args: Record<string, unknown>) => {
    if (!playSession) return;
    setPlayBusy(true);
    setErrorBanner(null);
    setInspectMinute(null);
    try {
      const res = await api.stepSession(
        playSession.session_id,
        name,
        args,
        playGodView,
        revealSecret,
      );
      setPlaySession(res.session);
    } catch (err) {
      setErrorBanner(String(err));
    } finally {
      setPlayBusy(false);
    }
  };

  const closePlaySession = async () => {
    if (!playSession) return;
    setPlayBusy(true);
    try {
      const updated = await api.controlSession(
        playSession.session_id,
        'close',
        playGodView,
        revealSecret,
      );
      setPlaySession(updated);
    } catch (err) {
      setErrorBanner(String(err));
    } finally {
      setPlayBusy(false);
    }
  };

  // Active view projection across modes
  const activeView = useMemo(() => {
    if (modeTab === 'play') {
      return {
        label: playSession ? `Human Operator (Seed ${playSession.seed})` : 'Human Operator',
        seed: playSession?.seed ?? playSeed,
        difficulty: playSession?.difficulty ?? playDifficulty,
        sessionId: playSession?.session_id ?? null,
        fog: playSession?.fog_state ?? EMPTY_FOG,
        god: playSession?.god_state ?? null,
        showGod: playGodView,
        toggleGod: () => setPlayGodView((v) => !v),
        steps: playSession?.steps ?? [],
        allSteps: playSession?.all_steps ?? [],
        score: playSession?.outcome?.score ?? null,
        status: playSession?.status ?? 'waiting',
        maxSteps: playSession?.max_steps ?? 80,
        agentLogs: null as string | null,
      };
    }
    if (modeTab === 'watch') {
      return {
        label: harborPayload ? `Harbor · seed ${harborPayload.header.seed}` : 'Harbor evaluation',
        seed: harborPayload?.header.seed ?? 0,
        difficulty: 0,
        sessionId: null,
        fog: harborPayload?.fog_state ?? EMPTY_FOG,
        god: harborPayload?.god_state ?? null,
        showGod: watchGodView,
        toggleGod: () => setWatchGodView(v => !v),
        steps: harborPayload?.steps ?? [],
        allSteps: harborPayload?.all_steps ?? [],
        score: harborPayload?.footer?.score ?? null,
        status: !harborPayload ? 'waiting' : harborPayload.live ? 'running' : 'closed',
        maxSteps: 80,
        agentLogs: harborPayload?.agent_logs ?? null,
      };
    }
    return {
      label: replayData
        ? `${replayData.header.policy} (Seed ${replayData.header.seed})`
        : 'Transcript Replay',
      seed: replayData?.header.seed ?? 0,
      difficulty:
        replayData?.god_state && replayData.god_state.services.length <= 8 ? 1 : 0,
      sessionId: null,
      fog: replayData?.fog_state ?? EMPTY_FOG,
      god: replayData?.god_state ?? null,
      showGod: watchGodView,
      toggleGod: () => setWatchGodView((v) => !v),
      steps: replayData?.steps ?? [],
      allSteps: replayData?.all_steps ?? [],
      score: replayData?.footer?.score ?? null,
      status: replayData?.live ? 'running' : 'closed',
      maxSteps: 80,
      agentLogs: replayData?.agent_logs ?? null,
    };
  }, [
    modeTab,
    playSession,
    playSeed,
    playDifficulty,
    playGodView,
    watchGodView,
    harborPayload,
    replayData,
  ]);

  const openCompare = async (policyOverride?: string) => {
    const pol = policyOverride ?? comparePolicy;
    setComparePolicy(pol);
    try {
      const base = await api.compareBaseline({
        task: 'oncall@1',
        seed: activeView.seed,
        difficulty: activeView.difficulty,
        policy: pol,
        sessionId: activeView.sessionId,
        revealSecret,
      });
      setCompareBaseline(base);
      setCompareOpen(true);
    } catch (err) {
      setErrorBanner(String(err));
    }
  };

  const currentMinute = activeView.god?.minute ?? activeView.fog.current_minute;
  const latestStep = activeView.steps[activeView.steps.length - 1] ?? null;
  const activeStepService = extractTargetService(latestStep ?? undefined);

  const knownServices = useMemo(() => {
    if (activeView.showGod && activeView.god) {
      return activeView.god.services.map((s) => s.name);
    }
    return activeView.fog.services.map((s) => s.name);
  }, [activeView]);

  const knownChanges = useMemo(() => {
    if (activeView.showGod && activeView.god) {
      return activeView.god.changes;
    }
    return activeView.fog.changes;
  }, [activeView]);

  const oncallTools = meta?.tools['oncall@1'] ?? [];
  const oncallPolicies = meta?.policies['oncall@1'] ?? ['reference', 'graph_walk', 'loudest_alert'];

  return (
    <div className="app-shell">
      {/* Top Bar */}
      <header className="topbar">
        <div className="brand">
          <div className="brand-mark">
            <Flame size={15} />
          </div>
          <div>
            <div style={{ fontSize: 13.5 }}>Crucible</div>
            <div style={{ fontSize: 10.5, color: 'var(--text-2)', fontWeight: 400 }}>
              Oncall evaluation
            </div>
          </div>
        </div>

        <nav className="mode-tabs">
          <button
            className={`mode-tab ${modeTab === 'play' ? 'active' : ''}`}
            onClick={() => {
              setModeTab('play');
              setInspectMinute(null);
            }}
          >
            <Gamepad2 size={14} /> Play
          </button>
          <button
            className={`mode-tab ${modeTab === 'watch' ? 'active' : ''}`}
            onClick={() => {
              setModeTab('watch');
              setInspectMinute(null);
            }}
          >
            <Bot size={14} /> Watch
          </button>
          <button
            className={`mode-tab ${modeTab === 'replay' ? 'active' : ''}`}
            onClick={() => {
              setModeTab('replay');
              setInspectMinute(null);
            }}
          >
            <Film size={14} /> Runs
          </button>
        </nav>

        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <button className="btn" disabled={modeTab !== 'play' || !playSession} title="Compare a manual episode with a built-in baseline" onClick={() => void openCompare()}>
            <GitCompare size={13} /> Compare
          </button>
          <button className="btn" onClick={() => setShowInstructions((v) => !v)}>
            <BookOpen size={13} /> Help
          </button>
        </div>
      </header>

      {/* Error banner */}
      {errorBanner && (
        <div
          style={{
            padding: '6px 14px',
            background: 'rgba(239,68,68,0.18)',
            borderBottom: '1px solid rgba(239,68,68,0.45)',
            color: '#fecaca',
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
          }}
        >
          <span>{errorBanner}</span>
          <button className="btn" onClick={() => setErrorBanner(null)}>
            Dismiss
          </button>
        </div>
      )}

      {/* Collapsible Task Instructions */}
      {showInstructions && (
        <div
          style={{
            padding: '10px 16px',
            background: 'var(--bg-2)',
            borderBottom: '1px solid var(--border)',
          }}
        >
          <pre className="code-block" style={{ maxHeight: 150 }}>
            {playSession?.instruction ?? 'Investigate the incident, repair the affected service, and verify recovery. Every tool call advances simulated time.'}
          </pre>
        </div>
      )}

      {/* Mode-Specific HUD Strip */}
      {!(modeTab === 'watch' && !harborPayload) && <div className="hud-bar">
        <div className="hud-group">
          <div className="stat-pill">
            <Clock size={13} color="var(--cyan)" />
            <span className="label">Sim Clock</span>
            <span className="val">
              m={currentMinute} / 150
            </span>
          </div>
          <div className="stat-pill">
            <Activity size={13} color="var(--emerald)" />
            <span className="label">Call Budget</span>
            <span className="val">
              {activeView.allSteps.length} / {activeView.maxSteps}
            </span>
          </div>
          <div className="stat-pill">
            {activeView.status === 'running' && <span className="live-dot" />}
            <span className="label">Status</span>
            <span className="val">{activeView.status === 'closed' ? 'Completed' : activeView.status}</span>
          </div>
          {activeView.score && (
            <div
              className="stat-pill"
              style={{ borderColor: 'rgba(16,185,129,0.5)', background: 'rgba(16,185,129,0.1)' }}
            >
              <span className="label">Reward</span>
              <span className="val" style={{ color: '#34d399' }}>
                {activeView.score.total.toFixed(4)}
              </span>
              <span className="mono" style={{ fontSize: 10.5, color: 'var(--text-1)' }}>
                {activeView.score.flags.verified ? 'Recovery verified' : 'Recovery unverified'}
              </span>
            </div>
          )}
        </div>

        {/* Right HUD Controls per mode */}
        {modeTab === 'play' && (
          <div className="hud-group">
            <label style={{ display: 'flex', alignItems: 'center', gap: 5, fontSize: 12 }}>
              Seed
              <input
                type="number"
                min={0}
                className="input mono"
                style={{ width: 64 }}
                value={playSeed}
                onChange={(e) => setPlaySeed(Number(e.target.value))}
              />
            </label>
            <select
              className="select"
              value={playDifficulty}
              onChange={(e) => setPlayDifficulty(Number(e.target.value))}
            >
              <option value={0}>Full Cluster (23 svcs, 6 faults)</option>
              <option value={1}>Thin Slice (8 svcs, 2 faults)</option>
            </select>
            <button
              className="btn btn-primary"
              disabled={playBusy}
              onClick={() => void startNewPlayEpisode(playSeed, playDifficulty)}
            >
              <RotateCcw size={13} /> New Episode
            </button>
            <button
              className="btn"
              disabled={playBusy}
              onClick={() => {
                const next = playSeed + 1;
                setPlaySeed(next);
                void startNewPlayEpisode(next, playDifficulty);
              }}
            >
              Next Seed →
            </button>
            {playSession && playSession.status !== 'closed' && (
              <button className="btn btn-danger" onClick={() => void closePlaySession()}>
                Abort &amp; Grade
              </button>
            )}
          </div>
        )}

        {modeTab === 'replay' && replayData && (
          <div className="hud-group">
            <button
              className="btn"
              onClick={() => {
                if ((scrubStep ?? replayData.total_steps) >= replayData.total_steps) {
                  setScrubStep(0);
                }
                setReplayPlaying((p) => !p);
              }}
            >
              {replayPlaying ? <Pause size={13} /> : <Play size={13} />}
              {replayPlaying ? 'Pause' : 'Play Steps'}
            </button>
            <span className="mono" style={{ fontSize: 11.5 }}>
              Step {replayData.selected_step} / {replayData.total_steps}
            </span>
            <input
              type="range"
              min={0}
              max={replayData.total_steps}
              value={replayData.selected_step}
              onChange={(e) => {
                setReplayPlaying(false);
                const val = Number(e.target.value);
                setScrubStep(val === replayData.total_steps ? null : val);
              }}
              style={{ width: 140, accentColor: 'var(--cyan)' }}
            />
            <span className="badge badge-info">{replayData.salt_status}</span>
          </div>
        )}
      </div>}

      {/* Main Two-Column Split */}
      <main className={`workspace ${modeTab !== 'play' ? 'wide-right' : ''} ${modeTab === 'watch' && !harborPayload ? 'awaiting-run' : ''}`}>
        {/* Left/Center: Cluster Topology + Telemetry/Traces Inspector */}
        <section className="center-col">
          <div className="graph-pane">
            {modeTab === 'watch' && !harborPayload ? (
              <div className="watch-welcome">
                <span className="eyebrow">Oncall incident gym</span>
                <h1>Watch a Harbor evaluation.</h1>
                <p>OpenCode investigates the incident inside Docker. Follow the live trial here, then reopen its transcript from Runs.</p>
                <div className="welcome-steps"><span>01 · Choose a prepared export and model</span><span>02 · Launch Harbor with OpenCode</span><span>03 · Watch the trial · results save automatically</span></div>
              </div>
            ) : <ClusterGraph
              godState={activeView.god}
              fogState={activeView.fog}
              showGodView={activeView.showGod}
              onToggleGodView={activeView.toggleGod}
              revealSecret={revealSecret}
              onToggleRevealSecret={() => setRevealSecret((v) => !v)}
              selectedService={selectedService}
              onSelectService={setSelectedService}
              activeStepService={activeStepService}
              canQuickRun={modeTab === 'play' && playSession?.status !== 'closed'}
              onQuickTool={(nm, args) => void executePlayTool(nm, args)}
            />}
          </div>

          <div className="inspector-pane">
            <InspectorTabs
              godState={activeView.god}
              fogState={activeView.fog}
              showGodView={activeView.showGod}
              steps={activeView.steps}
              selectedService={selectedService}
              onSelectService={setSelectedService}
              inspectMinute={inspectMinute}
              onChangeInspectMinute={setInspectMinute}
              canInteract={modeTab === 'play' && playSession?.status !== 'closed'}
              onQuickTool={(nm, args) => void executePlayTool(nm, args)}
              onStageRevert={(chg) => {
                setStagedRevert(chg);
                setPlayRightTab('mcp');
              }}
            />
          </div>
        </section>

        {/* Right Column: MCP Console / Agent Launcher & Harbor Container Picker / Replay Browser */}
        <aside className="right-col">
          {modeTab === 'play' && (
            <>
              <div className="subtabs">
                <button
                  className={`subtab ${playRightTab === 'mcp' ? 'active' : ''}`}
                  onClick={() => setPlayRightTab('mcp')}
                >
                  MCP Tool Console (7 tools)
                </button>
                <button
                  className={`subtab ${playRightTab === 'log' ? 'active' : ''}`}
                  onClick={() => setPlayRightTab('log')}
                >
                  Action Log ({activeView.steps.length})
                </button>
              </div>
              <div className="scroll-body">
                {playRightTab === 'mcp' ? (
                  <McpConsole
                    tools={oncallTools}
                    commandTemplates={meta?.command_templates ?? []}
                    faultKinds={meta?.fault_kinds ?? []}
                    remediations={meta?.remediations ?? []}
                    knownServices={knownServices}
                    knownChanges={knownChanges}
                    selectedService={selectedService}
                    stagedRevert={stagedRevert}
                    onClearStagedRevert={() => setStagedRevert(null)}
                    disabled={!playSession || playSession.status === 'closed'}
                    busy={playBusy}
                    onExecuteTool={executePlayTool}
                    lastStep={latestStep}
                    fogState={activeView.fog}
                    godState={activeView.god}
                  />
                ) : (
                  <ActionTimeline
                    steps={activeView.steps}
                    selectedStep={null}
                    onFocusService={setSelectedService}
                  />
                )}
              </div>
            </>
          )}

          {modeTab === 'watch' && (
            <div className="scroll-body watch-sidebar">
              <HarborWatchPanel
                containers={harborStatus.containers}
                dockerAvailable={harborStatus.docker_available}
                dockerError={harborStatus.error}
                selectedContainerId={selectedContainerId}
                onSelectContainer={id => { setHarborPayload(null); setSelectedContainerId(id); setInspectMinute(null); }}
                onOpenRuns={async jobPath => {
                  const runs = await api.listRuns();
                  setSavedRuns(runs);
                  const trial = runs.find(r => r.path.startsWith(jobPath + '/'));
                  setSelectedRunPath(trial?.path ?? null);
                  setRunFilter(jobPath.split('/').pop() ?? '');
                  setScrubStep(null);
                  setModeTab('replay');
                }}
              />
              {harborPayload && <>
                <div className="eyebrow">Activity · {activeView.steps.length} calls</div>
                <ActionTimeline steps={activeView.steps} selectedStep={null} onFocusService={setSelectedService} agentLogs={activeView.agentLogs} />
              </>}
            </div>
          )}

          {modeTab === 'replay' && (
            <>
              <div className="subtabs" style={{ justifyContent: 'space-between' }}>
                <span className="subtab active">
                  Harbor trials ({savedRuns.length})
                </span>
                <button
                  className="btn"
                  style={{ margin: '4px 0', padding: '3px 7px' }}
                  onClick={() => void refreshRuns()}
                >
                  <RefreshCw size={12} /> Refresh
                </button>
              </div>

              <div className="scroll-body" style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                <input
                  className="input"
                  placeholder="Filter by policy, group, or seed…"
                  value={runFilter}
                  onChange={(e) => setRunFilter(e.target.value)}
                />

                <div
                  style={{
                    maxHeight: 220,
                    overflowY: 'auto',
                    display: 'flex',
                    flexDirection: 'column',
                    gap: 5,
                  }}
                >
                  {savedRuns
                    .filter((r) => {
                      if (!runFilter.trim()) return true;
                      const q = runFilter.toLowerCase();
                      return (
                        r.policy.toLowerCase().includes(q) ||
                        r.group.toLowerCase().includes(q) ||
                        `seed ${r.seed}`.includes(q)
                      );
                    })
                    .map((r) => {
                      const active = r.path === selectedRunPath;
                      return (
                        <div
                          key={r.path}
                          onClick={() => {
                            setSelectedRunPath(r.path);
                            setScrubStep(null);
                            setReplayPlaying(false);
                          }}
                          style={{
                            padding: '7px 9px',
                            borderRadius: 6,
                            background: active ? 'rgba(56,189,248,0.12)' : 'var(--bg-2)',
                            border: `1px solid ${active ? 'var(--cyan)' : 'var(--border)'}`,
                            cursor: 'pointer',
                          }}
                        >
                          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                            <span className="mono" style={{ fontWeight: 600, fontSize: 11.5 }}>
                              {r.live && (
                                <span
                                  className="live-dot"
                                  style={{ display: 'inline-block', marginRight: 6 }}
                                />
                              )}
                              {r.policy} · seed {r.seed}
                            </span>
                            <span
                              className={`badge ${
                                r.live
                                  ? 'badge-info'
                                  : (r.score ?? 0) > 0.7
                                    ? 'badge-ok'
                                    : (r.score ?? 0) > 0
                                      ? 'badge-warn'
                                      : 'badge-err'
                              }`}
                            >
                              {r.live ? 'Live' : (r.score ?? 0).toFixed(3)}
                            </span>
                          </div>
                          <div
                            className="mono"
                            style={{ fontSize: 10, color: 'var(--text-2)', marginTop: 2 }}
                          >
                            {r.group} · {r.steps} steps · {r.source_kind}
                          </div>
                        </div>
                      );
                    })}
                  {savedRuns.length === 0 && (
                    <div style={{ color: 'var(--text-2)', textAlign: 'center', padding: 14 }}>
                      No Harbor transcripts yet. Completed trials will appear here automatically.
                    </div>
                  )}
                </div>

                <div style={{ fontWeight: 600, fontSize: 12 }}>
                  Recorded Steps ({activeView.steps.length} / {activeView.allSteps.length})
                </div>
                <ActionTimeline
                  steps={activeView.allSteps}
                  selectedStep={replayData?.selected_step ?? null}
                  onSelectStep={(stepNum) => {
                    setReplayPlaying(false);
                    setScrubStep(stepNum);
                  }}
                  onFocusService={setSelectedService}
                  agentLogs={activeView.agentLogs}
                />
              </div>
            </>
          )}
        </aside>
      </main>

      {/* Paired Comparison Modal */}
      {compareOpen && compareBaseline && (
        <CompareDrawer
          primaryLabel={activeView.label}
          primaryScore={activeView.score}
          primarySteps={activeView.allSteps}
          primaryGod={activeView.god}
          baseline={compareBaseline}
          baselinePolicy={comparePolicy}
          availablePolicies={oncallPolicies}
          onChangeBaselinePolicy={(pol) => void openCompare(pol)}
          onClose={() => setCompareOpen(false)}
        />
      )}
    </div>
  );
};
