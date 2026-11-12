// Pieces of the home dashboard and the project overview: number tiles, project avatars, the
// runs-per-day chart, the agent-type bars and the small decision history.
import { useEffect, useRef, useState, type ReactNode } from "react";
import { ago, cx, fmtDate } from "../lib/format";
import { domainLabel } from "../lib/links";
import { statusLabel, tone } from "../lib/status";
import type { Decision, DecisionDay } from "../lib/types";
import { TipG, useTip } from "./feedback";
import { Icon, type IconName } from "./Icon";

// A number that counts up from zero when it first appears ("101", "$0.36", "1 of 3"): the first number in the
// text animates, the rest of the text stays as written. Anything that isn't plain text is shown as it is.
const NUM = /\d[\d,]*(\.\d+)?/;
export function CountUp({ value }: { value: ReactNode }) {
  const text = typeof value === "number" ? String(value) : typeof value === "string" ? value : null;
  const m = text ? NUM.exec(text) : null;
  const target = m ? Number(m[0].replace(/,/g, "")) : NaN;
  const [shown, setShown] = useState<number | null>(null);
  const done = useRef(false);
  useEffect(() => {
    if (done.current || !Number.isFinite(target) || target === 0) return;
    done.current = true;
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return;
    const start = performance.now(), dur = 900;
    let raf = 0;
    const step = (t: number) => {
      const k = Math.min(1, (t - start) / dur);
      setShown(target * (1 - Math.pow(1 - k, 3)));
      if (k < 1) raf = requestAnimationFrame(step); else setShown(null);
    };
    raf = requestAnimationFrame(step);
    return () => { cancelAnimationFrame(raf); setShown(null); };
  }, [target]);
  if (!m || !text || shown == null) return <>{value}</>;
  const decimals = m[1] ? m[1].length - 1 : 0;
  const n = shown.toLocaleString("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals, useGrouping: m[0].includes(",") });
  return <>{text.slice(0, m.index)}{n}{text.slice(m.index + m[0].length)}</>;
}

// An agent's mark: a plain dark tile with an icon for how Vocto reaches it (a phone, a voice link, text, code).
const KIND_ICON: Record<string, IconName> = {
  phone: "phone", twilio_stream: "phone", websocket_audio: "wave", text_http: "text", in_repo: "bot",
};
export function ProjectAvatar({ size = 40, className, kind }: { name?: string; k?: string; size?: number; className?: string; kind?: string | null }) {
  return (
    <span className={cx("grid place-items-center flex-none bg-ink text-bg select-none", className)}
      style={{ width: size, height: size, borderRadius: Math.round(size * 0.24) }} aria-hidden="true">
      <Icon name={KIND_ICON[kind || ""] || "bot"} size={Math.round(size * 0.5)} strokeWidth={2} />
    </span>
  );
}

// Key numbers in one strip with thin dividers, each with what it is measured against underneath.
export function KpiStrip({ items, id }: { items: { label: ReactNode; value: ReactNode; foot?: ReactNode; tone?: "good" | "warn" | "bad" }[]; id?: string }) {
  return (
    <div id={id} className="grid grid-cols-2 lg:grid-cols-[repeat(auto-fit,minmax(150px,1fr))] gap-3">
      {items.map((it, i) => (
        <div key={i} className="glass rounded-[20px] px-[18px] py-4 min-w-0">
          <div className="text-muted text-[13px] font-medium truncate">{it.label}</div>
          <div className="text-[30px] font-bold tracking-[-.035em] tabular-nums leading-tight mt-1"><CountUp value={it.value} /></div>
          {it.foot != null && (
            <div className="text-ink-2 text-[13px] mt-0.5 flex items-center gap-1.5 min-w-0">
              {it.tone && <i className={cx("size-1.5 rounded-full flex-none", it.tone === "good" ? "bg-good" : it.tone === "warn" ? "bg-warn" : "bg-bad")} />}
              <span className="truncate">{it.foot}</span>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

export function Kpi({ icon, label, value, foot, tone: t = "accent" }: {
  icon: IconName; label: string; value: ReactNode; foot?: ReactNode; tone?: "accent" | "good" | "warn" | "bad";
}) {
  const dot = { accent: "bg-accent", good: "bg-good", warn: "bg-warn", bad: "bg-bad" }[t];
  return (
    <div className="glass rounded-[20px] px-4 py-3.5 grid gap-1 min-w-0">
      <div className="flex items-center gap-2 text-ink-2 text-[13px]">
        <Icon name={icon} size={14} className="text-muted" />{label}
        <i className={cx("ml-auto size-1.5 rounded-full", dot)} />
      </div>
      <div className="text-[28px] font-bold tracking-[-.035em] tabular-nums leading-[1.15] mt-1"><CountUp value={value} /></div>
      {foot && <div className="text-muted text-[13px]">{foot}</div>}
    </div>
  );
}

// ---- runs per day, stacked by decision ---------------------------------------------------------

const SERIES: { key: keyof DecisionDay; label: string; color: string }[] = [
  { key: "GO", label: "Passed", color: "var(--good)" },
  { key: "NO_GO", label: "Failed", color: "var(--bad)" },
  { key: "NO_GO_INSUFFICIENT_EVIDENCE", label: "Not enough evidence", color: "var(--warn)" },
  { key: "none", label: "No result", color: "var(--line-strong)" },
];

function niceCount(v: number) {
  for (const m of [2, 4, 6, 8, 10, 15, 20, 30, 40, 50, 80, 100]) if (m >= v) return m;
  return Math.ceil(v / 50) * 50;
}

// A bar with only its top corners rounded, so it sits flat on the baseline.
const topRounded = (x: number, y: number, w: number, h: number, r: number) => {
  r = Math.min(r, w / 2, h);
  return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
};
const dayLabel = (d: string) => new Date(`${d}T12:00:00Z`).toLocaleDateString(undefined, { day: "numeric", month: "short" });

function DayColumn({ d, x, slot, y0, scale, bar }: { d: DecisionDay; x: number; slot: number; y0: number; scale: number; bar: number }) {
  const total = SERIES.reduce((a, s) => a + (d[s.key] as number), 0);
  let y = y0;
  const segs = SERIES.filter((s) => (d[s.key] as number) > 0);
  return (
    <TipG content={(
      <div className="grid gap-1 min-w-[150px]">
        <b>{new Date(`${d.day}T12:00:00Z`).toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" })}</b>
        {total ? SERIES.map((s) => (d[s.key] as number) > 0 && (
          <div key={s.key} className="flex items-center gap-2"><i className="size-2.5 rounded-[3px]" style={{ background: s.color }} />
            <span className="flex-1 text-ink-2">{s.label}</span><b className="tabular-nums">{d[s.key]}</b></div>
        )) : <span className="text-muted">No runs</span>}
      </div>
    )}>
      <rect x={x} y={0} width={slot} height={y0 + 4} fill="transparent" />
      {segs.map((s, i) => {
        const h = Math.max(3, (d[s.key] as number) * scale);
        y -= h;
        const top = i === segs.length - 1;
        const gap = i > 0 ? 2 : 0;   // a 2px surface gap between stacked segments
        const bx = x + (slot - bar) / 2;
        return top
          ? <path key={s.key} className="bar-grow" d={topRounded(bx, y, bar, h - gap, 4)} fill={s.color} />
          : <rect key={s.key} className="bar-grow" x={bx} y={y} width={bar} height={h - gap} fill={s.color} />;
      })}
    </TipG>
  );
}

// The chart is drawn at its real pixel width, so its text stays 11px in a wide or a narrow column.
function useWidth<T extends HTMLElement>(initial: number): [React.RefObject<T | null>, number] {
  const ref = useRef<T>(null);
  const [w, setW] = useState(initial);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => setW(Math.max(200, Math.round(e.contentRect.width))));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, w];
}

export function ActivityChart({ days }: { days: DecisionDay[] }) {
  const [box, W] = useWidth<HTMLDivElement>(600);
  const H = 190, left = 26, bottom = 22, top = 8;
  const totals = days.map((d) => SERIES.reduce((a, s) => a + (d[s.key] as number), 0));
  const max = niceCount(Math.max(1, ...totals));
  const y0 = H - bottom;
  const scale = (y0 - top) / max;
  const slot = (W - left) / days.length;
  const bar = Math.min(26, slot * 0.56);
  const ticks = [0, max / 2, max];
  const every = slot >= 44 ? 1 : slot >= 22 ? 2 : 4;   // date labels that fit
  return (
    <div ref={box}>
      <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} className="chart block max-w-full" role="img"
        aria-label={`Runs per day for the last ${days.length} days: ${totals.reduce((a, b) => a + b, 0)} in total`}>
        {ticks.map((t) => (
          <g key={t}>
            <line x1={left} x2={W} y1={y0 - t * scale} y2={y0 - t * scale} stroke="var(--line)" strokeDasharray={t ? "3 4" : undefined} />
            <text x={left - 8} y={y0 - t * scale + 4} textAnchor="end">{t}</text>
          </g>
        ))}
        {days.map((d, i) => <DayColumn key={d.day} d={d} x={left + i * slot} slot={slot} y0={y0} scale={scale} bar={bar} />)}
        {days.map((d, i) => (days.length - 1 - i) % every === 0 && (
          <text key={d.day} x={i === days.length - 1 ? W - 2 : left + i * slot + slot / 2} y={H - 5} textAnchor={i === days.length - 1 ? "end" : "middle"}>{i === days.length - 1 ? "Today" : dayLabel(d.day)}</text>
        ))}
      </svg>
      <div className="flex gap-3.5 flex-wrap text-[13px] text-ink-2 mt-2">
        {SERIES.map((s) => <span key={s.key}><i className="inline-block size-2.5 rounded-[3px] mr-1.5 -mb-px" style={{ background: s.color }} />{s.label}</span>)}
      </div>
    </div>
  );
}

// ---- agent types: projects per domain (one series, so no legend) ---------------------------------

export function DomainBars({ rows }: { rows: { domain: string; projects: number }[] }) {
  const max = Math.max(1, ...rows.map((r) => r.projects));
  if (!rows.length) return <div className="text-muted text-[13px]">No projects yet.</div>;
  return (
    <div className="grid gap-2.5">
      {rows.map((r) => (
        <div key={r.domain} className="grid grid-cols-[110px_minmax(0,1fr)_28px] items-center gap-3 text-[13px]">
          <span className="text-ink-2 truncate" title={domainLabel(r.domain)}>{domainLabel(r.domain)}</span>
          <span className="h-2.5 rounded-r-[4px] bg-surface-3 overflow-hidden">
            <span className="grow-x block h-full rounded-r-[4px] bg-accent" style={{ width: `${(r.projects / max) * 100}%` }} />
          </span>
          <b className="tabular-nums text-right">{r.projects}</b>
        </div>
      ))}
    </div>
  );
}

// ---- the last decisions of one project, oldest left ------------------------------------------------

function HistoryTile({ d }: { d: Decision }) {
  const t = tone(d);
  const tip = useTip(statusLabel(d));
  const bg = { good: "bg-good", bad: "bg-bad", warn: "bg-warn", neutral: "bg-line-strong", run: "bg-accent" }[t];
  return (
    <span {...tip} aria-label={statusLabel(d)} className={cx("size-[15px] rounded-[4px] grid place-items-center text-white flex-none", bg)}>
      <Icon name={t === "good" ? "check" : t === "bad" ? "x" : "alert"} size={9} strokeWidth={3.4} />
    </span>
  );
}

export function MiniHistory({ items, empty = "No runs yet" }: { items: Decision[]; empty?: string }) {
  if (!items.length) return <span className="text-muted text-[13px]">{empty}</span>;
  return <span className="inline-flex gap-[3px] items-center">{items.map((d, i) => <HistoryTile key={i} d={d} />)}</span>;
}

export function When({ iso }: { iso: string | null | undefined }) {
  return <span title={fmtDate(iso)} className="whitespace-nowrap">{ago(iso)}</span>;
}

// ---- one agent over its runs: a line per measure, one dot per run (coloured by its result) ----------

const DOT_COLOR: Record<string, string> = { GO: "var(--good)", NO_GO: "var(--bad)", NO_GO_INSUFFICIENT_EVIDENCE: "var(--warn)" };

export interface RunPoint { run_id: string; at: string | null; label: string; decision: Decision | null; value: number | null }

export function RunLine({ points, max, fmt, tick, limit, limitLabel, onOpen, height = 150, aria }: {
  points: RunPoint[]; max?: number; fmt: (v: number) => string; tick?: (v: number) => string; limit?: number | null; limitLabel?: string;
  onOpen?: (runId: string) => void; height?: number; aria: string;
}) {
  const [box, W] = useWidth<HTMLDivElement>(420);
  const H = height, left = 40, right = 10, bottom = 20, top = 10;
  const vals = points.map((p) => p.value).filter((v): v is number => v != null);
  const hi = max ?? niceCeil(Math.max(1, ...vals, limit ?? 0) * 1.15);
  const y0 = H - bottom;
  const y = (v: number) => y0 - (v / hi) * (y0 - top);
  const n = points.length;
  const x = (i: number) => n <= 1 ? left + (W - left - right) / 2 : left + (i * (W - left - right)) / (n - 1);
  const drawn = points.map((p, i) => ({ p, i })).filter(({ p }) => p.value != null);
  const path = drawn.map(({ p, i }, j) => `${j ? "L" : "M"}${x(i).toFixed(1)},${y(p.value as number).toFixed(1)}`).join("");
  return (
    <div ref={box}>
      <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} className="chart block max-w-full" role="img" aria-label={aria}>
        {[0, hi / 2, hi].map((t) => (
          <g key={t}>
            <line x1={left} x2={W - right} y1={y(t)} y2={y(t)} stroke="var(--line)" strokeDasharray={t ? "3 4" : undefined} />
            <text x={left - 8} y={y(t) + 4} textAnchor="end">{(tick || fmt)(t)}</text>
          </g>
        ))}
        {limit != null && limit <= hi && (
          <g>
            <line x1={left} x2={W - right} y1={y(limit)} y2={y(limit)} stroke="var(--bad)" strokeDasharray="5 4" strokeOpacity={0.7} />
            <text x={W - right} y={y(limit) - 5} textAnchor="end" fill="var(--bad)">{limitLabel || `limit ${fmt(limit)}`}</text>
          </g>
        )}
        <defs>
          <linearGradient id="aur-area" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stopColor="var(--accent)" stopOpacity=".28" /><stop offset="1" stopColor="var(--accent-2)" stopOpacity="0" /></linearGradient>
          <linearGradient id="aur-line" x1="0" x2="1" y1="0" y2="0"><stop offset="0" stopColor="var(--accent)" /><stop offset="1" stopColor="var(--accent-2)" /></linearGradient>
        </defs>
        {drawn.length > 1 && <path d={`${path}L${x(drawn[drawn.length - 1].i).toFixed(1)},${y0}L${x(drawn[0].i).toFixed(1)},${y0}Z`} fill="url(#aur-area)" className="area-fade" />}
        {drawn.length > 1 && <path d={path} pathLength={1} className="line-draw" fill="none" stroke="url(#aur-line)" strokeWidth={2.25} strokeLinejoin="round" strokeLinecap="round" />}
        {drawn.map(({ p, i }) => (
          <TipG key={p.run_id} content={(
            <div className="grid gap-0.5 min-w-[140px]"><b>{fmt(p.value as number)}</b>
              <span className="text-ink-2">{p.label}</span><span className="text-muted">{p.at ? fmtDate(p.at) : ""}</span></div>
          )}>
            <circle cx={x(i)} cy={y(p.value as number)} r={9} fill="transparent" className={onOpen ? "cursor-pointer" : undefined}
              onClick={onOpen ? () => onOpen(p.run_id) : undefined} />
            <circle cx={x(i)} cy={y(p.value as number)} r={3.5} fill={DOT_COLOR[p.decision || ""] || "var(--line-strong)"} stroke="var(--surface)" strokeWidth={1.5} pointerEvents="none" />
          </TipG>
        ))}
        {n > 0 && <text x={left} y={H - 4} textAnchor="start">{points[0].at ? dayLabel(points[0].at.slice(0, 10)) : ""}</text>}
        {n > 1 && <text x={W - right} y={H - 4} textAnchor="end">{points[n - 1].at ? dayLabel(points[n - 1].at!.slice(0, 10)) : ""}</text>}
      </svg>
    </div>
  );
}

function niceCeil(v: number) {
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) if (m * p >= v) return m * p;
  return 10 * p;
}
