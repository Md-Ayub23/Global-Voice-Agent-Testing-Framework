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
  agent_process?: boolean;  // Vocto can start and stop the agent itself (agent_process)
  planted_bugs?: PlantedBug[];
  judges?: boolean;        // the AI judges are on for this profile
  safety?: SafetyBrief;    // the built-in safety tests, in short
  thresholds?: { repeats?: number; latency_p50_ms?: number; latency_p95_ms?: number };
  tools?: { name: string; effect: string }[];
  backend?: string;
  logs?: string;
  domain?: string;
  scenarios: Scenario[];
  personas?: { id: string; description: string }[];
}

// A bug planted in a real agent's own code or prompts, and the scenario (or judge item) that must catch it.
export interface PlantedBug { id: string; title: string; scenario: string; judge?: string | null }

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

export interface JudgeSummary { items: number; calibrated: number; labelled_calls: number; to_review: number; to_label: number; judged_runs: number; to_teach?: number }
export interface ProjectOverview { project: Project; stats: ProjectStats; activity: DecisionDay[]; recent_runs: RunSummary[]; judges?: JudgeSummary }

// ---- AI judges (src/vatf/web/judging.py) ----
export type Vote = "PASS" | "FAIL" | "NOT_APPLICABLE";
export interface RubricItem {
  id: string; title: string; question: string; pass_when: string; fail_when: string; applies_when: string;
  section: string; severity: string; pack: string; hash: string; needs?: string;
}
export interface Calibration {
  rubric_item: string; title: string; labelled_calls: number; stale_labels: number; labels: Record<Vote, number>;
  kappa: number | null; kappa_primary: number | null; kappa_secondary: number | null; agreement: number | null;
  tpr: number | null; tnr: number | null; caught: number; fails: number; cleared: number; passes: number; examples: number;
  can_block: boolean; status: string; min_labels: number; min_fails: number; min_tpr: number; min_tnr: number;
}
export type JudgeName = "primary" | "secondary" | "tiebreak";
export interface Disagreement {
  key: string; run_id: string; test_id: string; label: Vote; judges: string | null; excerpt: string; note: string;
  reasons: Partial<Record<JudgeName, string>>;
}
export interface Thresholds { min_labels: number; min_fails: number; min_tpr: number; min_tnr: number; min_kappa: number }
export interface JudgeItemRow extends RubricItem {
  scenarios: string[];
  calibration: Calibration;
  recent: Record<"PASS" | "FAIL" | "NEEDS_REVIEW" | "NOT_APPLICABLE" | "NOT_VERIFIED", number>;
  true_pass_rate: number | null;
  examples: { said: string; answer: Vote; why: string }[];
  disagreements: Disagreement[];
}
export interface CustomItem { id: string; title?: string; question: string; pass_when?: string; fail_when?: string; applies_when?: string; section?: string; severity?: string }
export interface JudgesPage {
  project: { key: string; name: string; domain: string; example: boolean };
  settings: { enabled: boolean; primary_model: string; secondary_model: string; tiebreak: boolean; tiebreak_model: string;
    policy: string; items: string[] | null; custom: CustomItem[] };
  thresholds: Thresholds;
  items: JudgeItemRow[];
  summary: JudgeSummary;
  catalog: { universal: RubricItem[]; packs: Record<string, RubricItem[]> };
  suggested_pack: string | null;
  default_items: string[];
  problems: string[];
}
export interface QueueItem {
  run_id: string; test_id: string; scenario_id: string; repeat: number; status: CallStatus; started_at: string;
  bugs: string; items: number; labelled: number; needs_review: number; fails: number; reason: string; rank: number;
}
export interface CallLabels { labels: Record<string, Vote | null>; relevant: string[] }

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
  needs_review?: number;
  judge_fails?: number;
  safety?: { calls: number; passed: number; failed: number; to_review?: number } | null;
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
  judge_flags?: string[];   // AI judge opinions (items not trusted yet) that said fail: they didn't change the result
  needs_you?: string[];     // judge items split between the judges: a person's label is wanted
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
  coverage: { planned_calls: number; verified_calls: number; error_calls: number; needs_review_calls?: number; ratio: number; min_required: number };
  judges?: { rubric_item: string; title: string; kappa: number | null; labelled_calls: number; can_block: boolean; calibration: string;
    passed: number; failed: number; needs_review: number; not_applicable: number }[];
  relabelled_at?: string;
  safety?: SafetyResult;
  basis?: { decided_by: Layer[]; judge_opinions: number; opinions_to_label?: number; trusted_items?: number };
  heard_numbers?: { numbers: number; heard_right: number; misheard: { test_id: string; seq: number; said: string; heard: string }[] };
}
export type Layer = "all_checks" | "hard_checks" | "trusted_judges" | "reliability" | "latency" | "evidence";

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
  judges?: { enabled?: boolean; why?: string; primary_model?: string; secondary_model?: string; tiebreak?: boolean; tiebreak_model?: string } | null;
  safety?: boolean;
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
  origin?: string;
  judge?: {
    rubric_item: string; title?: string; question?: string; item_hash?: string; applicable: boolean; calibration?: string;
    kappa?: number | null; tpr?: number | null; tnr?: number | null; labelled_calls?: number; human?: Vote;
    votes: { judge: JudgeName; model: string; result: Vote; reason: string; cites?: number[] }[];
  };
}
export interface Verdict {
  test_id?: string;
  run_id?: string;
  scenario_id?: string;
  status: CallStatus;
  outcome?: { class: string } | null;
  checks: Check[];
  metrics: Metrics & { judge_cost_usd?: number };
  judge_notes?: string[];
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

export interface ExitRow { label: string; bug: string | null; title?: string; scenario_id: string | null; ok: boolean; detail: string; run_id: string; decision: Decision | null }
export interface ExitSummary {
  mode: Mode; passed: boolean; finished: boolean; rows: ExitRow[];
  planned?: string[];                        // clean, then each bug, in order
  planted?: boolean;                         // a real agent restarted with each planted bug
  skipped?: { bug: string; why: string }[];
  error?: string;
}

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

// Built-in safety tests (src/vatf/attacks.py, web/safety.py).
export interface SafetyBrief { enabled: boolean; intensity: Intensity; attacks: number; calls: number; packs: number }
export type Intensity = "quick" | "standard" | "thorough";
export interface SafetyFinding {
  test_id: string; scenario_id: string; pack: string; title: string; check_id: string;
  severity: string; result: string; can_block: boolean; message: string;
}
export interface SafetyResult {
  calls: number; passed: number; failed: number; to_review?: number;
  packs: Record<string, { calls: number; failed: number; to_review?: number }>;
  findings: SafetyFinding[];
}
export interface SafetyPack { id: string; title: string; rubric: string; what: string; count: number; total: number }
export interface SafetyAttack {
  id: string; pack: string; title: string; level: Intensity; lines: string[]; adaptive: boolean; hard_checks: number; on: boolean;
  custom: boolean;
}
/** One of your own safety questions (profile safety.custom). */
export interface SafetyQuestion { pack?: string; title?: string; lines?: string[]; never_say?: string[]; safe_answer?: string; blocks?: boolean }
export interface SafetyPage {
  settings: { enabled: boolean; intensity: Intensity; off_topic: "strict" | "friendly"; packs: string[]; canaries?: string[]; skip?: string[]; custom?: SafetyQuestion[] };
  defaults: { enabled: boolean; intensity: Intensity; off_topic: string; packs: string[] };
  industry: string;
  policy_rules: string[];
  packs: SafetyPack[];
  attacks: SafetyAttack[];
  repeats: number;
  calls: number;
  live_only: boolean;
  problems: string[];
  last_run: ({ run_id: string; decision: Decision | null; started_at: string } & SafetyResult) | null;
}
