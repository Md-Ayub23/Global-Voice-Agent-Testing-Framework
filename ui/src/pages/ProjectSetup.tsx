// One agent's set-up, one page per step: connection, tools & data (tools, database, logs), test cases, callers,
// safety tests and pass rules; plus the judges' settings, shown under AI judges. Every save is checked with the same rules a run uses.
import type { ReactNode } from "react";
import { Icon, type IconName } from "../components/Icon";
import { Button, LinkTabs, Notice, Seg, Skeleton } from "../components/ui";
import { JudgesHeader } from "./Judges";
import { SafetyCard } from "./setup/SafetyCard";
import { qs } from "../lib/api";
import { useApp, useCrumbs, useLoad } from "../lib/app";
import { channelLabel, projectHref } from "../lib/links";
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
interface SetupMeta { value: SetupTab; label: string; icon: IconName; sub: string; step?: number }
// The set-up pages in the order the sidebar shows them. Tools, database and logs are tabs of one step, "Tools & data".
export const SETUP_TABS: SetupMeta[] = [
  { value: "connection", step: 1, label: "Connection", icon: "plug", sub: "Who the agent is and how Vocto reaches it." },
  { value: "tools", step: 2, label: "Tools", icon: "tool", sub: "The APIs the agent calls. Vocto watches every call and can break one on purpose." },
  { value: "backend", step: 2, label: "Database", icon: "db", sub: "Read-only access, so Vocto checks what really got saved, not what the agent said." },
  { value: "logs", step: 2, label: "Logs", icon: "logs", sub: "The agent's own log lines, kept next to each call and checked by scenarios." },
  { value: "cases", step: 3, label: "Test cases", icon: "plan", sub: "The situations the agent must handle. Write them yourself or let Claude draft them from the agent's instructions." },
  { value: "callers", step: 4, label: "Callers", icon: "user", sub: "Who calls: the personas a scenario can be run with, from calm to impatient." },
  { value: "safety", step: 5, label: "Safety tests", icon: "shield", sub: "Attack calls that try to make the agent leak, lie or go off its job." },
  { value: "rules", step: 6, label: "Pass rules", icon: "sliders", sub: "What a GO needs for this agent: response times, repeats, coverage, budget, and what may not get worse than the baseline." },
  { value: "ops", step: 7, label: "CI & alerts", icon: "bell", sub: "Run this agent's tests in CI on every change, on a schedule as health checks, and get a message when a release is blocked." },
  { value: "judges", label: "Judge settings", icon: "sparkles", sub: "What the AI judges score in every conversation, and what the agent may say." },
];
const STEPS = 7;
const DATA_TABS: SetupTab[] = ["tools", "backend", "logs"];
// Old addresses (before the redesign) and where they live now.
export const SETUP_ALIASES: Record<string, SetupTab> = { agent: "connection", plan: "cases", settings: "rules" };
export const setupSection = (tab: string) => (DATA_TABS.includes(tab as SetupTab) ? "setup:data" : tab === "judges" ? "judges" : `setup:${tab}`);

export function ProjectSetup({ project, tab, query }: { project: Project; tab: SetupTab; query: URLSearchParams }) {
  const { reloadProjects } = useApp();
  const profiles = project.profiles;
  const wanted = query.get("file");
  const file = profiles.some((p) => p.file === wanted) ? wanted! : profiles.find((p) => p.file === "profile.yaml")?.file || profiles[0]?.file || "profile.yaml";
  const edit = query.get("edit");
  const meta = SETUP_TABS.find((t) => t.value === tab)!;
  const { data: detail, error, reload } = useLoad(() => setupApi.profile(project.path, file), [project.path, file]);
  const dataTab = DATA_TABS.includes(tab);
  const title = dataTab ? "Tools & data" : meta.label;
  useCrumbs([{ label: "Agents", href: "#/projects" }, { label: project.name, href: projectHref(project.key) },
    ...(tab === "judges" ? [{ label: "AI judges", href: projectHref(project.key, "/judges") }] : []), { label: title }]);
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
  const order = SETUP_TABS.filter((t) => t.step);
  const idx = order.findIndex((t) => t.value === tab);
  const next = idx >= 0 ? order[idx + 1] : undefined;
  const shared = ["tools", "backend", "logs", "cases", "callers", "safety", "judges"].includes(tab);   // the same for every channel
  const channelPicker = profiles.length > 1 && !shared && (
    <div className="flex items-center gap-2.5 min-w-0 max-w-full">
      <span className="text-muted text-[13px] flex-none">Channel</span>
      <Seg id="profile-file" value={file} onChange={(f) => navigate(`${projectHref(project.key, `/setup/${tab}`)}?${qs({ file: f })}`)}
        options={profiles.map((p) => ({ value: p.file, label: channelLabel(p, profiles), icon: p.mode === "voice" ? "mic" : "text" }))} />
    </div>
  );

  return (
    <>
      {tab === "judges" ? (
        <JudgesHeader project={project} current="settings" />
      ) : (
        <div className="flex items-end gap-4 mt-3 mb-6 flex-wrap">
          <div className="flex-1 min-w-[260px]">
            <div className="text-muted text-[12px] font-medium flex items-center gap-1.5 uppercase tracking-[.06em]">
              <Icon name={meta.icon} size={13} />Set up · step {meta.step} of {STEPS}
            </div>
            <h1 className="text-[24px] font-semibold tracking-[-.025em] mt-1">{title}</h1>
            <div className="mt-1 text-ink-2 text-[13.5px] max-w-[760px]">{dataTab ? "What Vocto can see besides the voice: the agent's tool calls, its database and its logs." : meta.sub}</div>
          </div>
          {channelPicker}
        </div>
      )}
      {dataTab && (
        <LinkTabs value={tab} tabs={DATA_TABS.map((t) => {
          const m = SETUP_TABS.find((x) => x.value === t)!;
          return { value: t, label: m.label, icon: m.icon, href: projectHref(project.key, `/setup/${t}`) };
        })} />
      )}
      {dataTab && <p className="text-ink-2 text-[13px] -mt-2 mb-4">{meta.sub}</p>}
      {detail.problems.length > 0 && (
        <Notice tone="warn" className="mb-4">
          <b>To fix before the first run:</b>
          <ul className="mt-1 pl-[18px] list-disc">{detail.problems.slice(0, 8).map((p, i) => <li key={i} className="text-[12.5px] break-words">{p}</li>)}</ul>
        </Notice>
      )}
      <div key={tab} className="animate-view-in">{bodies[tab]()}</div>
      {!edit && tab !== "judges" && (
        <div className="flex items-center gap-3 mt-6 pt-5 border-t border-line flex-wrap">
          <span className="text-muted text-[12.5px]">{next ? (next.step !== meta.step ? `Next: step ${next.step} of ${STEPS}` : "Next tab of this step") : "Set up is done: time to run the tests"}</span>
          <span className="flex-1" />
          {next
            ? <Button id="next-step" icon="arrow" href={projectHref(project.key, `/setup/${next.value}`)}>Next: {next.label}</Button>
            : <Button id="next-step" variant="primary" icon="play" href={projectHref(project.key, "/new")}>Run tests</Button>}
        </div>
      )}
    </>
  );
}
