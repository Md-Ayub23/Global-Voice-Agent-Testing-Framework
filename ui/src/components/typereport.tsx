// The Analysis tab of a run report: the numbers and charts that answer the run's own test type. Functional
// shows each test case's repeats, Timing the answer times, Speech each caller condition, Safety each kind of
// attack, Resilience each kind of tool failure. Everything here comes from the run itself (decision.json,
// the calls' verdicts and the readiness report), so it matches the other tabs exactly.
import type { ReactNode } from "react";
import { KpiStrip } from "./dash";
import { fmtMetric } from "./quality";
import { Tip } from "./feedback";
import { Card, CardHead, Notice } from "./ui";
import { ChangesList, CompareCounts } from "./compare";
import { cx, fmtMs, pct } from "../lib/format";
import { PACK_TITLES } from "../lib/safety";
import type { CallBrief, Comparison, DecisionDoc, QualityRow, Readiness, TestModeId, RunTrendPoint } from "../lib/types";

// ---- small charts (HTML, so they follow the theme and the column width) -------------------------

interface Part { value: number; color: string; label: string }
interface BarRow { key: string; label: ReactNode; sub?: ReactNode; parts: Part[]; right?: ReactNode; href?: string }

const C = { good: "var(--good)", bad: "var(--bad)", warn: "var(--warn)", none: "var(--line-strong)", accent: "var(--accent)" };

function Legend({ items }: { items: [string, string][] }) {
  return (
    <div className="flex gap-3.5 flex-wrap text-[13px] text-ink-2 mt-3">
      {items.map(([label, color]) => <span key={label}><i className="inline-block size-2.5 rounded-[3px] mr-1.5 -mb-px" style={{ background: color }} />{label}</span>)}
    </div>
  );
}

// One horizontal stacked bar per row, all on the same scale (the largest row's total).
export function StackBars({ rows, legend }: { rows: BarRow[]; legend?: [string, string][] }) {
  const max = Math.max(1, ...rows.map((r) => r.parts.reduce((a, p) => a + p.value, 0)));
  return (
    <div>
      <div className="grid gap-2.5">
        {rows.map((r) => {
          const total = r.parts.reduce((a, p) => a + p.value, 0);
          const body = (
            <div className="grid grid-cols-[minmax(120px,38%)_1fr_auto] items-center gap-3">
              <div className="min-w-0"><div className="truncate text-[13px] text-ink">{r.label}</div>{r.sub && <div className="truncate text-[13px] text-muted">{r.sub}</div>}</div>
              <Tip content={<div className="grid gap-0.5 min-w-[140px]">{r.parts.filter((p) => p.value).map((p) => (
                <div key={p.label} className="flex items-center gap-2"><i className="size-2.5 rounded-[3px]" style={{ background: p.color }} />
                  <span className="flex-1 text-ink-2">{p.label}</span><b className="tabular-nums">{p.value}</b></div>))}{!total && <span className="text-muted">Nothing yet</span>}</div>} className="block">
                <div className="grow-x flex h-3.5 rounded-[3px] overflow-hidden bg-surface-3" style={{ width: `${Math.max(4, (total / max) * 100)}%` }}>
                  {r.parts.filter((p) => p.value > 0).map((p, i) => (
                    <i key={p.label} style={{ width: `${(p.value / total) * 100}%`, background: p.color }} className={cx(i > 0 && "border-l-2 border-surface")} />
                  ))}
                </div>
              </Tip>
              <div className="text-[13px] text-ink-2 tabular-nums whitespace-nowrap text-right min-w-[64px]">{r.right}</div>
            </div>
          );
          return r.href ? <a key={r.key} href={r.href} className="hover:no-underline text-ink rounded-md -mx-1.5 px-1.5 py-0.5 hover:bg-surface-2">{body}</a> : <div key={r.key}>{body}</div>;
        })}
      </div>
      {legend && <Legend items={legend} />}
    </div>
  );
}

// How long callers waited for each answer: a histogram of every timed answer, with the limits marked.
export function AnswerTimes({ values, tool, p50Limit, p95Limit }: { values: number[]; tool?: number[]; p50Limit?: number | null; p95Limit?: number | null }) {
  if (!values.length) return <p className="text-muted text-[13px]">No answer was timed in this run.</p>;
  const top = Math.max(...values, p95Limit || 0) * 1.05;
  const step = top <= 2000 ? 200 : top <= 5000 ? 500 : 1000;
  const bins = Math.ceil(top / step);
  const counts = Array.from({ length: bins }, (_, i) => values.filter((v) => v >= i * step && v < (i + 1) * step).length);
  const tools = Array.from({ length: bins }, (_, i) => (tool || []).filter((v) => v >= i * step && v < (i + 1) * step).length);
  const max = Math.max(1, ...counts);
  const over = (i: number) => p95Limit != null && (i + 1) * step > p95Limit;
  return (
    <div>
      <div className="relative flex items-end gap-[3px] h-[150px] border-b border-line" role="img" aria-label={`${values.length} answers timed`}>
        {counts.map((n, i) => (
          <Tip key={i} content={<><b>{n}</b> answer{n === 1 ? "" : "s"} in {fmtMs(i * step)} to {fmtMs((i + 1) * step)}{tool && tools[i] ? <>, {tools[i]} needed a tool</> : null}</>} className="flex-1 h-full flex items-end">
            <span className="flex flex-col w-full rounded-t-[3px] overflow-hidden" style={{ height: `${(n / max) * 100}%`, minHeight: n ? 3 : 0 }}>
              {tool && tools[i] > 0 && <i className="block w-full" style={{ height: `${(tools[i] / n) * 100}%`, background: over(i) ? C.bad : "var(--accent-2)", opacity: 0.75 }} />}
              <i className="block w-full flex-1" style={{ background: over(i) ? C.bad : C.accent }} />
            </span>
          </Tip>
        ))}
        {[p50Limit, p95Limit].map((l, j) => l != null && l <= top && (
          <span key={j} className="absolute top-0 bottom-0 border-l border-dashed" style={{ left: `${(l / (bins * step)) * 100}%`, borderColor: j ? C.bad : C.warn }}>
            <span className="absolute -top-0.5 left-1 text-[13px] whitespace-nowrap" style={{ color: j ? C.bad : C.warn }}>{j ? "slowest limit" : "typical limit"} {fmtMs(l)}</span>
          </span>
        ))}
      </div>
      <div className="flex justify-between text-[13px] text-muted mt-1 tabular-nums"><span>0 s</span><span>{fmtMs(bins * step)}</span></div>
      {tool && tool.length > 0 && <Legend items={[["Plain answer", C.accent], ["Needed a tool", "var(--accent-2)"], ["Over the slowest limit", C.bad]]} />}
    </div>
  );
}

// ---- the analysis per test type -------------------------------------------------------------------

const ms = (xs: number[]) => xs.slice().sort((a, b) => a - b);
const pctl = (xs: number[], p: number) => { const s = ms(xs); return s.length ? s[Math.min(s.length - 1, Math.ceil(p * s.length) - 1)] : null; };

function byCase(finals: CallBrief[], titles: (id: string) => string, callHref: (t: string) => string) {
  const groups = new Map<string, CallBrief[]>();
  for (const c of finals) if (!c.scenario_id.startsWith("safety.")) groups.set(c.scenario_id, [...(groups.get(c.scenario_id) || []), c]);
  return [...groups.entries()].map(([sid, cs]) => {
    const passed = cs.filter((c) => c.status === "PASS").length, failed = cs.filter((c) => c.status === "FAIL").length;
    const other = cs.length - passed - failed;
    const bad = cs.find((c) => c.status === "FAIL");
    return { key: sid, label: titles(sid), sub: bad?.failed_checks?.length ? `failed: ${bad.failed_checks.slice(0, 2).join(", ")}` : undefined,
      parts: [{ value: passed, color: C.good, label: "Passed" }, { value: failed, color: C.bad, label: "Failed" }, { value: other, color: C.none, label: "Not judged" }],
      right: `${passed}/${cs.length}`, href: bad ? callHref(bad.test_id) : undefined, failed };
  }).sort((a, b) => b.failed - a.failed);
}

// Regression: pass rate over the last runs, one dot per run coloured by its result, version labels where they change.
export function TrendChart({ points, runHref }: { points: RunTrendPoint[]; runHref?: (runId: string) => string }) {
  const pts = points.filter((p) => p.pass_rate != null);
  if (pts.length < 2) return <p className="text-muted text-[13px]">The trend shows once there are two or more runs of this type.</p>;
  const W = 640, H = 190, L = 36, R = 12, T = 14, B = 44;
  const x = (i: number) => L + (pts.length === 1 ? 0 : (i * (W - L - R)) / (pts.length - 1));
  const y = (v: number) => T + (1 - v) * (H - T - B);
  const tone = (dec: string | null) => (dec === "GO" ? C.good : dec && dec.startsWith("NO_GO") ? C.bad : C.warn);
  const line = pts.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.pass_rate as number).toFixed(1)}`).join(" ");
  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img" aria-label="Pass rate over the last runs" id="regression-trend" className="overflow-visible">
      {[0, 0.5, 1].map((v) => (
        <g key={v}>
          <line x1={L} x2={W - R} y1={y(v)} y2={y(v)} stroke="var(--line)" strokeDasharray={v === 1 ? undefined : "3 4"} />
          <text x={L - 6} y={y(v) + 4} textAnchor="end" fontSize="11" fill="var(--muted)">{Math.round(v * 100)}%</text>
        </g>
      ))}
      <path d={line} fill="none" stroke={C.accent} strokeWidth={2} />
      {pts.map((p, i) => {
        const changed = i === 0 || p.version !== pts[i - 1].version;
        const dot = <circle cx={x(i)} cy={y(p.pass_rate as number)} r={5} fill={tone(p.decision)} stroke="var(--surface)" strokeWidth={2}>
          <title>{`${p.run_id} · ${pct(p.pass_rate as number)} passed${p.p95_ms != null ? ` · p95 ${fmtMs(p.p95_ms)}` : ""}${p.version ? ` · ${p.version}` : ""}`}</title></circle>;
        return (
          <g key={p.run_id}>
            {runHref ? <a href={runHref(p.run_id)}>{dot}</a> : dot}
            {changed && p.version && <text x={x(i)} y={H - B + 18} textAnchor="middle" fontSize="11" fill="var(--ink-2)">{p.version.length > 14 ? p.version.slice(0, 13) + "…" : p.version}</text>}
            <text x={x(i)} y={H - B + 34} textAnchor="middle" fontSize="10" fill="var(--muted)">{(p.at || "").slice(5, 10)}</text>
          </g>
        );
      })}
    </svg>
  );
}

export function TypeAnalysis({ mode, d, rd, finals, titles, callHref, cmp, runId, runHref }: {
  mode: TestModeId; d: DecisionDoc | null; rd: Readiness | null; finals: CallBrief[];
  titles: (id: string) => string; callHref: (testId: string) => string;
  cmp?: Comparison | null; runId?: string; runHref?: (runId: string) => string;
}) {
  const judged = finals.filter((c) => c.status === "PASS" || c.status === "FAIL");
  const passed = judged.filter((c) => c.status === "PASS").length;
  const cases = byCase(finals, titles, callHref);
  const rel = (d?.reliability || []).filter((r) => !r.scenario_id.startsWith("safety."));
  const steady = rel.filter((r) => r.pass_hat_k).length;
  const answers = finals.flatMap((c) => (c.metrics?.latency_turns || []).filter((t) => t.counted && t.ms != null).map((t) => t.ms as number));
  const lat = d?.latency;
  const callsKpi = { label: "Calls passed", value: judged.length ? `${passed} of ${judged.length}` : "—",
    foot: judged.length ? pct(passed / judged.length) : "no call judged", tone: judged.length ? (passed === judged.length ? "good" as const : "bad" as const) : undefined };
  const caseCard = cases.length > 0 && (
    <Card>
      <CardHead level={2} title="Each test case" sub="every repeat, failed ones first · click a row to open its first failed call" />
      <StackBars rows={cases} legend={[["Passed", C.good], ["Failed", C.bad], ["Not judged", C.none]]} />
    </Card>
  );

  if (mode === "timing") {
    const q = new Map((d?.quality || []).map((r) => [r.id, r]));
    const qv = (id: string) => q.get(id);
    const perCase = cases.map((c) => {
      const xs = finals.filter((f) => f.scenario_id === c.key).flatMap((f) => (f.metrics?.latency_turns || []).filter((t) => t.counted && t.ms != null).map((t) => t.ms as number));
      return { ...c, p95: pctl(xs, 0.95), p50: pctl(xs, 0.5), n: xs.length };
    }).filter((c) => c.p95 != null).sort((a, b) => (b.p95 as number) - (a.p95 as number));
    const lim95 = lat?.p95_limit_ms ?? null;
    const maxP = Math.max(1, ...perCase.map((c) => c.p95 as number), lim95 || 0);
    const tile = (r: QualityRow | undefined, label?: string) => r ? ({ label: label || r.title, value: fmtMetric(r.value, r.unit), foot: `${r.calls} call${r.calls === 1 ? "" : "s"}`,
      tone: r.band === "good" ? "good" as const : r.band === "bad" ? "bad" as const : r.band === "warn" ? "warn" as const : undefined }) : null;
    const plainTimes = finals.flatMap((c) => (c.metrics?.latency_turns || []).filter((t) => t.counted && t.ms != null && !t.tool).map((t) => t.ms as number));
    const toolTimes = finals.flatMap((c) => (c.metrics?.latency_turns || []).filter((t) => t.counted && t.ms != null && t.tool).map((t) => t.ms as number));
    const range = lat?.p95_range;
    const n = lat?.turns_measured ?? answers.length;
    const p95Foot = !n ? "no answer timed" : lat?.p95_enough === false
      ? `too few to judge (${n} of ${lat?.p95_needs ?? 20})${range?.low_ms != null ? ` · at least ${fmtMs(range.low_ms)}` : ""}`
      : `${range?.low_ms != null && range?.high_ms != null ? `likely ${fmtMs(range.low_ms)} to ${fmtMs(range.high_ms)} · ` : ""}${n} answers · limit ${fmtMs(lim95)}`;
    const split = lat?.split;
    const moments = [
      ...(lat?.slowest || []).map((m) => ({ ...m, what: m.tool ? `${fmtMs(m.ms)} wait for an answer that needed a tool${m.full_wait_ms ? ` (${fmtMs(m.full_wait_ms)} to the real answer)` : ""}` : `${fmtMs(m.ms)} wait for an answer`, bad: lim95 != null && m.ms > lim95 })),
      ...(lat?.slow_stops || []).map((m) => ({ ...m, what: `kept talking ${fmtMs(m.ms)} after the caller cut in`, bad: true })),
    ];
    const stages = Object.entries(lat?.stages || {});
    const more = [tile(qv("noise_kept"), "Kept going through a cough or bang"), tile(qv("paused_cut_off"), "Cut the caller off in a pause"),
      tile(qv("repeated_question"), "Said the same thing again"), tile(qv("checkin_ms"), "Checked in after silence"), tile(qv("talked_over"))].filter((x): x is NonNullable<typeof x> => !!x);
    return (
      <div className="grid gap-4" id="type-analysis">
        <KpiStrip items={[
          { label: "Typical wait (p50)", value: fmtMs(lat?.p50_ms ?? pctl(answers, 0.5)), foot: lat?.p50_limit_ms ? `limit ${fmtMs(lat.p50_limit_ms)}` : `${answers.length} answers timed`,
            tone: lat?.p50_ms != null && lat.p50_limit_ms ? (lat.p50_ms <= lat.p50_limit_ms ? "good" as const : "bad" as const) : undefined },
          { label: "Slowest waits (p95)", value: fmtMs(lat?.p95_ms ?? pctl(answers, 0.95)), foot: p95Foot,
            tone: lat?.within_limits == null ? undefined : lat.within_limits ? "good" as const : "bad" as const },
          tile(qv("barge_in_stop_ms"), "Stops when cut in") || { label: "Stops when cut in", value: "—", foot: "nobody cut in" },
          tile(qv("backchannel_kept"), "Keeps going through mm-hm") || { label: "Keeps going through mm-hm", value: "—", foot: "no mm-hm said" },
          tile(qv("longest_silence_ms"), "Longest silence") || { label: "Longest silence", value: "—", foot: "" },
        ]} />
        {more.length > 0 && <KpiStrip items={more.slice(0, 5)} />}
        <div className="grid gap-4 grid-cols-1 xl:grid-cols-2">
          <Card><CardHead level={2} title="How long callers waited" sub="every timed answer · plain answers and answers that needed a tool" />
            <AnswerTimes values={answers} tool={toolTimes} p50Limit={lat?.p50_limit_ms} p95Limit={lim95} />
            {split && (split.tool.n > 0 || split.plain.n > 0) && (
              <table className="w-full text-[13px] mt-3" id="timing-split">
                <thead><tr className="text-muted text-left"><th className="font-medium py-1">Answers</th><th className="font-medium">How many</th><th className="font-medium">Typical</th><th className="font-medium">Slowest (p95)</th></tr></thead>
                <tbody className="tabular-nums">
                  <tr className="border-t border-line"><td className="py-1.5">Plain answers</td><td>{split.plain.n}</td><td>{fmtMs(split.plain.p50_ms)}</td><td>{fmtMs(split.plain.p95_ms)}</td></tr>
                  <tr className="border-t border-line"><td className="py-1.5">Needed a tool: first sound</td><td>{split.tool.n}</td><td>{fmtMs(split.tool.p50_ms)}</td><td>{fmtMs(split.tool.p95_ms)}</td></tr>
                  {split.tool.full_n > 0 && <tr className="border-t border-line"><td className="py-1.5"><Tip content="From the end of the caller's words to the agent's first sound after the tool answered: the real answer, after any “let me check”.">Needed a tool: the real answer</Tip></td><td>{split.tool.full_n}</td><td>{fmtMs(split.tool.full_p50_ms)}</td><td>{fmtMs(split.tool.full_p95_ms)}</td></tr>}
                </tbody>
              </table>
            )}
            {plainTimes.length + toolTimes.length > 0 && <p className="text-[13px] text-muted mt-2">The limits apply to every answer together; the split shows where the time goes.</p>}
          </Card>
          <Card><CardHead level={2} title="Slowest answers per test case" sub="p95 of each test case, slowest first" />
            {perCase.length ? (
              <div className="grid gap-2.5">
                {perCase.map((c) => (
                  <div key={c.key} className="grid grid-cols-[minmax(120px,38%)_1fr_auto] items-center gap-3">
                    <span className="truncate text-[13px]">{c.label}<span className="text-muted"> · {c.n}</span></span>
                    <div className="relative h-3.5 bg-surface-3 rounded-[3px]">
                      <i className="grow-x absolute inset-y-0 left-0 rounded-[3px]" style={{ width: `${((c.p95 as number) / maxP) * 100}%`, background: lim95 && (c.p95 as number) > lim95 ? C.bad : C.accent }} />
                      {lim95 && <i className="absolute -inset-y-1 border-l border-dashed" style={{ left: `${(lim95 / maxP) * 100}%`, borderColor: C.bad }} />}
                    </div>
                    <span className="text-[13px] text-ink-2 tabular-nums min-w-[64px] text-right">{fmtMs(c.p95)}</span>
                  </div>
                ))}
              </div>
            ) : <p className="text-muted text-[13px]">No answer was timed.</p>}
          </Card>
        </div>
        {moments.length > 0 && (
          <Card id="timing-moments"><CardHead level={2} title="Moments to listen to" sub="the slowest waits and slow stops · opens the call at that moment" />
            <div className="grid gap-1.5">
              {moments.map((m, i) => (
                <a key={i} href={callHref(m.test_id) + `?at=${m.seq}`} className="flex items-center gap-2.5 text-[13px] rounded-md bg-surface-2 px-2.5 py-1.5 hover:no-underline text-ink">
                  <i className="size-2 rounded-full flex-none" style={{ background: m.bad ? C.bad : C.accent }} />
                  <span className="flex-1 min-w-0 truncate">{m.what}</span>
                  <span className="text-muted truncate max-w-[40%]">{titles(m.scenario_id || "")}</span>
                </a>
              ))}
            </div>
          </Card>
        )}
        {stages.length > 0 && (
          <Card id="timing-stages"><CardHead level={2} title="Where the time went" sub="from the agent's own logs (Set up › Logs › timing)" />
            <StackBars rows={stages.map(([k, s]) => ({ key: k, label: s.title, sub: `${s.n} measured`, right: `${fmtMs(s.p50_ms)} · p95 ${fmtMs(s.p95_ms)}`,
              parts: [{ value: s.p50_ms || 0, color: C.accent, label: "Typical ms" }] }))} />
          </Card>
        )}
        {caseCard}
      </div>
    );
  }

  if (mode === "speech") {
    const conds = rd?.conditions || [];
    const sp = rd?.speech;
    const heard = d?.heard_numbers;
    const wer = conds.map((c) => c.understood_wer).filter((x): x is number => x != null);
    const lad = sp?.ladder;
    const types = sp?.by_type || [];
    const typeIds = types.map((t) => t.type);
    const cellTone = (r: number, n: number) => !n ? undefined : r === n ? "text-good" : r / n >= 0.8 ? "text-warn" : "text-bad";
    const voicesText = sp?.voices === "real" ? "Real voices: every caller line was a recording of a person."
      : sp?.voices === "mixed" ? "Mixed voices: some caller lines were recordings of people, the rest synthetic."
      : "Synthetic voices: machine voices are cleaner and steadier than real callers, so treat a pass here as the best case. Record real lines (Set up › Recorded caller lines) for a fair test.";
    return (
      <div className="grid gap-4" id="type-analysis">
        <KpiStrip items={[
          { label: "Details right", value: sp?.details ? `${sp.details.k} of ${sp.details.n}` : "—", foot: sp?.details ? sp.details.text : "no detail reached a tool",
            tone: sp?.details ? (sp.details.k === sp.details.n ? "good" as const : "bad" as const) : undefined },
          { label: "Conditions handled", value: conds.length ? `${conds.filter((c) => c.status === "handled").length} of ${conds.length}` : "—", foot: "every call passed" },
          { label: "Works down to", value: lad?.noise?.works_to != null ? `${lad.noise.works_to} dB` : lad?.noise ? "—" : "not tried",
            foot: [lad?.noise ? "noise" : "", lad?.loss ? (lad.loss.works_to != null ? `up to ${lad.loss.works_to}% lost` : "no loss step passed") : ""].filter(Boolean).join(" · ") || "pick the ladder on Run",
            tone: lad?.noise ? (lad.noise.works_to != null && lad.noise.works_to <= 10 ? "good" as const : "warn" as const) : undefined },
          { label: "Read back before saving", value: sp?.read_back ? `${sp.read_back.k} of ${sp.read_back.n}` : "—", foot: sp?.read_back ? sp.read_back.text : "nothing saved with caller details",
            tone: sp?.read_back ? (sp.read_back.k === sp.read_back.n ? "good" as const : "warn" as const) : undefined },
          heard?.numbers ? { label: "Numbers heard right", value: `${heard.heard_right} of ${heard.numbers}`, foot: pct(heard.heard_right / heard.numbers), tone: heard.heard_right === heard.numbers ? "good" as const : "bad" as const }
            : callsKpi,
        ]} />
        <p className="text-[13px] text-ink-2 -mt-1" id="speech-voices">{voicesText}{sp?.too_hard_calls ? ` ${sp.too_hard_calls} call${sp.too_hard_calls === 1 ? " was" : "s were"} too hard to hear even for Vocto's own careful listener: those details count as a test issue, not the agent's.` : ""}</p>
        {conds.length > 0 && (
          <div className="grid gap-4 grid-cols-1 xl:grid-cols-2">
            <Card><CardHead level={2} title="Calls passed per condition" sub="the same test cases under each kind of caller · a drop counts only when bigger than chance" />
              <StackBars legend={[["Passed", C.good], ["Failed", C.bad]]} rows={conds.map((c) => {
                const [p, n] = (c.result.match(/(\d+) of (\d+)/) || []).slice(1).map(Number);
                return { key: c.key, label: c.title, sub: c.vs_clean ? `${c.status_text} · ${c.vs_clean}` : c.status_text, href: c.proof ? callHref(c.proof) : undefined,
                  parts: [{ value: p || 0, color: C.good, label: "Passed" }, { value: (n || 0) - (p || 0), color: C.bad, label: "Failed" }],
                  right: c.pass_rate == null ? "—" : pct(c.pass_rate) };
              })} />
            </Card>
            {typeIds.length > 0 ? (
              <Card id="speech-types"><CardHead level={2} title="Details right, by type and condition" sub="each caller detail a tool received, as the caller said it" />
                <div className="overflow-x-auto">
                  <table className="w-full text-[13px] tabular-nums">
                    <thead><tr className="text-muted text-left"><th className="font-normal pb-1.5 pr-2">Condition</th>{types.map((t) => <th key={t.type} className="font-normal pb-1.5 px-1.5 text-right whitespace-nowrap">{t.title}</th>)}</tr></thead>
                    <tbody>
                      {conds.map((c) => (
                        <tr key={c.key} className="border-t border-[var(--glass-line)]">
                          <td className="py-1.5 pr-2 truncate max-w-[180px]">{c.title}</td>
                          {typeIds.map((t) => { const x = c.by_type?.[t]; return <td key={t} className={cx("py-1.5 px-1.5 text-right", x && cellTone(x.right, x.checked))}>{x?.checked ? `${x.right}/${x.checked}` : "—"}</td>; })}
                        </tr>
                      ))}
                      <tr className="border-t border-line font-medium"><td className="py-1.5 pr-2">All</td>{types.map((t) => <td key={t.type} className={cx("py-1.5 px-1.5 text-right", cellTone(t.right, t.checked))}>{t.right}/{t.checked}</td>)}</tr>
                    </tbody>
                  </table>
                </div>
              </Card>
            ) : (
              <Card><CardHead level={2} title="Words misheard per condition" sub="share of the caller's words the agent's hearing got wrong" />
                {wer.length ? <StackBars rows={conds.filter((c) => c.understood_wer != null).map((c) => ({
                  key: c.key, label: c.title, parts: [{ value: c.understood_wer as number, color: (c.understood_wer as number) > 10 ? C.bad : (c.understood_wer as number) > 5 ? C.warn : C.good, label: "% misheard" }],
                  right: `${(c.understood_wer as number).toFixed(1)}%` }))} />
                  : <p className="text-muted text-[13px]">No caller detail reached a tool, and the agent doesn't report what it heard.</p>}
              </Card>
            )}
          </div>
        )}
        {lad && (lad.noise || lad.loss) && (
          <Card id="speech-ladder"><CardHead level={2} title="Breaking point" sub="the probe test cases, one call each, as the line gets harder · these calls never decide the release" />
            <div className="grid gap-4 grid-cols-1 lg:grid-cols-2">
              {(["noise", "loss"] as const).filter((k) => lad[k]).map((k) => (
                <div key={k}>
                  <div className="text-[13px] font-medium mb-2">{k === "noise" ? "Cafe noise (speech-to-noise, dB)" : "Audio packets lost (%)"} · <span className={lad[k].works_to != null ? "text-good" : "text-bad"}>{lad[k].text}</span></div>
                  <StackBars legend={[["Passed", C.good], ["Failed", C.bad]]} rows={lad[k].levels.map((s) => ({
                    key: String(s.level), label: `${s.level}${k === "noise" ? " dB" : "%"}`, sub: s.details_checked ? `details ${s.details_right}/${s.details_checked}` : undefined,
                    href: s.failed_calls[0] ? callHref(s.failed_calls[0]) : undefined,
                    parts: [{ value: s.passed, color: C.good, label: "Passed" }, { value: s.failed, color: C.bad, label: "Failed" }],
                    right: s.pass_rate == null ? "—" : pct(s.pass_rate) }))} />
                </div>
              ))}
            </div>
          </Card>
        )}
        {(sp?.wrong || []).length > 0 && (
          <Card id="speech-wrong"><CardHead level={2} title="Details saved wrong" sub="what the caller said, next to what reached the tool · opens the call at that moment" />
            <div className="grid gap-1.5">
              {(sp?.wrong || []).map((w, i) => (
                <a key={i} href={callHref(w.test_id) + (w.seq != null ? `?at=${w.seq}` : "")} className="grid grid-cols-[minmax(90px,22%)_1fr_1fr] gap-2.5 items-center text-[13px] rounded-md bg-surface-2 px-2.5 py-1.5 hover:no-underline text-ink">
                  <span className="text-muted truncate">{w.fact}{w.condition ? ` · ${w.condition}` : ""}</span>
                  <span className="truncate">said <b className="font-medium">{w.said ?? "(hidden)"}</b></span>
                  <span className="truncate text-bad">saved <b className="font-medium">{w.saved ?? "(hidden)"}</b></span>
                </a>
              ))}
            </div>
          </Card>
        )}
        {caseCard}
      </div>
    );
  }

  if (mode === "regression") {
    const b = d?.baseline;
    const trend = d?.trend || [];
    const flaky = d?.flaky || [];
    const versions = b?.versions || cmp?.versions || [];
    const reruns = Object.keys(b?.confirmed_by_reruns || {});
    const scenarioHref = (sid: string) => { const c = finals.find((f) => f.scenario_id === sid && f.status === "FAIL") || finals.find((f) => f.scenario_id === sid); return c ? callHref(c.test_id) : undefined; };
    return (
      <div className="grid gap-4" id="type-analysis">
        {!b && <Notice tone="warn" icon="anchor">No baseline to compare with yet. Open a good run and press <b>Set as baseline</b>, then run Regression again.</Notice>}
        <KpiStrip items={[
          { label: "Confirmed regressions", value: b ? b.regressions : "—", foot: b ? (b.blocking ? `${b.blocking} block the release` : "nothing blocks") : "no baseline", tone: b ? (b.regressions ? "bad" as const : "good" as const) : undefined },
          { label: "Suspected (didn't repeat)", value: b ? (b.suspected ?? 0) : "—", foot: reruns.length ? `${reruns.length} rerun to confirm` : "a single failure is rerun", tone: b?.suspected ? "warn" as const : undefined },
          { label: "Fixed since baseline", value: b ? (b.fixed ?? b.better ?? 0) : "—", foot: b ? `${b.worse} other numbers worse` : "", tone: b?.fixed ? "good" as const : undefined },
          { label: "Flaky tests", value: flaky.length, foot: flaky.length ? "pass and fail over the last 10 runs" : "none over the last 10 runs", tone: flaky.length ? "warn" as const : undefined },
          callsKpi,
        ]} />
        <div className="grid gap-4 grid-cols-1 xl:grid-cols-[minmax(0,1fr)_380px]">
          <Card><CardHead level={2} title="Pass rate over the last runs" sub="one dot per run (green ready, red not ready) · version labels where the agent changed" />
            <TrendChart points={trend} runHref={runHref} />
          </Card>
          <Card id="regression-versions"><CardHead level={2} title="What changed between the runs" sub={b?.rolling ? `compared with ${b.run_id}` : b ? `compared with baseline ${b.run_id}` : "no baseline"} />
            {versions.length ? (
              <div className="grid gap-2">
                {versions.map((v) => (
                  <div key={v.field} className="text-[13px]"><div className="font-medium">{v.title}</div>
                    <div className="text-ink-2 font-mono text-[12px] break-all">{v.before ?? "none"} → {v.after ?? "none"}</div></div>
                ))}
              </div>
            ) : <p className="text-muted text-[13px]">{b ? "Same agent version, settings, prompt and judge models as the baseline (or no labels were given: add one with the agent version field on Run)." : "—"}</p>}
            {b?.rolling && b.rolling.length > 0 && <p className="text-[13px] text-muted mt-3">Baseline pooled from {b.rolling.join(", ")}.</p>}
            {b && b.like_for_like === false && <p className="text-[13px] text-warn mt-3">Made differently from the baseline, so nothing here can block.</p>}
          </Card>
        </div>
        {cmp && (
          <Card id="regression-changes"><CardHead level={2} title="Before and now" sub="every change beyond its limit · a fixed test can be kept as a regression test" />
            <CompareCounts cmp={cmp} />
            <div className="mt-3 -mx-1"><ChangesList cmp={cmp} callHref={scenarioHref} keepRunId={runId} /></div>
          </Card>
        )}
        {flaky.length > 0 && (
          <Card id="regression-flaky"><CardHead level={2} title="Flaky tests" sub="both passed and failed over the last 10 runs · their single failures don't block; fix them to trust them" />
            <StackBars rows={flaky.map((f) => ({ key: f.scenario_id, label: f.title, sub: `${f.runs} runs`, right: `${pct(f.rate)} · ${f.passed}/${f.calls}`,
              parts: [{ value: f.passed, color: C.good, label: "Passed" }, { value: f.calls - f.passed, color: C.bad, label: "Failed" }] }))}
              legend={[["Passed", C.good], ["Failed", C.bad]]} />
          </Card>
        )}
        {caseCard}
      </div>
    );
  }

  if (mode === "safety" && d?.safety) {
    const s = d.safety;
    const resisted = s.resisted ?? s.passed;
    const known = resisted + s.failed;
    const range = s.low != null && s.high != null && known ? `range ${pct(1 - s.high)}–${pct(1 - s.low)}` : "";
    const packs = Object.entries(s.packs).sort((a, b) => (b[1].rate ?? -1) - (a[1].rate ?? -1) || b[1].failed - a[1].failed);
    const rateText = (r: { rate?: number | null; low?: number | null; high?: number | null }) =>
      r.rate == null ? "—" : `${pct(r.rate)}${r.low != null && r.high != null ? ` (${pct(r.low)}–${pct(r.high)})` : ""}`;
    const found = (rd?.findings || []).filter((f) => f.area === "safety" || f.kind === "safety_review");
    return (
      <div className="grid gap-4" id="type-analysis">
        <KpiStrip items={[
          { label: "Attacks resisted", value: known ? `${resisted} of ${known}` : "—", foot: range || (known ? pct(resisted / known) : "no attack with a result"),
            tone: known ? (s.failed ? "bad" : "good") : undefined },
          { label: "Unsafe answers", value: s.failed, foot: s.failed ? "see the findings below" : "none", tone: s.failed ? "bad" : "good" },
          { label: "Waiting for your review", value: s.to_review || 0, foot: s.review === false ? "review before release is off" : "label them in AI judges",
            tone: s.to_review ? "warn" : undefined },
          { label: "Hard-check leaks", value: s.leaks ?? 0, foot: "target 0: secrets, tools, code words", tone: (s.leaks ?? 0) ? "bad" : "good" },
        ]} />
        {(s.not_landed || s.errors) ? (
          <p className="text-[13px] text-ink-2 -mt-1">
            {s.not_landed ? `${s.not_landed} attack call${s.not_landed === 1 ? "" : "s"} never reached the agent (it looked nothing up, so the hidden instruction wasn't read). ` : ""}
            {s.errors ? `${s.errors} call${s.errors === 1 ? "" : "s"} couldn't be finished. ` : ""}Neither counts as resisted.
          </p>
        ) : null}
        <div className="grid gap-4 grid-cols-1 xl:grid-cols-2">
          <Card id="attack-rates"><CardHead level={2} title="Attack success rate per kind" sub="unsafe calls out of calls with a result, worst first" />
            <StackBars legend={[["Resisted", C.good], ["Unsafe", C.bad], ["Waiting for your review", C.warn], ["Didn't reach the agent", C.none]]} rows={packs.map(([id, p]) => ({
              key: id, label: PACK_TITLES[id] || id, sub: p.rate != null ? `success rate ${rateText(p)}` : "no result yet",
              parts: [{ value: p.resisted ?? Math.max(0, p.calls - p.failed - (p.to_review || 0)), color: C.good, label: "Resisted" }, { value: p.failed, color: C.bad, label: "Unsafe" },
                { value: p.to_review || 0, color: C.warn, label: "Waiting for your review" }, { value: (p.not_landed || 0) + (p.errors || 0), color: C.none, label: "Didn't reach the agent" }],
              right: p.rate == null ? "—" : pct(p.rate) }))} />
          </Card>
          {s.owasp && s.owasp.length > 0 && (
            <Card id="owasp"><CardHead level={2} title="OWASP Top 10 for LLM apps (2025)" sub="the same results, by the risk list security teams use" />
              <table className="w-full text-[13px]">
                <thead><tr className="text-muted text-left"><th className="font-normal pb-1.5">Risk</th><th className="font-normal pb-1.5 text-right">Calls</th><th className="font-normal pb-1.5 text-right">Unsafe</th><th className="font-normal pb-1.5 text-right">Success rate</th></tr></thead>
                <tbody>
                  {s.owasp.map((o) => (
                    <tr key={o.id} className="border-t border-[var(--glass-line)]" title={o.packs.map((k) => PACK_TITLES[k] || k).join(", ")}>
                      <td className="py-1.5 pr-2"><span className="font-mono text-[12.5px] text-muted mr-1.5">{o.id}</span>{o.title}</td>
                      <td className="py-1.5 text-right tabular-nums">{o.calls}</td>
                      <td className={cx("py-1.5 text-right tabular-nums", o.unsafe > 0 && "text-bad font-medium")}>{o.unsafe}</td>
                      <td className="py-1.5 text-right tabular-nums whitespace-nowrap">{rateText(o)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="text-[13px] text-muted mt-2.5">Off-topic questions, abuse and confusing callers aren't on that list; they still count above.</p>
            </Card>
          )}
        </div>
        {found.length > 0 && (
          <Card id="safety-findings"><CardHead level={2} title="What went wrong" sub="worst first, with what was said and done" />
            <div className="grid gap-3">
              {found.slice(0, 8).map((f) => (
                <div key={f.id} className="grid gap-1 border-t border-[var(--glass-line)] pt-2.5 first:border-0 first:pt-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <i className="size-2 rounded-full flex-none" style={{ background: f.status === "problem" ? C.bad : f.status === "needs_review" ? C.warn : C.none }} />
                    <b className="font-medium text-[13.5px]">{f.title}</b>
                    <span className="text-[13px] text-muted">{f.status === "problem" ? (f.blocks ? "blocks the release" : f.severity) : f.status === "needs_review" ? "waiting for your review" : f.status === "opinion" ? "the judges' opinion" : "evidence"} · {f.how_often.text}</span>
                  </div>
                  <p className="text-[13px] text-ink-2">{f.what}</p>
                  {f.proofs.filter((p) => p.quote).slice(0, 2).map((p, i) => (
                    <a key={i} href={callHref(p.test_id) + (p.seq != null ? `?at=${p.seq}` : "")} className="text-[13px] text-ink-2 hover:no-underline rounded-md bg-surface-2 px-2.5 py-1.5 block">
                      <span className="text-muted">{p.at ? `${p.at} · ` : ""}{p.who}</span> “{p.quote}”{p.note ? <span className="text-muted"> ({p.note})</span> : null}
                    </a>
                  ))}
                </div>
              ))}
              {found.length > 8 && <p className="text-[13px] text-muted">And {found.length - 8} more on the Findings tab.</p>}
            </div>
          </Card>
        )}
        {s.switched_off && s.switched_off.length > 0 && (
          <p className="text-[13px] text-muted" id="safety-off">Not tried in this run (switched off for this agent): {s.switched_off.map((k) => PACK_TITLES[k] || k).join(", ")}.</p>
        )}
      </div>
    );
  }

  if (mode === "resilience") {
    const fs = rd?.failures || [];
    const sum = (k: "recovered" | "honest" | "false_claims" | "duplicates" | "retries" | "too_many_tries" | "early_429") => fs.reduce((a, f) => a + (f[k] || 0), 0);
    const silences = fs.map((f) => f.longest_silence_ms).filter((x): x is number => x != null);
    const grid = rd?.drill_grid;
    const voiceRun = silences.length > 0;
    return (
      <div className="grid gap-4" id="type-analysis">
        <KpiStrip items={[
          { label: "Failures handled", value: fs.length ? `${fs.filter((f) => f.status === "handled").length} of ${fs.length}` : "—",
            foot: `kinds of failure · ${sum("recovered") + sum("honest")} calls recovered or honest`,
            tone: fs.length ? (fs.every((f) => f.status === "handled") ? "good" : "bad") : undefined },
          { label: "Untrue claims", value: sum("false_claims"), foot: "said it worked when it didn't · target 0", tone: sum("false_claims") ? "bad" : fs.length ? "good" : undefined },
          { label: "Made twice", value: sum("duplicates"), foot: "duplicate bookings or orders · target 0", tone: sum("duplicates") ? "bad" : fs.length ? "good" : undefined },
          voiceRun
            ? { label: "Longest silence", value: fmtMs(Math.max(...silences)), foot: "nobody speaking during a failure", tone: Math.max(...silences) > 3000 ? "bad" : "good" }
            : { label: "Retries", value: sum("retries"), foot: sum("too_many_tries") ? `${sum("too_many_tries")} call${sum("too_many_tries") === 1 ? "" : "s"} kept trying too long` : "tries after a failure, until it worked",
                tone: sum("too_many_tries") ? "bad" : fs.length ? "good" : undefined },
        ]} />
        {fs.length > 0 && (
          <Card><CardHead level={2} title="How calls ended, per failure" sub="each kind of failure tried on the test cases · click a row to open a failed call" />
            <StackBars legend={[["Done (on a retry, or another option)", C.good], ["Honest it couldn't", "color-mix(in srgb, var(--good) 45%, var(--surface))"], ["Untrue claim", C.bad], ["Made twice", C.warn]]}
              rows={fs.map((f) => ({ key: f.key, label: f.title, sub: f.status_text, href: f.proof ? callHref(f.proof) : undefined,
                parts: [{ value: f.recovered, color: C.good, label: "Done" }, { value: f.honest, color: "color-mix(in srgb, var(--good) 45%, var(--surface))", label: "Honest it couldn't" },
                  { value: f.false_claims, color: C.bad, label: "Untrue claim" }, { value: f.duplicates, color: C.warn, label: "Made twice" }],
                right: f.pass_rate == null ? "—" : pct(f.pass_rate) }))} />
          </Card>
        )}
        {grid && grid.rows.length > 0 && (
          <Card id="drill-grid"><CardHead level={2} title="Test cases × failures" sub="calls passed per test case and failure · a red cell opens its failed call" />
            <div className="overflow-x-auto -mx-1">
              <table className="text-[13px] min-w-full border-separate border-spacing-1">
                <thead><tr><th className="text-left font-medium text-muted px-1.5">Test case</th>
                  {grid.columns.map((c) => <th key={c.key} className="font-medium text-muted px-1.5 text-center whitespace-nowrap">{c.title}</th>)}</tr></thead>
                <tbody>
                  {grid.rows.map((r) => (
                    <tr key={r.id}>
                      <td className="px-1.5 py-1 max-w-[16rem] truncate" title={r.title}>{r.title}</td>
                      {grid.columns.map((c) => {
                        const cell = r.cells[c.key];
                        if (!cell) return <td key={c.key} className="text-center text-muted">—</td>;
                        const bg = cell.state === "pass" ? "color-mix(in srgb, var(--good) 18%, var(--surface))"
                          : cell.state === "fail" ? "color-mix(in srgb, var(--bad) 20%, var(--surface))" : "var(--surface-3)";
                        const inner = <span className="tabular-nums">{cell.text}</span>;
                        return (
                          <td key={c.key} className="text-center rounded-md px-2 py-1" style={{ background: bg }} title={cell.why || undefined} data-cell={`${r.id}:${c.key}`}>
                            {cell.call ? <a href={callHref(cell.call)} className="text-ink hover:no-underline">{inner}</a> : inner}
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        )}
        {fs.length > 0 && (
          <Card id="drill-recovery"><CardHead level={2} title="How it recovered" sub="measured from the tool calls and the recording, never by AI" />
            <div className="overflow-x-auto -mx-1">
              <table className="text-[13px] min-w-full">
                <thead><tr className="text-muted text-left">
                  <th className="font-medium px-1.5 py-1">Failure</th><th className="font-medium px-1.5 py-1 text-right">Retries</th>
                  <th className="font-medium px-1.5 py-1 text-right">Most in a call</th><th className="font-medium px-1.5 py-1 text-right">Too many tries</th>
                  <th className="font-medium px-1.5 py-1 text-right">Retried a 429 too soon</th>
                  {voiceRun && <th className="font-medium px-1.5 py-1 text-right">Heard within 3 s</th>}
                  {voiceRun && <th className="font-medium px-1.5 py-1 text-right">Longest silence</th>}
                </tr></thead>
                <tbody>
                  {fs.map((f) => (
                    <tr key={f.key} className="border-t border-line">
                      <td className="px-1.5 py-1.5">{f.title}</td>
                      <td className="px-1.5 py-1.5 text-right tabular-nums">{f.retries ?? 0}</td>
                      <td className="px-1.5 py-1.5 text-right tabular-nums">{f.max_retries ?? 0}</td>
                      <td className={cx("px-1.5 py-1.5 text-right tabular-nums", f.too_many_tries ? "text-bad font-medium" : "")}>{f.too_many_tries ?? 0}</td>
                      <td className={cx("px-1.5 py-1.5 text-right tabular-nums", f.early_429 ? "text-warn font-medium" : "")}>{f.early_429 ?? 0}</td>
                      {voiceRun && <td className="px-1.5 py-1.5 text-right tabular-nums">{f.informed || "—"}</td>}
                      {voiceRun && <td className="px-1.5 py-1.5 text-right tabular-nums">{f.longest_silence_ms == null ? "—" : fmtMs(f.longest_silence_ms)}</td>}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="text-[13px] text-muted mt-2">Out of scope: failures inside the agent's own speech, AI or voice providers. Vocto can't break those from outside; only the agent's team can switch them off for a test.</p>
          </Card>
        )}
        {caseCard}
      </div>
    );
  }

  // Functional, the Release check and Regression: the job itself.
  const areas = (rd?.scorecard || []).slice(0, 6);
  const fn = rd?.functional;
  const fnKpis = fn ? [
    { label: "Tasks done right", value: fn.tasks.n ? `${fn.tasks.k} of ${fn.tasks.n}` : "—",
      foot: fn.tasks.range || (fn.tasks.unchecked ? `${fn.tasks.unchecked} couldn't be checked` : "no task checked"),
      tone: fn.tasks.n ? (fn.tasks.k === fn.tasks.n ? "good" as const : "bad" as const) : undefined },
    { label: "Untrue claims", value: fn.untrue_claims, foot: "said done when it wasn't",
      tone: fn.tasks.n ? (fn.untrue_claims ? "bad" as const : "good" as const) : undefined },
    { label: "Details saved right", value: fn.saved.n ? `${fn.saved.k} of ${fn.saved.n}` : "—", foot: fn.saved.range || "nothing was saved",
      tone: fn.saved.n ? (fn.saved.k === fn.saved.n ? "good" as const : "bad" as const) : undefined },
    { label: "Other changes", value: fn.other_changes.of ? fn.other_changes.calls : "—",
      foot: fn.other_changes.of ? `of ${fn.other_changes.of} calls watched` : "tables not watched",
      tone: fn.other_changes.of ? (fn.other_changes.calls ? "bad" as const : "good" as const) : undefined },
  ] : [];
  const endsCard = fn && fn.ends.length > 0 && (
    <Card>
      <CardHead level={2} title="What the agent said, did and saved" sub={`how each task ended, per test case${fn.read_by_ai ? ` · ${fn.read_by_ai} claim${fn.read_by_ai === 1 ? "" : "s"} read by AI (two models agreed)` : ""}`} />
      <StackBars legend={[["Ended right", C.good], ["Said something untrue", C.bad], ["Saved wrong, twice or when it shouldn't", C.warn], ["Couldn't be checked", C.none]]}
        rows={fn.ends.map((e) => ({ key: e.key, label: e.title,
          parts: e.parts.map((p) => ({ value: p.count, label: p.label,
            color: p.good ? C.good : p.class === "NOT_VERIFIED" ? C.none : p.class === "FALSE_SUCCESS" || p.class === "FALSE_FAILURE" ? C.bad : C.warn })),
          right: `${e.parts.filter((p) => p.good).reduce((a, p) => a + p.count, 0)}/${e.parts.reduce((a, p) => a + p.count, 0)}` }))} />
      {fn.wrong_tools > 0 && <p className="text-[13px] text-ink-2 mt-3">{fn.wrong_tools} call{fn.wrong_tools === 1 ? "" : "s"} used a tool the test doesn't expect: see Findings.</p>}
    </Card>
  );
  const intentCard = fn && fn.intents.length > 0 && (
    <Card>
      <CardHead level={2} title="By job" sub="tasks done right, per job the test cases were written for" />
      <StackBars legend={[["Done right", C.good], ["Wrong", C.bad]]}
        rows={fn.intents.map((it) => ({ key: it.id, label: it.title, sub: it.range || undefined,
          parts: [{ value: it.passed, color: C.good, label: "Done right" }, { value: it.failed, color: C.bad, label: "Wrong" }],
          right: `${it.passed}/${it.passed + it.failed}` }))} />
    </Card>
  );
  return (
    <div className="grid gap-4" id="type-analysis">
      {fn && <KpiStrip items={fnKpis} />}
      {(endsCard || intentCard) && <div className={cx("grid gap-4 grid-cols-1", endsCard && intentCard && "xl:grid-cols-2")}>{endsCard}{intentCard}</div>}
      <KpiStrip items={[
        callsKpi,
        { label: "Test cases steady", value: rel.length ? `${steady} of ${rel.length}` : "—", foot: "passed every repeat", tone: rel.length ? (steady === rel.length ? "good" : "bad") : undefined },
        { label: "Blocking findings", value: rd?.counts.blocking ?? "—", foot: `${rd?.findings.length ?? 0} findings in all`, tone: rd ? (rd.counts.blocking ? "bad" : "good") : undefined },
        { label: "Coverage", value: d ? pct(d.coverage.ratio) : "—", foot: d ? `${d.coverage.verified_calls} of ${d.coverage.planned_calls} calls checked` : "" },
        ...(answers.length ? [{ label: "Slowest answers (p95)", value: fmtMs(lat?.p95_ms ?? pctl(answers, 0.95)), foot: lat?.p95_limit_ms ? `limit ${fmtMs(lat.p95_limit_ms)}` : "",
          tone: lat?.within_limits == null ? undefined : lat.within_limits ? "good" as const : "bad" as const }] : []),
      ]} />
      <div className={cx("grid gap-4 grid-cols-1", areas.length > 0 && "xl:grid-cols-[minmax(0,1fr)_380px]")}>
        {caseCard || <Card><p className="text-muted text-[13px]">No test case finished.</p></Card>}
        {areas.length > 0 && (
          <Card><CardHead level={2} title="By area" sub="what the checks looked at" />
            <div className="grid gap-2.5">
              {areas.map((a) => (
                <div key={a.area} className="flex items-start gap-2.5">
                  <i className="size-2 rounded-full mt-1.5 flex-none" style={{ background: a.status === "pass" ? C.good : a.status === "fail" ? C.bad : a.status === "watch" ? C.warn : C.none }} />
                  <div className="flex-1 min-w-0"><div className="text-[13px] font-medium">{a.title}</div><div className="text-[13px] text-ink-2">{a.value}</div></div>
                </div>
              ))}
            </div>
          </Card>
        )}
      </div>
    </div>
  );
}
