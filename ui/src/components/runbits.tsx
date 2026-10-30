// Pieces several pages share: a run's setting chips, a call's checks, the big decision banner.
import type { ReactNode } from "react";
import { cx } from "../lib/format";
import { tone as toneOf } from "../lib/status";
import { JUDGE_NAME, judgeState, modelName, VOTE_LABEL } from "../lib/judges";
import type { Check, Options, Verdict } from "../lib/types";
import { Tip } from "./feedback";
import { Icon, type IconName } from "./Icon";
import { Badge, Chip, Notice } from "./ui";

export function OptionChips({ run }: { run: { mode?: string | null; offline?: boolean | null; agent_options?: Options | null } }) {
  const bugs = (run.agent_options?.bugs || "").split(",").filter(Boolean);
  return (
    <>
      {run.mode && <Chip tone={run.mode === "voice" ? "agent" : "caller"} icon={run.mode === "voice" ? "mic" : "text"}>{run.mode}</Chip>}
      {run.offline && <Chip icon="bolt">dry run</Chip>}
      {bugs.map((b) => <Chip key={b} tone="bad" icon="alert">{b}</Chip>)}
    </>
  );
}

const SECTION: Record<string, string> = { accuracy: "Accuracy", experience: "Experience", security: "Security" };
const LEVEL: Record<string, string> = {
  backend: "checked in the database", tool_trace: "checked in the tool calls", transcript: "from the transcript",
  speech: "from the recording", ai_opinion: "AI opinion", none: "no evidence",
};
const ORDER: Record<string, number> = { FAIL: 0, ERROR: 1, NEEDS_REVIEW: 2, NOT_VERIFIED: 3, PASS: 4, NOT_APPLICABLE: 5 };

export function ChecksList({ verdict, onEvidence }: { verdict: Verdict | null | undefined; onEvidence?: (seq: number) => void }) {
  const own = (verdict?.checks || []).filter((c) => c.origin !== "judge");
  if (!own.length) return <div className="text-muted text-[12.5px]">No checks.</div>;
  const checks = [...own].sort((a, b) => (ORDER[a.result] ?? 9) - (ORDER[b.result] ?? 9));
  return (
    <div>
      {checks.map((c: Check) => (
        <div key={c.check_id} className="grid grid-cols-[minmax(74px,auto)_1fr] gap-x-2.5 gap-y-1 py-[11px] border-b border-line last:border-b-0">
          <div><Badge status={c.result} /></div>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-1.5">
              <b className="font-mono text-[12.5px]">{c.check_id}</b>
              <Chip>{c.severity}</Chip>
              <Chip>{SECTION[c.section] || c.section}</Chip>
            </div>
            <div className="mt-1">{c.message}</div>
          </div>
          <div className="col-start-2 flex gap-1.5 flex-wrap items-center">
            <span className="text-muted text-[11.5px]">{LEVEL[c.evidence_level] || c.evidence_level}{c.can_block === false ? " · can't block on its own" : ""}</span>
            {(c.evidence || []).map((r) => (
              <Tip key={r.event} content={r.note}>
                <EvRef onClick={() => onEvidence?.(r.event)} data-ev={r.event}>#{r.event}</EvRef>
              </Tip>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

/** How much a judge item counts: decided by a person, calibrated (can block), or advisory. */
export function JudgeWeight({ c }: { c: Check }) {
  if (c.judge?.human) return <Chip tone="accent" icon="user">labelled by you</Chip>;
  if (c.can_block) return <Chip tone="good" icon="shield">trusted</Chip>;
  return <Tip content={`An opinion until your labels show the judges get this item right${c.judge?.calibration ? ` (${c.judge.calibration})` : ""}. It doesn't change the call's result.`}>
    <span><Chip icon="info">not trusted yet</Chip></span></Tip>;
}

/** A judge result as Pass / Problem / Needs you / Opinion: fail / No answer. */
export function JudgeBadge({ c }: { c: Check }) {
  const st = judgeState(c);
  const chip = { good: "good", bad: "bad", warn: "warn", neutral: "default" } as const;
  return <Chip tone={chip[st.tone]} icon={st.icon} className="font-bold">{st.label}</Chip>;
}

// The AI judges' results for one call: what each judge said and why.
export function JudgeList({ verdict, onEvidence, labelHref }: { verdict: Verdict | null | undefined; onEvidence?: (seq: number) => void; labelHref?: string }) {
  const judged = (verdict?.checks || []).filter((c) => c.origin === "judge");
  if (!judged.length) return <div className="text-muted text-[12.5px]">This project has no judge items.</div>;
  const ran = judged.some((c) => c.judge?.votes?.length);
  if (!ran) return <div className="text-muted text-[12.5px]">{judged[0].message}</div>;
  const shown = [...judged].sort((a, b) => (ORDER[a.result] ?? 9) - (ORDER[b.result] ?? 9));
  const quiet = shown.filter((c) => c.result === "NOT_APPLICABLE");
  const loud = shown.filter((c) => c.result !== "NOT_APPLICABLE");
  const row = (c: Check) => (
    <div key={c.check_id} data-judge={c.check_id} className="grid grid-cols-[minmax(74px,auto)_1fr] gap-x-2.5 gap-y-1 py-[11px] border-b border-line last:border-b-0">
      <div><JudgeBadge c={c} /></div>
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-1.5">
          <b className="text-[13px]">{c.judge?.title || c.check_id}</b>
          <Chip>{c.severity}</Chip>
          <JudgeWeight c={c} />
        </div>
        <div className={cx("mt-1 text-[13px]", judgeState(c).kind.startsWith("opinion") && "text-ink-2")}>{c.message}</div>
        {(c.judge?.votes?.length ?? 0) > 0 && (
          <details className="mt-1.5 group">
            <summary className="cursor-pointer text-[12px] text-muted list-none flex items-center gap-1 hover:text-ink">
              <Icon name="chevron" size={13} className="transition-transform group-open:rotate-90" />What each judge said
            </summary>
            <div className="mt-1.5 grid gap-1.5">
              {c.judge!.votes.map((v) => (
                <div key={v.judge} className="text-[12.5px] rounded-lg bg-surface-2 px-2.5 py-1.5">
                  <div className="flex items-center gap-1.5 flex-wrap"><b>{JUDGE_NAME[v.judge]}</b><span className="text-muted">{modelName(v.model)}</span>
                    <Badge status={v.result} label={VOTE_LABEL[v.result]} /></div>
                  {v.reason && <div className="mt-0.5 text-ink-2">{v.reason}</div>}
                </div>
              ))}
            </div>
          </details>
        )}
      </div>
      {c.result !== "NOT_APPLICABLE" && (c.evidence || []).length > 0 && (
        <div className="col-start-2 flex gap-1.5 flex-wrap items-center">
          {(c.evidence || []).map((r) => <EvRef key={r.event} onClick={() => onEvidence?.(r.event)} data-ev={r.event}>#{r.event}</EvRef>)}
        </div>
      )}
    </div>
  );
  return (
    <div>
      {loud.map(row)}
      {quiet.length > 0 && (
        <details className="py-2 group">
          <summary className="cursor-pointer text-[12.5px] text-muted list-none flex items-center gap-1 hover:text-ink">
            <Icon name="chevron" size={13} className="transition-transform group-open:rotate-90" />{quiet.length} item(s) didn't come up in this call
          </summary>
          <div className="mt-1">{quiet.map(row)}</div>
        </details>
      )}
      {(verdict?.judge_notes || []).map((n, i) => <Notice key={i} tone="warn" className="mt-2 text-[12.5px]">{n}</Notice>)}
      {labelHref && <div className="mt-2.5"><a href={labelHref} className="inline-flex items-center gap-1.5 text-[13px] font-semibold"><Icon name="tag" size={14} />Label this call</a></div>}
    </div>
  );
}

export function EvRef({ children, onClick, href, ...rest }: { children: ReactNode; onClick?: () => void; href?: string; "data-ev"?: number }) {
  const cls = "font-mono text-[11.5px] text-muted border border-line rounded-md px-[5px] bg-surface cursor-pointer hover:text-accent-ink hover:border-accent hover:no-underline";
  return href ? <a className={cls} href={href}>{children}</a> : <button type="button" className={cls} onClick={onClick} {...rest}>{children}</button>;
}

const HERO_TONE = {
  good: { text: "text-good", seal: "bg-good shadow-[0_14px_30px_-14px_var(--good)]", glow: "var(--good-soft)" },
  bad: { text: "text-bad", seal: "bg-bad shadow-[0_14px_30px_-14px_var(--bad)]", glow: "var(--bad-soft)" },
  warn: { text: "text-warn", seal: "bg-warn shadow-[0_14px_30px_-14px_var(--warn)]", glow: "var(--warn-soft)" },
  neutral: { text: "text-ink", seal: "bg-accent shadow-[0_14px_30px_-14px_var(--accent)]", glow: "var(--accent-soft)" },
};
export type HeroTone = keyof typeof HERO_TONE;
export const heroToneOf = (decision: string | null | undefined): HeroTone => {
  const t = toneOf(decision);
  return t === "good" || t === "bad" || t === "warn" ? t : "neutral";
};

// The big decision banner: a seal, a title, and the reasons below.
export function Hero({ tone, icon, eyebrow, title, children, actions, reasons, footer, className }: {
  tone: HeroTone; icon?: IconName; eyebrow?: ReactNode; title: ReactNode; children?: ReactNode; actions?: ReactNode; reasons?: ReactNode[]; footer?: ReactNode; className?: string;
}) {
  const t = HERO_TONE[tone];
  const ic: IconName = icon || (tone === "good" ? "check" : tone === "bad" ? "x" : tone === "warn" ? "alert" : "play");
  return (
    <div data-hero={tone} className={cx("relative overflow-hidden rounded-[18px] px-4 sm:px-[26px] py-5 sm:py-6 border border-line bg-surface shadow-card animate-reveal", className)}>
      <div className="absolute inset-0 pointer-events-none opacity-90" style={{ background: `radial-gradient(1200px 300px at -10% -40%, ${t.glow}, transparent 60%)` }} />
      <div className="relative">
        <div className="flex flex-wrap items-start gap-5">
          <div className="flex items-center gap-4 flex-[1_1_320px] min-w-0">
            <div className={cx("size-12 sm:size-16 rounded-[14px] sm:rounded-[18px] grid place-items-center text-white flex-none animate-pop [&_svg]:size-7 sm:[&_svg]:size-[34px]", t.seal)}>
              <Icon name={ic} size={34} strokeWidth={2.4} />
            </div>
            <div className="min-w-0">
              {eyebrow && <div className="text-muted text-[12.5px] font-semibold">{eyebrow}</div>}
              <div className={cx("text-[26px] sm:text-[34px] font-extrabold tracking-[-.03em] leading-[1.05]", t.text)}>{title}</div>
              {children}
            </div>
          </div>
          {actions && <div className="flex flex-wrap items-center gap-2.5">{actions}</div>}
        </div>
        {reasons && reasons.length > 0 && (
          <ul className="mt-4 grid gap-2">
            {reasons.map((r, i) => (
              <li key={i} className="flex gap-2.5 items-start">
                <Icon name={tone === "good" ? "check" : "alert"} className={cx("mt-0.5 flex-none", t.text)} />
                <span className="min-w-0">{r}</span>
              </li>
            ))}
          </ul>
        )}
        {footer && <div className="mt-4">{footer}</div>}
      </div>
    </div>
  );
}

// The said/did/stored outcome, and whether other checks failed the call anyway.
export function OutcomeNotice({ verdict }: { verdict: Verdict | null }) {
  if (!verdict) return null;
  const o = verdict.checks.find((x) => x.check_id === "outcome");
  const others = verdict.checks.filter((x) => x.check_id !== "outcome" && ["FAIL", "ERROR", "NEEDS_REVIEW"].includes(x.result) && x.can_block !== false);
  const t = verdict.status === "PASS" ? "good" : verdict.status === "FAIL" ? "bad" : "warn";
  const cls = { good: "bg-good-soft text-good", bad: "bg-bad-soft text-bad", warn: "bg-warn-soft text-warn" }[t];
  return (
    <div className={cx("flex gap-2.5 items-start px-[13px] py-[11px] rounded-[11px] text-[13px] mb-4", cls.split(" ")[0])}>
      <Icon name={t === "good" ? "check" : "alert"} size={17} className={cx("flex-none mt-px", cls.split(" ")[1])} />
      <div className="text-ink">
        {o ? (
          <><b>Outcome {(verdict.outcome?.class || "").replace(/_/g, " ")}: {o.result === "PASS" ? "as expected" : "not as expected"}.</b> {o.message}</>
        ) : <b>Call {verdict.status}.</b>}
        {others.length > 0 && (
          <div className="mt-1">
            The call {verdict.status === "FAIL" ? "failed" : `is marked ${verdict.status}`} on:{" "}
            {others.map((x, i) => <span key={x.check_id}>{i > 0 && ", "}<b className="font-mono text-[12.5px]">{x.check_id}</b></span>)}.
          </div>
        )}
      </div>
    </div>
  );
}
