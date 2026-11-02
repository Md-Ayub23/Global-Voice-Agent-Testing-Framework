// The dashboard's building blocks, styled with Tailwind utilities over the design tokens in index.css.
import { useLayoutEffect, useRef, useState, type ButtonHTMLAttributes, type ReactNode } from "react";
import { cx } from "../lib/format";
import { statusInfo, type Tone } from "../lib/status";
import { Icon, type IconName } from "./Icon";

// ---- buttons -------------------------------------------------------------------------------

type Variant = "default" | "primary" | "danger" | "ghost";
type Size = "sm" | "md" | "lg";
const BTN_BASE =
  "inline-flex items-center justify-center gap-1.5 font-medium tracking-[-.005em] whitespace-nowrap border cursor-pointer select-none " +
  "transition-[transform,background-color,border-color,box-shadow,filter] duration-150 ease-out-soft active:translate-y-px " +
  "disabled:opacity-50 disabled:cursor-not-allowed disabled:active:translate-y-0 hover:no-underline focus-visible:outline-2 " +
  "focus-visible:outline-offset-2 focus-visible:outline-accent";
const BTN_VARIANT: Record<Variant, string> = {
  default: "bg-surface text-ink border-line-strong shadow-soft hover:bg-surface-2 hover:border-[color-mix(in_srgb,var(--line-strong)_70%,var(--ink-2))]",
  primary:
    "text-btn-ink border-transparent bg-btn shadow-[0_1px_2px_rgba(17,17,19,.12),inset_0_1px_0_rgba(255,255,255,.14)] " +
    "hover:bg-[color-mix(in_srgb,var(--btn)_88%,black)]",
  danger: "bg-surface text-bad border-[color-mix(in_srgb,var(--bad)_40%,var(--line))] hover:bg-bad-soft",
  ghost: "bg-transparent text-ink border-transparent hover:bg-surface-2",
};
const BTN_SIZE: Record<Size, string> = {
  sm: "h-[30px] text-[13px] rounded-[7px]",
  md: "h-[34px] text-[13.5px] rounded-lg",
  lg: "h-10 text-[14px] rounded-lg",
};
// Padding lives apart from the size so an icon-only button never gets two competing px- classes.
const BTN_PAD: Record<Size, string> = { sm: "px-2.5", md: "px-3.5", lg: "px-5" };
const ICON_ONLY: Record<Size, string> = { sm: "w-[30px] px-0", md: "w-[34px] px-0", lg: "w-10 px-0" };

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
  icon?: IconName;
  iconOnly?: boolean;
  href?: string;
  target?: string;
  busy?: boolean;
}
export function Button({ variant = "default", size = "md", icon, iconOnly, href, target, busy, className, children, ...rest }: ButtonProps) {
  const cls = cx(BTN_BASE, BTN_VARIANT[variant], BTN_SIZE[size], iconOnly ? ICON_ONLY[size] : BTN_PAD[size], className);
  const inner = (
    <>
      {busy ? <Spinner /> : icon && <Icon name={icon} />}
      {children}
    </>
  );
  if (href) {
    return (
      <a id={rest.id} className={cls} href={href} target={target} rel={target ? "noreferrer" : undefined} title={rest.title} aria-label={rest["aria-label"]}>
        {inner}
      </a>
    );
  }
  return (
    <button type="button" className={cls} {...rest}>
      {inner}
    </button>
  );
}

// ---- surfaces --------------------------------------------------------------------------------

export function Card({ className, children, flush, ...rest }: { className?: string; children: ReactNode; flush?: boolean } & React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div className={cx("bg-surface border border-line rounded-[10px] min-w-0", flush ? "p-0" : "p-5", className)} {...rest}>
      {children}
    </div>
  );
}

export function CardHead({ title, sub, children, level = 3, className }: { title: ReactNode; sub?: ReactNode; children?: ReactNode; level?: 2 | 3; className?: string }) {
  const H = level === 2 ? "h2" : "h3";
  return (
    <div className={cx("flex items-center gap-x-2.5 gap-y-1 mb-3.5 flex-wrap", className)}>
      <H className={cx("grow min-w-0 font-semibold tracking-[-.01em]", level === 2 ? "text-[15px]" : "text-[14px]")}>{title}</H>
      {sub != null && <span className="text-muted text-[12.5px]">{sub}</span>}
      {children}
    </div>
  );
}

export function PageHead({ title, sub, children }: { title: ReactNode; sub?: ReactNode; children?: ReactNode }) {
  return (
    <div className="flex items-end gap-4 mt-3 mb-6 flex-wrap">
      <div className="flex-1 min-w-[min(100%,340px)]">
        {typeof title === "string" ? <h1 className="text-[24px] font-semibold tracking-[-.025em] leading-tight">{title}</h1> : title}
        {sub && <div className="mt-1 text-ink-2 text-[13.5px] max-w-[760px]">{sub}</div>}
      </div>
      {children}
    </div>
  );
}

// ---- status ---------------------------------------------------------------------------------

const BADGE_TONE: Record<Tone, [string, string]> = {
  good: ["bg-good-soft text-good", "bg-good"],
  bad: ["bg-bad-soft text-bad", "bg-bad"],
  warn: ["bg-warn-soft text-warn", "bg-warn"],
  neutral: ["bg-surface-2 text-ink-2", "bg-muted"],
  run: ["bg-accent-soft text-accent-ink", "bg-accent"],
};
// A status: a coloured dot and its words (the words carry the meaning, the colour helps scanning).
export function Badge({ status, label, className }: { status: string | null | undefined; label?: ReactNode; className?: string }) {
  const [t, , text] = statusInfo(status);
  const [cls, dot] = BADGE_TONE[t];
  return (
    <span className={cx("inline-flex items-center gap-1.5 h-[22px] px-2 rounded-md text-[12px] font-medium whitespace-nowrap", cls, className)}>
      {status === "running" ? <Spinner size={10} /> : <i className={cx("size-1.5 rounded-full flex-none", dot)} />}
      {label ?? text}
    </span>
  );
}

export type ChipTone = "default" | "accent" | "good" | "bad" | "warn" | "caller" | "agent" | "tool";
// Chips are quiet: a thin outline, with colour only on the icon. Status colours stay for good / bad / warn.
const CHIP_TONE: Record<ChipTone, [string, string]> = {
  default: ["bg-surface text-ink-2 border-line", "text-muted"],
  accent: ["bg-surface text-ink-2 border-line", "text-accent"],
  good: ["bg-good-soft text-good border-transparent", ""],
  bad: ["bg-bad-soft text-bad border-transparent", ""],
  warn: ["bg-warn-soft text-warn border-transparent", ""],
  caller: ["bg-surface text-ink-2 border-line", "text-caller"],
  agent: ["bg-surface text-ink-2 border-line", "text-agent"],
  tool: ["bg-surface text-ink-2 border-line", "text-tool"],
};
export function Chip({ tone = "default", icon, children, className, title }: { tone?: ChipTone; icon?: IconName; children: ReactNode; className?: string; title?: string }) {
  const [cls, ic] = CHIP_TONE[tone];
  return (
    <span title={title} className={cx("inline-flex items-center gap-1.5 h-[22px] px-2 rounded-md text-[12px] font-medium border whitespace-nowrap", cls, className)}>
      {icon && <Icon name={icon} size={12} className={ic} />}
      {children}
    </span>
  );
}

const NOTICE_TONE = {
  info: ["bg-info-soft", "text-accent-ink"],
  warn: ["bg-warn-soft", "text-warn"],
  bad: ["bg-bad-soft", "text-bad"],
  good: ["bg-good-soft", "text-good"],
} as const;
export function Notice({ tone = "info", icon, children, className }: { tone?: keyof typeof NOTICE_TONE; icon?: IconName; children: ReactNode; className?: string }) {
  const [bg, ic] = NOTICE_TONE[tone];
  const fallback: IconName = tone === "good" ? "check" : tone === "info" ? "info" : "alert";
  return (
    <div data-notice={tone} className={cx("flex gap-2.5 items-start px-3.5 py-3 rounded-lg text-[13px] text-ink border border-[color-mix(in_srgb,currentColor_0%,transparent)]", bg, className)}>
      <Icon name={icon || fallback} size={17} className={cx("flex-none mt-px", ic)} />
      <div className="min-w-0 flex-1">{children}</div>
    </div>
  );
}

export function Spinner({ size = 14, className }: { size?: number; className?: string }) {
  return (
    <span
      aria-hidden="true"
      style={{ width: size, height: size }}
      className={cx("inline-block flex-none rounded-full border-2 border-[color-mix(in_srgb,currentColor_25%,transparent)] border-t-current animate-spin", className)}
    />
  );
}

export function LiveDot({ className }: { className?: string }) {
  return (
    <span className={cx("relative inline-block size-2 flex-none rounded-full bg-accent", className)}>
      <span className="absolute -inset-1 rounded-full border-2 border-accent opacity-0 animate-ping-soft" />
    </span>
  );
}

export function Progress({ value, running }: { value: number; running?: boolean }) {
  return (
    <div className="h-2 rounded-full bg-surface-3 overflow-hidden relative">
      <i
        className="absolute inset-y-0 left-0 rounded-full bg-accent transition-[width] duration-600 ease-out-soft overflow-hidden"
        style={{ width: `${Math.round(Math.max(0, Math.min(1, value)) * 100)}%` }}
      >
        {running && <span className="absolute inset-0 bg-linear-90 from-transparent via-white/45 to-transparent animate-shimmer" />}
      </i>
    </div>
  );
}

// ---- inputs ----------------------------------------------------------------------------------

export const inputCls =
  "h-[34px] w-full px-[11px] rounded-lg border border-line-strong bg-surface text-ink outline-none shadow-soft transition-[border-color,box-shadow] " +
  "focus:border-accent focus:shadow-[0_0_0_3px_color-mix(in_srgb,var(--accent)_22%,transparent)] disabled:opacity-60";

export function Field({ label, hint, children, className }: { label: ReactNode; hint?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <label className={cx("grid content-start gap-1.5 font-medium text-[13px] min-w-0", className)}>
      {label}
      {children}
      {hint && <span className="font-normal text-muted text-xs">{hint}</span>}
    </label>
  );
}

export function Seg<T extends string>({ value, options, onChange, className, id }: {
  value: T; options: { value: T; label: ReactNode; icon?: IconName }[]; onChange: (v: T) => void; className?: string; id?: string;
}) {
  return (
    <div id={id} className={cx("inline-flex max-w-full overflow-x-auto p-[3px] gap-[2px] bg-surface-2 border border-line rounded-lg scrollbar-thin", className)}>
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          data-v={o.value}
          onClick={() => onChange(o.value)}
          className={cx(
            "flex-none inline-flex items-center gap-[7px] px-2.5 py-1 rounded-md text-[13px] font-medium whitespace-nowrap cursor-pointer transition-[background-color,color,box-shadow] duration-150",
            o.value === value ? "bg-surface text-ink shadow-[0_1px_2px_rgba(17,17,19,.08),0_0_0_1px_var(--line)]" : "text-ink-2 hover:text-ink",
          )}
        >
          {o.icon && <Icon name={o.icon} size={15} />}
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function Switch({ checked, onChange, label, id }: { checked: boolean; onChange: (v: boolean) => void; label: ReactNode; id?: string }) {
  return (
    <span className="inline-flex items-center gap-2.5 cursor-pointer select-none font-semibold">
      <input id={id} type="checkbox" className="peer sr-only" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      <span
        aria-hidden="true"
        className={cx(
          "relative w-[38px] h-[22px] rounded-full flex-none transition-colors duration-200 peer-focus-visible:outline-2 peer-focus-visible:outline-accent",
          "after:absolute after:top-[3px] after:left-[3px] after:size-4 after:rounded-full after:bg-white after:shadow after:transition-transform after:duration-200",
          checked ? "bg-accent after:translate-x-4" : "bg-line-strong",
        )}
      />
      {label}
    </span>
  );
}

// A large selectable option: a radio (one of) or a checkbox (any of).
export function Choice({ on, off, radio, onClick, children, className, title, ...data }: {
  on: boolean; off?: boolean; radio?: boolean; onClick?: () => void; children: ReactNode; className?: string; title?: string;
  [data: `data-${string}`]: string | undefined;
}) {
  return (
    <div
      role={radio ? "radio" : "checkbox"}
      aria-checked={on}
      aria-disabled={off || undefined}
      tabIndex={off ? -1 : 0}
      title={title}
      onClick={() => !off && onClick?.()}
      onKeyDown={(e) => {
        if (!off && (e.key === " " || e.key === "Enter")) { e.preventDefault(); onClick?.(); }
      }}
      className={cx(
        "flex gap-3 items-start px-3.5 py-3 border rounded-[10px] bg-surface transition-[border-color,background-color,box-shadow] duration-150 outline-none",
        "focus-visible:shadow-[0_0_0_3px_color-mix(in_srgb,var(--accent)_30%,transparent)]",
        off ? "opacity-55 cursor-not-allowed border-line" : "cursor-pointer",
        on && !off ? "border-accent bg-[color-mix(in_srgb,var(--accent-soft)_55%,var(--surface))] shadow-[0_0_0_3px_color-mix(in_srgb,var(--accent)_14%,transparent)]" : !off && "border-line hover:border-line-strong",
        className,
      )}
      {...data}
    >
      <span className={cx("size-5 flex-none mt-px grid place-items-center border-[1.5px] transition-colors", radio ? "rounded-full" : "rounded-md",
        on && !off ? "bg-accent border-accent" : "border-line-strong")}>
        <Icon name="check" size={13} strokeWidth={3} className={cx("text-white transition-[opacity,transform] duration-200", on && !off ? "opacity-100 scale-100" : "opacity-0 scale-50")} />
      </span>
      <div className="min-w-0 flex-1">{children}</div>
    </div>
  );
}

// Tabs with an underline that slides to the chosen one. LinkTabs are the same look for tabs that are pages.
function useSlider(activeKey: string) {
  const box = useRef<HTMLDivElement>(null);
  const [bar, setBar] = useState<{ left: number; width: number } | null>(null);
  useLayoutEffect(() => {
    const measure = () => {
      const el = box.current?.querySelector<HTMLElement>("[aria-selected=true]");
      if (el) setBar({ left: el.offsetLeft, width: el.offsetWidth });
    };
    measure();
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, [activeKey]);
  return { box, bar };
}
const TAB_CLS = "relative px-3 py-2.5 text-[13.5px] font-medium cursor-pointer whitespace-nowrap transition-colors hover:no-underline inline-flex items-center gap-1.5";
function Slider({ bar }: { bar: { left: number; width: number } | null }) {
  return bar ? <span aria-hidden="true" className="absolute -bottom-px h-0.5 rounded-full bg-accent transition-[left,width] duration-300 ease-out-soft" style={{ left: bar.left, width: bar.width }} /> : null;
}

export function Tabs<T extends string>({ value, tabs, onChange, className }: { value: T; tabs: { value: T; label: ReactNode }[]; onChange: (v: T) => void; className?: string }) {
  const { box, bar } = useSlider(value);
  return (
    <div ref={box} className={cx("relative flex gap-1 border-b border-line mb-4 overflow-x-auto scrollbar-thin", className)} role="tablist">
      {tabs.map((t) => (
        <button key={t.value} role="tab" type="button" data-tab={t.value} aria-selected={t.value === value} onClick={() => onChange(t.value)}
          className={cx(TAB_CLS, t.value === value ? "text-ink" : "text-muted hover:text-ink-2")}>
          {t.label}
        </button>
      ))}
      <Slider bar={bar} />
    </div>
  );
}

export function LinkTabs({ value, tabs, className }: { value: string; tabs: { value: string; label: ReactNode; href: string; icon?: IconName }[]; className?: string }) {
  const { box, bar } = useSlider(value);
  return (
    <div ref={box} className={cx("relative flex gap-1 border-b border-line mb-5 overflow-x-auto scrollbar-thin", className)} role="tablist">
      {tabs.map((t) => (
        <a key={t.value} role="tab" href={t.href} data-tab={t.value} aria-selected={t.value === value}
          className={cx(TAB_CLS, t.value === value ? "text-ink" : "text-muted hover:text-ink-2")}>
          {t.icon && <Icon name={t.icon} size={15} />}{t.label}
        </a>
      ))}
      <Slider bar={bar} />
    </div>
  );
}

export function Kbd({ children }: { children: ReactNode }) {
  return <span className="font-mono text-[11px] px-[5px] py-px border border-line-strong border-b-2 rounded-[5px]">{children}</span>;
}

export function EmptyState({ title, text, icon = "layers", action }: { title: string; text?: string; icon?: IconName; action?: ReactNode }) {
  return (
    <div className="text-center px-5 py-12 text-muted animate-fade">
      <span className="mx-auto mb-3 size-12 rounded-[10px] grid place-items-center bg-surface-2 border border-line text-muted"><Icon name={icon} size={22} strokeWidth={1.8} /></span>
      <h3 className="text-ink font-semibold mb-1">{title}</h3>
      {text && <div>{text}</div>}
      {action && <div className="mt-3.5">{action}</div>}
    </div>
  );
}

export function Skeleton({ height = 420, className }: { height?: number; className?: string }) {
  return (
    <div
      className={cx("rounded-lg mt-5 animate-skeleton bg-linear-90 from-surface-2 via-surface-3 to-surface-2 bg-size-[200%_100%]", className)}
      style={{ height }}
    />
  );
}

export function Divider() {
  return <div className="h-px bg-line my-3.5" />;
}

// A table that scrolls sideways on its own instead of the page, with the dashboard's row style.
export function Table({ head, children, compact }: { head?: ReactNode[]; children: ReactNode; compact?: boolean }) {
  return (
    <div className="overflow-x-auto">
      <table className={cx("w-full border-separate border-spacing-0", compact ? "[&_td]:px-2.5 [&_td]:py-[7px]" : "[&_td]:px-3 [&_td]:py-2.5",
        "[&_td]:border-b [&_td]:border-line [&_td]:align-middle [&_tr:last-child_td]:border-b-0")}>
        {head && (
          <thead>
            <tr>
              {head.map((h, i) => (
                <th key={i} className="text-left font-mono text-[10.5px] uppercase tracking-[.06em] text-muted font-medium px-3 py-2.5 border-b border-line whitespace-nowrap">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
        )}
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}
export const rowLink = "cursor-pointer transition-colors hover:bg-surface-2";

export function Legend({ items }: { items: [string, string][] }) {
  return (
    <div className="flex gap-3.5 flex-wrap text-xs text-ink-2">
      {items.map(([color, label]) => (
        <span key={label}>
          <i className="inline-block size-2.5 rounded-[3px] mr-1.5 -mb-px" style={{ background: color }} />
          {label}
        </span>
      ))}
    </div>
  );
}

export function Cmd({ children, action, pre }: { children: ReactNode; action?: ReactNode; pre?: boolean }) {
  return (
    <div className="flex items-start gap-2 font-mono text-xs bg-surface-2 border border-line rounded-[10px] px-2.5 py-[9px] text-ink-2">
      <span className={cx("flex-1 min-w-0", pre ? "whitespace-pre-wrap break-words" : "break-all")}>{children}</span>
      {action}
    </div>
  );
}
