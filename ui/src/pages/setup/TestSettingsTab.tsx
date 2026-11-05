// The release gate and cost limits for one agent: p50 / p95, repeats, pass rule, coverage, budget...
// Empty fields use what the profile extends, or Vocto's defaults.
import type { ReactNode } from "react";
import { Block, Grid, Num, Problems, SaveBar, type Obj } from "../../components/form";
import { bandWords } from "../../components/quality";
import { Card, Field, Switch, Table } from "../../components/ui";
import { useApp } from "../../lib/app";
import type { MetricDef } from "../../lib/types";
import { asObj, useDraft, type TabProps } from "./common";

type Row = [key: string, label: string, hint: ReactNode, step?: number];
const GATE: Row[] = [
  ["latency_p50_ms", "Typical answer time, p50 (ms)", "Half of the agent's answers must come within this. Voice runs only. Production agents: about 800."],
  ["latency_p95_ms", "Slow answer time, p95 (ms)", "95% of answers must come within this. Voice runs only. Production agents: about 1500."],
  ["repeats", "Repeats per scenario", "Each scenario is run this many times: agents are not deterministic."],
  ["pass_k_required", "Repeats a critical scenario must pass", "Default: all of them (pass^k)."],
  ["min_coverage", "Minimum verified share (0 to 1)", "Calls that end PASS or FAIL (not error / not verified) out of all planned. Below it: NO-GO, not enough evidence.", 0.05],
  ["judge_min_labels", "Labelled calls needed per judge item", "Calls you labelled for an item before the judges can be trusted on it. More labels, more trustworthy."],
  ["judge_min_fails", "Of those, failures needed (and as many passes)", "So the judges are tested on real problems, not only on good calls. Bug-switch runs give you failures to label."],
  ["judge_min_tpr", "Real failures the judges must catch (0 to 1)", "Of the calls you labelled Fail, the share the judges failed too. Below it, the item only gives an opinion.", 0.05],
  ["judge_min_tnr", "Good calls the judges must pass (0 to 1)", "Of the calls you labelled Pass, the share the judges passed too. Below it, the item only gives an opinion.", 0.05],
  ["max_harness_retries", "Retries after a Vocto error", "A call that fails because of Vocto itself is run again (never blamed on the agent)."],
];
// The regression gate's defaults (src/vatf/compare.py DEFAULTS) and what each change means.
const GATE_DEFAULTS: Record<string, Obj> = {
  new_failures: { blocks: true }, latency: { blocks: false, increase_pct: 20, increase_ms: 150 },
  scores: { blocks: false, drop_points: 10 }, metrics: { blocks: false }, judges: { blocks: false, drop_points: 20 }, safety: { blocks: false },
};
const REGRESSION: [string, string, string, [string, string][]][] = [
  ["new_failures", "New failures", "A test that passed every repeat in the baseline fails at least once.", []],
  ["latency", "Slower answers", "p50 or p95 response time rose by more than both limits (voice runs).", [["increase_pct", "%"], ["increase_ms", "ms"]]],
  ["scores", "Lower scores", "Accuracy, Experience, Security or Safety fell by this many points or more.", [["drop_points", "points"]]],
  ["metrics", "Call quality", "A call quality metric moved to a worse band (good to warn, warn to bad).", []],
  ["judges", "AI judge items", "An item's pass rate fell by this many points or more.", [["drop_points", "points"]]],
  ["safety", "Safety", "A safety test fails a check it passed in the baseline (both runs with safety tests).", []],
];
const LIMITS: Row[] = [
  ["max_turns", "Most caller turns per call", "Stops a conversation that goes round in circles."],
  ["follow_up_turns", "Turns Claude adds after a script's fixed lines", "Live runs: Claude listens to the agent and carries on the call (pushes on in a safety test, finishes an order). 0: calls end with their fixed lines."],
  ["max_call_seconds", "Longest call (seconds)", ""],
  ["max_reply_tokens", "Longest simulated-caller reply (tokens)", "Keeps the simulated caller's turns short and cheap."],
  ["budget_usd", "Budget per run (USD)", "Claude spend for the simulated caller and checks; the run stops when it is reached.", 0.5],
  ["end_of_turn_ms", "Caller waits this long after the agent stops (ms)", "Voice: silence that means the agent has finished speaking."],
  ["no_reply_ms", "Caller waits this long for an answer (ms)", "Voice: then it reacts (\"Hello, are you there?\")."],
];

export function TestSettingsTab({ detail, reload }: TabProps) {
  const { system } = useApp();
  const d = useDraft({ thresholds: asObj(detail.raw.thresholds), limits: asObj(detail.raw.limits), metrics: asObj(detail.raw.metrics),
    regression: asObj(detail.raw.regression) }, detail.file, detail.project, reload);
  // Regression gate (Phase 6): Vocto's defaults, then a profile this one extends, then this file.
  const gateOwn = (k: string) => asObj(asObj(d.draft.regression)[k]);
  const gateBase = (k: string) => ({ ...asObj(GATE_DEFAULTS[k]), ...asObj(asObj(detail.base.regression)[k]) });
  const gateVal = (k: string, f: string) => (gateOwn(k)[f] ?? gateBase(k)[f]) as number | boolean;
  const setGate = (k: string, f: string, v: number | boolean | undefined) => {
    const row = { ...gateOwn(k) } as Obj;
    if (v === undefined || v === gateBase(k)[f]) delete row[f]; else row[f] = v;
    const all = { ...asObj(d.draft.regression) } as Obj;
    if (Object.keys(row).length) all[k] = row; else delete all[k];
    d.setDraft({ ...d.draft, regression: all });
  };
  const gateEnabled = (asObj(d.draft.regression).enabled ?? asObj(detail.base.regression).enabled ?? true) as boolean;
  const own = (id: string) => asObj(asObj(d.draft.metrics)[id]);
  const base = (id: string) => asObj(asObj(detail.base.metrics)[id]);
  const setMetric = (id: string, key: "good" | "warn" | "blocks", v: number | boolean | undefined) => {
    const row = { ...own(id) } as Obj;
    if (v === undefined || (v === false && !base(id).blocks)) delete row[key]; else row[key] = v;
    const all = { ...asObj(d.draft.metrics) } as Obj;
    if (Object.keys(row).length) all[id] = row; else delete all[id];
    d.setDraft({ ...d.draft, metrics: all });
  };
  const metricPh = (m: MetricDef, key: "good" | "warn") => {
    const fromBase = base(m.id)[key];
    if (fromBase != null) return String(fromBase);
    const def = key === "good" ? m.default_good : m.default_warn;
    return def == null ? "no band" : String(def);
  };
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
      <Block title="Call quality" sub="Each metric is read off every call's recording and gets a good, warn or bad band. A metric only stops a release when Blocks is on and the run's value is in the bad band; otherwise it is a warning on the report. An empty field uses the default shown in grey.">
        <div id="metric-bands">
          <Table compact head={["Metric", "Good", "Warn", "Blocks"]}>
            {(system?.metrics || []).map((m) => (
              <tr key={m.id} data-metric={m.id}>
                <td className="min-w-[220px]">
                  <b>{m.title}</b>{m.voice_only && <span className="text-muted text-[11.5px]"> · voice</span>}
                  <div className="text-muted text-[11.5px]">{m.about}</div>
                  <div className="text-muted text-[11.5px]">{m.unit === "%" ? "percent" : m.unit} · {m.better === "higher" ? "higher is better" : "lower is better"}{m.default_good != null ? ` · default ${bandWords({ ...m, good: m.default_good, warn: m.default_warn })}` : ""}</div>
                </td>
                <td className="w-[130px]"><Num id={`metric-${m.id}-good`} value={own(m.id).good as number} placeholder={metricPh(m, "good")} min={0}
                  onChange={(v) => setMetric(m.id, "good", v)} /></td>
                <td className="w-[130px]"><Num id={`metric-${m.id}-warn`} value={own(m.id).warn as number} placeholder={metricPh(m, "warn")} min={0}
                  onChange={(v) => setMetric(m.id, "warn", v)} /></td>
                <td className="w-[90px]">
                  <label><Switch id={`metric-${m.id}-blocks`} checked={!!(own(m.id).blocks ?? base(m.id).blocks)} label=""
                    onChange={(v) => setMetric(m.id, "blocks", v)} /></label>
                </td>
              </tr>
            ))}
          </Table>
        </div>
      </Block>
      <Block title="Regression gate" sub="Every run is compared with the agent's baseline (Results › Set as baseline). Each change for the worse is listed on the run's report; the ones with Blocks on also turn GO into NO-GO. With 3 repeats, small timing and score changes are mostly noise, so only new failures block unless you switch more on. Runs made under different conditions are compared but never blocked.">
        <label><Switch id="gate-on" checked={gateEnabled} label="Compare runs with the baseline" onChange={(v) => {
          const all = { ...asObj(d.draft.regression) } as Obj;
          if (v) delete all.enabled; else all.enabled = false;
          d.setDraft({ ...d.draft, regression: all });
        }} /></label>
        <div id="gate-rules">
          <Table compact head={["Change", "Limit", "Blocks"]}>
            {REGRESSION.map(([k, title, about, limits]) => (
              <tr key={k} data-gate={k}>
                <td className="min-w-[240px]"><b>{title}</b><div className="text-muted text-[11.5px]">{about}</div></td>
                <td className="min-w-[200px]">
                  {limits.length ? (
                    <div className="flex gap-2">
                      {limits.map(([f, unit]) => (
                        <label key={f} className="flex items-center gap-1.5 text-[12.5px] text-muted">
                          <span className="w-[88px]"><Num id={`gate-${k}-${f}`} min={0} value={gateOwn(k)[f] as number}
                            placeholder={String(gateBase(k)[f])} onChange={(v) => setGate(k, f, v)} /></span>{unit}
                        </label>
                      ))}
                    </div>
                  ) : <span className="text-muted text-[12px]">any</span>}
                </td>
                <td className="w-[90px]"><label><Switch id={`gate-${k}-blocks`} checked={!!gateVal(k, "blocks")} label=""
                  onChange={(v) => setGate(k, "blocks", v)} /></label></td>
              </tr>
            ))}
          </Table>
        </div>
      </Block>
      <Problems items={d.problems} />
      <SaveBar id="save-settings" dirty={d.dirty} busy={d.busy} onSave={() => void d.save()} onRevert={d.revert} />
    </Card>
  );
}
