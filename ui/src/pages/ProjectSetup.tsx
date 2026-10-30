// One project's setup, one page per part: the agent and its connection, tools, backend, logs,
// test plan (with the safety tests), AI judges and test settings. Every save is checked with the same rules a run uses.
import type { ReactNode } from "react";
import { Icon, type IconName } from "../components/Icon";
import { Button, Notice, Seg, Skeleton } from "../components/ui";
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

export type SetupTab = "agent" | "tools" | "backend" | "logs" | "plan" | "judges" | "settings";
export const SETUP_TABS: { value: SetupTab; label: string; icon: IconName; sub: string }[] = [
  { value: "agent", label: "Agent & connection", icon: "plug", sub: "Who the agent is and how Vocto reaches it." },
  { value: "tools", label: "Tools", icon: "tool", sub: "The APIs the agent calls. Vocto watches every call and can break one on purpose." },
  { value: "backend", label: "Backend & database", icon: "db", sub: "Read-only access, so Vocto checks what really got saved, not what the agent said." },
  { value: "logs", label: "Logs", icon: "logs", sub: "The agent's own log lines, kept next to each call and checked by scenarios." },
  { value: "plan", label: "Test plan", icon: "plan", sub: "What to test: scenarios (situations), safety tests (attack calls) and personas (who calls)." },
  { value: "judges", label: "AI judges", icon: "scale", sub: "What the AI judges score in every conversation, and what the agent may say." },
  { value: "settings", label: "Test settings", icon: "sliders", sub: "This agent's limits: response times, repeats, coverage, budget." },
];

export function ProjectSetup({ project, tab, query }: { project: Project; tab: SetupTab; query: URLSearchParams }) {
  const { reloadProjects } = useApp();
  const profiles = project.profiles;
  const wanted = query.get("file");
  const file = profiles.some((p) => p.file === wanted) ? wanted! : profiles.find((p) => p.file === "profile.yaml")?.file || profiles[0]?.file || "profile.yaml";
  const edit = query.get("edit");
  const meta = SETUP_TABS.find((t) => t.value === tab)!;
  const { data: detail, error, reload } = useLoad(() => setupApi.profile(project.path, file), [project.path, file]);
  useCrumbs([{ label: "Projects", href: "#/projects" }, { label: project.name, href: projectHref(project.key) }, { label: meta.label }]);
  const reloadAll = () => { reload(); void reloadProjects(); };

  if (error) return <Notice tone="bad" className="mt-5">{error.message}</Notice>;
  if (!detail) return <Skeleton />;
  const bodies: Record<SetupTab, () => ReactNode> = {
    agent: () => <AgentTab key={`a${file}`} detail={detail} reload={reloadAll} />,
    tools: () => <ToolsTab key={`t${file}`} detail={detail} reload={reloadAll} />,
    backend: () => <BackendTab key={`b${file}`} detail={detail} reload={reloadAll} />,
    logs: () => <LogsTab key={`l${file}`} detail={detail} reload={reloadAll} />,
    plan: () => <TestPlanTab detail={detail} reload={reloadAll} edit={edit} projectKey={project.key} />,
    judges: () => <JudgesTab key={`j${file}`} detail={detail} reload={reloadAll} projectKey={project.key} />,
    settings: () => <TestSettingsTab key={`s${file}`} detail={detail} reload={reloadAll} />,
  };
  const idx = SETUP_TABS.findIndex((t) => t.value === tab);
  const next = SETUP_TABS[idx + 1];
  const shared = ["tools", "backend", "logs", "plan", "judges"].includes(tab);   // the same for every channel

  return (
    <>
      <div className="flex items-end gap-4 mt-2 mb-5 flex-wrap">
        <div className="flex-1 min-w-[260px]">
          <div className="text-muted text-[12.5px] font-semibold flex items-center gap-1.5"><Icon name={meta.icon} size={14} />Step {idx + 1} of {SETUP_TABS.length} · {project.name}</div>
          <h1 className="text-2xl font-bold tracking-[-.02em] mt-1">{meta.label}</h1>
          <div className="mt-1 text-ink-2">{meta.sub}</div>
        </div>
        {profiles.length > 1 && !shared && (
          <div className="flex items-center gap-2.5">
            <span className="text-muted text-[13px]">Channel</span>
            <Seg id="profile-file" value={file} onChange={(f) => navigate(`${projectHref(project.key, `/setup/${tab}`)}?${qs({ file: f })}`)}
              options={profiles.map((p) => ({ value: p.file, label: channelLabel(p, profiles), icon: p.mode === "voice" ? "mic" : "text" }))} />
          </div>
        )}
      </div>
      {detail.problems.length > 0 && (
        <Notice tone="warn" className="mb-4">
          <b>To fix before the first run:</b>
          <ul className="mt-1 pl-[18px] list-disc">{detail.problems.slice(0, 8).map((p, i) => <li key={i} className="text-[12.5px] break-words">{p}</li>)}</ul>
        </Notice>
      )}
      <div key={tab}>{bodies[tab]()}</div>
      {!edit && (
        <div className="flex items-center gap-3 mt-5 flex-wrap">
          <span className="flex-1" />
          {next
            ? <Button id="next-step" icon="arrow" href={projectHref(project.key, `/setup/${next.value}`)}>Next: {next.label}</Button>
            : <Button id="next-step" variant="primary" icon="play" href={projectHref(project.key, "/new")}>Run tests</Button>}
        </div>
      )}
    </>
  );
}
