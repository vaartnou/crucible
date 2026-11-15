export type ToolKind = 'read' | 'action' | 'clock' | 'end';

export interface ToolInfo {
  name: string;
  description: string;
  kind: ToolKind;
  input_schema: Record<string, unknown>;
}

export interface CommandTemplate {
  id: string;
  label: string;
  template: string;
  cost: number;
  kind: 'read' | 'action';
  needs_service: boolean;
  needs_revision: boolean;
}

export interface WorkbenchMeta {
  tasks: string[];
  default_task: string;
  policies: Record<string, string[]>;
  tools: Record<string, ToolInfo[]>;
  fault_kinds: string[];
  remediations: string[];
  command_templates: CommandTemplate[];
  eval_salt_configured: boolean;
}

export interface PodSample {
  minute: number;
  service: string;
  pod: string;
  version: string;
  state: string;
  request_rate: number;
  error_ratio: number;
  p99_latency: number;
  memory: number;
  pool_in_use: number;
  queue_depth: number;
  message: string;
}

export interface GodService {
  name: string;
  kind: string;
  dependencies: string[];
  replicas: number;
  capacity: number;
  base_latency_ms: number;
  version: string;
  status: string;
  request_rate: number;
  error_ratio: number;
  p99_latency: number;
  memory: number;
  pool_in_use: number;
  queue_depth: number;
  restarted_at: number | null;
  pods: PodSample[];
}

export interface EdgeRecord {
  minute?: number;
  caller: string;
  callee: string;
  request_rate: number;
  attempt_rate: number;
  error_ratio: number;
  latency: number;
  concurrency: number;
  pool_in_use: number;
  timeout_ms: number;
  pool_size: number;
  retries: number;
  offered_attempt_rate: number;
  pool_rejection_ratio: number;
  solver_residual?: number;
  solver_iterations?: number;
  observed_at?: number;
}

export interface ChangeRecord {
  id: string;
  minute: number;
  service: string;
  kind: string;
  summary?: string;
  before?: Record<string, number>;
  after?: Record<string, number>;
  callee?: string | null;
  author: string;
  reverted: boolean;
  is_root_cause?: boolean;
  observed_at?: number;
}

export interface AlertRecord {
  id: string;
  service: string;
  severity: number;
  error_ratio: number;
  started_at: number;
  observed_at?: number;
}

export interface TraceSpan {
  span_id: string;
  parent_id: string | null;
  depth: number;
  caller: string | null;
  service: string;
  kind: string;
  start_ms: number;
  duration_ms: number;
  effective_wait_ms: number;
  self_ms: number;
  timeout_ms: number | null;
  error_ratio: number;
  edge_error_ratio: number | null;
  pod_status: string;
  sample_message: string;
  request_rate: number;
  offered_attempt_rate: number | null;
  attempt_rate: number | null;
  retries: number;
  pool_in_use: number | null;
  pool_size: number | null;
  pool_rejection_ratio: number;
  queue_depth: number;
  memory_mib: number;
  tags: string[];
}

export interface CallPathTrace {
  trace_id: string;
  branch: string;
  minute: number;
  root_service: string;
  leaf_service: string;
  total_latency_ms: number;
  max_error_ratio: number;
  has_error: boolean;
  bottleneck: string;
  spans: TraceSpan[];
}

export interface GodState {
  minute: number;
  selected_minute: number;
  min_minute: number;
  max_minute: number;
  horizon: number;
  calls: number;
  max_steps: number;
  penalty: number;
  repaired: boolean;
  stampede_until: number | null;
  pending_action: { operation: string; target: string; revision: string } | null;
  reverted_changes: string[];
  report: Record<string, string> | null;
  services: GodService[];
  edges: EdgeRecord[];
  alerts: AlertRecord[];
  changes: ChangeRecord[];
  traces: CallPathTrace[];
  timeline: {
    minutes: number[];
    actual_loss: number[];
    baseline_loss: number[];
    service_heatmap: Record<string, number[]>;
  };
  secret: {
    culprit: string;
    fault: string;
    change_id: string;
    expected_remediation: string;
  } | null;
}

export interface FogService {
  name: string;
  kind: string | null;
  dependencies: string[] | null;
  metrics: Record<string, number>;
  observed_at: number | null;
}

export interface FogPod {
  pod: string;
  service: string;
  ready?: string;
  state?: string;
  restarts?: number;
  version?: string;
  metrics: Record<string, number>;
  observed_at?: number;
}

export interface FogState {
  current_minute: number;
  topology_known: boolean;
  services: FogService[];
  pods: FogPod[];
  edges: EdgeRecord[];
  alerts: AlertRecord[];
  changes: ChangeRecord[];
  events: Array<{ pod: string; reason: string; minute: number; observed_at?: number }>;
  logs: Array<{
    service: string;
    minute: number;
    pod: string;
    level: string;
    message: string;
    observed_at?: number;
  }>;
  series: Record<string, Array<{ name: string; minute: number; value: number }>>;
  actions_taken: Array<{
    step: number;
    command: string;
    observed_at: number;
    minute: number;
    message: string;
    revision: string;
  }>;
}

export interface SerializedStep {
  step: number;
  call: { name: string; args: Record<string, unknown> };
  result: {
    ok: boolean;
    content: { minute?: number; observed_at?: number; data?: unknown } | string | null;
    error: string | null;
  };
  observed_at: number;
  minute_after: number;
  cost_minutes: number;
  thought?: string;
}

export interface ScoreData {
  total: number;
  breakdown: Record<string, number>;
  flags: Record<string, boolean>;
}

export interface WorkbenchSession {
  session_id: string;
  task: string;
  seed: number;
  difficulty: number;
  salted: boolean;
  mode: 'human' | 'policy' | 'mcp';
  actor_label: string;
  instruction: string;
  status: 'waiting' | 'running' | 'paused' | 'grading' | 'closed';
  paused: boolean;
  step_delay_ms: number;
  steps_used: number;
  max_steps: number;
  selected_step: number;
  total_steps: number;
  started_at: string;
  error: string | null;
  mcp_path: string;
  steps: SerializedStep[];
  all_steps: SerializedStep[];
  fog_state: FogState;
  god_state: GodState | null;
  outcome: {
    stop_reason: string;
    score: ScoreData;
    solved: boolean;
  } | null;
}

export interface SavedRunItem {
  id: string;
  path: string;
  filename: string;
  group: string;
  source_kind: 'jsonl' | 'harbor';
  live: boolean;
  task: string;
  policy: string;
  seed: number;
  salted: boolean;
  steps: number;
  score: number | null;
  breakdown: Record<string, number>;
  flags: Record<string, boolean>;
  stop_reason: string | null;
  started_at: string;
  ended_at: string | null;
  mtime: number;
}

export interface ReplayPayload {
  path: string;
  source_kind: string;
  live: boolean;
  header: {
    task: string;
    policy: string;
    seed: number;
    salted: boolean;
    started_at: string;
    job_id: string;
  };
  footer: {
    score: ScoreData;
    steps: number;
    stop_reason: string;
    error: string | null;
    ended_at: string;
  } | null;
  total_steps: number;
  selected_step: number;
  steps: SerializedStep[];
  all_steps: SerializedStep[];
  fog_state: FogState;
  god_state: GodState | null;
  salt_status: string;
  has_matched_world: boolean;
  agent_logs: string | null;
  compose_project?: string;
  container_id?: string;
}

export interface HarborContainerInfo {
  container_id: string;
  container_name: string;
  image: string;
  compose_project: string;
  working_dir: string;
  task: string;
  seed: number;
  salted: boolean;
  status: string;
  health: string;
  started_at: string;
  steps: number;
  episode_closed: boolean;
  agent_container: { id: string; name: string; image: string } | null;
}

export interface BaselineComparePayload {
  policy: string;
  task: string;
  seed: number;
  difficulty: number;
  total_steps: number;
  selected_step: number;
  steps: SerializedStep[];
  all_steps: SerializedStep[];
  score: ScoreData;
  stop_reason: string;
  fog_state: FogState;
  god_state: GodState | null;
}

export interface HarborJob {
  id: string;
  path: string;
  status: 'running' | 'completed' | 'failed' | 'unknown';
  agent: string;
  model: string | null;
  completed_trials: number;
  total_trials: number | null;
}

export interface HarborJobsState {
  jobs_dir: string;
  exports_dir: string;
  harbor_available: boolean;
  exports: Array<{ id: string; name: string; tasks: number }>;
  jobs: HarborJob[];
}
