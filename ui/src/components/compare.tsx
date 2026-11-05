// Phase 6 pieces shared by the run report, Results and the Compare page: the list of changes since
// the baseline (or between two runs), the counts, and the "Set as baseline" button.
import { useState } from "react";
import { api } from "../lib/api";
import { fmtDate } from "../lib/format";
import type { BaselineBrief, Change, Comparison, Direction } from "../lib/types";
import { useToast } from "./feedback";
import { Icon, type IconName } from "./Icon";
import { Badge, Button, Card, CardHead, Chip, EmptyState, Notice, Table, type ChipTone } from "./ui";

export const DIRECTION: Record<Direction, { label: string; tone: ChipTone; icon: IconName }> = {
  regression: { label: "New failure", tone: "bad", icon: "alert" },
  worse: { label: "Worse", tone: "warn", icon: "trend" },
  better: { label: "Better", tone: "good", icon: "check" },
  new: { label: "New test", tone: "accent", icon: "plus" },
  removed: { label: "Not run", tone: "default", icon: "minus" },
  same: { label: "Still failing", tone: "default", icon: "minus" },
};
const KIND: Record<Change["kind"], string> = {
  test: "Test", latency: "Response time", score: "Score", metric: "Call quality", judge: "AI judge", safety: "Safety", load: "Load test",
};

function value(v: number | string | null, c: Change): string {
  if (v == null) return "—";
  if (typeof v === "string") return v;
  if (c.kind === "latency") return `${v.toLocaleString()} ms`;
  if (c.kind === "score" || c.kind === "judge") return `${Math.round(v * 100)}%`;
  if (c.kind === "load") return c.id.startsWith("p95@") ? `${v.toLocaleString()} ms` : c.id === "capacity" ? `${v} at once` : `${v}%`;
  if (c.kind === "metric") return c.unit === "%" ? `${v}%` : `${v.toLocaleString()} ${c.unit || ""}`.trim();
  return String(v);
}

export function CompareCounts({ cmp }: { cmp: Comparison }) {
  const n = cmp.counts;
  const tiles: [string, number, string][] = [
    ["New failures", n.regression, n.regression ? "text-bad" : ""], ["Worse", n.worse, n.worse ? "text-warn" : ""],
    ["Better", n.better, n.better ? "text-good" : ""], ["New tests", n.new, ""], ["Not run", n.removed, ""],
  ];
  return (
    <div className="grid gap-3 grid-cols-[repeat(auto-fit,minmax(120px,1fr))]" id="compare-counts">
      {tiles.map(([label, count, cls]) => (
        <Card key={label} className="py-3">
          <div className="font-mono text-[10.5px] uppercase tracking-[.08em] text-muted">{label}</div>
          <div className={`text-[24px] font-semibold tabular-nums leading-tight mt-0.5 ${cls}`}>{count}</div>
        </Card>
      ))}
    </div>
  );
}

export function LikeForLike({ cmp }: { cmp: Comparison }) {
  if (cmp.like_for_like && !cmp.differences.length) return null;
  const words = (v: unknown) => (v === true ? "yes" : v === false ? "no" : v == null || v === "" ? "none" : String(v));
  return (
    <Notice tone={cmp.like_for_like ? "info" : "warn"} icon="info">
      {cmp.like_for_like ? <b>The runs differ in: </b> : <b>Not like-for-like, so nothing here can block a release. </b>}
      {cmp.differences.map((d) => `${d.title} ${words(d.before)} → ${words(d.after)}`).join("; ")}.
    </Notice>
  );
}

export function ChangesList({ cmp, callHref, showSame }: { cmp: Comparison; callHref?: (scenarioId: string) => string | undefined; showSame?: boolean }) {
  const rows = cmp.changes.filter((c) => showSame || c.direction !== "same");
  if (!rows.length) {
    return <EmptyState icon="check-circle" title="No changes beyond the limits"
      text="Every test that passed before still passes, and no number moved by more than its limit." />;
  }
  return (
    <Table head={["Change", "What", "Before", "Now", "Blocks"]}>
      {rows.map((c) => {
        const d = DIRECTION[c.direction];
        const href = c.kind === "test" ? callHref?.(c.id) : undefined;
        return (
          <tr key={`${c.kind}-${c.id}`} data-change={c.direction}>
            <td className="whitespace-nowrap"><Chip tone={d.tone} icon={d.icon}>{d.label}</Chip></td>
            <td className="min-w-[260px]">
              <div className="font-mono text-[10.5px] uppercase tracking-[.08em] text-muted">{KIND[c.kind]}</div>
              <b>{href ? <a href={href}>{c.title}</a> : c.title}</b>
              <div className="text-[12.5px] text-ink-2">{c.message}</div>
            </td>
            <td className="tabular-nums whitespace-nowrap">{c.band_before ? <>{value(c.before, c)} <span className="text-muted">({c.band_before})</span></> : value(c.before, c)}</td>
            <td className="tabular-nums whitespace-nowrap">{c.band_after ? <>{value(c.after, c)} <span className="text-muted">({c.band_after})</span></> : value(c.after, c)}</td>
            <td>{c.blocks ? <Badge status="FAIL" label="blocks" /> : <span className="text-muted text-[12px]">no</span>}</td>
          </tr>
        );
      })}
    </Table>
  );
}

// The run report's "Vs baseline" tab.
export function VsBaseline({ cmp, baselineHref, callHref }: { cmp: Comparison; baselineHref: string; callHref: (scenarioId: string) => string | undefined }) {
  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHead level={2} title="Compared with the baseline"
          sub={<>Baseline run <a href={baselineHref} className="font-mono">{cmp.baseline.run_id}</a> ({cmp.baseline.decision}) from {fmtDate(cmp.baseline.finished_at)}. {cmp.blocking
            ? `${cmp.blocking} change(s) set to block made this run “Not ready”.` : "Nothing set to block got worse."}</>} />
        <CompareCounts cmp={cmp} />
        <div className="mt-3"><LikeForLike cmp={cmp} /></div>
      </Card>
      <Card flush><ChangesList cmp={cmp} callHref={callHref} showSame /></Card>
    </div>
  );
}

// "Set as baseline" for a finished run. Bug-switch runs are refused by the server with the reason.
export function SetBaselineButton({ runId, current, onDone, size }: { runId: string; current?: boolean; onDone?: (b: BaselineBrief) => void; size?: "sm" | "md" }) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  if (current) return <Chip tone="accent" icon="anchor">Baseline</Chip>;
  const go = async () => {
    setBusy(true);
    try {
      const r = await api.post<{ baseline: BaselineBrief }>(`/api/runs/${runId}/baseline`, {});
      toast(r.baseline.warnings?.length ? `Baseline set. Note: ${r.baseline.warnings[0]}` : "Baseline set: new runs are compared with this one");
      onDone?.(r.baseline);
    } catch (e) {
      toast((e as Error).message, "bad");
    } finally {
      setBusy(false);
    }
  };
  return <Button id={`set-baseline-${runId}`} size={size} icon="anchor" busy={busy} onClick={(e) => { e.stopPropagation(); void go(); }}>Set as baseline</Button>;
}

export function BaselineIcon() {
  return <Icon name="anchor" size={13} className="text-accent" />;
}
