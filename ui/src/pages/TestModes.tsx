// One test type of one agent (Tests › <type> in the sidebar): what it checks, its own test cases, the Run form
// for this kind of testing and only its own reports. Every type uses the same call engine and checks every call in
// full; src/vatf/modes.py decides which test cases each type runs.
import { useEffect } from "react";
import { LevelPill, fixHref, modeHref } from "../components/modecards";
import { Button, LinkTabs, Notice, Skeleton } from "../components/ui";
import { api } from "../lib/api";
import { useApp, useCrumbs, useLoad } from "../lib/app";
import { mainProfile, projectHref } from "../lib/links";
import { MODE_TITLE } from "../lib/modes";
import { navigate } from "../lib/router";
import type { Project, ProjectOverview, TestModeId } from "../lib/types";
import { History } from "./History";
import { LoadTestPage } from "./LoadTest";
import { NewRun } from "./NewRun";
import { SafetyCard } from "./setup/SafetyCard";
import { setupApi } from "../lib/setup";
import { TypeCases, isCaseType } from "./TypeCases";

export const MODE_IDS = Object.keys(MODE_TITLE) as TestModeId[];

type Tab = "cases" | "run" | "reports";

function useModes(project: Project) {
  const { activeJob } = useApp();
  return useLoad(() => api.get<ProjectOverview>(`/api/project/${project.key}`).then((o) => o.modes || []), [project.key, activeJob?.status]);
}

// Safety's own "test cases" are its attack calls and custom questions (the Safety tests card).
function SafetyCases({ project }: { project: Project }) {
  const { reloadProjects } = useApp();
  const main = mainProfile(project);
  const { data: detail, error, reload } = useLoad(() => setupApi.profile(project.path, main?.file || "profile.yaml"), [project.path, main?.file]);
  if (error) return <Notice tone="bad">{error.message}</Notice>;
  if (!detail) return <Skeleton height={240} />;
  return <SafetyCard detail={detail} reload={() => { reload(); void reloadProjects(); }} projectKey={project.key} />;
}

export function ModePage({ project, mode, tab: asked, query }: { project: Project; mode: TestModeId; tab: "run" | "reports" | null; query: URLSearchParams }) {
  const title = MODE_TITLE[mode];
  const k = project.key;
  useCrumbs([{ label: project.name, href: projectHref(k) }, { label: title }]);
  const { data: modes, error } = useModes(project);
  // Real calls have their own pages (Results › Real calls).
  useEffect(() => { if (mode === "monitor") navigate(projectHref(k, "/calls"), true); }, [mode, k]);
  if (error) return <Notice tone="bad" className="mt-5">{error.message}</Notice>;
  if (!modes || mode === "monitor") return <Skeleton />;
  const m = modes.find((x) => x.id === mode);
  if (!m) return <Notice tone="bad" className="mt-5">Unknown test type.</Notice>;

  // Types that own test cases open on them; the Release check and Regression run what the others own, Load has its own plan.
  const hasCases = isCaseType(mode) || mode === "safety";
  const tab: Tab = asked || (hasCases ? "cases" : "run");
  const edit = query.get("edit");
  const casesCount = isCaseType(mode) ? (mainProfile(project)?.scenarios || []).filter((s) => s.test_type === mode).length : null;
  const tabs = [
    ...(hasCases ? [{ value: "cases", label: mode === "safety" ? "Attack calls" : `Test cases${casesCount != null ? ` (${casesCount})` : ""}`, href: modeHref(k, mode) }] : []),
    { value: "run", label: m.kind === "load" ? "Plan and run" : "Run", href: hasCases ? modeHref(k, mode, "/run") : modeHref(k, mode) },
    { value: "reports", label: `Reports${m.runs ? ` (${m.runs})` : ""}`, href: modeHref(k, mode, "/reports") },
  ];

  return (
    <>
      <div className="mt-2 mb-4 flex flex-wrap items-start gap-4" id="type-head" data-mode={mode}>
        <div className="flex-1 min-w-[260px]">
          <h1 className="text-[21px] font-semibold tracking-[-.015em] leading-tight">{title}</h1>
          <div className="mt-1 text-ink-2 text-[13.5px] max-w-[760px]">{m.question} {m.about}</div>
        </div>
        <div className="flex items-center gap-3">
          {m.latest && (
            <a href={projectHref(k, `/runs/${m.latest.run_id}`)} className="flex items-center gap-2 hover:no-underline text-ink text-[13px]" id="mode-latest">
              <span className="text-muted">Latest</span><LevelPill level={m.latest.level} mode={mode} />
            </a>
          )}
          {tab !== "run" && m.available.ok && (
            <Button id="open-run" variant="primary" icon="play" href={tabs.find((t) => t.value === "run")!.href}>{mode === "full" ? "Run the Release check" : `Run ${title}`}</Button>
          )}
        </div>
      </div>
      {m.checks.length > 0 && tab !== "reports" && !edit && (
        <ul className="flex flex-wrap gap-x-5 gap-y-1 mb-4 text-[13px] text-ink-2" id="mode-checks">
          {m.checks.map((c) => <li key={c} className="flex items-center gap-1.5"><i className="size-1 rounded-full bg-line-strong" />{c}</li>)}
        </ul>
      )}
      {!m.available.ok && tab === "run" && (
        <Notice tone="warn" className="mb-4">{m.available.why} {hasCases
          ? <a href={modeHref(k, mode)}>Open {mode === "safety" ? "Attack calls" : "Test cases"}</a>
          : <a href={fixHref(k, m)}>{m.available.fixable ? "Fix this" : "Set up"}</a>}</Notice>
      )}
      {mode === "regression" && tab === "run" && m.available.ok && (
        <Notice className="mb-4">Runs every test case again and compares the result with your baseline. A test that passed on the baseline and fails now fails the run. <a href={projectHref(k, "/compare")}>Compare any two runs</a></Notice>
      )}
      {mode === "full" && tab === "run" && (
        <Notice className="mb-4">The Release check runs every test case of every type, plus the safety attack calls, and writes one combined report. Run it before each release; use a single test type to look at one thing in depth.</Notice>
      )}
      {!edit && <LinkTabs value={tab} tabs={tabs} />}
      {tab === "cases" ? (mode === "safety" ? <SafetyCases project={project} /> : isCaseType(mode) ? <TypeCases project={project} type={mode} edit={edit} /> : null)
        : tab === "reports" ? <History project={project} mode={mode} />
        : m.kind === "load" ? <LoadTestPage project={project} query={query} embedded />
        : <NewRun project={project} query={query} mode={mode} />}
    </>
  );
}
