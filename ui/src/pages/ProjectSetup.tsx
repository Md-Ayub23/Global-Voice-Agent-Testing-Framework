// One agent's set-up pages: Connect (connection, tools, database, logs) and Agent settings (pass rules, callers,
// AI judges, CI & alerts), plus the test cases and safety tests pages. Every save is checked with the same rules a run uses.
import type { ReactNode } from "react";
import type { IconName } from "../components/Icon";
import { LinkTabs, Notice, PageHead, Seg, Skeleton } from "../components/ui";
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

export type SetupTab = "connection" | "tools" | "backend" | "logs" | "cases" | "callers" | "safety" | "rules" | "ops" | "judges";
interface SetupMeta { value: SetupTab; label: string; icon: IconName; sub: string }
// The set-up pages. Connect holds how Vocto reaches the agent and what else it can check; Agent settings
// holds the rules and helpers every test type shares. Test cases and safety tests belong to their test type.
export const SETUP_TABS: SetupMeta[] = [
  { value: "connection", label: "Connection", icon: "plug", sub: "How callers reach the agent, and the agent brief Vocto writes test cases from." },
  { value: "tools", label: "Tools", icon: "tool", sub: "The APIs the agent calls. Vocto watches every call and can break one on purpose." },
  { value: "backend", label: "Database", icon: "db", sub: "Read-only access, so Vocto checks what really got saved, not what the agent said." },
  { value: "logs", label: "Logs", icon: "logs", sub: "The agent's own log lines, kept next to each call and checked by test cases." },
  { value: "cases", label: "Test cases", icon: "plan", sub: "The situations the agent must handle." },
  { value: "safety", label: "Safety tests", icon: "shield", sub: "Attack calls that try to make the agent leak, lie or go off its job." },
  { value: "rules", label: "Pass rules", icon: "sliders", sub: "What a pass needs for this agent: response times, repeats, coverage, budget, and what may not get worse than the baseline." },
  { value: "callers", label: "Callers", icon: "user", sub: "The people who call in your tests, from calm to impatient. Every test case picks one." },
  { value: "judges", label: "AI judges", icon: "sparkles", sub: "What the AI judges score in every conversation, and what the agent may say." },
  { value: "ops", label: "CI & alerts", icon: "bell", sub: "Run this agent's tests in CI, on a schedule as health checks, and get a message when a release is blocked." },
];
const CONNECT_TABS: SetupTab[] = ["connection", "tools", "backend", "logs"];
const SETTINGS_TABS: SetupTab[] = ["rules", "callers", "judges", "ops"];
// Old addresses and where they live now.
export const SETUP_ALIASES: Record<string, SetupTab> = { agent: "connection", plan: "cases", settings: "rules" };
export const setupSection = (tab: string) => (CONNECT_TABS.includes(tab as SetupTab) ? "connect"
  : SETTINGS_TABS.includes(tab as SetupTab) ? "agent-settings" : tab === "safety" ? "mode:safety" : "mode:functional");

export function ProjectSetup({ project, tab, query }: { project: Project; tab: SetupTab; query: URLSearchParams }) {
  const { reloadProjects } = useApp();
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
      <span className="text-muted text-[13px] flex-none">{tab === "connection" ? "Connection" : "Settings for"}</span>
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
      {group && <p className="text-ink-2 text-[13px] -mt-2 mb-4">{meta.sub}</p>}
      {detail.problems.length > 0 && (
        <Notice tone="warn" className="mb-4">
          <b>To fix before the first run:</b>
          <ul className="mt-1 pl-[18px] list-disc">{detail.problems.slice(0, 8).map((p, i) => <li key={i} className="text-[13px] break-words">{p}</li>)}</ul>
        </Notice>
      )}
      <div key={tab} >{bodies[tab]()}</div>
    </>
  );
}
