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
  accents?: { id: string; label: string; voice: string }[];
  /** The languages Vocto's caller speaks (languages.py), each with its own voices. */
  languages?: { id: string; name: string; voices: { id: string; label: string }[] }[];
  conditions?: { id: string; title: string; about: string }[];
  /** Speech mode: the caller conditions a run can compare (line and voice, then accents). */
  sweep_conditions?: SweepCondition[];
  /** Resilience: the kinds of tool failure a run can drill (drills.py). */
  drill_failures?: DrillFailure[];
  metrics?: MetricDef[];
  noises?: string[];
}

// Phase 5: call quality metrics (src/vatf/metrics.py) and caller conditions (conditions.py).
export type Band = "good" | "warn" | "bad" | null;
export type MetricUnit = "ms" | "%" | "wpm" | "turns" | "s";
export interface MetricDef {
  id: string; title: string; unit: MetricUnit; better: "lower" | "higher"; about: string; voice_only: boolean;
  default_good: number | null; default_warn: number | null; good: number | null; warn: number | null; blocks: boolean;
}
export interface QualityValue { value: number | boolean | null; band: Band; n?: number; never?: boolean; note?: string; [k: string]: unknown }
export interface QualityRow {
  id: string; title: string; unit: MetricUnit; better: "lower" | "higher"; value: number | null; band: Band;
  good: number | null; warn: number | null; blocks: boolean; calls: number; about: string; detail?: Record<string, number | string>;
}
export interface SweepCondition { id: string; title: string; about: string; group: "line" | "accent" | "ladder"; default: boolean }
export interface ConditionRow {
  key?: string; label: string; preset?: string | null; lines?: string[];
  calls: number; passed: number; failed: number; pass_rate: number | null;
  details_right?: number; details_checked?: number; readback_right?: number; readback_checked?: number;
  understood_wer?: number | null; understood_words?: number; failed_calls?: string[]; failed_tests?: string[];
  p50_ms: number | null; p90_ms: number | null; talked_over_pct: number | null; behaviours: string[];
}
export interface CallConditions { label: string; preset?: string; voice?: string; accent?: string; behaviours?: string[]; [k: string]: unknown }

export interface Scenario {
  id: string;
  title: string;
  /** The test type it belongs to: its own test_type, or placed by what it does (src/vatf/modes.py type_of). */
  test_type: "functional" | "timing" | "speech" | "resilience";
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
  /** Resilience: the test case can be drilled (a simulated call that ends in a booking or order). */
  drill?: boolean; no_drill_why?: string | null;
  audio: { noise?: string; snr_db?: number; barge_in?: unknown } | null;
}

export interface Profile {
  file: string;
  ok: boolean;
  problems: string[];
  baseline?: string | null;       // Phase 6: the run id of this channel's baseline
  health_checks?: boolean;
  alerts?: number;
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
  pass_rules?: Record<string, PassRule[]>;
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

export type TypeStatus = "passed" | "watch" | "failed" | "evidence" | "not_run" | "unavailable";
export interface ModeBrief { id: TestModeId; question?: string | null; status: TypeStatus; why?: string | null; run_id?: string | null; at?: string | null; headline?: string | null; top_finding?: string | null; runs: number }
export interface ConnectionBrief { kind: string | null; label: string; connected: boolean; text_too: boolean }
export interface TrendPoint { run_id: string; at: string | null; test_mode: string | null; decision: Decision | null; pass_rate: number | null; p50_ms: number | null; p95_ms: number | null; cost_usd: number | null }

export interface Dashboard {
  totals: {
    projects: number; client_projects: number; examples: number; runs: number; runs_7d: number; decided: number;
    go: number; no_go: number; insufficient: number; go_rate: number | null; bug_runs: number; calls: number; cost_usd: number; scenarios: number;
  };
  agent_types: { domains: { domain: string; projects: number }[]; channels: Record<Mode, number> };
  activity: DecisionDay[];
  projects: (Pick<Project, "key" | "name" | "description" | "domain" | "example" | "channels" | "ready"> & { stats: ProjectStats; connection: ConnectionBrief; modes: ModeBrief[] })[];
  recent_runs: RunSummary[];
}

export interface JudgeSummary { items: number; calibrated: number; labelled_calls: number; to_review: number; to_label: number; judged_runs: number; to_teach?: number }
export interface ProjectOverview {
  project: Project; stats: ProjectStats; activity: DecisionDay[]; recent_runs: RunSummary[]; judges?: JudgeSummary; modes?: ModeCard[];
  mode_summary: ModeBrief[]; connection: ConnectionBrief; trend: TrendPoint[];
}

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

/** One side of an A/B test: two agent versions run on the same tests (vatf run --ab-with). */
export interface ABSide { group: string; side: "A" | "B"; label: string; other?: string; other_label?: string }
export interface RunSummary {
  run_id: string;
  ab?: ABSide | null;
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
  trigger?: "manual" | "ci" | "health";
  vs_baseline?: BaselineCounts | null;
  kind?: "run" | "load";
  load?: { shape: LoadShape; calls_at_once: number; capacity?: number | null; calls?: number; failed?: number } | null;
  test_mode?: TestModeId | null;
}

// Phase 6: the comparison with the agent's baseline (src/vatf/compare.py).
export interface BaselineCounts {
  run_id: string | null; decision?: string | null; finished_at?: string | null; like_for_like: boolean;
  regressions: number; worse: number; better: number; new?: number; removed?: number; blocking: number;
  /** Regression upgrade: failed once and didn't repeat on the reruns / flaky over the last 10 runs / edited since the baseline. */
  suspected?: number; flaky?: number; changed?: number; fixed?: number;
  rolling?: string[]; versions?: VersionChange[]; confirmed_by_reruns?: Record<string, number>;
}
/** What the two runs were made with, where it differs (agent version, profile, prompt, judge models...). */
export interface VersionChange { field: string; title: string; before: string | null; after: string | null }
export interface RunTrendPoint { run_id: string; at: string | null; decision: string | null; pass_rate: number | null; p95_ms: number | null; calls: number | null; version: string | null }
export interface FlakyTest { scenario_id: string; title: string; passed: number; calls: number; runs: number; rate: number }
export type Direction = "regression" | "worse" | "better" | "new" | "removed" | "same" | "suspected" | "flaky" | "changed";
export interface Change {
  kind: "test" | "latency" | "score" | "metric" | "judge" | "safety" | "load" | "cost";
  id: string; title: string; direction: Direction; before: number | string | null; after: number | string | null;
  message: string; blocks: boolean; unit?: string; band_before?: string; band_after?: string; criticality?: string;
  flaky_rate?: number;
}
export interface Comparison {
  source: string;
  baseline: { run_id: string | null; finished_at: string | null; decision: string | null; profile_file?: string | null };
  current: { run_id: string | null; finished_at: string | null; decision: string | null };
  like_for_like: boolean;
  differences: { field: string; title: string; before: unknown; after: unknown }[];
  versions?: VersionChange[];
  rolling?: string[];
  confirmed_by_reruns?: Record<string, number>;
  gate_enabled: boolean;
  changes: Change[];
  counts: Record<Direction, number>;
  blocking: number;
}
export interface Snapshot {
  run_id: string; decision: string; finished_at: string | null;
  how: { mode: Mode | null; conditions: string; speech_engine: string | null; offline: boolean; safety: boolean; bugs: string };
  tests: Record<string, { title: string; criticality: string; k: number; passed: number; failed: number; unverified: number }>;
  scores: Record<string, number | null>;
  latency: { p50_ms: number | null; p95_ms: number | null; turns: number } | null;
  /** The agent's running cost at the owner's prices (Set up › Connection › Running costs). */
  cost?: { currency: string; per_task: number | null; per_call: number | null; tasks_done: number; task_calls: number };
}
export interface CompareResult extends Comparison {
  runs: { a: RunSummary; b: RunSummary };
  /** An A/B test of two agent versions: version A is `a`, version B is `b`. */
  ab?: { labels: { A: string; B: string } } | null;
  same_agent: boolean;
  snapshots: { a: Snapshot; b: Snapshot };
}
export interface BaselineBrief {
  run_id: string; decision: string; finished_at: string | null; set_at: string | null; note: string | null;
  warnings: string[] | null; how: Snapshot["how"]; tests: number;
  latency: Snapshot["latency"]; scores: Record<string, number | null> | null;
}
export interface BaselinePage {
  file: string; baseline: BaselineBrief | null; problem: string | null; committed_with_project: boolean;
  candidates: { run_id: string; decision: string; started_at: string; mode: Mode | null; offline: boolean | null; trigger?: string; bugs: boolean }[];
}
export type GateRules = {
  enabled: boolean;
  new_failures: { blocks: boolean };
  latency: { blocks: boolean; increase_pct: number; increase_ms: number };
  scores: { blocks: boolean; drop_points: number };
  metrics: { blocks: boolean };
  judges: { blocks: boolean; drop_points: number };
  safety: { blocks: boolean };
};
export interface AlertTarget { name?: string; type?: "slack" | "discord" | "webhook"; url: string; enabled?: boolean; events?: string[] }
export interface HealthSettings { enabled: boolean; every_minutes: number; scenarios: string[]; repeats: number; conditions: string; judges: boolean; start_agent: boolean }
export interface OpsPage {
  file: string; mode: Mode;
  alerts: AlertTarget[]; alert_events: string[]; alert_types: string[]; default_events: string[];
  health: HealthSettings; health_tests: string[];
  tests: { id: string; title: string; criticality: string }[];
  health_runs: RunSummary[]; conditions: string[];
  regression: GateRules; regression_defaults: Omit<GateRules, "enabled">;
  providers: Record<string, string>; secrets: Record<string, boolean>;
  committed_with_project: boolean; agent_process: boolean;
}
export interface CiFile { path: string; text: string; secrets: string[] }

export interface LatencyTurn { seq: number; turn: number; ms: number | null; counted: boolean; note: string; tool?: boolean; full_wait_ms?: number | null }
export interface Metrics {
  turns?: number;
  duration_ms?: number;
  latency_turns?: LatencyTurn[];
  latency_p50_ms?: number | null;
  latency_p95_ms?: number | null;
  quality?: Record<string, QualityValue>;
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

export interface TimingMoment { test_id: string; scenario_id?: string | null; seq: number; ms: number; tool?: boolean; full_wait_ms?: number | null }

export interface DecisionDoc {
  decision: Decision;
  exit_code: number;
  reasons: { rule: string; message: string; refs?: string[] }[];
  scorecard: Record<string, { score: number | null; passed: number; failed: number; total: number; coverage: number }>;
  reliability: { scenario_id: string; criticality: string; k: number; passed: number; failed: number; pass_hat_k: boolean | null }[];
  latency: {
    turns_measured: number; p50_ms: number | null; p95_ms: number | null; p50_limit_ms: number; p95_limit_ms: number; within_limits: boolean | null;
    p95_enough?: boolean | null; p95_needs?: number; over_p95_limit?: number;
    /** Plain answers and answers that needed a tool, timed apart (tool: also the full wait for the real answer). */
    split?: { plain: { n: number; p50_ms: number | null; p95_ms: number | null };
      tool: { n: number; p50_ms: number | null; p95_ms: number | null; full_p50_ms: number | null; full_p95_ms: number | null; full_n: number } };
    p95_range?: { low_ms: number | null; high_ms: number | null };
    slowest?: TimingMoment[]; slow_stops?: TimingMoment[];
    stages?: Record<string, { title: string; n: number; p50_ms: number | null; p95_ms: number | null }>;
  };
  coverage: { planned_calls: number; verified_calls: number; error_calls: number; needs_review_calls?: number; ratio: number; min_required: number };
  judges?: { rubric_item: string; title: string; kappa: number | null; labelled_calls: number; can_block: boolean; calibration: string;
    passed: number; failed: number; needs_review: number; not_applicable: number }[];
  relabelled_at?: string;
  safety?: SafetyResult;
  basis?: { decided_by: Layer[]; judge_opinions: number; opinions_to_label?: number; trusted_items?: number };
  heard_numbers?: { numbers: number; heard_right: number; misheard: { test_id: string; seq: number; said: string; heard: string }[] };
  quality?: QualityRow[];
  by_condition?: ConditionRow[];
  baseline?: BaselineCounts;
  load?: LoadDecision;
  /** Regression runs: one point per run (newest last) and the flaky tests over the last 10 runs. */
  trend?: RunTrendPoint[];
  flaky?: FlakyTest[];
}
export type Layer = "all_checks" | "hard_checks" | "trusted_judges" | "reliability" | "latency" | "call_quality" | "evidence" | "regression" | "load";

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
  plan: { scenario_id: string; persona_id: string | null; repeat: number; conditions?: string }[];
  judges?: { enabled?: boolean; why?: string; primary_model?: string; secondary_model?: string; tiebreak?: boolean; tiebreak_model?: string } | null;
  safety?: boolean;
  conditions?: string;
  sweep?: string[];
  drills?: string[];
  drill_targets?: string[];
  drill_repeats?: string[];
  /** Every caller spoke this language on this run (languages.py); absent = each caller's own. */
  caller_language?: string;
  trigger?: string;
  baseline?: string;
  speech?: { engine: string; ears_model: string; check_model: string } | null;
  kind?: "run" | "load";
  load?: { shape: LoadShape; calls_at_once: number; seconds: number; peak: number; max_calls: number; stages: LoadStage[];
    settings: LoadSettings; p95_limit_ms: number | null; launched_per_call: boolean } | null;
}

export interface RunDetail {
  summary: RunSummary;
  info: RunInfo | null;
  decision: DecisionDoc | null;
  calls: CallBrief[];
  files: Record<string, boolean>;
  comparison?: Comparison | null;
  load?: LoadSummary | null;
  load_live?: LoadLive | null;
  alerts?: { events: string[]; text: string; sent: { name: string; type: string; events: string[]; sent: boolean; status?: number; error?: string }[] } | null;
  readiness?: Readiness | { error: string } | null;
  analysis?: RunAnalysis | null;
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
  conditions?: CallConditions;
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
  /** A real phone call (connection phone_call): the provider's own record of it. */
  phone?: { provider: string; call_sid: string; to: string; from: string; status: string;
    duration_s: number | null; price: number | null; price_unit: string | null } | null;
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
  kind: "run" | "exit-check" | "health" | "load";
  request: { project?: string; profile?: string; offline?: boolean; test_mode?: string; ab_with?: string };
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
  severity: string; result: string; can_block: boolean; message: string; waiting?: boolean;
}
/** Attack success rate: unsafe / (unsafe + resisted), with its 95% range. */
export interface AttackRate { rate?: number | null; low?: number | null; high?: number | null }
export interface SafetyPackResult extends AttackRate {
  calls: number; failed: number; to_review?: number; resisted?: number; not_landed?: number; errors?: number;
}
export interface OwaspRow extends AttackRate { id: string; title: string; packs: string[]; calls: number; unsafe: number; resisted: number; to_review: number }
export interface SafetyResult extends AttackRate {
  calls: number; passed: number; failed: number; to_review?: number;
  resisted?: number; not_landed?: number; errors?: number; leaks?: number; review?: boolean;
  packs: Record<string, SafetyPackResult>;
  owasp?: OwaspRow[];
  switched_off?: string[];
  findings: SafetyFinding[];
}
export interface SafetyPack { id: string; title: string; rubric: string; what: string; count: number; total: number; owasp?: string[] }
export interface SafetyAttack {
  id: string; pack: string; title: string; level: Intensity; lines: string[]; adaptive: boolean; hard_checks: number; on: boolean;
  custom: boolean; owasp?: string[]; voice_only?: boolean; poisoned?: boolean; language?: string;
}
/** One of your own safety questions (profile safety.custom). */
export interface SafetyQuestion {
  pack?: string; title?: string; lines?: string[]; never_say?: string[]; safe_answer?: string; blocks?: boolean;
  language?: string; from?: string;
}
export interface AttackDraft { n: number; question: SafetyQuestion; why: string; problems: string[] }
export interface SafetyPage {
  settings: {
    enabled: boolean; intensity: Intensity; off_topic: "strict" | "friendly"; packs: string[]; canaries?: string[]; skip?: string[]; custom?: SafetyQuestion[];
    review?: boolean; identity?: { enabled?: boolean; verify?: string; guarded?: string[] } | null; poison?: { tools?: string[]; lines?: string[] } | null;
  };
  defaults: { enabled: boolean; intensity: Intensity; off_topic: string; packs: string[] };
  industry: string;
  policy_rules: string[];
  identity: { verify: string; guarded: string[] } | null;
  poison_tools: string[];
  owasp: { id: string; title: string }[];
  languages: { id: string; name: string }[];
  tools: { name: string; effect: string }[];
  packs: SafetyPack[];
  attacks: SafetyAttack[];
  repeats: number;
  calls: number;
  live_only: boolean;
  problems: string[];
  last_run: ({ run_id: string; decision: Decision | null; started_at: string } & SafetyResult) | null;
}

// Phase 7: load tests (src/vatf/load.py, loadstats.py, loadrun.py).
export type LoadShape = "ramp" | "spike" | "soak" | "steady" | "find_limit";
export type LoadCaller = "free" | "claude";
export interface LoadLimits {
  max_failed_pct: number; p95_ms: number | null;
  slowdown: { blocks: boolean; pct: number; ms: number };
  pass_drop: { blocks: boolean; points: number };
  drift: { blocks: boolean; pct: number };
  recovery: { blocks: boolean; pct: number; ms: number };
  max_lag_ms: number; min_calls: number; min_answers: number;
}
export interface LoadSettings {
  shape: LoadShape; calls_at_once: number; start_at: number; steps: number; step_minutes: number; minutes: number;
  before_minutes: number; after_minutes: number; window_minutes: number; pause_seconds: number; max_calls: number;
  scenarios: string[]; caller: LoadCaller; judges: boolean; conditions: string; start_agent: boolean; limits: LoadLimits;
  /** Calls per hour (Little's law) and the busiest time expected: the target and the headroom sentence. */
  peak_calls_per_hour: number | null; call_minutes: number | null; expected_peak: number | null;
  light: boolean; unique_facts: string[];
}
export interface LoadHeadroom { capacity: number; exact: boolean; peak: number | null; pct: number | null; text: string }
export interface LoadLeak { in_test: string; in_scenario: string | null; seq: number; where: "said" | "tool"; from_test: string; from_scenario: string | null; fact: string; text: string }
export interface PhoneLoadWarning { calls: number; calls_known: boolean; at_once: number; cost: number | null; currency: string | null; provider: string; text: string }
export interface LoadStage { index: number; name: string; kind: string; level: number; start_s: number; seconds: number; end_s: number }
export interface LoadPlan { shape: LoadShape; calls_at_once: number; seconds: number; peak: number; max_calls: number; stages: LoadStage[];
  expected_peak?: number | null; expected_calls?: number | null; warnings?: string[]; phone?: PhoneLoadWarning }
export interface LoadProblem { rule: string; message: string; refs?: string[] }
export interface LoadStageStats extends LoadStage {
  calls: number; finished: number; ok: number; failed: number; harness_errors: number; failed_pct: number | null;
  failed_why: Record<string, number>; failed_refs: string[]; unanswered_turns: number; unanswered_pct: number | null;
  latency: { turns: number; p50_ms: number | null; p90_ms: number | null; p95_ms: number | null; p99_ms: number | null; max_ms: number | null };
  first_word_p95_ms: number | null; tool_p95_ms: number | null; tool_calls: number; tool_errors: number;
  passed: number; failed_tests: number; pass_rate: number | null; blockers: Record<string, string[]>;
  quality: QualityRow[]; lag_p95_ms: number | null; lag_max_ms: number | null;
  in_flight_mean: number | null; in_flight_max: number | null;
  trusted: boolean; untrusted_why: string; problems: LoadProblem[]; meets_limits: boolean;
  too_few?: boolean; p95_enough?: boolean; reached?: boolean;
}
export interface LoadSummary {
  mode: Mode; shape: LoadShape; calls_at_once: number; caller: LoadCaller; max_calls: number; p95_limit_ms: number | null;
  limits: LoadLimits; calls: number; failed: number; harness_errors: number; harness_share: number;
  capacity: number | null; breaking: { level: number; stage: string; why: string[]; untrusted?: boolean } | null;
  stages: LoadStageStats[];
  comparisons: { slowdown?: { low_level: number; top_level: number; low_p95_ms: number; top_p95_ms: number };
    pass_rate?: { low: number; top: number }; drift?: Record<string, number | null>;
    recovery?: { before_p95_ms: number; after_p95_ms: number | null; stage_p95_ms?: number | null; normal_up_to_ms: number; back_to_normal_s: number | null;
      recovered: boolean; before_failed_pct: number | null; after_failed_pct: number | null } };
  blocking: LoadProblem[]; warnings: LoadProblem[]; unattributed_tool_calls: number;
  lag_p95_ms: number | null; lag_max_ms: number | null;
  timeline: [number, number, number][];   // [seconds from the start, calls in progress, Vocto's event-loop lag ms]
  notes: string[];
  expected_peak?: number | null; headroom?: LoadHeadroom; leaks?: LoadLeak[]; light?: boolean;
}
export interface LoadDecision {
  shape: LoadShape; calls_at_once: number; capacity: number | null; breaking: LoadSummary["breaking"];
  calls: number; failed: number; harness_errors: number; p95_limit_ms: number | null; unattributed_tool_calls: number;
  expected_peak?: number | null; headroom?: LoadHeadroom; leaks?: LoadLeak[];
  stages: { index: number; name: string; kind: string; level: number; calls: number; finished: number; failed: number;
    failed_pct: number | null; pass_rate: number | null; trusted: boolean; meets_limits: boolean; p95_ms: number | null; p50_ms: number | null }[];
}
export interface LoadLive {
  finished: number; elapsed_s: number; seconds: number; stage: number; stage_name: string; level: number;
  in_flight: number; started: number; failed: number; harness: number; lag_ms: number; timeline: [number, number, number][];
}
export interface LoadPage {
  file: string; mode: Mode; saved: Partial<LoadSettings>; defaults: Omit<LoadSettings, "limits"> & { limits: LoadLimits };
  settings: LoadSettings | null; plan: LoadPlan | null; tests_used: string[]; error: string | null;
  p95_limit_ms: number | null; voice_p95_ms: number | null;
  tests: { id: string; title: string; criticality: string; faults: boolean; style: string; facts?: string[] }[];
  agent: { launched_per_call: boolean; agent_process: boolean; uses_gateway: boolean; test_id_sent: boolean; test_id_header: string; max_call_seconds: number };
  baseline: (BaselineBrief & { load?: { capacity: number | null; levels: Record<string, unknown> } | null }) | null;
  baseline_problem: string | null;
  runs: RunSummary[];
}

// ---- Phase 8: real calls (src/vatf/monitor, web/monitoring.py) ----------------------------------
export interface MonitorSettings {
  enabled: boolean;
  intake: { token: string | null; listen: boolean; port: number };
  privacy: { redact: string[]; keep_recording_links: boolean; retention_days: number; patterns: { name: string; regex: string }[] };
  scoring: { judges: boolean; judge_share_pct: number; judge_max_per_day: number; judge_budget_usd_per_day: number; always_judge_flagged: boolean };
  alerts: { window_hours: number; min_calls: number; pass_rate_below: number | null; flagged_above: number | null;
    metrics_bad: boolean; safety: boolean; false_success: boolean };
}
export interface MonitorSummary {
  calls: number; passed: number; failed: number; review: number; flagged: number; escalated: number; judged: number;
  pass_rate: number | null; flagged_pct: number | null; escalated_pct: number | null; p90_ms: number | null; answers: number;
  repeat_callers: number;
}
export interface MonitorDay { day: string; calls: number; passed: number; failed: number; flagged: number; escalated: number;
  pass_rate: number | null; flagged_pct: number | null; p90_ms: number | null }
export interface MonitorAlert { title: string; message: string; calls: string[]; since?: string }
export interface MonitorPage {
  agent: string; settings: MonitorSettings; saved: Partial<MonitorSettings>; defaults: MonitorSettings; detectors: string[];
  have_key: boolean; totals: MonitorSummary; window: MonitorSummary;
  week: { this_week: MonitorSummary; last_week: MonitorSummary }; daily: MonitorDay[]; quality: QualityRow[];
  versions: { version: string; first_seen: string | null; calls: number; pass_rate: number | null; flagged_pct: number | null;
    escalated_pct: number | null; p90_ms: number | null }[];
  top_flags: { kind: string; id: string; calls: number }[];
  alerts: { active: Record<string, MonitorAlert>; history: { at: string; id: string; event: string; message: string }[];
    last_sent: { name: string; sent: boolean; error?: string }[] };
  pending: number; judging: boolean;
  intake: { token_name: string | null; token_set: boolean; listen: boolean; port: number; url: string | null; running: boolean;
    error: string | null; received: number };
  metric_defs: MetricDef[];
  project: { key: string; name: string; domain: string };
}
export interface MonitorFlag { kind: "check" | "judge" | "review" | "metric"; id: string; section?: string; trusted?: boolean; review?: boolean; text?: string }
export interface MonitorRow {
  call_id: string; started_at: string | null; imported_at: string; source: string; agent_version: string | null; mode: Mode;
  duration_s: number; turns: number; status: CallStatus; flagged: boolean; flags: MonitorFlag[]; flag_text: string[];
  escalated: boolean; judge_state: "pending" | "done" | "skipped"; redacted: number; summary: string;
}
export interface MonitorCallDetail {
  record: { call_id: string; source: string; received_via: string; started_at: string | null; imported_at: string;
    agent_version: string | null; mode: Mode; duration_ms: number | null; ended_reason: string | null; recording_url: string | null;
    redacted: Record<string, number>; external_id: string | null; caller_hash: string | null };
  events: TraceEvent[];
  verdict: (Verdict & { flags: MonitorFlag[]; flagged: boolean; judges: { state: string; note: string; cost_usd?: number } }) | null;
  story: { seq: number; who: string; text: string }[];
  metric_defs: MetricDef[];
  judge_items: Record<string, string>;
}

// ---- Test modes (src/vatf/modes.py) and the release readiness report (src/vatf/readiness.py) ----
export type TestModeId = "full" | "functional" | "timing" | "speech" | "safety" | "resilience" | "load" | "regression" | "monitor";
export type ReadinessLevel = "ready" | "ready_with_notes" | "not_ready" | "not_enough_evidence" | "no_result";
export type AreaStatus = "pass" | "watch" | "fail" | "not_tested";
export interface ModeDef {
  id: TestModeId; title: string; icon: string; kind: "run" | "load" | "compare" | "monitor"; voice_only: boolean;
  question: string; about: string; checks: string[]; areas: string[];
}
export interface ModeCard extends ModeDef {
  available: { ok: boolean; why?: string; fixable?: boolean };
  runs: number;
  latest: { run_id: string; at: string | null; decision: Decision; level: ReadinessLevel | null; headline: string | null; top_finding: string | null; calls: number | null } | null;
}
export interface ScoreRow { area: string; title: string; status: AreaStatus; status_text: string; value: string; detail: string; percent?: number | null; range?: string | null }
export interface Proof { test_id: string; check_id: string | null; seq: number | null; at: string | null; who: string; quote: string | null; note: string }
export interface Finding {
  id: string; title: string; area: string; area_title: string; kind: string;
  status: "problem" | "needs_review" | "opinion" | "evidence"; severity: "blocker" | "major" | "minor" | "advice"; blocks: boolean;
  what: string; expected: string; fix: string; caution: string; cause: "agent" | "setup" | "judges" | "opinion" | "evidence"; cause_text: string;
  how_often: { calls: number; of: number; text: string };
  tests: { id: string; title: string; calls: number }[];
  calls: string[]; messages: string[]; proofs: Proof[];
}
export interface MetricLine { group: string; title: string; value: string; note: string; band: Band | null; rate?: number | null; range?: string | null }
export interface Readiness {
  version: number; run_id: string; agent: string | null; channel: Mode;
  mode: { id: TestModeId; title: string; question: string };
  verdict: { level: ReadinessLevel; headline: string; line: string; code: Decision | null };
  summary: string[];
  scorecard: ScoreRow[];
  findings: Finding[];
  counts: { blocking: number; problems: number; to_review: number; opinions: number; evidence: number };
  metrics: MetricLine[];
  coverage: {
    test_cases_run: number; test_cases_total: number; not_run: string[]; attack_calls: number; attack_kinds: string[];
    tools_total: number; tools_used: string[]; tools_never_used: string[]; conditions: string[];
    calls_planned: number; calls_verified: number | null;
  };
  method: { label: string; value: string }[];
  /** Voice: results by caller condition (Speech mode compares them side by side). */
  conditions?: ConditionResult[];
  /** Resilience failure drills: how the calls ended, per kind of tool failure. */
  failures?: FailureResult[];
  drill_grid?: DrillGrid | null;
  speech?: SpeechSummary | null;
  timing?: Record<string, unknown> | null;
  /** Functional: the job itself in numbers (every task step, readiness.py _functional). */
  functional?: FunctionalResult | null;
}
export interface FunctionalResult {
  tasks: { k: number; n: number; range: string | null; unchecked: number };
  untrue_claims: number;
  saved: { k: number; n: number; range: string | null };
  other_changes: { calls: number; of: number };
  wrong_tools: number;
  read_by_ai: number;
  ends: { key: string; title: string; parts: { class: string; label: string; count: number; good: boolean }[] }[];
  intents: { id: string; title: string; passed: number; failed: number; range: string | null }[];
}
export interface DrillFailure { id: string; title: string; about: string; default: boolean; expected: string;
  lookup?: boolean; every?: boolean; refusal?: boolean; text_only?: boolean }
export interface FailureResult {
  key: string; failure?: string; target?: "action" | "lookup"; repeat?: "once" | "every";
  title: string; about: string; status: "handled" | "struggled" | "broke" | "no_result"; status_text: string;
  result: string; pass_rate: number | null; ended: string; recovered: number; honest: number; false_claims: number;
  duplicates: number; expected: string; failed_tests: string[]; proof: string | null;
  retries?: number; max_retries?: number; early_429?: number; too_many_tries?: number;
  longest_silence_ms?: number | null; informed?: string | null;
}
export interface DrillGrid {
  columns: { key: string; title: string }[];
  rows: { id: string; title: string; cells: Record<string, { state: "pass" | "fail" | "other"; text: string; why: string; call: string | null }> }[];
}
export interface ConditionResult {
  key: string; title: string; lines: string[]; status: "handled" | "struggled" | "broke" | "no_result"; status_text: string;
  result: string; pass_rate: number | null; understanding: string; understood_wer: number | null; p50: string | null;
  worse_than_clean: boolean; failed_tests: string[]; proof: string | null;
  vs_clean?: string | null; by_type?: Record<string, { right: number; checked: number }>;
  read_back_before_save?: number | null; saves_checked?: number | null; too_hard_calls?: number;
}
export interface RateRange { k: number; n: number; rate: number; low: number; high: number; text: string }
export interface LadderStep { level: number; calls: number; passed: number; failed: number; pass_rate: number | null; details_right: number; details_checked: number; ok: boolean; failed_calls: string[] }
export interface SpeechSummary {
  details?: RateRange | null;
  by_type?: { type: string; title: string; right: number; checked: number; rate: RateRange | null }[];
  read_back?: RateRange | null; too_hard_calls?: number; voices?: "synthetic" | "real" | "mixed" | null;
  wrong?: { test_id: string; scenario_id?: string | null; fact: string; type: string; said: string | null; saved: string | null; seq?: number | null; condition?: string | null }[];
  ladder?: Record<"noise" | "loss", { unit: string; levels: LadderStep[]; works_to: number | null; text: string }> | null;
}

// One rule of a test type's "What passes" card (src/vatf/modes.py pass_rules).
export interface PassRule { text: string; blocks: boolean }

// The AI analysis of a run (src/vatf/analysis.py). Always AI, never part of the decision.
export interface RunAnalysisCause { title: string; explanation: string; who: string; fix: string; confidence: string; findings: string[]; calls: string[]; blocks: boolean }
export interface RunAnalysis { run_id: string; created_at: string; model: string; cost_usd: number; fingerprint: string; stale?: boolean; summary: string; check_first: string; causes: RunAnalysisCause[]; not_covered: string[] }
