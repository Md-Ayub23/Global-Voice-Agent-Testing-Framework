// One agent's set-up pages: Connect (connection, tools, database, logs) and Agent settings (pass rules, callers,
// AI judges, CI & alerts), plus the test cases and safety tests pages. Every save is checked with the same rules a run uses.
import type { ReactNode } from "react";
import { LinkTabs, Notice, PageHead, ReadOnly, Seg, Skeleton } from "../components/ui";
import { useCanEdit } from "../lib/auth";
import { SafetyCard } from "./setup/SafetyCard";
import { qs } from "../lib/api";
import { useApp, useCrumbs, useLoad } from "../lib/app";
import { channelLabel, mainProfile, projectHref } from "../lib/links";
import { navigate } from "../lib/router";
import { setupApi } from "../lib/setup";
import type { Project } from "../lib/types";
import { AgentTab } from "./setup/AgentTab";
import { BackendTab } from "./setup/BackendTab";
import { JudgesTab } from "./setup/JudgesTab";
import { LogsTab } from "./setup/LogsTab";
import { TestPlanTab } from "./setup/TestPlanTab";
import { TestSettingsTab } from "./setup/TestSettingsTab";
import { ToolsTab } from "./setup/ToolsTab";
import { OpsTab } from "./setup/OpsTab";

import { CONNECT_TABS, SETTINGS_TABS, SETUP_TABS, type SetupTab } from "../lib/setupnav";
export { SETUP_ALIASES, SETUP_TABS, setupSection, type SetupTab } from "../lib/setupnav";

export function ProjectSetup({ project, tab, query }: { project: Project; tab: SetupTab; query: URLSearchParams }) {
  const { reloadProjects } = useApp();
  const canEdit = useCanEdit();
  const profiles = project.profiles;
  const wanted = query.get("file");
  const main = mainProfile(project);
  const file = profiles.some((p) => p.file === wanted) ? wanted! : main?.file || profiles[0]?.file || "profile.yaml";
  const edit = query.get("edit");
  const meta = SETUP_TABS.find((t) => t.value === tab)!;
  const { data: detail, error, reload } = useLoad(() => setupApi.profile(project.path, file), [project.path, file]);
  const group = CONNECT_TABS.includes(tab) ? "connect" : SETTINGS_TABS.includes(tab) ? "settings" : null;
  const title = group === "connect" ? "Connect" : group === "settings" ? "Agent settings" : meta.label;
  useCrumbs([{ label: project.name, href: projectHref(project.key) },
    ...(group ? [{ label: title, href: projectHref(project.key, `/setup/${group === "connect" ? "connection" : "rules"}`) }] : []), { label: meta.label }]);
  const reloadAll = () => { reload(); void reloadProjects(); };

  if (error) return <Notice tone="bad" className="mt-5">{error.message}</Notice>;
  if (!detail) return <Skeleton />;
  const bodies: Record<SetupTab, () => ReactNode> = {
    connection: () => <AgentTab key={`a${file}`} detail={detail} reload={reloadAll} />,
    tools: () => <ToolsTab key={`t${file}`} detail={detail} reload={reloadAll} />,
    backend: () => <BackendTab key={`b${file}`} detail={detail} reload={reloadAll} />,
    logs: () => <LogsTab key={`l${file}`} detail={detail} reload={reloadAll} />,
    cases: () => <TestPlanTab part="cases" detail={detail} reload={reloadAll} edit={edit} projectKey={project.key} />,
    callers: () => <TestPlanTab part="callers" detail={detail} reload={reloadAll} edit={edit} projectKey={project.key} />,
    safety: () => <SafetyCard key={`sf${file}`} detail={detail} reload={reloadAll} projectKey={project.key} />,
    rules: () => <TestSettingsTab key={`s${file}`} detail={detail} reload={reloadAll} />,
    ops: () => <OpsTab key={`o${file}`} detail={detail} reload={reloadAll} projectKey={project.key} />,
    judges: () => <JudgesTab key={`j${file}`} detail={detail} reload={reloadAll} projectKey={project.key} />,
  };
  const shared = ["tools", "backend", "logs", "cases", "callers", "safety", "judges"].includes(tab);   // the same for every connection
  const channelPicker = profiles.length > 1 && !shared && (
    <div className="flex items-center gap-2.5 min-w-0 max-w-full">
      <span className="text-muted text-sm flex-none">{tab === "connection" ? "Connection" : "Settings for"}</span>
      <Seg id="profile-file" value={file} onChange={(f) => navigate(`${projectHref(project.key, `/setup/${tab}`)}?${qs({ file: f })}`)}
        options={profiles.map((p) => ({ value: p.file, label: <>{channelLabel(p, profiles, project.example)}{p.file === main?.file && profiles.length > 1 ? <span className="text-muted font-normal">main</span> : null}</>, icon: p.mode === "voice" ? "mic" : "text" }))} />
    </div>
  );
  const tabs = group === "connect" ? CONNECT_TABS : group === "settings" ? SETTINGS_TABS : [];

  return (
    <>
      <PageHead title={title} sub={group ? (group === "connect" ? "How Vocto reaches the agent, and what else it can check." : "Rules and helpers every test type of this agent uses.") : meta.sub}>
        {channelPicker}
      </PageHead>
      {tabs.length > 0 && (
        <LinkTabs value={tab} tabs={[...tabs.map((t) => {
          const m = SETUP_TABS.find((x) => x.value === t)!;
          return { value: t, label: m.label, href: projectHref(project.key, `/setup/${t}`) };
        }), ...(group === "settings" ? [{ value: "files", label: "Files", href: projectHref(project.key, "/files") }] : [])]} />
      )}
      {group && <p className="text-ink-2 text-sm -mt-2 mb-4">{meta.sub}</p>}
      {detail.problems.length > 0 && (
        <Notice tone="warn" className="mb-4">
          <b>To fix before the first run:</b>
          <ul className="mt-1 pl-4.5 list-disc">{detail.problems.slice(0, 8).map((p, i) => <li key={i} className="text-sm break-words">{p}</li>)}</ul>
        </Notice>
      )}
      <ReadOnly when={!canEdit}><div key={tab}>{bodies[tab]()}</div></ReadOnly>
    </>
  );
}
