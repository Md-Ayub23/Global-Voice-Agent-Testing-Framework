// The release readiness report (src/vatf/readiness.py) as cards: the verdict, the executive summary,
// the scorecard by area, the findings with their proof, the numbers, what was covered and how the run was made.
// The same parts make the run page and the printable report (pages/PrintReport.tsx).
import { useMemo, useState, type ReactNode } from "react";
import { Icon } from "./Icon";
import { Card, CardHead, Notice } from "./ui";
import { cx } from "../lib/format";
import { AREA_STATUS, LEVEL, TONE_BAR, TONE_SOFT, TONE_TEXT, severityOf, type LevelTone } from "../lib/modes";
import type { Finding, MetricLine, Proof, Readiness, ScoreRow } from "../lib/types";

function Pill({ tone, icon, children, className }: { tone: LevelTone; icon?: Parameters<typeof Icon>[0]["name"]; children: ReactNode; className?: string }) {
  return (
    <span className={cx("inline-flex items-center gap-1 h-[22px] px-2 rounded-md text-[12px] font-semibold whitespace-nowrap", TONE_SOFT[tone], className)}>
      {icon && <Icon name={icon} size={12} strokeWidth={2.5} />}{children}
    </span>
  );
}

// ---- verdict ----------------------------------------------------------------------------------

export function VerdictBanner({ r, actions, note }: { r: Readiness; actions?: ReactNode; note?: ReactNode }) {
  const lv = LEVEL[r.verdict.level];
  const c = r.counts;
  const chips: [number, string, LevelTone][] = [
    [c.blocking, "blocking", "bad"], [c.problems - c.blocking, "to watch", "warn"],
    [c.to_review, "need you", "warn"], [c.evidence, "evidence gaps", "warn"], [c.opinions, "judge opinions", "neutral"],
  ];
  return (
    <div id="readiness-verdict" data-level={r.verdict.level}
      className="relative overflow-hidden rounded-[10px] px-4 sm:px-6 py-5 border border-line bg-surface animate-reveal print:animate-none">
      <div className={cx("absolute left-0 inset-y-0 w-1", TONE_BAR[lv.tone])} />
      <div className="flex flex-wrap items-start gap-5">
        <div className="flex items-start gap-4 flex-[1_1_360px] min-w-0">
          <div className={cx("size-12 rounded-[10px] grid place-items-center flex-none", TONE_SOFT[lv.tone])}>
            <Icon name={lv.icon} size={26} strokeWidth={2.4} />
          </div>
          <div className="min-w-0">
            <div className="font-mono text-[11px] uppercase tracking-[.08em] text-muted">Release readiness · {r.mode.title}</div>
            <div className={cx("text-[24px] sm:text-[28px] font-bold tracking-[-.03em] leading-[1.15] mt-0.5", TONE_TEXT[lv.tone])}>{r.verdict.headline}</div>
            <div className="mt-1.5 text-[14px] text-ink-2 max-w-[720px]">{r.verdict.line}</div>
            <div className="flex flex-wrap gap-1.5 mt-3">
              {chips.filter(([n]) => n > 0).map(([n, label, tone]) => <Pill key={label} tone={tone}>{n} {label}</Pill>)}
              {chips.every(([n]) => !n) && r.verdict.level !== "no_result" && <Pill tone="good" icon="check">No findings</Pill>}
            </div>
          </div>
        </div>
        {actions && <div className="flex flex-wrap items-center gap-2.5 print:hidden">{actions}</div>}
      </div>
      {note && <div className="mt-4">{note}</div>}
    </div>
  );
}

export function ExecutiveSummary({ r, skipVerdict }: { r: Readiness; skipVerdict?: boolean }) {
  // The second line repeats the verdict; on the run page the banner already shows it.
  const lines = skipVerdict ? r.summary.filter((_, i) => i !== 1) : r.summary;
  return (
    <Card id="executive-summary">
      <CardHead level={2} title="Executive summary" sub={r.mode.question} />
      <ul className="grid gap-2">
        {lines.map((l, i) => (
          <li key={i} className="flex gap-2.5 items-start text-[13.5px] leading-[1.5]">
            <span className="mt-[7px] size-1.5 rounded-full bg-ink-2 flex-none" />
            <span className="min-w-0">{l}</span>
          </li>
        ))}
      </ul>
    </Card>
  );
}

// ---- scorecard ----------------------------------------------------------------------------------

function ScoreTile({ row }: { row: ScoreRow }) {
  const st = AREA_STATUS[row.status];
  return (
    <div data-area={row.area} className="bg-surface border border-line rounded-[10px] p-4 flex flex-col gap-1.5 min-w-0 break-inside-avoid">
      <div className="flex items-center gap-2">
        <span className="flex-1 min-w-0 text-[12.5px] font-semibold text-ink-2 truncate">{row.title}</span>
        <Pill tone={st.tone} icon={st.icon}>{st.label}</Pill>
      </div>
      <div className="text-[15px] font-semibold tracking-[-.01em] leading-snug">{row.value}</div>
      {row.percent != null && (
        <div className="h-1.5 rounded-full bg-surface-3 overflow-hidden" aria-hidden="true">
          <div className={cx("h-full rounded-full", TONE_BAR[st.tone])} style={{ width: `${Math.round(row.percent * 100)}%` }} />
        </div>
      )}
      {row.detail && <div className="text-[12px] text-muted">{row.detail}</div>}
    </div>
  );
}

export function Scorecard({ r }: { r: Readiness }) {
  if (!r.scorecard.length) return null;
  return (
    <div id="scorecard">
      <div className="flex items-baseline gap-2 mb-2.5">
        <h2 className="font-semibold text-[15px] tracking-[-.01em]">Scorecard</h2>
        <span className="text-muted text-[12.5px]">each area on its own · no overall score, so one serious problem can't be averaged away</span>
      </div>
      <div className="grid gap-3 grid-cols-[repeat(auto-fill,minmax(220px,1fr))] stagger print:grid-cols-3">
        {r.scorecard.map((row) => <ScoreTile key={row.area} row={row} />)}
      </div>
    </div>
  );
}

// ---- findings -----------------------------------------------------------------------------------

function ProofLine({ p, callHref }: { p: Proof; callHref?: (testId: string, seq?: number | null) => string }) {
  const href = callHref?.(p.test_id, p.seq);
  return (
    <div className="flex gap-2.5 items-start text-[12.5px] py-1.5 border-t border-line first:border-t-0 break-inside-avoid">
      <Icon name="quote" size={13} className="mt-[3px] text-muted flex-none" />
      <div className="min-w-0 flex-1">
        {p.quote ? (
          <div><span className="font-semibold capitalize text-ink-2">{p.who || "call"}{p.at ? ` at ${p.at}` : ""}: </span>
            <span className="text-ink">“{p.quote}”</span></div>
        ) : <div className="text-ink-2">{p.note || "See the call"}</div>}
        {p.quote && p.note && <div className="text-muted">{p.note}</div>}
      </div>
      {href ? <a href={href} className="font-mono text-[11px] text-muted whitespace-nowrap hover:text-accent print:hidden">{p.test_id.slice(-9)} <Icon name="arrow" size={11} className="inline" /></a>
        : <span className="font-mono text-[11px] text-muted whitespace-nowrap">{p.test_id.slice(-9)}</span>}
    </div>
  );
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid gap-x-4 gap-y-0.5 sm:grid-cols-[150px_minmax(0,1fr)] text-[13px]">
      <dt className="text-muted font-medium">{label}</dt>
      <dd className="min-w-0 break-words">{children}</dd>
    </div>
  );
}

export function FindingCard({ f, open: start, callHref, print }: { f: Finding; open?: boolean; callHref?: (testId: string, seq?: number | null) => string; print?: boolean }) {
  const [open, setOpen] = useState(!!start || !!print);
  const sev = severityOf(f);
  const shown = open || print;
  return (
    <div data-finding={f.id} data-status={f.status}
      className={cx("border rounded-[10px] bg-surface break-inside-avoid", f.blocks ? "border-[color-mix(in_srgb,var(--bad)_35%,var(--line))]" : "border-line")}>
      <button type="button" onClick={() => setOpen((o) => !o)} aria-expanded={shown}
        className="w-full flex items-start gap-3 p-3.5 text-left cursor-pointer print:cursor-default">
        <span className="font-mono text-[11px] text-muted mt-[3px] w-6 flex-none">{f.id}</span>
        <div className="flex-1 min-w-0">
          <div className="flex flex-wrap items-center gap-1.5">
            <Pill tone={sev.tone} icon={sev.icon}>{sev.label}</Pill>
            <b className="text-[14px]">{f.title}</b>
          </div>
          <div className="text-[12px] text-muted mt-1 flex flex-wrap gap-x-2">
            <span>{f.area_title}</span><span>·</span><span>{f.how_often.text}</span>
            {f.tests.length > 0 && <><span>·</span><span className="truncate max-w-[420px]">{f.tests.length === 1 ? f.tests[0].title : `${f.tests.length} test cases`}</span></>}
          </div>
        </div>
        <Icon name="chevron" size={15} className={cx("text-muted mt-1 transition-transform print:hidden", shown && "rotate-90")} />
      </button>
      {shown && (
        <div className="px-3.5 pb-3.5 pl-[50px] grid gap-2.5">
          <dl className="grid gap-2">
            <Row label="What happened">{f.what}</Row>
            {f.expected && <Row label="What should happen">{f.expected}</Row>}
            <Row label="Who to look at">{f.cause_text}</Row>
            {f.fix && <Row label="Where to look">{f.fix}</Row>}
            {f.tests.length > 1 && <Row label="Test cases">{f.tests.map((t) => `${t.title} (${t.calls})`).join(" · ")}</Row>}
          </dl>
          {f.caution && <Notice tone="warn" icon="info" className="text-[12.5px]">{f.caution}</Notice>}
          {f.proofs.length > 0 && (
            <div className="rounded-lg bg-surface-2 px-3 py-1.5">
              <div className="font-mono text-[10.5px] uppercase tracking-[.08em] text-muted pt-1 pb-0.5">Proof from the calls</div>
              {f.proofs.map((p, i) => <ProofLine key={i} p={p} callHref={callHref} />)}
            </div>
          )}
          {f.calls.length > f.proofs.length && callHref && (
            <div className="text-[12px] text-muted flex flex-wrap gap-x-2 gap-y-1 print:hidden">
              All calls with this finding:
              {f.calls.slice(0, 20).map((t) => <a key={t} href={callHref(t)} className="font-mono">{t.slice(-9)}</a>)}
              {f.calls.length > 20 && <span>+{f.calls.length - 20} more</span>}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

type FindingFilter = "all" | "blocking" | "problems" | "check" | string;

export function FindingsList({ r, callHref, print }: { r: Readiness; callHref?: (testId: string, seq?: number | null) => string; print?: boolean }) {
  const [filter, setFilter] = useState<FindingFilter>("all");
  const areas = useMemo(() => [...new Map(r.findings.map((f) => [f.area, f.area_title])).entries()], [r.findings]);
  const pick = (f: Finding) => filter === "all" || (filter === "blocking" && f.blocks)
    || (filter === "problems" && f.status === "problem") || (filter === "check" && (f.status === "needs_review" || f.status === "opinion" || f.status === "evidence"))
    || f.area === filter;
  const shown = print ? r.findings : r.findings.filter(pick);
  const filters: [FindingFilter, string, number][] = [
    ["all", "All", r.findings.length], ["blocking", "Blocking", r.counts.blocking], ["problems", "Problems", r.counts.problems],
    ["check", "To confirm", r.counts.to_review + r.counts.opinions + r.counts.evidence],
    ...areas.map(([a, t]) => [a, t, r.findings.filter((f) => f.area === a).length] as [string, string, number]),
  ];
  return (
    <Card id="findings">
      <CardHead level={2} title="Findings" sub="each problem once, with how often it happened and proof from the calls" />
      {!r.findings.length ? (
        <Notice tone="good" icon="check">No findings: every check that ran in this test passed.</Notice>
      ) : (
        <>
          {!print && (
            <div className="flex flex-wrap gap-1.5 mb-3" role="tablist">
              {filters.filter(([v, , n]) => n > 0 || v === "all").map(([v, label, n]) => (
                <button key={v} type="button" role="tab" aria-selected={filter === v} onClick={() => setFilter(v)} data-filter={v}
                  className={cx("h-[26px] px-2.5 rounded-md text-[12px] font-medium border transition-colors cursor-pointer",
                    filter === v ? "bg-ink text-surface border-ink" : "bg-surface text-ink-2 border-line hover:border-line-strong")}>
                  {label} <span className="tabular-nums opacity-70">{n}</span>
                </button>
              ))}
            </div>
          )}
          <div className="grid gap-2">
            {shown.map((f, i) => <FindingCard key={f.id} f={f} open={i < 2 && f.status === "problem"} callHref={callHref} print={print} />)}
            {!shown.length && <div className="text-muted text-[13px]">Nothing in this group.</div>}
          </div>
          {r.counts.opinions > 0 && (
            <div className="text-[12px] text-muted mt-3">
              Judges' opinions come from AI judges that aren't trusted for this agent yet (too few calls labelled by a person). They never change the verdict.
            </div>
          )}
        </>
      )}
    </Card>
  );
}

// ---- numbers, coverage, method ---------------------------------------------------------------------

const BAND_DOT: Record<string, string> = { good: "bg-good", warn: "bg-warn", bad: "bg-bad" };

export function MetricsCard({ r }: { r: Readiness }) {
  const groups = useMemo(() => {
    const m = new Map<string, MetricLine[]>();
    for (const x of r.metrics) m.set(x.group, [...(m.get(x.group) || []), x]);
    return [...m.entries()];
  }, [r.metrics]);
  if (!r.metrics.length) return null;
  return (
    <Card id="metrics">
      <CardHead level={2} title="Key numbers" />
      <div className="grid gap-5 md:grid-cols-2 print:grid-cols-2">
        {groups.map(([g, rows]) => (
          <div key={g} className="break-inside-avoid">
            <div className="font-mono text-[10.5px] uppercase tracking-[.08em] text-muted mb-1.5">{g}</div>
            <div className="grid">
              {rows.map((x) => (
                <div key={x.title} className="flex items-baseline gap-3 py-1.5 border-t border-line first:border-t-0">
                  <div className="flex-1 min-w-0">
                    <div className="text-[13px] font-medium flex items-center gap-1.5">
                      {x.band && <i className={cx("size-1.5 rounded-full flex-none", BAND_DOT[x.band])} title={x.band} />}{x.title}
                    </div>
                    {x.note && <div className="text-[11.5px] text-muted">{x.note}</div>}
                  </div>
                  <div className="text-[14px] font-semibold tabular-nums whitespace-nowrap">{x.value}</div>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </Card>
  );
}

export function CoverageCard({ r }: { r: Readiness }) {
  const c = r.coverage;
  const rows: [string, ReactNode][] = [
    ["Test cases run", <>{c.test_cases_run} of {c.test_cases_total}{c.not_run.length > 0 && <span className="text-muted"> · not run: {c.not_run.slice(0, 8).join(", ")}{c.not_run.length > 8 ? ` and ${c.not_run.length - 8} more` : ""}</span>}</>],
    ...(c.attack_calls ? [["Attack calls", <>{c.attack_calls} <span className="text-muted">· {c.attack_kinds.join(", ")}</span></>] as [string, ReactNode]] : []),
    ...(c.tools_total ? [["Tools used", <>{c.tools_used.length} of {c.tools_total}{c.tools_never_used.length > 0 && <span className="text-muted"> · never used: {c.tools_never_used.join(", ")}</span>}</>] as [string, ReactNode]] : []),
    ...(c.conditions.length ? [["Caller conditions", c.conditions.join(", ").replace(/_/g, " ")] as [string, ReactNode]] : []),
    ["Calls with a clear result", c.calls_verified != null ? `${c.calls_verified} of ${c.calls_planned}` : `${c.calls_planned} planned`],
  ];
  return (
    <Card id="coverage">
      <CardHead level={2} title="What was tested" sub="and what wasn't, so a clean result can't hide a gap" />
      <dl className="grid gap-2">{rows.map(([k, v]) => <Row key={k} label={k}>{v}</Row>)}</dl>
    </Card>
  );
}

export function MethodCard({ r }: { r: Readiness }) {
  return (
    <Card id="method">
      <CardHead level={2} title="How this test was run" />
      <dl className="grid gap-x-6 gap-y-2.5 grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 print:grid-cols-3 text-[13px]">
        {r.method.map((m) => (
          <div key={m.label}><dt className="text-muted text-[12px]">{m.label}</dt><dd className="font-medium break-words">{m.value}</dd></div>
        ))}
      </dl>
    </Card>
  );
}

export const isReadiness = (x: unknown): x is Readiness => !!x && typeof x === "object" && "verdict" in (x as object);
