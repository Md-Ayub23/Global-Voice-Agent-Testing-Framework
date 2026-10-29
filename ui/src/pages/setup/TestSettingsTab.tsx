// The release gate and cost limits for one agent: p50 / p95, repeats, pass rule, coverage, budget...
// Empty fields use what the profile extends, or Vocto's defaults.
import type { ReactNode } from "react";
import { Block, Grid, Num, Problems, SaveBar, type Obj } from "../../components/form";
import { Card, Field } from "../../components/ui";
import { asObj, useDraft, type TabProps } from "./common";

type Row = [key: string, label: string, hint: ReactNode, step?: number];
const GATE: Row[] = [
  ["latency_p50_ms", "Typical answer time, p50 (ms)", "Half of the agent's answers must come within this. Voice runs only. Production agents: about 800."],
  ["latency_p95_ms", "Slow answer time, p95 (ms)", "95% of answers must come within this. Voice runs only. Production agents: about 1500."],
  ["repeats", "Repeats per scenario", "Each scenario is run this many times: agents are not deterministic."],
  ["pass_k_required", "Repeats a critical scenario must pass", "Default: all of them (pass^k)."],
  ["min_coverage", "Minimum verified share (0 to 1)", "Calls that end PASS or FAIL (not error / not verified) out of all planned. Below it: NO-GO, not enough evidence.", 0.05],
  ["judge_min_kappa", "Judge agreement needed to block (kappa)", "AI judges (Phase 3) below this agreement with human labels only advise.", 0.05],
  ["max_harness_retries", "Retries after a Vocto error", "A call that fails because of Vocto itself is run again (never blamed on the agent)."],
];
const LIMITS: Row[] = [
  ["max_turns", "Most caller turns per call", "Stops a conversation that goes round in circles."],
  ["max_call_seconds", "Longest call (seconds)", ""],
  ["max_reply_tokens", "Longest simulated-caller reply (tokens)", "Keeps the simulated caller's turns short and cheap."],
  ["budget_usd", "Budget per run (USD)", "Claude spend for the simulated caller and checks; the run stops when it is reached.", 0.5],
  ["end_of_turn_ms", "Caller waits this long after the agent stops (ms)", "Voice: silence that means the agent has finished speaking."],
  ["no_reply_ms", "Caller waits this long for an answer (ms)", "Voice: then it reacts (\"Hello, are you there?\")."],
];

export function TestSettingsTab({ detail, reload }: TabProps) {
  const d = useDraft({ thresholds: asObj(detail.raw.thresholds), limits: asObj(detail.raw.limits) }, detail.file, detail.project, reload);
  const inherited = (section: "thresholds" | "limits", key: string) => {
    const fromBase = asObj(detail.base[section])[key];
    if (fromBase != null) return `${fromBase} (from ${detail.extends})`;
    const def = detail.defaults[section][key];
    return def == null ? (key === "pass_k_required" ? "all repeats" : "") : `${def} (default)`;
  };
  const set = (section: "thresholds" | "limits", key: string, v: number | undefined) => {
    const next = { ...asObj(d.draft[section]) } as Obj;
    if (v === undefined) delete next[key]; else next[key] = v;
    d.setDraft({ ...d.draft, [section]: next });
  };
  const rows = (section: "thresholds" | "limits", list: Row[]) => (
    <Grid>
      {list.map(([key, label, hint, step]) => (
        <Field key={key} label={label} hint={hint}>
          <Num id={`${section}-${key}`} value={asObj(d.draft[section])[key] as number} step={step ?? 1} min={0}
            placeholder={inherited(section, key)} onChange={(v) => set(section, key, v)} />
        </Field>
      ))}
    </Grid>
  );
  return (
    <Card>
      <Block title="Release gate" sub={`What GO means for this agent. An empty field uses ${detail.extends ? "the shared setting or " : ""}Vocto's default (shown in grey).`}>
        {rows("thresholds", GATE)}
      </Block>
      <Block title="Limits" sub="Safety and cost caps for each run.">
        {rows("limits", LIMITS)}
      </Block>
      <Problems items={d.problems} />
      <SaveBar id="save-settings" dirty={d.dirty} busy={d.busy} onSave={() => void d.save()} onRevert={d.revert} />
    </Card>
  );
}
