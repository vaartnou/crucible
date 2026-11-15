import type {
  BaselineComparePayload,
  HarborContainerInfo,
  ReplayPayload,
  SavedRunItem,
  WorkbenchMeta,
  WorkbenchSession,
} from './types';

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
    ...init,
  });
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try {
      const body = (await res.json()) as { detail?: string };
      if (body.detail) msg = body.detail;
    } catch {
      // ignore non-json error body
    }
    throw new Error(msg);
  }
  return (await res.json()) as T;
}

export const api = {
  getHarborJobs: () => request<import('./types').HarborJobsState>('/ui/harbor/jobs'),
  launchHarbor: (payload: { export: string; model: string; concurrency: number }) =>
    request<{ id: string; path: string; status: string }>('/ui/harbor/jobs', { method: 'POST', body: JSON.stringify(payload) }),
  getMeta: () => request<WorkbenchMeta>('/ui/meta'),

  createSession: (
    payload: {
      task_id?: string;
      seed: number;
      difficulty: number;
      mode: 'human' | 'policy' | 'mcp';
      policy?: string;
      step_delay_ms?: number;
      auto_run?: boolean;
      use_eval_salt?: boolean;
    },
    includeGod: boolean,
    revealSecret: boolean,
  ) =>
    request<WorkbenchSession>(
      `/ui/sessions?include_god=${includeGod}&reveal_secret=${revealSecret}`,
      {
        method: 'POST',
        body: JSON.stringify(payload),
      },
    ),

  getSession: (
    sessionId: string,
    opts: {
      includeGod: boolean;
      revealSecret: boolean;
      inspectMinute?: number | null;
      upToStep?: number | null;
    },
  ) => {
    const q = new URLSearchParams({
      include_god: String(opts.includeGod),
      reveal_secret: String(opts.revealSecret),
    });
    if (opts.inspectMinute != null) q.set('inspect_minute', String(opts.inspectMinute));
    if (opts.upToStep != null) q.set('up_to_step', String(opts.upToStep));
    return request<WorkbenchSession>(`/ui/sessions/${sessionId}?${q.toString()}`);
  },

  stepSession: (
    sessionId: string,
    name: string,
    args: Record<string, unknown>,
    includeGod: boolean,
    revealSecret: boolean,
  ) =>
    request<{ result: WorkbenchSession['steps'][number]['result']; session: WorkbenchSession }>(
      `/ui/sessions/${sessionId}/step`,
      {
        method: 'POST',
        body: JSON.stringify({
          name,
          args,
          include_god: includeGod,
          reveal_secret: revealSecret,
        }),
      },
    ),

  controlSession: (
    sessionId: string,
    action: 'pause' | 'resume' | 'step_once' | 'set_speed' | 'close',
    includeGod: boolean,
    revealSecret: boolean,
    stepDelayMs?: number,
  ) =>
    request<WorkbenchSession>(`/ui/sessions/${sessionId}/control`, {
      method: 'POST',
      body: JSON.stringify({
        action,
        step_delay_ms: stepDelayMs,
        include_god: includeGod,
        reveal_secret: revealSecret,
      }),
    }),

  listRuns: () => request<SavedRunItem[]>('/ui/runs'),

  replayRun: (
    path: string,
    opts: {
      upToStep?: number | null;
      inspectMinute?: number | null;
      revealSecret?: boolean;
    },
  ) => {
    const q = new URLSearchParams({
      path,
      reveal_secret: String(Boolean(opts.revealSecret)),
    });
    if (opts.upToStep != null) q.set('up_to_step', String(opts.upToStep));
    if (opts.inspectMinute != null) q.set('inspect_minute', String(opts.inspectMinute));
    return request<ReplayPayload>(`/ui/runs/replay?${q.toString()}`);
  },

  listHarborContainers: () =>
    request<{
      docker_available: boolean;
      error: string | null;
      containers: HarborContainerInfo[];
    }>('/ui/harbor/containers'),

  tailHarborContainer: (
    containerId: string,
    opts: {
      upToStep?: number | null;
      inspectMinute?: number | null;
      revealSecret?: boolean;
    },
  ) => {
    const q = new URLSearchParams({
      reveal_secret: String(Boolean(opts.revealSecret)),
    });
    if (opts.upToStep != null) q.set('up_to_step', String(opts.upToStep));
    if (opts.inspectMinute != null) q.set('inspect_minute', String(opts.inspectMinute));
    return request<ReplayPayload>(`/ui/harbor/containers/${containerId}?${q.toString()}`);
  },

  compareBaseline: (opts: {
    task?: string;
    seed: number;
    difficulty: number;
    policy: string;
    sessionId?: string | null;
    replayPath?: string | null;
    upToStep?: number | null;
    inspectMinute?: number | null;
    revealSecret?: boolean;
  }) => {
    const q = new URLSearchParams({
      task: opts.task ?? 'oncall@1',
      seed: String(opts.seed),
      difficulty: String(opts.difficulty),
      policy: opts.policy,
      reveal_secret: String(Boolean(opts.revealSecret)),
    });
    if (opts.sessionId) q.set('session_id', opts.sessionId);
    if (opts.replayPath) q.set('replay_path', opts.replayPath);
    if (opts.upToStep != null) q.set('up_to_step', String(opts.upToStep));
    if (opts.inspectMinute != null) q.set('inspect_minute', String(opts.inspectMinute));
    return request<BaselineComparePayload>(`/ui/compare?${q.toString()}`);
  },
};
