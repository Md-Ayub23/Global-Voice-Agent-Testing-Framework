// Load tests (Phase 7): the shape chart (calls at once over time, planned and as it happened), the
// answer time per stage against its limit, the stage table and the load report on a run's page.
import { useEffect, useState, type ReactNode } from "react";
import { fmtMs, plural } from "../lib/format";
import { SHAPE_LABEL, fmtSeconds } from "../lib/load";
import type { LoadLive, LoadStage, LoadStageStats, LoadSummary } from "../lib/types";
import { TipG } from "./feedback";
import { Icon } from "./Icon";
import { Badge, Card, CardHead, Chip, Legend, Notice, Table } from "./ui";

// The width of an element, kept up to date (for SVG charts drawn to fit). A callback ref: the element
// may appear only after data loads.
export function useWidth<T extends HTMLElement>(initial = 760): [(el: T | null) => void, number] {
  const [el, setEl] = useState<T | null>(null);
  const [w, setW] = useState(initial);
  useEffect(() => {
    if (!el) return;
    const ro = new ResizeObserver(() => setW(el.clientWidth));
    ro.observe(el);
    setW(el.clientWidth);
    return () => ro.disconnect();
  }, [el]);
  return [setEl, w];
}

function niceMax(v: number) {
  if (v <= 0) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) if (m * p >= v) return m * p;
  return 10 * p;
}
const intTicks = (max: number) => {
  const step = Math.max(1, Math.ceil(max / 4));
  const out: number[] = [];
  for (let t = 0; t <= max; t += step) out.push(t);
  return out;
};
const timeLabel = (s: number) => (s < 120 ? `${Math.round(s)}s` : s < 7200 ? `${Math.round(s / 60)}m` : `${(s / 3600).toFixed(1)}h`);

// Calls at once over time: the plan as a stepped area, and (in a run) the calls really in progress as a line.
export function LoadChart({ stages, timeline, width = 760, height, now, stageTone }: {
  stages: LoadStage[]; timeline?: [number, number, number][] | null; width?: number; height?: number; now?: number | null;
  stageTone?: (s: LoadStage) => "good" | "bad" | "warn" | null;
}) {
  if (!stages.length) return null;
  const W = Math.max(280, width), H = height || Math.round(Math.max(150, Math.min(240, W * 0.3))), L = 34, R = 10, T = 14, B = 24;
  const total = stages[stages.length - 1].end_s;
  const seen = Math.max(0, ...(timeline || []).map((p) => p[1]));
  const top = Math.max(1, ...stages.map((s) => s.level), seen);
  const max = top <= 5 ? top + 1 : niceMax(top * 1.1);
  const xEnd = Math.max(total, ...(timeline || []).map((p) => p[0]));
  const x = (s: number) => L + (W - L - R) * (s / (xEnd || 1));
  const y = (v: number) => T + (H - T - B) * (1 - v / max);
  const area = stages.map((s) => `L${x(s.start_s)},${y(s.level)} L${x(s.end_s)},${y(s.level)}`).join(" ");
  const path = `M${x(0)},${y(0)} ${area} L${x(total)},${y(0)} Z`;
  const line = (timeline || []).length > 1
    ? (timeline || []).map((p, i) => `${i ? "L" : "M"}${x(p[0])},${y(p[1])}`).join(" ") : "";
  const xticks = Array.from({ length: 5 }, (_, i) => (xEnd * i) / 4);
  return (
    <svg className="chart w-full block overflow-visible" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Calls at once over time">
      {intTicks(max).map((t) => (
        <g key={t}>
          <line x1={L} x2={W - R} y1={y(t)} y2={y(t)} stroke="var(--line)" />
          <text x={L - 8} y={y(t) + 4} textAnchor="end">{t}</text>
        </g>
      ))}
      {xticks.map((t, i) => <text key={i} x={x(t)} y={H - 6} textAnchor={i === 0 ? "start" : i === 4 ? "end" : "middle"}>{timeLabel(t)}</text>)}
      <path d={path} fill="var(--accent-soft)" stroke="var(--accent)" strokeOpacity={0.35} />
      {stages.map((s) => {
        const t = stageTone?.(s);
        return (
          <TipG key={s.index} content={<><b>{s.name}</b><br />{s.level} call{s.level === 1 ? "" : "s"} at once · {fmtSeconds(s.seconds)}</>}>
            <rect x={x(s.start_s)} y={T} width={Math.max(1, x(s.end_s) - x(s.start_s))} height={H - T - B} fill="transparent" />
            {t && <rect x={x(s.start_s) + 1} y={y(0) - 4} width={Math.max(1, x(s.end_s) - x(s.start_s) - 2)} height={4} rx={1}
              fill={`var(--${t})`} />}
            {s.index > 0 && <line x1={x(s.start_s)} x2={x(s.start_s)} y1={y(s.level)} y2={y(0)} stroke="var(--accent)" strokeOpacity={0.18} />}
          </TipG>
        );
      })}
      {line && <path d={line} fill="none" stroke="var(--accent)" strokeWidth={2} strokeLinejoin="round" />}
      {now != null && now <= xEnd && <line x1={x(now)} x2={x(now)} y1={T} y2={y(0)} stroke="var(--ink-2)" strokeDasharray="3 3" />}
    </svg>
  );
}

// Answer time p95 per stage, against the limit (voice). Text runs have no answer times.
export function StageP95Chart({ rows, limit, width = 760 }: { rows: LoadStageStats[]; limit: number | null; width?: number }) {
  const timed = rows.filter((r) => r.latency.p95_ms != null);
  if (!timed.length) return null;
  const W = Math.max(280, width), H = Math.round(Math.max(150, Math.min(220, W * 0.28))), L = 44, R = 10, T = 18, B = 26;
  const max = niceMax(Math.max(...timed.map((r) => r.latency.p95_ms || 0), limit || 0, 1) * 1.1);
  const y = (v: number) => T + (H - T - B) * (1 - v / max);
  const step = (W - L - R) / rows.length;
  const bw = Math.min(46, step * 0.56);
  const ticks = [0, max / 4, max / 2, (3 * max) / 4, max];
  return (
    <svg className="chart w-full block overflow-visible" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Answer time p95 per stage">
      {ticks.map((t) => (
        <g key={t}>
          <line x1={L} x2={W - R} y1={y(t)} y2={y(t)} stroke="var(--line)" />
          <text x={L - 8} y={y(t) + 4} textAnchor="end">{t >= 1000 ? `${(t / 1000).toFixed(t % 1000 ? 1 : 0)}s` : `${Math.round(t)}ms`}</text>
        </g>
      ))}
      {rows.map((r, i) => {
        const cx = L + step * i + step / 2;
        const v = r.latency.p95_ms;
        const over = limit != null && v != null && v > limit;
        const fill = v == null ? "var(--line-strong)" : !r.trusted ? "var(--muted)" : over ? "var(--bad)" : "var(--caller)";
        const h = v == null ? 3 : Math.max(3, y(0) - y(v));
        const top = y(0) - h;
        return (
          <TipG key={r.index} content={<><b>{r.name}</b> · {r.level} at once<br />p95 {fmtMs(v)} · p50 {fmtMs(r.latency.p50_ms)}<br />{r.latency.turns} answers timed{!r.trusted ? <><br />not trusted: {r.untrusted_why}</> : null}</>}>
            <rect x={cx - step / 2} y={T} width={step} height={H - T - B} fill="transparent" />
            <path d={`M${cx - bw / 2},${y(0)} V${top + 4} q0,-4 4,-4 H${cx + bw / 2 - 4} q4,0 4,4 V${y(0)} Z`} className="bar-grow" fill={fill} />
            {v != null && <text x={cx} y={top - 5} textAnchor="middle" style={{ fill: "var(--ink-2)", fontWeight: 600 }}>{over ? "! " : ""}{step < 58 ? `${(v / 1000).toFixed(1)}s` : fmtMs(v)}</text>}
            <text x={cx} y={H - 10} textAnchor="middle">{r.level}×</text>
          </TipG>
        );
      })}
      {limit != null && limit <= max && (
        <g>
          <line x1={L} x2={W - R} y1={y(limit)} y2={y(limit)} stroke="var(--warn)" strokeDasharray="5 4" strokeWidth={1.5} />
          <text x={W - R} y={y(limit) - 5} textAnchor="end" style={{ fill: "var(--warn)", fontWeight: 600 }}>p95 limit {fmtMs(limit)}</text>
        </g>
      )}
    </svg>
  );
}

const pctText = (v: number | null | undefined) => (v == null ? "—" : `${v % 1 ? v.toFixed(1) : v}%`);

function StageStatus({ r }: { r: LoadStageStats }) {
  if (!r.trusted) return <Chip icon="help" title={r.untrusted_why}>not enough evidence</Chip>;
  return r.meets_limits ? <Badge status="PASS" label="within limits" /> : <Badge status="FAIL" label="over a limit" />;
}

export function StageTable({ rows, voice, callHref }: { rows: LoadStageStats[]; voice: boolean; callHref: (id: string) => string }) {
  return (
    <Table head={["Stage", "At once", "Calls", "Failed", ...(voice ? ["p50", "p95", "p99"] : []), "Tests passed", "Result"]}>
      {rows.map((r) => (
        <tr key={r.index} data-stage={r.index}>
          <td className="min-w-[180px]">
            <b>{r.name}</b>
            <div className="text-muted text-[13px]">{fmtSeconds(r.seconds)}{r.in_flight_max != null ? ` · up to ${r.in_flight_max} in progress` : ""}</div>
            {r.problems.map((p, i) => <div key={i} className="text-[13px] text-bad mt-0.5">{p.message}</div>)}
            {!r.trusted && <div className="text-[13px] text-ink-2 mt-0.5">{r.untrusted_why}</div>}
            {Object.keys(r.blockers).length > 0 && (
              <div className="flex flex-wrap gap-1 mt-1">{Object.entries(r.blockers).map(([k, ids]) => (
                <a key={k} href={callHref(ids[0])} className="hover:no-underline"><Chip tone="bad" icon="alert">{k} · {ids.length}</Chip></a>
              ))}</div>
            )}
          </td>
          <td className="tabular-nums font-semibold">{r.level}</td>
          <td className="tabular-nums">{r.finished}</td>
          <td className="tabular-nums whitespace-nowrap">
            {r.failed ? <a href={callHref(r.failed_refs[0])} className={r.problems.some((p) => p.rule === "failed_calls") ? "text-bad font-semibold" : "text-ink"}>{r.failed} ({pctText(r.failed_pct)})</a> : <span className="text-muted">0</span>}
            {Object.keys(r.failed_why).length > 0 && <div className="text-muted text-[13px]">{Object.entries(r.failed_why).map(([k, n]) => `${n} ${k}`).join(" · ")}</div>}
          </td>
          {voice && <td className="tabular-nums whitespace-nowrap">{fmtMs(r.latency.p50_ms)}</td>}
          {voice && <td className={"tabular-nums whitespace-nowrap " + (r.problems.some((p) => p.rule === "latency") ? "text-bad font-semibold" : "")}>{fmtMs(r.latency.p95_ms)}</td>}
          {voice && <td className="tabular-nums whitespace-nowrap">{fmtMs(r.latency.p99_ms)}</td>}
          <td className="tabular-nums whitespace-nowrap">{r.pass_rate == null ? "—" : `${pctText(r.pass_rate)}`}<div className="text-muted text-[13px]">{r.passed} of {r.passed + r.failed_tests}</div></td>
          <td><StageStatus r={r} /></td>
        </tr>
      ))}
    </Table>
  );
}

function Kpi({ label, value, foot, tone }: { label: string; value: ReactNode; foot?: ReactNode; tone?: "good" | "bad" | "warn" }) {
  return (
    <Card className="grid gap-1">
      <div className="text-ink-2 font-semibold text-[13px]">{label}</div>
      <div className={"text-[26px] font-semibold tracking-[-.02em] tabular-nums leading-[1.15] " + (tone ? `text-${tone}` : "")}>{value}</div>
      {foot && <div className="text-muted text-[13px]">{foot}</div>}
    </Card>
  );
}

// The Load tab of a load run's report.
export function LoadReport({ load, voice, callHref }: { load: LoadSummary; voice: boolean; callHref: (id: string) => string }) {
  const [box, w] = useWidth<HTMLDivElement>();
  const [box2, w2] = useWidth<HTMLDivElement>();
  const top = Math.max(...load.stages.map((s) => s.level));
  const failedPct = load.calls ? (100 * load.failed) / load.calls : 0;
  const cmp = load.comparisons;
  return (
    <div className="flex flex-col gap-4" id="load-report">
      <div className="grid gap-4 grid-cols-[repeat(auto-fit,minmax(190px,1fr))]">
        <Kpi label="Capacity" tone={load.capacity == null ? undefined : load.capacity >= top ? "good" : "bad"}
          value={<span id="load-capacity">{load.capacity == null ? "—" : load.capacity}</span>}
          foot={load.capacity == null ? "no stage had enough evidence" : load.capacity >= top ? `held the target of ${top} at once`
            : load.capacity === 0 ? `failed already at ${Math.min(...load.stages.map((s) => s.level))} at once` : `calls at once within every limit (target ${top})`} />
        <Kpi label="Calls" value={load.calls} foot={`${load.failed} failed (${failedPct.toFixed(failedPct % 1 ? 1 : 0)}%) · limit ${load.limits.max_failed_pct}% per stage`}
          tone={load.failed ? "bad" : undefined} />
        {voice && <Kpi label="Answer time p95, top level" value={fmtMs(cmp.slowdown?.top_p95_ms ?? load.stages[load.stages.length - 1]?.latency.p95_ms)}
          foot={load.p95_limit_ms != null ? `limit ${fmtMs(load.p95_limit_ms)}${cmp.slowdown ? ` · ${fmtMs(cmp.slowdown.low_p95_ms)} at ${cmp.slowdown.low_level} at once` : ""}` : "no limit"} />}
        <Kpi label="Tests passed, top level" value={cmp.pass_rate ? pctText(cmp.pass_rate.top) : pctText(load.stages[load.stages.length - 1]?.pass_rate)}
          foot={cmp.pass_rate ? `${pctText(cmp.pass_rate.low)} at the lowest level` : "of the calls that finished"} />
        {cmp.recovery && (
          <Kpi label="Recovery after the spike" tone={cmp.recovery.recovered ? "good" : "bad"}
            value={<span id="load-recovery">{cmp.recovery.recovered ? (cmp.recovery.back_to_normal_s != null ? `${cmp.recovery.back_to_normal_s} s` : "yes") : "Didn't recover"}</span>}
            foot={cmp.recovery.recovered ? `until answers were back to normal speed (p95 ${fmtMs(cmp.recovery.after_p95_ms)} once back, ${fmtMs(cmp.recovery.before_p95_ms)} before the spike)`
              : `p95 ${fmtMs(cmp.recovery.after_p95_ms)} after the spike, ${fmtMs(cmp.recovery.before_p95_ms)} before it`} />
        )}
      </div>

      {load.breaking && (
        <Notice tone={load.breaking.untrusted ? "warn" : "bad"} icon="alert">
          <b>{load.breaking.untrusted ? "Couldn't tell" : "Broke"} at {load.breaking.level} calls at once</b> ({load.breaking.stage}): {load.breaking.why.join(" ")}
        </Notice>
      )}
      {load.warnings.map((x, i) => <Notice key={i} tone="warn" icon="info">{x.message}</Notice>)}

      <Card>
        <CardHead level={2} title="Calls at once" sub={`${SHAPE_LABEL[load.shape]} · the plan (shaded) and the calls really in progress (line) · the bar under each stage is its result`} />
        <div ref={box}>
          <LoadChart stages={load.stages} timeline={load.timeline} width={w}
            stageTone={(s) => { const r = load.stages[s.index]; return !r ? null : !r.trusted ? "warn" : r.meets_limits ? "good" : "bad"; }} />
        </div>
        <div className="mt-1.5"><Legend items={[["var(--accent)", "in progress"], ["var(--good)", "stage within limits"], ["var(--bad)", "over a limit"], ["var(--warn)", "not enough evidence"]]} /></div>
      </Card>

      {voice && load.stages.some((s) => s.latency.p95_ms != null) && (
        <Card>
          <CardHead level={2} title="Answer time by stage" sub="p95 of the agent's answers while each stage ran · grey: the stage isn't trusted" />
          <div ref={box2}><StageP95Chart rows={load.stages} limit={load.p95_limit_ms} width={w2} /></div>
        </Card>
      )}

      <Card flush>
        <CardHead level={2} title="Stages" sub="every stage runs at or below the target, so every stage must meet the limits" className="px-[18px] pt-4 mb-2" />
        <StageTable rows={load.stages} voice={voice} callHref={callHref} />
      </Card>

      {(load.notes.length > 0 || load.lag_p95_ms != null) && (
        <Card id="load-notes">
          <CardHead level={3} title="Notes" />
          <ul className="grid gap-1.5 text-[13px] text-ink-2 list-none">
            {load.notes.map((n, i) => <li key={i} className="flex gap-2"><Icon name="info" size={14} className="text-muted mt-[3px] flex-none" />{n}</li>)}
            {load.lag_p95_ms != null && (
              <li className="flex gap-2"><Icon name="activity" size={14} className="text-muted mt-[3px] flex-none" />
                Vocto's own computer kept up: event-loop lag p95 {load.lag_p95_ms} ms, worst {load.lag_max_ms} ms (a stage over {load.limits.max_lag_ms} ms is not trusted).</li>
            )}
            {load.harness_errors > 0 && <li className="flex gap-2"><Icon name="alert" size={14} className="text-warn mt-[3px] flex-none" />{plural(load.harness_errors, "call")} failed because of Vocto itself; they are not counted against the agent.</li>}
          </ul>
        </Card>
      )}
    </div>
  );
}

// The Live page's card for a load test in progress.
export function LoadLiveCard({ stages, live, voice }: { stages: LoadStage[]; live: LoadLive | null; voice: boolean }) {
  const [box, w] = useWidth<HTMLDivElement>();
  const total = stages.length ? stages[stages.length - 1].end_s : 0;
  return (
    <Card className="mb-4" id="load-live">
      <CardHead level={2} title="Load" sub={live ? `${live.stage_name} · ${live.level} at once · ${live.in_flight} in progress now` : "Starting the first calls…"} />
      <div className="grid gap-3 grid-cols-2 md:grid-cols-4 mb-3 text-[13px]">
        <div><div className="text-muted text-[13px]">Time</div><b className="tabular-nums">{live ? `${fmtSeconds(Math.min(live.elapsed_s, total))} of ${fmtSeconds(total)}` : `0 of ${fmtSeconds(total)}`}</b></div>
        <div><div className="text-muted text-[13px]">Calls started</div><b className="tabular-nums" id="load-started">{live?.started ?? 0}</b></div>
        <div><div className="text-muted text-[13px]">Finished</div><b className="tabular-nums">{live?.finished ?? 0}</b></div>
        <div><div className="text-muted text-[13px]">Failed</div><b className={"tabular-nums " + (live?.failed ? "text-bad" : "")}>{live?.failed ?? 0}</b>
          {live && live.harness > 0 && <span className="text-muted text-[13px]"> · {plural(live.harness, "Vocto error")}</span>}</div>
      </div>
      <div ref={box}><LoadChart stages={stages} timeline={live?.timeline} width={w} now={live?.elapsed_s ?? 0} /></div>
      <div className="text-muted text-[13px] mt-1.5">
        {voice ? "Each call is a normal test call: answer times are measured and every check runs. " : "Each call is a normal test call: every check runs. "}
        Vocto's own lag now: {live ? `${Math.round(live.lag_ms)} ms` : "—"}.
      </div>
    </Card>
  );
}
