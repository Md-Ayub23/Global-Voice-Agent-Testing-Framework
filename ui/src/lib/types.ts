// The shapes the dashboard's JSON API returns (src/vatf/web/data.py, jobs.py).

export type Mode = "text" | "voice";
export type Decision = "GO" | "NO_GO" | "NO_GO_INSUFFICIENT_EVIDENCE";
export type CallStatus = "PASS" | "FAIL" | "ERROR" | "NOT_VERIFIED" | "NEEDS_REVIEW";
export type Options = Record<string, string>;

export interface System {
  version: string;
  python: string;
  platform: string;
  root: string;
  runs_dir: string;
  default_model: string;
  api_key: { set: boolean; hint: string | null; source: string | null };
  models: { folder: string; piper_voices: string[]; whisper: { name: string; bytes: number }[]; bytes: number };
  active_job: Job | null;
}

export interface Scenario {
  id: string;
  title: string;
  criticality: string;
  style: string;
  modes: string[];
  in_mode: boolean;
  persona: string | null;
  repeats: number;
  file: string;
  expected: string | null;
  checks: number;
  faults: string[];
  audio: { noise?: string; snr_db?: number; barge_in?: unknown } | null;
}

export interface Profile {
  file: string;
  ok: boolean;
  problems: string[];
  id?: string;
  name?: string;
  description?: string;
  mode: Mode;
  connection: { type: string; target: string; launch: string };
  options: Options;
  offline_options: Options;
  bugs: { name: string; help: string }[] | null;
  thresholds?: { repeats?: number; latency_p50_ms?: number; latency_p95_ms?: number };
  tools?: { name: string; effect: string }[];
  backend?: string;
  logs?: string;
  domain?: string;
  scenarios: Scenario[];
  personas?: { id: string; description: string }[];
}

export interface Project {
  path: string;          // where it lives; used in API calls only, never shown
  key: string;           // the short id in the dashboard's links
  name: string;
  description: string;
  domain: string;
  example: boolean;      // one of Vocto's own practice agents
  channels: Mode[];
  ready: boolean;
  profiles: Profile[];
}

export type DecisionDay = { day: string; none: number } & Record<Decision, number>;

export interface ProjectStats {
  runs: number;
  decided: number;
  go: number;
  no_go: number;
  go_rate: number | null;
  calls: number;
  cost_usd: number;
  last_run: RunSummary | null;
  last_decided: RunSummary | null;
  history: Decision[];
  bug_runs: number;
  latency: { p50_ms: number | null; p95_ms: number | null; at: string } | null;
  latency_limits: { p50_ms: number | null; p95_ms: number | null } | null;
  scenarios: number;
  critical: number;
  personas: number;
}

export interface Dashboard {
  totals: {
    projects: number; client_projects: number; examples: number; runs: number; runs_7d: number; decided: number;
    go: number; no_go: number; insufficient: number; go_rate: number | null; bug_runs: number; calls: number; cost_usd: number; scenarios: number;
  };
  agent_types: { domains: { domain: string; projects: number }[]; channels: Record<Mode, number> };
  activity: DecisionDay[];
  projects: (Pick<Project, "key" | "name" | "description" | "domain" | "example" | "channels" | "ready"> & { stats: ProjectStats })[];
  recent_runs: RunSummary[];
}

export interface ProjectOverview { project: Project; stats: ProjectStats; activity: DecisionDay[]; recent_runs: RunSummary[] }

export interface Secret { name: string; projects: string[]; set: boolean; hint: string | null }

export interface RunSummary {
  run_id: string;
  started_at: string;
  finished_at: string | null;
  status: string;
  decision: Decision | null;
  profile_id: string | null;
  profile_name: string | null;
  project: string | null;
  profile_file: string | null;
  mode: Mode | null;
  offline: boolean | null;
  agent_options: Options | null;
  pii_masked: boolean | null;
  planned: number | null;
  calls_started: number;
  counts: Partial<Record<CallStatus, number>>;
  p50_ms: number | null;
  p95_ms: number | null;
  cost_usd: number | null;
  has_report: boolean;
  project_key?: string | null;
  project_name?: string | null;
}

export interface LatencyTurn { seq: number; turn: number; ms: number | null; counted: boolean; note: string }
export interface Metrics {
  turns?: number;
  duration_ms?: number;
  latency_turns?: LatencyTurn[];
  latency_p50_ms?: number | null;
  latency_p95_ms?: number | null;
}

export interface CallBrief {
  test_id: string;
  scenario_id: string;
  repeat: number;
  attempt: number;
  persona_id: string | null;
  mode: Mode;
  started_at: string;
  cost_usd: number | null;
  status: CallStatus | null;
  headline: string | null;
  failed_checks: string[];
  metrics: Metrics | null;
  final: boolean;
}

export interface Limits { p50: number; p95: number }

export interface DecisionDoc {
  decision: Decision;
  exit_code: number;
  reasons: { rule: string; message: string; refs?: string[] }[];
  scorecard: Record<string, { score: number | null; passed: number; failed: number; total: number; coverage: number }>;
  reliability: { scenario_id: string; criticality: string; k: number; passed: number; failed: number; pass_hat_k: boolean | null }[];
  latency: { turns_measured: number; p50_ms: number | null; p95_ms: number | null; p50_limit_ms: number; p95_limit_ms: number; within_limits: boolean | null };
  coverage: { planned_calls: number; verified_calls: number; error_calls: number; ratio: number; min_required: number };
}

export interface RunInfo {
  run_id: string;
  status: string;
  framework_version: string;
  project: string;
  profile_file: string;
  mode: Mode;
  offline: boolean;
  repeats: number | null;
  scenario_ids: string[];
  agent_options: Options;
  caller_model: string | null;
  pii_masked: boolean;
  plan: { scenario_id: string; persona_id: string | null; repeat: number }[];
  speech?: { engine: string; ears_model: string; check_model: string } | null;
}

export interface RunDetail {
  summary: RunSummary;
  info: RunInfo | null;
  decision: DecisionDoc | null;
  calls: CallBrief[];
  files: Record<string, boolean>;
}

export interface Evidence { event: number; note?: string }
export interface Check {
  check_id: string;
  result: string;
  severity: string;
  section: string;
  message: string;
  evidence?: Evidence[];
  evidence_level: string;
  can_block?: boolean;
}
export interface Verdict {
  status: CallStatus;
  outcome?: { class: string } | null;
  checks: Check[];
  metrics: Metrics;
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type EventData = Record<string, any>;
export interface TraceEvent { seq: number; t_ms: number; type: string; turn?: number | null; data: EventData }

export interface CallRecord {
  test_id: string;
  run_id: string;
  scenario_id: string;
  repeat: number;
  attempt: number;
  mode: Mode;
  persona_id: string | null;
  started_at: string;
  locked?: boolean;
  events_sha256?: string;
  cost_usd?: number | null;
}

export interface CallDetail {
  record: CallRecord;
  live: boolean;
  pii_masked: boolean;
  events: TraceEvent[];
  verdict: Verdict | null;
  story: { seq: number; who: string; text: string }[];
  judgement: { label: string; text: string }[];
  database: { probe: string; rows: Record<string, unknown>[] }[];
  recording: { url: string; duration_ms: number } | null;
  files: string[];
  llm_logs: number;
}

export interface ExitRow { label: string; bug: string | null; scenario_id: string | null; ok: boolean; detail: string; run_id: string; decision: Decision | null }
export interface ExitSummary { mode: Mode; passed: boolean; finished: boolean; rows: ExitRow[] }

export interface Job {
  id: string;
  kind: "run" | "exit-check";
  request: { project?: string; profile?: string; offline?: boolean };
  command: string;
  started_at: string;
  finished_at: string | null;
  status: "running" | "finished" | "failed" | "stopped";
  exit_code: number | null;
  run_ids: string[];
  labels: Record<string, string>;
  line_count: number;
  summary?: ExitSummary | null;
}

export interface ExitCheckRecord extends ExitSummary { job_id: string; started_at?: string | null; project?: string | null }

export interface ProjectFile { file: string; kind: string; id?: string; title?: string }
export interface CheckResult { ok: boolean; syntax: boolean; problems: string[]; saved?: boolean }
