// Pieces several pages share: a run's setting chips, a call's checks, the big decision banner.
import type { ReactNode } from "react";
import { cx } from "../lib/format";
import { tone as toneOf } from "../lib/status";
import type { Check, Options, Verdict } from "../lib/types";
import { Tip } from "./feedback";
import { Icon, type IconName } from "./Icon";
import { Badge, Chip } from "./ui";

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
  if (!verdict?.checks?.length) return <div className="text-muted text-[12.5px]">No checks.</div>;
  const checks = [...verdict.checks].sort((a, b) => (ORDER[a.result] ?? 9) - (ORDER[b.result] ?? 9));
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
  const others = verdict.checks.filter((x) => x.check_id !== "outcome" && ["FAIL", "ERROR"].includes(x.result));
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
