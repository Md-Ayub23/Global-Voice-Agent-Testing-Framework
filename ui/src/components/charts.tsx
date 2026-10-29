// Hand-drawn SVG/canvas charts: score rings, response-time bars and dots, the decision strip, waveforms.
// Colours follow the job they do: caller blue / agent green for the two sides of a call, the reserved
// status colours (with an icon or a label beside them) for good / bad / warning.
import { useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import { fmtDate, fmtMs, shortScenario } from "../lib/format";
import { runHref } from "../lib/links";
import { statusLabel, tone } from "../lib/status";
import type { CallBrief, LatencyTurn, Limits, RunSummary, TraceEvent } from "../lib/types";
import { TipG, useTip } from "./feedback";
import { Icon } from "./Icon";

const cssVar = (name: string) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

export function Ring({ value, size = 64, stroke = 7, toneName }: { value: number | null | undefined; size?: number; stroke?: number; toneName?: "good" | "warn" | "bad" }) {
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const v = value == null ? 0 : Math.max(0, Math.min(1, value));
  const t = toneName || (value == null ? "neutral" : v >= 0.999 ? "good" : v >= 0.8 ? "warn" : "bad");
  const color = { good: "var(--good)", warn: "var(--warn)", bad: "var(--bad)", neutral: "var(--line-strong)" }[t];
  const [offset, setOffset] = useState(c);
  useEffect(() => {
    const id = setTimeout(() => setOffset(c * (1 - v)), 60);
    return () => clearTimeout(id);
  }, [c, v]);
  return (
    <div className="relative flex-none" style={{ width: size, height: size }}>
      <svg width={size} height={size} className="-rotate-90">
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="var(--surface-3)" strokeWidth={stroke} />
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke={color} strokeWidth={stroke} strokeLinecap="round"
          strokeDasharray={c} strokeDashoffset={offset} style={{ transition: "stroke-dashoffset 1s cubic-bezier(.2,.8,.2,1)" }} />
      </svg>
      <b className="absolute inset-0 grid place-items-center text-sm font-extrabold">{value == null ? "—" : `${Math.round(v * 100)}%`}</b>
    </div>
  );
}

function niceMax(v: number) {
  if (v <= 0) return 1000;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) if (m * p >= v) return m * p;
  return 10 * p;
}
const tickLabel = (t: number) => (t >= 1000 ? `${(t / 1000).toFixed(t % 1000 ? 1 : 0)}s` : `${Math.round(t)}ms`);

// Response time per agent answer in one call (voice): verdict.metrics.latency_turns.
export function LatencyBars({ rows, limits, width = 560 }: { rows: LatencyTurn[] | undefined; limits: Limits | null; width?: number }) {
  if (!rows || !rows.length) return <div className="text-muted text-[12.5px]">No answers were timed in this call.</div>;
  const W = width, H = Math.round(Math.max(170, W * 0.42)), L = 44, R = 10, T = 16, B = 26;
  const narrow = (W - L - R) / rows.length < 58;
  const max = niceMax(Math.max(...rows.map((r) => r.ms ?? 0), limits?.p95 || 0, 1) * 1.08);
  const y = (v: number) => T + (H - T - B) * (1 - Math.max(0, v) / max);
  const step = (W - L - R) / rows.length;
  const bw = Math.min(46, step * 0.56);
  const ticks = [0, max / 4, max / 2, (3 * max) / 4, max];
  return (
    <svg className="chart w-full block overflow-visible" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Response time per answer">
      <g>{ticks.map((t) => <line key={t} x1={L} x2={W - R} y1={y(t)} y2={y(t)} stroke="var(--line)" />)}</g>
      {ticks.map((t) => <text key={t} x={L - 8} y={y(t) + 4} textAnchor="end">{tickLabel(t)}</text>)}
      {rows.map((r, i) => {
        const cx = L + step * i + step / 2;
        const over = !!limits && r.ms != null && r.counted && r.ms > limits.p95;
        const fill = r.ms == null ? "var(--line-strong)" : !r.counted ? "var(--muted)" : over ? "var(--bad)" : "var(--caller)";
        const h = r.ms == null ? 3 : Math.max(3, y(0) - y(r.ms));
        const top = y(0) - h;
        return (
          <TipG key={r.seq} content={<><b>Turn {r.turn}</b> · event #{r.seq}<br />{r.ms == null ? "no answer" : fmtMs(r.ms)}<br />{r.counted ? (over ? "over the p95 limit" : "counts toward the gate") : `not counted: ${r.note}`}</>}>
            <rect x={cx - step / 2} y={T} width={step} height={H - T - B} fill="transparent" />
            <path d={`M${cx - bw / 2},${y(0)} V${top + 4} q0,-4 4,-4 H${cx + bw / 2 - 4} q4,0 4,4 V${y(0)} Z`} fill={fill}
              opacity={r.counted ? 1 : 0.55} style={{ animation: `rise .5s cubic-bezier(.2,.8,.2,1) ${i * 0.05}s both` }} />
            {r.ms != null && (
              <text x={cx} y={top - 5} textAnchor="middle" style={{ fill: "var(--ink-2)", fontWeight: 600 }}>
                {over ? "! " : ""}{narrow ? `${(r.ms / 1000).toFixed(1)}s` : fmtMs(r.ms)}
              </text>
            )}
            <text x={cx} y={H - 10} textAnchor="middle">T{r.turn}</text>
          </TipG>
        );
      })}
      {limits && ([[limits.p50, "p50 limit"], [limits.p95, "p95 limit"]] as const).map(([v, label]) =>
        v == null || v > max ? null : (
          <g key={label}>
            <line x1={L} x2={W - R} y1={y(v)} y2={y(v)} stroke="var(--warn)" strokeDasharray="5 4" strokeWidth={1.5} />
            <text x={W - R} y={y(v) - 5} textAnchor="end" style={{ fill: "var(--warn)", fontWeight: 600 }}>{label} {fmtMs(v)}</text>
          </g>
        ))}
    </svg>
  );
}

// Every timed answer of a run, one row per scenario (voice). Click a dot to open its call.
export function LatencyDots({ calls, limits, runId, width = 760 }: { calls: CallBrief[]; limits: Limits | null; runId: string; width?: number }) {
  const byScenario = new Map<string, (LatencyTurn & { test_id: string; repeat: number })[]>();
  for (const c of calls) {
    if (c.status === "ERROR" || c.final === false) continue;
    for (const r of c.metrics?.latency_turns || []) {
      if (r.ms == null) continue;
      if (!byScenario.has(c.scenario_id)) byScenario.set(c.scenario_id, []);
      byScenario.get(c.scenario_id)!.push({ ...r, test_id: c.test_id, repeat: c.repeat });
    }
  }
  if (!byScenario.size) return <div className="text-muted text-[12.5px]">No answers were timed.</div>;
  const names = [...byScenario.keys()];
  const all = names.flatMap((n) => byScenario.get(n)!.map((r) => r.ms as number));
  const max = niceMax(Math.max(...all, limits?.p95 || 0) * 1.05);
  const min = Math.min(0, ...all);
  const W = Math.max(520, Math.round(width)), L = Math.min(220, W * 0.28), R = 16, rowH = 34, T = 18, B = 28;
  const H = T + B + rowH * names.length;
  const x = (v: number) => L + ((W - L - R) * (v - min)) / (max - min);
  const ticks = [0, 1, 2, 3, 4].map((i) => min + ((max - min) * i) / 4);
  return (
    <svg className="chart w-full block overflow-visible" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Response times by scenario">
      <g>{ticks.map((t) => <line key={t} x1={x(t)} x2={x(t)} y1={T} y2={H - B} stroke="var(--line)" />)}</g>
      {ticks.map((t) => <text key={t} x={x(t)} y={H - 8} textAnchor="middle">{(t / 1000).toFixed(1)}s</text>)}
      {names.map((n, i) => {
        const cy = T + rowH * i + rowH / 2;
        return (
          <g key={n}>
            <text x={L - 12} y={cy + 4} textAnchor="end" style={{ fill: "var(--ink-2)", fontWeight: 600 }}>{shortScenario(n)}</text>
            {byScenario.get(n)!.map((r, j) => {
              const ms = r.ms as number;
              const over = !!limits && r.counted && ms > limits.p95;
              const fill = !r.counted ? "var(--muted)" : over ? "var(--bad)" : "var(--caller)";
              return (
                <a key={`${r.test_id}-${r.seq}`} href={runHref({ run_id: runId }, `/calls/${r.test_id}`)}>
                  <TipG content={<><b>{shortScenario(n)}</b> · repeat {r.repeat}, turn {r.turn}<br />{fmtMs(ms)} · {r.counted ? (over ? "over the p95 limit" : "counts") : `not counted: ${r.note}`}</>}>
                    <circle cx={x(ms)} cy={cy + ((j % 5) - 2) * 3} r={5.5} fill={fill} stroke="var(--surface)" strokeWidth={2} opacity={r.counted ? 0.95 : 0.6} />
                  </TipG>
                </a>
              );
            })}
          </g>
        );
      })}
      {limits && ([[limits.p50, "p50 limit"], [limits.p95, "p95 limit"]] as const).map(([v, label]) =>
        v == null || v > max ? null : (
          <g key={label}>
            <line x1={x(v)} x2={x(v)} y1={T} y2={H - B} stroke="var(--warn)" strokeDasharray="5 4" strokeWidth={1.5} />
            <text x={x(v) + 4} y={T - 4} style={{ fill: "var(--warn)", fontWeight: 600 }}>{label}</text>
          </g>
        ))}
    </svg>
  );
}

// The last runs as a row of tiles, oldest left: tone + icon carry the decision.
function StripTile({ run }: { run: RunSummary }) {
  const s = run.decision || run.status;
  const t = run.decision ? tone(run.decision) : "neutral";
  const tip = useTip(<><b>{statusLabel(s)}</b><br />{run.profile_name || run.profile_id || ""} · {run.mode || ""}<br />{fmtDate(run.started_at)}</>);
  const bg = { good: "bg-good", bad: "bg-bad", warn: "bg-warn", neutral: "bg-line-strong", run: "bg-accent" }[t];
  return (
    <a href={runHref(run)} aria-label={statusLabel(s)} {...tip}
      className={`flex-1 max-w-[18px] min-w-1 overflow-hidden h-full rounded grid place-items-center text-white transition-transform duration-150 hover:-translate-y-0.5 ${bg}`}>
      <Icon name={t === "good" ? "check" : t === "bad" ? "x" : t === "warn" ? "alert" : "minus"} size={10} strokeWidth={3} />
    </a>
  );
}
export function DecisionStrip({ runs }: { runs: RunSummary[] }) {
  const list = runs.slice(0, 40).reverse();
  if (!list.length) return <div className="text-muted text-[12.5px]">No runs yet.</div>;
  return <div className="flex gap-1 items-end h-[34px]">{list.map((r) => <StripTile key={r.run_id} run={r} />)}</div>;
}

// Stereo recording (left caller, right agent) as two lanes, with a playhead synced to an <audio>.
export function Waveform({ runId, testId, audio, events }: { runId: string; testId: string; audio: HTMLAudioElement | null; events: TraceEvent[] }) {
  const box = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const head = useRef<HTMLDivElement>(null);
  const [data, setData] = useState<{ duration_ms: number; channels: Record<string, number[]> } | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    api.get<{ duration_ms: number; channels: Record<string, number[]> }>(`/api/runs/${runId}/calls/${testId}/waveform?points=1400`)
      .then((d) => live && setData(d), (e: Error) => live && setError(e.message));
    return () => { live = false; };
  }, [runId, testId]);

  useEffect(() => {
    const el = box.current, cv = canvas.current;
    if (!data || !el || !cv) return;
    const lanes = Object.entries(data.channels);
    const draw = () => {
      const dpr = window.devicePixelRatio || 1;
      const w = el.clientWidth, h = 120;
      cv.width = w * dpr;
      cv.height = h * dpr;
      const ctx = cv.getContext("2d");
      if (!ctx) return;
      ctx.scale(dpr, dpr);
      ctx.clearRect(0, 0, w, h);
      const laneH = h / lanes.length;
      for (const e of events) {   // each turn as a faint band behind its lane
        const a = e.data?.audio;
        if (!a || (e.type !== "caller_turn" && e.type !== "agent_turn")) continue;
        const lane = e.type === "caller_turn" ? 0 : 1;
        ctx.fillStyle = cssVar(lane === 0 ? "--caller-soft" : "--agent-soft");
        ctx.fillRect((a.start_ms / data.duration_ms) * w, lane * laneH + 2, ((a.end_ms - a.start_ms) / data.duration_ms) * w, laneH - 4);
      }
      lanes.forEach(([name, peaks], i) => {
        const mid = i * laneH + laneH / 2;
        ctx.fillStyle = cssVar(name === "caller" ? "--caller" : "--agent");
        const bw = w / peaks.length;
        const peak = Math.max(0.05, ...peaks);
        peaks.forEach((p, j) => {
          const amp = Math.max(0.5, (p / peak) * (laneH / 2 - 6));
          ctx.fillRect(j * bw, mid - amp, Math.max(1, bw - 0.4), amp * 2);
        });
      });
    };
    draw();
    const ro = new ResizeObserver(draw);
    ro.observe(el);
    const repaint = () => draw();
    window.addEventListener("vatf-theme", repaint);
    return () => { ro.disconnect(); window.removeEventListener("vatf-theme", repaint); };
  }, [data, events]);

  useEffect(() => {
    if (!data || !audio) return;
    const sync = () => {
      if (head.current) head.current.style.left = `${((audio.currentTime * 1000) / data.duration_ms) * 100}%`;
    };
    let raf = 0;
    const loop = () => { sync(); raf = requestAnimationFrame(loop); };
    const play = () => { cancelAnimationFrame(raf); loop(); };
    const pause = () => cancelAnimationFrame(raf);
    audio.addEventListener("timeupdate", sync);
    audio.addEventListener("play", play);
    audio.addEventListener("pause", pause);
    return () => {
      cancelAnimationFrame(raf);
      audio.removeEventListener("timeupdate", sync);
      audio.removeEventListener("play", play);
      audio.removeEventListener("pause", pause);
    };
  }, [data, audio]);

  if (error) return <div className="text-muted text-[12.5px] p-3.5">{error}</div>;
  if (!data) return <div className="h-[120px] rounded-xl animate-skeleton bg-linear-90 from-surface-2 via-surface-3 to-surface-2 bg-size-[200%_100%]" />;
  const lanes = Object.keys(data.channels);
  return (
    <div
      ref={box}
      className="relative rounded-xl bg-surface-2 border border-line overflow-hidden cursor-pointer"
      onClick={(e) => {
        if (!audio) return;
        const r = e.currentTarget.getBoundingClientRect();
        audio.currentTime = (((e.clientX - r.left) / r.width) * data.duration_ms) / 1000;
      }}
    >
      <canvas ref={canvas} className="block w-full h-[120px]" />
      <div ref={head} className="absolute inset-y-0 left-0 w-0.5 bg-accent pointer-events-none shadow-[0_0_0_3px_color-mix(in_srgb,var(--accent)_20%,transparent)]" />
      {lanes.map((name, i) => (
        <span key={name} className="absolute left-2.5 text-[11px] font-bold px-1.5 py-px rounded-[5px] bg-surface"
          style={{ top: 6 + i * 60, color: name === "caller" ? "var(--caller)" : "var(--agent)" }}>
          {name === "caller" ? "Caller (left)" : name === "agent" ? "Agent (right)" : name}
        </span>
      ))}
    </div>
  );
}
