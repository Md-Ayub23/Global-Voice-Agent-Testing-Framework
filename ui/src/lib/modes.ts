// Test modes (src/vatf/modes.py) and the plain words of the release readiness report (src/vatf/readiness.py).
import type { IconName } from "../components/Icon";
import type { AreaStatus, Decision, Finding, ReadinessLevel, TestModeId } from "./types";

export const MODE_ICON: Record<string, IconName> = {
  full: "play-circle", functional: "check-circle", timing: "clock", speech: "mic", safety: "shield",
  resilience: "bolt", load: "activity", regression: "compare", monitor: "phone",
};
export const MODE_TITLE: Record<TestModeId, string> = {
  full: "Full check", functional: "Functional", timing: "Timing & turns", speech: "Speech & accents", safety: "Safety & security",
  resilience: "Resilience", load: "Load & capacity", regression: "Regression", monitor: "Real calls",
};
export const modeTitle = (m: string | null | undefined) => MODE_TITLE[(m || "full") as TestModeId] || "Test run";

export type LevelTone = "good" | "warn" | "bad" | "neutral";
export const LEVEL: Record<ReadinessLevel, { tone: LevelTone; icon: IconName; short: string }> = {
  ready: { tone: "good", icon: "check", short: "Ready" },
  ready_with_notes: { tone: "good", icon: "check", short: "Ready, with notes" },
  not_ready: { tone: "bad", icon: "x", short: "Not ready" },
  not_enough_evidence: { tone: "warn", icon: "alert", short: "Not enough evidence" },
  no_result: { tone: "neutral", icon: "minus", short: "No result" },
};

// Modes that test one thing: a clean result there reads "Passed", not "Ready" (only the Full check and
// Regression run everything). Same list as FOCUSED_PASS in src/vatf/readiness.py.
export const FOCUSED: string[] = ["functional", "timing", "speech", "safety", "resilience", "load"];
export function levelShort(level: ReadinessLevel | null | undefined, mode?: string | null): string {
  const lv = LEVEL[level || "no_result"];
  if (mode && FOCUSED.includes(mode) && (level === "ready" || level === "ready_with_notes")) {
    return level === "ready" ? "Passed" : "Passed, with notes";
  }
  return lv.short;
}

// The release decision in plain words, where only the decision code is known (lists, charts).
export const DECISION_WORDS: Record<Decision, string> = {
  GO: "Ready", NO_GO: "Not ready", NO_GO_INSUFFICIENT_EVIDENCE: "Not enough evidence",
};
export const decisionWords = (d: string | null | undefined) => DECISION_WORDS[d as Decision] || "No result";
// A run's badge label: "Passed" for a clean run of a mode that tests one thing, else the decision in words.
export const runLabel = (r: { decision?: string | null; test_mode?: string | null }) =>
  r.decision === "GO" && FOCUSED.includes(r.test_mode || "") ? "Passed" : undefined;

export const AREA_STATUS: Record<AreaStatus, { tone: LevelTone; icon: IconName; label: string }> = {
  pass: { tone: "good", icon: "check", label: "Good" },
  watch: { tone: "warn", icon: "alert", label: "Watch" },
  fail: { tone: "bad", icon: "x", label: "Problem" },
  not_tested: { tone: "neutral", icon: "minus", label: "Not tested" },
};

export function severityOf(f: Finding): { label: string; tone: LevelTone; icon: IconName } {
  if (f.status === "needs_review") return { label: "Needs you", tone: "warn", icon: "help" };
  if (f.status === "opinion") return { label: "Judges' opinion", tone: "neutral", icon: "info" };
  if (f.status === "evidence") return { label: "Evidence gap", tone: "warn", icon: "alert" };
  if (f.blocks) return { label: "Blocks release", tone: "bad", icon: "x" };
  return f.severity === "minor" ? { label: "Minor", tone: "warn", icon: "alert" } : { label: "Major", tone: "warn", icon: "alert" };
}

export const TONE_TEXT: Record<LevelTone, string> = { good: "text-good", warn: "text-warn", bad: "text-bad", neutral: "text-ink-2" };
export const TONE_SOFT: Record<LevelTone, string> = {
  good: "bg-good-soft text-good", warn: "bg-warn-soft text-warn", bad: "bg-bad-soft text-bad", neutral: "bg-surface-3 text-ink-2",
};
export const TONE_BAR: Record<LevelTone, string> = { good: "bg-good", warn: "bg-warn", bad: "bg-bad", neutral: "bg-line-strong" };
