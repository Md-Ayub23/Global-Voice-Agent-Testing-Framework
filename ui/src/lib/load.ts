// Load tests (Phase 7): the stages a load test's settings make, the same way src/vatf/load.py makes
// them, so the Load test page can show the plan while the settings are edited (before they are saved).
import type { LoadSettings, LoadShape, LoadStage } from "./types";

export const SHAPES: { value: LoadShape; label: string; about: string }[] = [
  { value: "ramp", label: "Ramp", about: "Start low and add calls in steps up to the target: finds how many calls at once the agent can take." },
  { value: "spike", label: "Spike", about: "Normal traffic, then a sudden jump to the target, then back down: checks it copes and recovers." },
  { value: "soak", label: "Soak", about: "Hold the target for a long time, read in windows: finds slow leaks (answers getting slower over time)." },
  { value: "steady", label: "Steady", about: "Hold the target for a while: a plain check at the busiest time you expect." },
  { value: "find_limit", label: "Find the limit", about: "Step up like a ramp and stop at the first level that breaks a limit: finds where the agent breaks and your headroom over your peak." },
];
export const SHAPE_LABEL: Record<LoadShape, string> = { ramp: "Ramp", spike: "Spike", soak: "Soak", steady: "Steady", find_limit: "Find the limit" };

// Little's law, as src/vatf/load.py: calls at once = calls per minute x minutes per call, rounded up.
export function peakFromHour(callsPerHour: number, callMinutes: number): number {
  return Math.max(1, Math.ceil((callsPerHour / 60) * callMinutes - 1e-9));
}

const MAX_CALLS_AT_ONCE = 200;
const MAX_TOTAL_MINUTES = 24 * 60;

// Python's round(): halves go to the even number (so the levels match the server's exactly).
function pyRound(x: number): number {
  const f = Math.floor(x);
  const d = x - f;
  if (Math.abs(d - 0.5) < 1e-9) return f % 2 === 0 ? f : f + 1;
  return Math.round(x);
}

export function levels(start: number, target: number, steps: number): number[] {
  if (steps <= 1 || start >= target) return [target];
  const out: number[] = [];
  for (let i = 0; i < steps; i++) {
    const lv = pyRound(start + ((target - start) * i) / (steps - 1));
    if (!out.length || lv > out[out.length - 1]) out.push(lv);
  }
  return out;
}

const whole = (v: unknown) => typeof v === "number" && Number.isInteger(v) && v >= 1;
const nonNeg = (v: unknown) => typeof v === "number" && Number.isFinite(v) && v >= 0;

// The settings' problems, in the server's words (it checks again when saving).
export function problems(s: LoadSettings): string[] {
  const out: string[] = [];
  for (const k of ["calls_at_once", "start_at", "steps", "max_calls"] as const) {
    if (!whole(s[k])) out.push(`${k.replace(/_/g, " ")} must be a whole number of 1 or more`);
  }
  if (s.calls_at_once > MAX_CALLS_AT_ONCE) out.push(`calls at once can be at most ${MAX_CALLS_AT_ONCE}`);
  if ((s.shape === "ramp" || s.shape === "spike") && s.start_at > s.calls_at_once) out.push("the starting level must not be above the target");
  for (const k of ["step_minutes", "minutes", "before_minutes", "after_minutes", "window_minutes", "pause_seconds"] as const) {
    if (!nonNeg(s[k])) out.push(`${k.replace(/_/g, " ")} must be a number of 0 or more`);
  }
  if (s.limits.p95_ms != null && !(s.limits.p95_ms > 0)) out.push("the p95 limit must be a positive number of milliseconds, or empty");
  if (s.peak_calls_per_hour && !s.call_minutes) out.push("with peak calls per hour, also give the average call length");
  return out;
}

export function stages(s: LoadSettings): { stages: LoadStage[]; error: string | null } {
  const bad = problems(s);
  if (bad.length) return { stages: [], error: bad[0] };
  const out: LoadStage[] = [];
  let t = 0;
  const add = (name: string, kind: string, level: number, minutes: number) => {
    const secs = minutes * 60;
    if (secs <= 0) return;
    out.push({ index: out.length, name, kind, level, start_s: t, seconds: secs, end_s: t + secs });
    t += secs;
  };
  const target = s.calls_at_once;
  if (s.shape === "ramp" || s.shape === "find_limit") {
    const lv = levels(s.start_at, target, s.steps);
    lv.forEach((level, i) => add(`Step ${i + 1} of ${lv.length}`, "step", level, s.step_minutes));
  } else if (s.shape === "spike") {
    const normal = Math.min(s.start_at, target);
    add("Before the spike", "normal", normal, s.before_minutes);
    add("Spike", "spike", target, s.minutes);
    add("Recovery", "recovery", normal, s.after_minutes);
  } else if (s.shape === "soak") {
    const total = s.minutes, window = s.window_minutes || s.minutes;
    const n = Math.max(1, Math.ceil(total / window - 1e-9));
    for (let i = 0; i < n; i++) add(`Window ${i + 1} of ${n}`, "window", target, Math.min(window, total - i * window));
  } else {
    add("Hold", "hold", target, s.minutes);
  }
  if (!out.length) return { stages: [], error: "this load test has no time in it: set its minutes above 0" };
  if (t > MAX_TOTAL_MINUTES * 60) return { stages: [], error: `a load test can last at most ${MAX_TOTAL_MINUTES / 60} hours` };
  return { stages: out, error: null };
}

// A rough count of the calls a load test makes: each caller's call takes about `callSeconds`.
export function roughCalls(st: LoadStage[], callSeconds: number, pauseSeconds: number, maxCalls: number): number {
  const per = Math.max(1, callSeconds + pauseSeconds);
  const n = st.reduce((a, x) => a + x.level * Math.max(1, Math.ceil(x.seconds / per)), 0);
  return Math.min(n, maxCalls);
}

export function fmtSeconds(s: number): string {
  if (s < 60) return `${Math.round(s)} s`;
  const m = s / 60;
  if (m < 60) return `${Number.isInteger(m) ? m : m.toFixed(1)} min`;
  const h = Math.floor(m / 60), r = Math.round(m % 60);
  return r ? `${h} h ${r} min` : `${h} h`;
}

export const WHY_WORDS: Record<string, string> = {
  "couldn't connect": "couldn't connect", "agent error": "agent error", "stopped answering": "stopped answering", "ran too long": "ran too long",
};
