// The AI judges' API (src/vatf/web/judging.py) and the words the pages use for them.
import { api } from "./api";
import { domainLabel } from "./links";
import type { CallLabels, Check, JudgesPage, Layer, QueueItem, Verdict, Vote } from "./types";

export const judgesApi = {
  page: (key: string) => api.get<JudgesPage>(`/api/project/${key}/judges`),
  queue: (key: string) => api.get<QueueItem[]>(`/api/project/${key}/labels`),
  callLabels: (runId: string, testId: string) => api.get<CallLabels>(`/api/runs/${runId}/calls/${testId}/labels`),
  save: (runId: string, testId: string, labels: Record<string, Vote | null>, notes: Record<string, string> = {}) =>
    api.post<{ verdict: Verdict; decision: string } & CallLabels>("/api/labels", { run_id: runId, test_id: testId, labels, notes }),
  teach: (key: string, body: { label_key?: string; why?: string; remove?: boolean; item?: string; said?: string }) =>
    api.post<{ saved: boolean }>(`/api/project/${key}/judges/examples`, body),
};

/** How a judge result reads: Pass / Problem / Needs you, with opinions (items not trusted yet) in grey. */
export type JudgeState = { tone: "good" | "bad" | "warn" | "neutral"; label: string; icon: "check" | "x" | "help" | "minus" | "info"; kind: string };
export function judgeState(c: Check): JudgeState {
  const trusted = !!c.can_block || !!c.judge?.human;
  if (c.result === "NEEDS_REVIEW") return { tone: "warn", label: "Needs you", icon: "help", kind: "the judges split: your label decides" };
  if (c.result === "NOT_VERIFIED") return { tone: "neutral", label: "No answer", icon: "minus", kind: "a judge gave no usable answer" };
  if (c.result === "NOT_APPLICABLE") return { tone: "neutral", label: "Didn't come up", icon: "minus", kind: "" };
  if (c.result === "PASS") return { tone: "good", label: "Pass", icon: "check", kind: trusted ? "" : "opinion" };
  if (c.result === "FAIL") return trusted ? { tone: "bad", label: "Problem", icon: "x", kind: "counts" }
    : { tone: "neutral", label: "Opinion: fail", icon: "info", kind: "opinion, not trusted yet: it doesn't change the result" };
  return { tone: "neutral", label: c.result, icon: "minus", kind: "" };
}

export const LAYER_WORDS: Record<Layer, string> = {
  all_checks: "every check", hard_checks: "hard checks (tools, database, words said)", trusted_judges: "trusted AI judges",
  reliability: "a critical scenario that failed a repeat", latency: "response time", evidence: "not enough evidence",
};

export const VOTE_LABEL: Record<Vote, string> = { PASS: "Pass", FAIL: "Fail", NOT_APPLICABLE: "Doesn't apply" };
export const JUDGE_NAME = { primary: "First judge", secondary: "Second judge", tiebreak: "Deciding judge" } as const;

export function packLabel(pack: string): string {
  if (pack === "universal") return "Every agent";
  if (pack === "custom") return "This agent";
  if (pack === "scenario") return "One scenario";
  if (pack === "safety") return "Safety tests";
  return domainLabel(pack);
}

/** "claude-sonnet-5-5" -> "Sonnet 5.5" */
export function modelName(model: string | undefined | null): string {
  const m = (model || "").match(/^claude-([a-z]+)-(\d+)-(\d+)/);
  return m ? `${m[1][0].toUpperCase()}${m[1].slice(1)} ${m[2]}.${m[3]}` : model || "—";
}

export const MODELS = ["claude-sonnet-5-5", "claude-haiku-5-5", "claude-opus-5-5"];
export const pctOf = (v: number | null | undefined) => (v == null ? "—" : `${Math.round(v * 100)}%`);
