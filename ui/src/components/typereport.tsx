// The Analysis tab of a run report: the numbers and charts that answer the run's own test type. Functional
// shows each test case's repeats, Timing the answer times, Speech each caller condition, Safety each kind of
// attack, Resilience each kind of tool failure. Everything here comes from the run itself (decision.json,
// the calls' verdicts and the readiness report), so it matches the other tabs exactly.
import type { ReactNode } from "react";
import { KpiStrip } from "./dash";
import { fmtMetric } from "./quality";
import { Tip } from "./feedback";
import { Card, CardHead } from "./ui";
import { cx, fmtMs, pct } from "../lib/format";
import { PACK_TITLES } from "../lib/safety";
import type { CallBrief, DecisionDoc, Readiness, TestModeId } from "../lib/types";

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
export function AnswerTimes({ values, p50Limit, p95Limit }: { values: number[]; p50Limit?: number | null; p95Limit?: number | null }) {
  if (!values.length) return <p className="text-muted text-[13px]">No answer was timed in this run.</p>;
  const top = Math.max(...values, p95Limit || 0) * 1.05;
  const step = top <= 2000 ? 200 : top <= 5000 ? 500 : 1000;
  const bins = Math.ceil(top / step);
  const counts = Array.from({ length: bins }, (_, i) => values.filter((v) => v >= i * step && v < (i + 1) * step).length);
  const max = Math.max(1, ...counts);
  const over = (i: number) => p95Limit != null && (i + 1) * step > p95Limit;
  return (
    <div>
      <div className="relative flex items-end gap-[3px] h-[150px] border-b border-line" role="img" aria-label={`${values.length} answers timed`}>
        {counts.map((n, i) => (
          <Tip key={i} content={<><b>{n}</b> answer{n === 1 ? "" : "s"} in {fmtMs(i * step)} to {fmtMs((i + 1) * step)}</>} className="flex-1 h-full flex items-end">
            <i className="block w-full rounded-t-[3px]" style={{ height: `${(n / max) * 100}%`, minHeight: n ? 3 : 0, background: over(i) ? C.bad : C.accent }} />
          </Tip>
        ))}
        {[p50Limit, p95Limit].map((l, j) => l != null && l <= top && (
          <span key={j} className="absolute top-0 bottom-0 border-l border-dashed" style={{ left: `${(l / (bins * step)) * 100}%`, borderColor: j ? C.bad : C.warn }}>
            <span className="absolute -top-0.5 left-1 text-[13px] whitespace-nowrap" style={{ color: j ? C.bad : C.warn }}>{j ? "slowest limit" : "typical limit"} {fmtMs(l)}</span>
          </span>
        ))}
      </div>
      <div className="flex justify-between text-[13px] text-muted mt-1 tabular-nums"><span>0 s</span><span>{fmtMs(bins * step)}</span></div>
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

export function TypeAnalysis({ mode, d, rd, finals, titles, callHref }: {
  mode: TestModeId; d: DecisionDoc | null; rd: Readiness | null; finals: CallBrief[];
  titles: (id: string) => string; callHref: (testId: string) => string;
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
      return { ...c, p95: pctl(xs, 0.95), p50: pctl(xs, 0.5) };
    }).filter((c) => c.p95 != null).sort((a, b) => (b.p95 as number) - (a.p95 as number));
    const lim95 = lat?.p95_limit_ms ?? null;
    const maxP = Math.max(1, ...perCase.map((c) => c.p95 as number), lim95 || 0);
    const pick = (ids: string[]) => ids.map(qv).find(Boolean);
    const cut = pick(["barge_in_stop_ms"]), back = pick(["backchannel_kept"]), checkin = pick(["checkin_ms"]);
    const tile = (r: NonNullable<typeof cut>) => ({ label: r.title, value: fmtMetric(r.value, r.unit), foot: `${r.calls} call${r.calls === 1 ? "" : "s"}`,
      tone: r.band === "good" ? "good" as const : r.band === "bad" ? "bad" as const : r.band === "warn" ? "warn" as const : undefined });
    return (
      <div className="grid gap-4" id="type-analysis">
        <KpiStrip items={[
          { label: "Typical answer (p50)", value: fmtMs(lat?.p50_ms ?? pctl(answers, 0.5)), foot: lat?.p50_limit_ms ? `limit ${fmtMs(lat.p50_limit_ms)}` : `${answers.length} answers timed`,
            tone: lat?.p50_ms != null && lat.p50_limit_ms ? (lat.p50_ms <= lat.p50_limit_ms ? "good" as const : "bad" as const) : undefined },
          { label: "Slowest answers (p95)", value: fmtMs(lat?.p95_ms ?? pctl(answers, 0.95)), foot: lim95 ? `limit ${fmtMs(lim95)}` : "",
            tone: lat?.within_limits == null ? undefined : lat.within_limits ? "good" as const : "bad" as const },
          { label: "Answers over the limit", value: lim95 ? `${answers.filter((a) => a > lim95).length}` : "—", foot: `of ${answers.length} timed` },
          ...[cut, back, checkin].filter((r): r is NonNullable<typeof cut> => !!r).map(tile),
          callsKpi,
        ].slice(0, 5)} />
        <div className="grid gap-4 grid-cols-1 xl:grid-cols-2">
          <Card><CardHead level={2} title="How long callers waited" sub="every timed answer in the run" />
            <AnswerTimes values={answers} p50Limit={lat?.p50_limit_ms} p95Limit={lim95} /></Card>
          <Card><CardHead level={2} title="Slowest answers per test case" sub="p95 of each test case, slowest first" />
            {perCase.length ? (
              <div className="grid gap-2.5">
                {perCase.map((c) => (
                  <div key={c.key} className="grid grid-cols-[minmax(120px,38%)_1fr_auto] items-center gap-3">
                    <span className="truncate text-[13px]">{c.label}</span>
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
        {caseCard}
      </div>
    );
  }

  if (mode === "speech") {
    const conds = rd?.conditions || [];
    const heard = d?.heard_numbers;
    const wer = conds.map((c) => c.understood_wer).filter((x): x is number => x != null);
    return (
      <div className="grid gap-4" id="type-analysis">
        <KpiStrip items={[
          callsKpi,
          { label: "Conditions handled", value: conds.length ? `${conds.filter((c) => c.status === "handled").length} of ${conds.length}` : "—", foot: "every call passed" },
          { label: "Numbers heard right", value: heard?.numbers ? `${heard.heard_right} of ${heard.numbers}` : "—", foot: heard?.numbers ? pct(heard.heard_right / heard.numbers) : "no numbers said",
            tone: heard?.numbers ? (heard.heard_right === heard.numbers ? "good" : "bad") : undefined },
          { label: "Caller's words misheard", value: wer.length ? `${Math.max(...wer).toFixed(1)}%` : "—", foot: wer.length ? "in the hardest condition" : "not measured" },
        ]} />
        {conds.length > 0 && (
          <div className="grid gap-4 grid-cols-1 xl:grid-cols-2">
            <Card><CardHead level={2} title="Calls passed per condition" sub="the same test cases under each kind of caller" />
              <StackBars legend={[["Passed", C.good], ["Failed", C.bad]]} rows={conds.map((c) => {
                const [p, n] = (c.result.match(/(\d+) of (\d+)/) || []).slice(1).map(Number);
                return { key: c.key, label: c.title, sub: c.status_text, href: c.proof ? callHref(c.proof) : undefined,
                  parts: [{ value: p || 0, color: C.good, label: "Passed" }, { value: (n || 0) - (p || 0), color: C.bad, label: "Failed" }],
                  right: c.pass_rate == null ? "—" : pct(c.pass_rate) };
              })} />
            </Card>
            <Card><CardHead level={2} title="Words misheard per condition" sub="share of the caller's words the agent's hearing got wrong" />
              {wer.length ? <StackBars rows={conds.filter((c) => c.understood_wer != null).map((c) => ({
                key: c.key, label: c.title, parts: [{ value: c.understood_wer as number, color: (c.understood_wer as number) > 10 ? C.bad : (c.understood_wer as number) > 5 ? C.warn : C.good, label: "% misheard" }],
                right: `${(c.understood_wer as number).toFixed(1)}%` }))} />
                : <p className="text-muted text-[13px]">Not measured: switch on the hearing check (Run › Caller's voice and ears) to see what the agent heard.</p>}
            </Card>
          </div>
        )}
        {caseCard}
      </div>
    );
  }

  if (mode === "safety" && d?.safety) {
    const s = d.safety;
    const packs = Object.entries(s.packs).sort((a, b) => b[1].failed - a[1].failed);
    return (
      <div className="grid gap-4" id="type-analysis">
        <KpiStrip items={[
          { label: "Attacks handled safely", value: `${s.passed} of ${s.calls}`, foot: s.calls ? pct(s.passed / s.calls) : "", tone: s.failed ? "bad" : "good" },
          { label: "Unsafe answers", value: s.failed, foot: s.failed ? "see Findings" : "none", tone: s.failed ? "bad" : "good" },
          { label: "Need your label", value: s.to_review || 0, foot: "judges split" },
          { label: "Kinds of attack", value: packs.length, foot: `${packs.filter(([, p]) => !p.failed).length} with no failure` },
        ]} />
        <Card><CardHead level={2} title="Results per kind of attack" sub="unsafe ones first" />
          <StackBars legend={[["Safe", C.good], ["Unsafe", C.bad], ["Needs your label", C.warn]]} rows={packs.map(([id, p]) => ({
            key: id, label: PACK_TITLES[id] || id,
            parts: [{ value: p.calls - p.failed - (p.to_review || 0), color: C.good, label: "Safe" }, { value: p.failed, color: C.bad, label: "Unsafe" },
              { value: p.to_review || 0, color: C.warn, label: "Needs your label" }],
            right: `${p.calls - p.failed}/${p.calls}` }))} />
        </Card>
      </div>
    );
  }

  if (mode === "resilience") {
    const fs = rd?.failures || [];
    const sum = (k: "recovered" | "honest" | "false_claims" | "duplicates") => fs.reduce((a, f) => a + f[k], 0);
    return (
      <div className="grid gap-4" id="type-analysis">
        <KpiStrip items={[
          callsKpi,
          { label: "Failures handled", value: fs.length ? `${fs.filter((f) => f.status === "handled").length} of ${fs.length}` : "—", foot: "kinds of tool failure" },
          { label: "Recovered or honest", value: sum("recovered") + sum("honest"), foot: `${sum("recovered")} done on a retry` },
          { label: "Untrue claims", value: sum("false_claims"), foot: "said it worked when it didn't", tone: sum("false_claims") ? "bad" : fs.length ? "good" : undefined },
          { label: "Made twice", value: sum("duplicates"), foot: "duplicate bookings or orders", tone: sum("duplicates") ? "bad" : fs.length ? "good" : undefined },
        ]} />
        {fs.length > 0 && (
          <Card><CardHead level={2} title="How calls ended, per failure" sub="each kind of tool failure tried on the test cases" />
            <StackBars legend={[["Done on a retry", C.good], ["Honest it couldn't", "color-mix(in srgb, var(--good) 45%, var(--surface))"], ["Untrue claim", C.bad], ["Made twice", C.warn]]}
              rows={fs.map((f) => ({ key: f.key, label: f.title, sub: f.status_text, href: f.proof ? callHref(f.proof) : undefined,
                parts: [{ value: f.recovered, color: C.good, label: "Done on a retry" }, { value: f.honest, color: "color-mix(in srgb, var(--good) 45%, var(--surface))", label: "Honest it couldn't" },
                  { value: f.false_claims, color: C.bad, label: "Untrue claim" }, { value: f.duplicates, color: C.warn, label: "Made twice" }],
                right: f.pass_rate == null ? "—" : pct(f.pass_rate) }))} />
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
