import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Activity,
  BookOpen,
  Bot,
  Box,
  Clock,
  Copy,
  Film,
  Flame,
  Gamepad2,
  GitCompare,
  Pause,
  Play,
  Plug,
  RefreshCw,
  RotateCcw,
  SkipForward,
  Square,
} from 'lucide-react';
import { api } from './api';
import { ActionTimeline, extractTargetService } from './components/ActionTimeline';
import { ClusterGraph } from './components/ClusterGraph';
import { CompareDrawer } from './components/CompareDrawer';
import { InspectorTabs } from './components/InspectorTabs';
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
  const [modeTab, setModeTab] = useState<'play' | 'watch' | 'replay'>('play');
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

  // Live Watch state (`policy` | `llm` | `harbor` | `mcp`)
  const [watchSource, setWatchSource] = useState<'runner' | 'harbor' | 'mcp'>('runner');
  const [watchRunnerKind, setWatchRunnerKind] = useState<'policy' | 'llm'>('policy');
  const [watchSeed, setWatchSeed] = useState(2);
  const [watchDifficulty, setWatchDifficulty] = useState(0);
  const [watchPolicy, setWatchPolicy] = useState('reference');
  const [watchModel, setWatchModel] = useState('gemini-3.8-flash');
  const [watchBaseUrl, setWatchBaseUrl] = useState(
    'https://generativelanguage.googleapis.com/v1beta/openai/',
  );
  const [watchApiKeyEnv, setWatchApiKeyEnv] = useState('GOOGLE_GENERATIVE_AI_API_KEY');
  const [watchDelayMs, setWatchDelayMs] = useState(550);
  const [watchSession, setWatchSession] = useState<WorkbenchSession | null>(null);

  // Live Harbor container state
  const [harborStatus, setHarborStatus] = useState<{
    docker_available: boolean;
    error: string | null;
    containers: HarborContainerInfo[];
  }>({ docker_available: true, error: null, containers: [] });
  const [selectedContainerId, setSelectedContainerId] = useState<string | null>(null);
  const [harborPayload, setHarborPayload] = useState<ReplayPayload | null>(null);

  // Replay & Saved Runs state
  const [savedRuns, setSavedRuns] = useState<SavedRunItem[]>([]);
  const [runFilter, setRunFilter] = useState('');
  const [selectedRunPath, setSelectedRunPath] = useState<string | null>(null);
  const [replayData, setReplayData] = useState<ReplayPayload | null>(null);
  const [scrubStep, setScrubStep] = useState<number | null>(null);
  const [replayPlaying, setReplayPlaying] = useState(false);

  // Side-by-side Compare Drawer
  const [compareOpen, setCompareOpen] = useState(false);
  const [comparePolicy, setComparePolicy] = useState('graph_walk');
  const [compareBaseline, setCompareBaseline] = useState<BaselineComparePayload | null>(null);

  // Initial load: metadata + start first Human Play episode
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const m = await api.getMeta();
        if (cancelled) return;
        setMeta(m);
        const sess = await api.createSession(
          { task_id: 'oncall@1', seed: 0, difficulty: 0, mode: 'human' },
          false,
          false,
        );
        if (!cancelled) setPlaySession(sess);
      } catch (err) {
        if (!cancelled) setErrorBanner(String(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

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

  // Poll live watch session when active
  useEffect(() => {
    if (
      modeTab !== 'watch' ||
      watchSource === 'harbor' ||
      !watchSession ||
      (watchSession.status === 'closed' && inspectMinute === null)
    ) {
      return;
    }
    let cancelled = false;
    const tick = async () => {
      try {
        const updated = await api.getSession(watchSession.session_id, {
          includeGod: watchGodView,
          revealSecret,
          inspectMinute,
        });
        if (!cancelled) setWatchSession(updated);
      } catch {
        // ignore transient poll error
      }
    };
    void tick();
    if (watchSession.status === 'closed') return;
    const timer = window.setInterval(() => void tick(), 350);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    modeTab,
    watchSource,
    watchSession?.session_id,
    watchSession?.status,
    watchGodView,
    revealSecret,
    inspectMinute,
  ]);

  // Poll Harbor containers when in Harbor watch mode
  useEffect(() => {
    if (modeTab !== 'watch' || watchSource !== 'harbor') return;
    let cancelled = false;
    const pollList = async () => {
      try {
        const res = await api.listHarborContainers();
        if (cancelled) return;
        setHarborStatus(res);
        if (!selectedContainerId && res.containers.length > 0) {
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
  }, [modeTab, watchSource, selectedContainerId]);

  // Tail selected Harbor container
  useEffect(() => {
    if (modeTab !== 'watch' || watchSource !== 'harbor' || !selectedContainerId) return;
    let cancelled = false;
    const pollContainer = async () => {
      try {
        const data = await api.tailHarborContainer(selectedContainerId, {
          inspectMinute,
          revealSecret,
        });
        if (!cancelled) setHarborPayload(data);
      } catch {
        // container may have exited after finishing
      }
    };
    void pollContainer();
    const timer = window.setInterval(() => void pollContainer(), 900);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [modeTab, watchSource, selectedContainerId, inspectMinute, revealSecret]);

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

  const launchWatchSession = async (modeOverride?: 'policy' | 'llm' | 'mcp') => {
    setErrorBanner(null);
    setInspectMinute(null);
    setSelectedService(null);
    const chosenMode = modeOverride ?? (watchSource === 'mcp' ? 'mcp' : watchRunnerKind);
    try {
      const sess = await api.createSession(
        {
          task_id: 'oncall@1',
          seed: watchSeed,
          difficulty: watchDifficulty,
          mode: chosenMode,
          policy: watchPolicy,
          model: watchModel,
          base_url: watchBaseUrl,
          api_key_env: watchApiKeyEnv,
          step_delay_ms: watchDelayMs,
          auto_run: true,
        },
        watchGodView,
        revealSecret,
      );
      setWatchSession(sess);
    } catch (err) {
      setErrorBanner(String(err));
    }
  };

  const controlWatch = async (
    action: 'pause' | 'resume' | 'step_once' | 'set_speed' | 'close',
    delayMs?: number,
  ) => {
    if (!watchSession) return;
    try {
      const s = await api.controlSession(
        watchSession.session_id,
        action,
        watchGodView,
        revealSecret,
        delayMs,
      );
      setWatchSession(s);
    } catch (err) {
      setErrorBanner(String(err));
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
      if (watchSource === 'harbor') {
        return {
          label: harborPayload
            ? `Harbor ${harborPayload.compose_project || harborPayload.container_id} (Seed ${harborPayload.header.seed})`
            : 'Harbor Live Container',
          seed: harborPayload?.header.seed ?? 0,
          difficulty: 0,
          sessionId: null,
          fog: harborPayload?.fog_state ?? EMPTY_FOG,
          god: harborPayload?.god_state ?? null,
          showGod: watchGodView,
          toggleGod: () => setWatchGodView((v) => !v),
          steps: harborPayload?.steps ?? [],
          allSteps: harborPayload?.all_steps ?? [],
          score: harborPayload?.footer?.score ?? null,
          status: harborPayload?.live ? 'running' : 'closed',
          maxSteps: 80,
          agentLogs: harborPayload?.agent_logs ?? null,
        };
      }
      return {
        label: watchSession
          ? `${watchSession.actor_label} (Seed ${watchSession.seed})`
          : 'Agent Watch',
        seed: watchSession?.seed ?? watchSeed,
        difficulty: watchSession?.difficulty ?? watchDifficulty,
        sessionId: watchSession?.session_id ?? null,
        fog: watchSession?.fog_state ?? EMPTY_FOG,
        god: watchSession?.god_state ?? null,
        showGod: watchGodView,
        toggleGod: () => setWatchGodView((v) => !v),
        steps: watchSession?.steps ?? [],
        allSteps: watchSession?.all_steps ?? [],
        score: watchSession?.outcome?.score ?? null,
        status: watchSession?.status ?? 'waiting',
        maxSteps: watchSession?.max_steps ?? 80,
        agentLogs: null,
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
    watchSource,
    watchSession,
    watchSeed,
    watchDifficulty,
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
            <div style={{ fontSize: 13.5 }}>CRUCIBLE // ONCALL@1</div>
            <div style={{ fontSize: 10.5, color: 'var(--text-2)', fontWeight: 400 }}>
              Incident Gym, MCP Console &amp; Live Agent Evaluation Workbench
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
            <Gamepad2 size={14} /> Human Play (MCP)
          </button>
          <button
            className={`mode-tab ${modeTab === 'watch' ? 'active' : ''}`}
            onClick={() => {
              setModeTab('watch');
              setInspectMinute(null);
            }}
          >
            <Bot size={14} /> Live Agent Watch
          </button>
          <button
            className={`mode-tab ${modeTab === 'replay' ? 'active' : ''}`}
            onClick={() => {
              setModeTab('replay');
              setInspectMinute(null);
            }}
          >
            <Film size={14} /> Transcripts &amp; Replay
          </button>
        </nav>

        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <button className="btn btn-primary" onClick={() => void openCompare()}>
            <GitCompare size={13} /> Compare vs `{comparePolicy}`
          </button>
          <button className="btn" onClick={() => setShowInstructions((v) => !v)}>
            <BookOpen size={13} /> Task Rules
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
      {showInstructions && playSession && (
        <div
          style={{
            padding: '10px 16px',
            background: 'var(--bg-2)',
            borderBottom: '1px solid var(--border)',
          }}
        >
          <pre className="code-block" style={{ maxHeight: 150 }}>
            {playSession.instruction}
          </pre>
        </div>
      )}

      {/* Mode-Specific HUD Strip */}
      <div className="hud-bar">
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
            <span className="val">{activeView.status.toUpperCase()}</span>
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
                (imp {(activeView.score.breakdown.impact ?? 0).toFixed(2)} · ver{' '}
                {activeView.score.flags.verified ? '✓' : '✗'} · diag{' '}
                {((activeView.score.breakdown.diagnosis ?? 0) * 100).toFixed(0)}% · pen -
                {(activeView.score.breakdown.penalty ?? 0).toFixed(2)})
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

        {modeTab === 'watch' && watchSource === 'runner' && watchSession && (
          <div className="hud-group">
            {watchSession.status !== 'closed' && (
              <>
                {watchSession.paused ? (
                  <button className="btn btn-emerald" onClick={() => void controlWatch('resume')}>
                    <Play size={13} /> Resume
                  </button>
                ) : (
                  <button className="btn btn-action" onClick={() => void controlWatch('pause')}>
                    <Pause size={13} /> Pause
                  </button>
                )}
                <button className="btn" onClick={() => void controlWatch('step_once')}>
                  <SkipForward size={13} /> Step Once
                </button>
                <button className="btn btn-danger" onClick={() => void controlWatch('close')}>
                  <Square size={12} /> Stop &amp; Grade
                </button>
              </>
            )}
            <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11.5 }}>
              Pace {watchDelayMs}ms
              <input
                type="range"
                min={0}
                max={1500}
                step={50}
                value={watchDelayMs}
                onChange={(e) => {
                  const v = Number(e.target.value);
                  setWatchDelayMs(v);
                  void controlWatch('set_speed', v);
                }}
                style={{ width: 85, accentColor: 'var(--cyan)' }}
              />
            </label>
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
      </div>

      {/* Main Two-Column Split */}
      <main className={`workspace ${modeTab !== 'play' ? 'wide-right' : ''}`}>
        {/* Left/Center: Cluster Topology + Telemetry/Traces Inspector */}
        <section className="center-col">
          <div className="graph-pane">
            <ClusterGraph
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
            />
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
            <>
              <div className="subtabs">
                <button
                  className={`subtab ${watchSource === 'runner' ? 'active' : ''}`}
                  onClick={() => setWatchSource('runner')}
                >
                  <Bot size={13} /> Policy / LLM
                </button>
                <button
                  className={`subtab ${watchSource === 'harbor' ? 'active' : ''}`}
                  onClick={() => setWatchSource('harbor')}
                >
                  <Box size={13} /> Harbor Containers ({harborStatus.containers.length})
                </button>
                <button
                  className={`subtab ${watchSource === 'mcp' ? 'active' : ''}`}
                  onClick={() => setWatchSource('mcp')}
                >
                  <Plug size={13} /> External `/mcp`
                </button>
              </div>

              <div className="scroll-body" style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                {watchSource === 'runner' && (
                  <div className="card" style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                    <div style={{ display: 'flex', gap: 6 }}>
                      <button
                        className={`btn ${watchRunnerKind === 'policy' ? 'btn-primary' : ''}`}
                        style={{ flex: 1 }}
                        onClick={() => setWatchRunnerKind('policy')}
                      >
                        Built-in Policy / Bot
                      </button>
                      <button
                        className={`btn ${watchRunnerKind === 'llm' ? 'btn-primary' : ''}`}
                        style={{ flex: 1 }}
                        onClick={() => setWatchRunnerKind('llm')}
                      >
                        OpenAI-Compatible LLM
                      </button>
                    </div>

                    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 6 }}>
                      <label style={{ display: 'flex', flexDirection: 'column', gap: 3, fontSize: 11 }}>
                        Seed
                        <input
                          type="number"
                          min={0}
                          className="input mono"
                          value={watchSeed}
                          onChange={(e) => setWatchSeed(Number(e.target.value))}
                        />
                      </label>
                      <label style={{ display: 'flex', flexDirection: 'column', gap: 3, fontSize: 11 }}>
                        Cluster Size
                        <select
                          className="select"
                          value={watchDifficulty}
                          onChange={(e) => setWatchDifficulty(Number(e.target.value))}
                        >
                          <option value={0}>23 Services (Standard)</option>
                          <option value={1}>8 Services (Thin Slice)</option>
                        </select>
                      </label>
                    </div>

                    {watchRunnerKind === 'policy' ? (
                      <label style={{ display: 'flex', flexDirection: 'column', gap: 3, fontSize: 11 }}>
                        Policy
                        <select
                          className="select mono"
                          value={watchPolicy}
                          onChange={(e) => setWatchPolicy(e.target.value)}
                        >
                          {oncallPolicies.map((p) => (
                            <option key={p} value={p}>
                              {p}
                            </option>
                          ))}
                        </select>
                      </label>
                    ) : (
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                        <input
                          className="input mono"
                          value={watchModel}
                          onChange={(e) => setWatchModel(e.target.value)}
                          placeholder="Model ID (e.g. gemini-3.8-flash)"
                        />
                        <input
                          className="input mono"
                          value={watchBaseUrl}
                          onChange={(e) => setWatchBaseUrl(e.target.value)}
                          placeholder="Base URL"
                        />
                        <input
                          className="input mono"
                          value={watchApiKeyEnv}
                          onChange={(e) => setWatchApiKeyEnv(e.target.value)}
                          placeholder="Server env var holding API key"
                        />
                      </div>
                    )}

                    <button
                      className="btn btn-primary"
                      onClick={() => void launchWatchSession(watchRunnerKind)}
                    >
                      <Play size={13} /> Launch &amp; Watch Live
                    </button>
                  </div>
                )}

                {watchSource === 'harbor' && (
                  <div className="card" style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                    <div style={{ fontWeight: 600, fontSize: 12 }}>
                      Live Harbor Trial Container Selector
                    </div>
                    <div style={{ fontSize: 11.5, color: 'var(--text-2)', lineHeight: 1.45 }}>
                      Discovers active <code>mcp-server</code> sidecars (<code>crucible-sidecar:*</code>)
                      via <code>docker ps</code>, grouped by Compose trial project, and tails{' '}
                      <code>/tmp/runs/episodes/sidecar-*.jsonl(.tmp)</code> + reconstructs the salted
                      world on the host without exposing anything inside the container.
                    </div>
                    {!harborStatus.docker_available ? (
                      <div className="badge badge-warn" style={{ padding: 6 }}>
                        Docker unavailable: {harborStatus.error}
                      </div>
                    ) : harborStatus.containers.length === 0 ? (
                      <div style={{ color: 'var(--text-2)', fontSize: 11.5, padding: '6px 0' }}>
                        No running <code>crucible-sidecar</code> containers detected right now. Start{' '}
                        <code>harbor run -p runs/oncall-export ...</code> and active trials will
                        appear here automatically.
                      </div>
                    ) : (
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                        {harborStatus.containers.map((c) => (
                          <div
                            key={c.container_id}
                            onClick={() => setSelectedContainerId(c.container_id)}
                            style={{
                              padding: 8,
                              borderRadius: 6,
                              background: 'var(--bg-1)',
                              border: `1px solid ${
                                selectedContainerId === c.container_id
                                  ? 'var(--cyan)'
                                  : 'var(--border)'
                              }`,
                              cursor: 'pointer',
                            }}
                          >
                            <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                              <span className="mono" style={{ fontWeight: 600, fontSize: 11.5 }}>
                                {c.compose_project}
                              </span>
                              <span className="badge badge-ok">
                                Seed {c.seed} · {c.steps} steps
                              </span>
                            </div>
                            <div
                              className="mono"
                              style={{ fontSize: 10.5, color: 'var(--text-2)', marginTop: 3 }}
                            >
                              sidecar: {c.container_id} ({c.health})
                              {c.agent_container ? ` · agent: ${c.agent_container.id}` : ''}
                            </div>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                )}

                {watchSource === 'mcp' && (
                  <div className="card" style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                    <div style={{ fontWeight: 600, fontSize: 12 }}>
                      Attach External MCP Client (Claude Code / OpenCode / Cursor)
                    </div>
                    <div style={{ display: 'flex', gap: 6 }}>
                      <input
                        type="number"
                        min={0}
                        className="input mono"
                        style={{ width: 80 }}
                        value={watchSeed}
                        onChange={(e) => setWatchSeed(Number(e.target.value))}
                      />
                      <button
                        className="btn btn-primary"
                        style={{ flex: 1 }}
                        onClick={() => void launchWatchSession('mcp')}
                      >
                        <Plug size={13} /> Create Live `/mcp` Session
                      </button>
                    </div>
                    {watchSession && watchSession.mode === 'mcp' && (
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                        <div className="mono" style={{ fontSize: 11, color: 'var(--cyan)' }}>
                          {window.location.origin}
                          {watchSession.mcp_path}
                        </div>
                        <div style={{ display: 'flex', gap: 6 }}>
                          <button
                            className="btn"
                            onClick={() =>
                              void navigator.clipboard.writeText(
                                `${window.location.origin}${watchSession.mcp_path}`,
                              )
                            }
                          >
                            <Copy size={12} /> Copy MCP URL
                          </button>
                          <button
                            className="btn"
                            onClick={() =>
                              void navigator.clipboard.writeText(
                                `uv run --directory engine crucible play --task oncall@1 --policy reference --mcp ${window.location.origin}${watchSession.mcp_path}`,
                              )
                            }
                          >
                            <Copy size={12} /> Copy `crucible play` Cmd
                          </button>
                        </div>
                      </div>
                    )}
                  </div>
                )}

                <div style={{ fontWeight: 600, fontSize: 12, marginTop: 4 }}>
                  Live Agent Action Stream ({activeView.steps.length} calls)
                </div>
                <ActionTimeline
                  steps={activeView.steps}
                  selectedStep={null}
                  onFocusService={setSelectedService}
                  agentLogs={activeView.agentLogs}
                />
              </div>
            </>
          )}

          {modeTab === 'replay' && (
            <>
              <div className="subtabs" style={{ justifyContent: 'space-between' }}>
                <span className="subtab active">
                  Saved &amp; Live `.tmp` Runs ({savedRuns.length})
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
                              {r.live ? 'LIVE .tmp' : (r.score ?? 0).toFixed(3)}
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
                      No transcripts in <code>runs/</code> or <code>jobs/</code> yet.
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
