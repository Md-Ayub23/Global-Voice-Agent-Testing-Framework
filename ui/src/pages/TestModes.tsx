// Test › Test modes: one agent, several kinds of testing. The list page shows every mode with what it
// answers and its latest result; a mode's page has its own Run tab (the right form for that kind of
// testing) and Reports tab (only that mode's runs). Every mode uses the same call engine and checks
// every call in full; src/vatf/modes.py decides which tests each mode runs.
import { useEffect } from "react";
import { Icon, type IconName } from "../components/Icon";
import { LevelPill, ModeTile, fixHref, modeHref } from "../components/modecards";
import { Button, LinkTabs, Notice, PageHead, Skeleton } from "../components/ui";
import { api } from "../lib/api";
import { useApp, useCrumbs, useLoad } from "../lib/app";
import { projectHref } from "../lib/links";
import { MODE_ICON, MODE_TITLE } from "../lib/modes";
import { navigate } from "../lib/router";
import type { ModeCard, Project, ProjectOverview, TestModeId } from "../lib/types";
import { History } from "./History";
import { LoadTestPage } from "./LoadTest";
import { NewRun } from "./NewRun";

export const MODE_IDS = Object.keys(MODE_TITLE) as TestModeId[];

function useModes(project: Project) {
  const { activeJob } = useApp();
  return useLoad(() => api.get<ProjectOverview>(`/api/project/${project.key}`).then((o) => o.modes || []), [project.key, activeJob?.status]);
}

export function TestModesPage({ project }: { project: Project }) {
  useCrumbs([{ label: "Agents", href: "#/projects" }, { label: project.name, href: projectHref(project.key) }, { label: "Test modes" }]);
  const { data: modes, error } = useModes(project);
  if (error) return <Notice tone="bad" className="mt-5">{error.message}</Notice>;
  if (!modes) return <Skeleton height={520} />;
  const full = modes.find((m) => m.id === "full");
  const rest = modes.filter((m) => m.id !== "full");
  return (
    <>
      <PageHead title="Test modes" sub={<>Pick what you want to find out about <b className="text-ink">{project.name}</b>. Each mode runs its own tests and writes its own report. Every call is still checked in full, so a good-sounding call can never hide a wrong action.</>} />
      {full && <FullCheck m={full} k={project.key} />}
      <h2 className="font-mono text-[11px] uppercase tracking-[.08em] text-muted mt-7 mb-3">Or test one thing in depth</h2>
      <div className="grid gap-4 grid-cols-1 md:grid-cols-2 xl:grid-cols-3 stagger" id="mode-grid">
        {rest.map((m) => <ModeTile key={m.id} m={m} k={project.key} />)}
      </div>
    </>
  );
}

// The Full check leads: the one run that answers "can we release?".
function FullCheck({ m, k }: { m: ModeCard; k: string }) {
  return (
    <div id="mode-full" data-mode="full" className="flex flex-wrap items-center gap-5 p-5 rounded-[10px] border border-[color-mix(in_srgb,var(--accent)_30%,var(--line))] bg-accent-soft/40">
      <span className="size-12 rounded-[12px] grid place-items-center bg-accent text-white flex-none"><Icon name="play-circle" size={24} /></span>
      <div className="flex-1 min-w-[260px]">
        <div className="font-mono text-[11px] uppercase tracking-[.08em] text-accent-ink">Recommended before every release</div>
        <b className="text-[18px] font-semibold tracking-[-.01em]">{m.title}: {m.question}</b>
        <div className="text-[13px] text-ink-2 mt-0.5">{m.about}</div>
        {m.latest && (
          <a href={projectHref(k, `/runs/${m.latest.run_id}`)} className="inline-flex items-center gap-2 mt-2 text-[12.5px] text-ink-2 hover:no-underline">
            Last time: <LevelPill level={m.latest.level} mode="full" />{m.latest.top_finding && <span className="text-ink-2">{m.latest.top_finding}</span>}
          </a>
        )}
        {!m.available.ok && <div className="text-[12.5px] text-warn mt-2">{m.available.why}</div>}
      </div>
      {m.available.ok
        ? <Button variant="primary" size="lg" icon="play" href={modeHref(k, "full")} data-open="full">Run the full check</Button>
        : <Button icon="settings" href={fixHref(k, m)}>Finish set-up</Button>}
    </div>
  );
}

export function ModePage({ project, mode, tab, query }: { project: Project; mode: TestModeId; tab: "run" | "reports"; query: URLSearchParams }) {
  const title = MODE_TITLE[mode];
  useCrumbs([{ label: "Agents", href: "#/projects" }, { label: project.name, href: projectHref(project.key) },
    { label: "Test modes", href: projectHref(project.key, "/test") }, { label: title }]);
  const { data: modes, error } = useModes(project);
  // Real calls have their own pages (Quality › Real calls).
  useEffect(() => { if (mode === "monitor") navigate(projectHref(project.key, "/calls"), true); }, [mode, project.key]);
  if (error) return <Notice tone="bad" className="mt-5">{error.message}</Notice>;
  if (!modes || mode === "monitor") return <Skeleton />;
  const m = modes.find((x) => x.id === mode);
  if (!m) return <Notice tone="bad" className="mt-5">Unknown test mode.</Notice>;
  const k = project.key;

  return (
    <>
      <div className="mt-3 mb-5 flex flex-wrap items-start gap-4">
        <span className="size-11 rounded-[11px] grid place-items-center bg-accent-soft text-accent flex-none"><Icon name={(MODE_ICON[mode] || "play") as IconName} size={22} /></span>
        <div className="flex-1 min-w-[260px]">
          <h1 className="text-[24px] font-semibold tracking-[-.025em] leading-tight">{title}</h1>
          <div className="mt-1 text-ink-2 text-[13.5px] max-w-[760px]">{m.question} {m.about}</div>
        </div>
        {m.latest && (
          <a href={projectHref(k, `/runs/${m.latest.run_id}`)} className="flex flex-col items-end gap-1 hover:no-underline text-ink" id="mode-latest">
            <span className="text-[11.5px] text-muted">Latest result</span><LevelPill level={m.latest.level} mode={mode} />
          </a>
        )}
      </div>
      {m.checks.length > 0 && (
        <div className="grid gap-2 grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 mb-5" id="mode-checks">
          {m.checks.map((c) => (
            <div key={c} className="flex gap-2 p-3 rounded-[10px] border border-line bg-surface text-[12.5px] text-ink-2">
              <Icon name="check" size={14} className="text-good mt-px flex-none" />{c}
            </div>
          ))}
        </div>
      )}
      {!m.available.ok && (
        <Notice tone="warn" className="mb-4">{m.available.why} <a href={fixHref(k, m)}>{m.available.fixable ? "Fix this" : "Set up"}</a></Notice>
      )}
      {mode === "regression" && m.available.ok && (
        <Notice className="mb-4">Runs the same tests as before and compares them with your baseline. A test that passed on the baseline and fails now blocks the release. <a href={projectHref(k, "/compare")}>Compare any two runs</a></Notice>
      )}
      <LinkTabs value={tab} tabs={[
        { value: "run", label: m.kind === "load" ? "Plan and run" : "Run", href: modeHref(k, mode), icon: "play" },
        { value: "reports", label: `Reports${m.runs ? ` (${m.runs})` : ""}`, href: modeHref(k, mode, "/reports"), icon: "file" },
      ]} />
      {tab === "reports" ? <History project={project} mode={mode} />
        : m.kind === "load" ? <LoadTestPage project={project} query={query} embedded />
        : <NewRun project={project} query={query} mode={mode} />}
    </>
  );
}
