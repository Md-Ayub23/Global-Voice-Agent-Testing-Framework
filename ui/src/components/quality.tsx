// Phase 5 call quality: metric tiles with good / warn / bad bands (run report), results by caller
// condition, and one call's metrics (call page). Data: decision.quality, decision.by_condition,
// verdict.metrics.quality (src/vatf/metrics.py).
import { Tip } from "./feedback";
import { Icon } from "./Icon";
import { Card, CardHead, Chip, Table } from "./ui";
import { cx, fmtMs, pct } from "../lib/format";
import type { Band, CallConditions, ConditionRow, MetricDef, MetricUnit, QualityRow, QualityValue } from "../lib/types";

const BAND: Record<"good" | "warn" | "bad" | "none", [string, string, string]> = {
  good: ["bg-good-soft text-good", "bg-good", "good"],
  warn: ["bg-warn-soft text-warn", "bg-warn", "warn"],
  bad: ["bg-bad-soft text-bad", "bg-bad", "bad"],
  none: ["bg-surface-2 text-ink-2", "bg-muted", "info"],
};

export function BandBadge({ band, blocks }: { band: Band; blocks?: boolean }) {
  const [cls, dot, word] = BAND[band || "none"];
  return (
    <span className={cx("inline-flex items-center gap-1.5 h-[22px] px-2 rounded-md text-[12px] font-medium whitespace-nowrap", cls)}>
      <i className={cx("size-1.5 rounded-full flex-none", dot)} />
      {word}{blocks && band === "bad" ? " · blocks" : ""}
    </span>
  );
}

export function fmtMetric(value: number | boolean | null | undefined, unit: MetricUnit): string {
  if (value == null) return "—";
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (unit === "ms") return fmtMs(value);
  if (unit === "%") return `${Number(value.toFixed(1))}%`;
  if (unit === "wpm") return `${Math.round(value)} wpm`;
  if (unit === "s") return `${Number(value.toFixed(1))} s`;
  return `${Number(value.toFixed(1))} turns`;
}

// "good ≤ 1,500 ms · warn ≤ 3,000 ms" / "no band"
export function bandWords(m: { good: number | null; warn: number | null; better: string; unit: MetricUnit }): string {
  if (m.good == null) return "shown only, no band";
  const op = m.better === "higher" ? "≥" : "≤";
  const w = m.warn ?? m.good;
  return `good ${op} ${fmtMetric(m.good, m.unit)} · warn ${op} ${fmtMetric(w, m.unit)}`;
}

const DETAIL_WORDS: Record<string, string> = {
  turns: "timed answers", cut_ins: "cut-ins", backchannels: "mm-hms", words: "words compared", silences: "silences",
  never: "never checked in", hits: "calls",
};

function detailLine(r: QualityRow): string {
  const d = r.detail || {};
  const parts = Object.entries(d).filter(([k]) => k !== "note").map(([k, v]) => `${v} ${DETAIL_WORDS[k] || k}`);
  if (d.note) parts.unshift(String(d.note));
  return [`${r.calls} call(s)`, ...parts].join(" · ");
}

const TILES_SUB = "Read off each call's recording · bands are this agent's (Set up › Pass rules) · only a metric set to block can stop a release";
export function QualityTiles({ rows, sub = TILES_SUB }: { rows: QualityRow[]; sub?: string }) {
  return (
    <Card>
      <CardHead level={2} title="Call quality" sub={sub} />
      <div className="grid gap-3 grid-cols-[repeat(auto-fill,minmax(210px,1fr))]" id="quality-tiles">
        {rows.map((r) => (
          <div key={r.id} data-metric={r.id} className="rounded-[10px] border border-line p-3.5 grid gap-1 min-w-0">
            <div className="flex items-center gap-1.5 text-ink-2 text-[12.5px] font-semibold min-w-0">
              <span className="truncate">{r.title}</span>
              <Tip content={r.about}><span className="text-muted flex-none"><Icon name="info" size={12} /></span></Tip>
            </div>
            <div className="text-[22px] font-semibold tracking-[-.02em] tabular-nums leading-[1.2]">{fmtMetric(r.value, r.unit)}</div>
            <div className="flex items-center gap-2 flex-wrap"><BandBadge band={r.band} blocks={r.blocks} />
              {r.blocks && <Chip icon="flag">blocks</Chip>}</div>
            <div className="text-muted text-[11.5px]">{bandWords(r)}</div>
            <div className="text-muted text-[11.5px]">{detailLine(r)}</div>
          </div>
        ))}
      </div>
    </Card>
  );
}

const BEHAVIOUR: Record<string, string> = {
  interrupts: "cuts in", backchannels: "mm-hm", pauses_mid_sentence: "pauses", fillers: "um / uh", goes_quiet: "goes quiet",
};

export function ConditionsTable({ rows }: { rows: ConditionRow[] }) {
  return (
    <Card>
      <CardHead level={2} title="Results by caller condition" sub="Does noise, a phone line or an accent change how the agent does?" />
      <Table head={["Line and voice", "Calls", "Passed", "Pass rate", "Response p50", "p90", "Talked over", "Caller behaviour"]}>
        {rows.map((r) => (
          <tr key={r.label}>
            <td><b>{r.label}</b></td>
            <td className="tabular-nums">{r.calls}</td>
            <td className="tabular-nums">{r.passed}/{r.passed + r.failed}</td>
            <td className="tabular-nums">{pct(r.pass_rate)}</td>
            <td className="tabular-nums">{fmtMs(r.p50_ms)}</td>
            <td className="tabular-nums">{fmtMs(r.p90_ms)}</td>
            <td className="tabular-nums">{r.talked_over_pct == null ? "—" : `${r.talked_over_pct}%`}</td>
            <td><div className="flex flex-wrap gap-1">{r.behaviours.length ? r.behaviours.map((b) => <Chip key={b}>{BEHAVIOUR[b] || b}</Chip>) : <span className="text-muted">—</span>}</div></td>
          </tr>
        ))}
      </Table>
    </Card>
  );
}

// One call: its conditions and each metric with its band.
export function CallQuality({ quality, conditions, defs }: { quality: Record<string, QualityValue>; conditions?: CallConditions; defs: MetricDef[] }) {
  const known = defs.filter((d) => d.id in quality);
  return (
    <Card className="animate-view-in" id="call-quality">
      <CardHead title="Call quality" sub="this call's own numbers and bands" />
      {conditions && (
        <div className="mb-3 text-[12.5px] grid gap-1">
          <div><span className="text-muted">Line and voice:</span> <b>{conditions.label}</b></div>
          {!!conditions.behaviours?.length && <div className="flex flex-wrap gap-1 items-center"><span className="text-muted">Caller:</span>
            {conditions.behaviours.map((b) => <Chip key={b}>{BEHAVIOUR[b] || b}</Chip>)}</div>}
        </div>
      )}
      <div className="flex flex-col divide-y divide-line">
        {known.map((d) => {
          const q = quality[d.id];
          return (
            <div key={d.id} className="py-2 flex items-center gap-3 min-w-0">
              <div className="flex-1 min-w-0">
                <div className="text-[12.5px] font-semibold truncate">{d.title}</div>
                <div className="text-muted text-[11.5px]">{q.never ? "the agent never checked in" : q.note ? String(q.note) : bandWords(d)}</div>
              </div>
              <div className="tabular-nums text-[13px] font-semibold">{q.never ? "never" : fmtMetric(q.value, d.unit)}</div>
              <BandBadge band={q.band} />
            </div>
          );
        })}
      </div>
    </Card>
  );
}
