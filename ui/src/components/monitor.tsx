// Phase 8 real calls: the calls-per-day chart and the words for why a call was flagged.
import { plural } from "../lib/format";
import { useEffect, useRef, useState } from "react";
import type { MetricDef, MonitorDay, MonitorFlag } from "../lib/types";
import { Chip, type ChipTone } from "./ui";

export const CHECK_WORDS: Record<string, string> = {
  "real.tool_failed": "A tool failed",
  "real.false_success": "Possible false success",
  "real.dropped": "Dropped or left hanging",
};

export function flagLabel(f: { kind: string; id: string }, defs: MetricDef[], items: Record<string, string> = {}): string {
  if (f.kind === "metric") return defs.find((d) => d.id === f.id)?.title || f.id;
  if (f.kind === "check") return CHECK_WORDS[f.id] || f.id;
  return items[f.id] || f.id.replace(/[-_]/g, " ");
}

export function FlagChip({ f, defs, items }: { f: MonitorFlag; defs: MetricDef[]; items?: Record<string, string> }) {
  const tone: ChipTone = f.kind === "metric" ? "warn" : f.kind === "review" || f.review ? "warn" : f.kind === "judge" && !f.trusted ? "default" : "bad";
  const icon = f.kind === "metric" ? "activity" : f.kind === "check" ? "flag" : "scale";
  return <Chip tone={tone} icon={icon} title={f.text}>{flagLabel(f, defs, items)}{f.kind === "judge" && !f.trusted ? " (AI, not yet trusted)" : ""}</Chip>;
}

function useWidth<T extends HTMLElement>(initial: number): [React.RefObject<T | null>, number] {
  const ref = useRef<T>(null);
  const [w, setW] = useState(initial);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setW(Math.max(260, el.clientWidth)));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, w];
}

const SERIES: { key: "passed" | "failed" | "other"; label: string; color: string }[] = [
  { key: "passed", label: "Passed", color: "var(--good)" },
  { key: "failed", label: "Failed", color: "var(--bad)" },
  { key: "other", label: "To review or not checked", color: "var(--line-strong)" },
];
const nice = (v: number) => {
  for (const m of [2, 4, 6, 8, 10, 15, 20, 30, 40, 50, 80, 100, 150, 200, 300, 400, 500]) if (m >= v) return m;
  return Math.ceil(v / 500) * 500;
};
const dayLabel = (d: string) => new Date(`${d}T12:00:00Z`).toLocaleDateString(undefined, { day: "numeric", month: "short" });

// Real calls per day, stacked by how they scored. The flagged count is in the tooltip.
export function CallsPerDay({ days }: { days: MonitorDay[] }) {
  const [box, W] = useWidth<HTMLDivElement>(600);
  const H = 190, left = 30, bottom = 22, top = 8;
  const max = nice(Math.max(1, ...days.map((d) => d.calls)));
  const y0 = H - bottom;
  const scale = (y0 - top) / max;
  const slot = (W - left) / days.length;
  const bar = Math.min(26, slot * 0.56);
  const every = slot >= 44 ? 1 : slot >= 22 ? 2 : 4;
  return (
    <div ref={box}>
      <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} className="chart block max-w-full" role="img"
        aria-label={`Real calls per day for the last ${days.length} days: ${days.reduce((a, d) => a + d.calls, 0)} in total`}>
        {[0, max / 2, max].map((t) => (
          <g key={t}>
            <line x1={left} x2={W} y1={y0 - t * scale} y2={y0 - t * scale} stroke="var(--line)" strokeDasharray={t ? "3 4" : undefined} />
            <text x={left - 8} y={y0 - t * scale + 4} textAnchor="end">{t}</text>
          </g>
        ))}
        {days.map((d, i) => {
          const parts = { passed: d.passed, failed: d.failed, other: Math.max(0, d.calls - d.passed - d.failed) };
          let y = y0;
          const x = left + i * slot + (slot - bar) / 2;
          return (
            <g key={d.day} data-day={d.day}>
              <title>{`${dayLabel(d.day)}: ${plural(d.calls, "call")}, ${d.passed} passed, ${d.failed} failed, ${d.flagged} flagged`
                + (d.p90_ms != null ? `, p90 ${d.p90_ms} ms` : "")}</title>
              <rect x={left + i * slot} y={top} width={slot} height={y0 - top} fill="transparent" />
              {SERIES.map((s) => {
                const h = parts[s.key] * scale;
                if (!h) return null;
                y -= h;
                return <rect key={s.key} className="bar-grow" x={x} y={y} width={bar} height={Math.max(1, h - 1)} rx={2} fill={s.color} />;
              })}
            </g>
          );
        })}
        {days.map((d, i) => (days.length - 1 - i) % every === 0 && (
          <text key={d.day} x={i === days.length - 1 ? W - 2 : left + i * slot + slot / 2} y={H - 5}
            textAnchor={i === days.length - 1 ? "end" : "middle"}>{i === days.length - 1 ? "Today" : dayLabel(d.day)}</text>
        ))}
      </svg>
      <div className="flex gap-3.5 flex-wrap text-[13px] text-ink-2 mt-2">
        {SERIES.map((s) => <span key={s.key}><i className="inline-block size-2.5 rounded-[3px] mr-1.5 -mb-px" style={{ background: s.color }} />{s.label}</span>)}
      </div>
    </div>
  );
}
