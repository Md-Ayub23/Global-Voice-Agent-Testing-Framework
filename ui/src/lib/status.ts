// One place for what each status looks like: tone + icon + plain-English label.
// Colour never carries a status alone: every badge pairs an icon with words.

export type Tone = "good" | "bad" | "warn" | "neutral" | "run";
export type StatusIcon = "check" | "x" | "alert" | "help" | "minus" | "stop" | null;

const STATUS: Record<string, [Tone, StatusIcon, string]> = {
  GO: ["good", "check", "Ready"],
  NO_GO: ["bad", "x", "Not ready"],
  NO_GO_INSUFFICIENT_EVIDENCE: ["warn", "alert", "Not enough evidence"],
  PASS: ["good", "check", "Pass"],
  FAIL: ["bad", "x", "Fail"],
  ERROR: ["warn", "alert", "Vocto error"],
  NOT_VERIFIED: ["warn", "help", "Not verified"],
  NEEDS_REVIEW: ["warn", "help", "Needs review"],
  NOT_APPLICABLE: ["neutral", "minus", "N/A"],
  running: ["run", null, "Running"],
  finished: ["good", "check", "Finished"],
  stopped: ["neutral", "stop", "Stopped"],
  interrupted: ["neutral", "stop", "Interrupted"],
  failed: ["bad", "alert", "Vocto failed"],
  incomplete: ["neutral", "minus", "Incomplete"],
  passed: ["good", "check", "Passed"],
  not_passed: ["bad", "x", "Not passed"],
};

export function statusInfo(s: string | null | undefined): [Tone, StatusIcon, string] {
  return STATUS[s || ""] || ["neutral", "minus", s || "—"];
}
export const tone = (s: string | null | undefined): Tone => statusInfo(s)[0];
export const statusLabel = (s: string | null | undefined) => statusInfo(s)[2];
// The release decision in plain words (the codes GO / NO_GO stay in files, CI and the API).
export const decisionTitle = (d: string | null | undefined) =>
  ({ GO: "Ready", NO_GO: "Not ready", NO_GO_INSUFFICIENT_EVIDENCE: "Not enough evidence" } as Record<string, string>)[d || ""] || "—";
