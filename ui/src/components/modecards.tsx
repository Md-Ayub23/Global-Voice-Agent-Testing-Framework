// Test modes (src/vatf/modes.py) as cards: the Test modes page shows one card per mode, the agent's
// overview shows the latest readiness of each mode as one board.
import { When } from "./dash";
import { Icon, type IconName } from "./Icon";
import { Button, Card, CardHead } from "./ui";
import { cx } from "../lib/format";
import { projectHref } from "../lib/links";
import { LEVEL, MODE_ICON, TONE_SOFT, TONE_TEXT, levelShort } from "../lib/modes";
import type { ModeCard, ReadinessLevel } from "../lib/types";

export const modeHref = (key: string, id: string, rest = "") =>
  id === "monitor" ? projectHref(key, "/calls") : projectHref(key, `/test/${id}${rest}`);

// Where to fix what stops a mode from running.
export function fixHref(key: string, m: ModeCard): string {
  const why = m.available.why || "";
  if (m.id === "safety") return projectHref(key, "/setup/safety");
  if (m.id === "regression" && why.startsWith("No baseline")) return projectHref(key, "/runs");
  if (why.startsWith("Finish")) return projectHref(key, "/setup/connection");
  return projectHref(key, "/setup/cases");
}

export function LevelPill({ level, mode, className }: { level: ReadinessLevel | null; mode?: string; className?: string }) {
  const lv = LEVEL[level || "no_result"];
  return (
    <span className={cx("inline-flex items-center gap-1 h-[22px] px-2 rounded-md text-[13px] font-semibold whitespace-nowrap", TONE_SOFT[lv.tone], className)}>
      <Icon name={lv.icon} size={12} strokeWidth={2.5} />{levelShort(level, mode)}
    </span>
  );
}

function ModeIcon({ id, size = 36 }: { id: string; size?: number }) {
  return (
    <span className="rounded-[12px] grid place-items-center bg-accent-soft text-accent flex-none" style={{ width: size, height: size }}>
      <Icon name={(MODE_ICON[id] || "play") as IconName} size={Math.round(size / 2)} />
    </span>
  );
}

function Latest({ m, k }: { m: ModeCard; k: string }) {
  const l = m.latest;
  if (!l) return <div className="text-[13px] text-muted">Not run yet</div>;
  return (
    <a href={projectHref(k, `/runs/${l.run_id}`)} className="block rounded-lg -mx-1 px-1 py-0.5 hover:bg-surface-2 hover:no-underline text-ink">
      <div className="flex items-center gap-2 flex-wrap">
        <LevelPill level={l.level} mode={m.id} />
        <span className="text-[13px] text-muted"><When iso={l.at} />{m.runs > 1 ? ` · ${m.runs} runs` : ""}</span>
      </div>
      {l.top_finding && <div className="text-[13px] text-ink-2 mt-1 line-clamp-2">Top finding: {l.top_finding}</div>}
    </a>
  );
}

export function ModeTile({ m, k }: { m: ModeCard; k: string }) {
  const ok = m.available.ok;
  return (
    <Card className="flex flex-col gap-3" data-mode={m.id} id={`mode-${m.id}`}>
      <div className="flex items-start gap-3">
        <ModeIcon id={m.id} />
        <div className="min-w-0 flex-1">
          <a href={modeHref(k, m.id)} className="text-ink hover:no-underline hover:text-accent-ink"><b className="text-[15px] font-semibold">{m.title}</b></a>
          <div className="text-[13px] text-ink-2 mt-0.5">{m.question}</div>
        </div>
      </div>
      <div className="text-[13px] text-muted">{m.about}</div>
      {m.checks.length > 0 && (
        <ul className="grid gap-1 text-[13px] text-ink-2">
          {m.checks.map((c) => <li key={c} className="flex gap-2"><Icon name="check" size={13} className="text-good mt-[3px] flex-none" />{c}</li>)}
        </ul>
      )}
      <div className="mt-auto pt-3 border-t border-line grid gap-2.5">
        {ok ? (m.kind === "monitor" ? <div className="text-[13px] text-muted">Results live on the Real calls pages.</div> : <Latest m={m} k={k} />) : (
          <div className="text-[13px] text-warn flex gap-1.5"><Icon name="alert" size={14} className="mt-px flex-none" />{m.available.why}</div>
        )}
        <div className="flex flex-wrap gap-2">
          {ok ? (
            <Button size="sm" variant={m.latest ? "default" : "primary"} icon={m.kind === "monitor" ? "phone" : "play"} href={modeHref(k, m.id)} data-open={m.id}>
              {m.kind === "monitor" ? "Open real calls" : m.latest ? "Run again" : "Set up and run"}
            </Button>
          ) : (
            <Button size="sm" icon="settings" href={fixHref(k, m)}>{m.available.fixable ? "Fix this" : "Set up"}</Button>
          )}
          {m.latest && m.kind !== "monitor" && <Button size="sm" variant="ghost" href={modeHref(k, m.id, "/reports")}>Reports</Button>}
        </div>
      </div>
    </Card>
  );
}

// The agent's overview: where each kind of testing stands now, one row per mode.
export function ReadinessBoard({ modes, k }: { modes: ModeCard[]; k: string }) {
  const tried = modes.filter((m) => m.latest);
  const untried = modes.filter((m) => !m.latest && m.kind !== "monitor");
  return (
    <Card flush id="readiness-board">
      <div className="px-[18px] pt-4 pb-1">
        <CardHead level={2} title="Release readiness by test mode" sub="the latest result of each kind of testing (runs with bugs switched on are left out)">
          <Button size="sm" variant="ghost" href={projectHref(k, "/test")}>All test modes <Icon name="arrow" /></Button>
        </CardHead>
      </div>
      {tried.length ? (
        <div className="divide-y divide-line border-t border-line">
          {tried.map((m) => {
            const l = m.latest!;
            const tone = LEVEL[l.level || "no_result"].tone;
            return (
              <a key={m.id} href={projectHref(k, `/runs/${l.run_id}`)} data-board={m.id}
                className="flex items-center gap-3 px-[18px] py-3 hover:bg-surface-2 hover:no-underline text-ink flex-wrap">
                <ModeIcon id={m.id} size={30} />
                <span className="min-w-[150px] flex-[0_0_180px]"><b className="text-[13.5px] font-medium">{m.title}</b>
                  <span className="block text-[13px] text-muted"><When iso={l.at} /></span></span>
                <LevelPill level={l.level} mode={m.id} />
                <span className={cx("flex-1 min-w-[200px] text-[13px]", l.top_finding ? TONE_TEXT[tone] : "text-ink-2")}>
                  {l.top_finding ? l.top_finding : `No findings${l.calls ? ` in ${l.calls} calls` : ""}`}
                </span>
                <Icon name="chevron" size={15} className="text-muted" />
              </a>
            );
          })}
        </div>
      ) : (
        <div className="px-[18px] pb-4 text-[13px] text-ink-2">No test mode has a result yet. <a href={projectHref(k, "/test")}>Pick a test mode</a> to start.</div>
      )}
      {tried.length > 0 && untried.length > 0 && (
        <div className="px-[18px] py-3 border-t border-line text-[13px] text-muted flex flex-wrap items-center gap-x-2 gap-y-1">
          Not tested yet:
          {untried.map((m) => <a key={m.id} href={modeHref(k, m.id)} className="text-ink-2 hover:text-accent-ink">{m.title}</a>)}
        </div>
      )}
    </Card>
  );
}
