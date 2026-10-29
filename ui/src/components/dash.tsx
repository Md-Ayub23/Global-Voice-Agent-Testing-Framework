// Pieces of the home dashboard and the project overview: number tiles, project avatars, the
// runs-per-day chart, the agent-type bars and the small decision history.
import { useEffect, useRef, useState, type ReactNode } from "react";
import { ago, cx, fmtDate } from "../lib/format";
import { avatarHue, domainLabel, initials } from "../lib/links";
import { statusLabel, tone } from "../lib/status";
import type { Decision, DecisionDay } from "../lib/types";
import { TipG, useTip } from "./feedback";
import { Icon, type IconName } from "./Icon";

export function ProjectAvatar({ name, k, size = 40, className }: { name: string; k: string; size?: number; className?: string }) {
  return (
    <span className={cx("avatar grid place-items-center flex-none font-bold tracking-[-.02em] select-none", className)}
      style={{ ["--h" as string]: avatarHue(k), width: size, height: size, borderRadius: size * 0.3, fontSize: size * 0.38 }}
      aria-hidden="true">
      {initials(name)}
    </span>
  );
}

export function Kpi({ icon, label, value, foot, tone: t = "accent" }: {
  icon: IconName; label: string; value: ReactNode; foot?: ReactNode; tone?: "accent" | "good" | "warn" | "bad";
}) {
  const tile = { accent: "bg-accent-soft text-accent-ink", good: "bg-good-soft text-good", warn: "bg-warn-soft text-warn", bad: "bg-bad-soft text-bad" }[t];
  return (
    <div className="bg-surface border border-line rounded-[14px] shadow-soft p-[18px] grid gap-1.5 min-w-0">
      <div className="flex items-center gap-2.5 text-ink-2 font-semibold text-[12.5px]">
        <span className={cx("size-8 rounded-[10px] grid place-items-center", tile)}><Icon name={icon} /></span>{label}
      </div>
      <div className="text-[30px] font-extrabold tracking-[-.03em] tabular-nums leading-[1.1] mt-1">{value}</div>
      {foot && <div className="text-muted text-xs">{foot}</div>}
    </div>
  );
}

// ---- runs per day, stacked by decision ---------------------------------------------------------

const SERIES: { key: keyof DecisionDay; label: string; color: string }[] = [
  { key: "GO", label: "GO", color: "var(--good)" },
  { key: "NO_GO", label: "NO-GO", color: "var(--bad)" },
  { key: "NO_GO_INSUFFICIENT_EVIDENCE", label: "Not enough evidence", color: "var(--warn)" },
  { key: "none", label: "No decision", color: "var(--line-strong)" },
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
          ? <path key={s.key} d={topRounded(bx, y, bar, h - gap, 4)} fill={s.color} />
          : <rect key={s.key} x={bx} y={y} width={bar} height={h - gap} fill={s.color} />;
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
      <div className="flex gap-3.5 flex-wrap text-xs text-ink-2 mt-2">
        {SERIES.map((s) => <span key={s.key}><i className="inline-block size-2.5 rounded-[3px] mr-1.5 -mb-px" style={{ background: s.color }} />{s.label}</span>)}
      </div>
    </div>
  );
}

// ---- agent types: projects per domain (one series, so no legend) ---------------------------------

export function DomainBars({ rows }: { rows: { domain: string; projects: number }[] }) {
  const max = Math.max(1, ...rows.map((r) => r.projects));
  if (!rows.length) return <div className="text-muted text-[12.5px]">No projects yet.</div>;
  return (
    <div className="grid gap-2.5">
      {rows.map((r) => (
        <div key={r.domain} className="grid grid-cols-[110px_minmax(0,1fr)_28px] items-center gap-3 text-[13px]">
          <span className="text-ink-2 truncate" title={domainLabel(r.domain)}>{domainLabel(r.domain)}</span>
          <span className="h-2.5 rounded-r-[4px] bg-surface-3 overflow-hidden">
            <span className="block h-full rounded-r-[4px] bg-accent transition-[width] duration-700 ease-out-soft" style={{ width: `${(r.projects / max) * 100}%` }} />
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
  if (!items.length) return <span className="text-muted text-[12.5px]">{empty}</span>;
  return <span className="inline-flex gap-[3px] items-center">{items.map((d, i) => <HistoryTile key={i} d={d} />)}</span>;
}

export function When({ iso }: { iso: string | null | undefined }) {
  return <span title={fmtDate(iso)} className="whitespace-nowrap">{ago(iso)}</span>;
}
