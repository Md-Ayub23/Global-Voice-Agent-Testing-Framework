// The home dashboard: every agent under test at a glance. How many projects and of which kinds,
// how the runs went, recent activity, and each project's latest result.
import { useState } from "react";
import { CountUp } from "../components/CountUp";
import { ActivityChart, DomainBars, Kpi, MiniHistory, ProjectAvatar, When } from "../components/dash";
import { Icon } from "../components/Icon";
import { Badge, Button, Card, CardHead, Chip, EmptyState, LiveDot, Notice, PageHead, Seg, Skeleton, Table, rowLink } from "../components/ui";
import { api } from "../lib/api";
import { useApp, useCrumbs, useLoad } from "../lib/app";
import { money, pct } from "../lib/format";
import { domainLabel, modeLabel, projectHref, projectOfJob, runHref } from "../lib/links";
import { navigate } from "../lib/router";
import type { Dashboard as DashboardData } from "../lib/types";

type Show = "all" | "mine" | "practice";

export function Dashboard() {
  useCrumbs([{ label: "Dashboard" }]);
  const { system, activeJob, projects } = useApp();
  const { data, error } = useLoad(() => api.get<DashboardData>("/api/dashboard"), [activeJob?.id, activeJob?.status]);
  const [show, setShow] = useState<Show>("all");
  if (error) return <Notice tone="bad" className="mt-5">{error.message}</Notice>;
  if (!data) return <Skeleton height={520} />;
  const t = data.totals;
  const jobProject = projectOfJob(activeJob, projects);
  const rows = data.projects.filter((p) => show === "all" || (show === "mine" ? !p.example : p.example));
  const voice = data.agent_types.channels.voice, text = data.agent_types.channels.text;

  return (
    <>
      <PageHead title="Dashboard" sub="Every voice agent you test, and how its releases are doing.">
        <Button icon="grid" href="#/projects">All projects</Button>
        <Button id="new-project" variant="primary" icon="plus" href="#/projects/new">New project</Button>
      </PageHead>

      <div className="flex flex-col gap-4">
        {system && !system.api_key.set && (
          <Notice tone="warn" icon="key">
            <b>Add your Anthropic API key to run real tests.</b> Free dry runs work without it. <a href="#/settings">Open Settings</a>
          </Notice>
        )}
        {activeJob && (
          <a href={jobProject ? projectHref(jobProject.key, `/live/${activeJob.id}`) : "#/"} id="live-banner"
            className="flex items-center gap-3.5 p-4 rounded-[14px] border border-[color-mix(in_srgb,var(--accent)_30%,transparent)] bg-accent-soft text-ink hover:no-underline animate-rise">
            <span className="size-9 rounded-full bg-accent text-white grid place-items-center flex-none"><Icon name="activity" /></span>
            <span className="flex-1 min-w-0">
              <b className="flex items-center gap-2">{activeJob.kind === "exit-check" ? "Exit check running" : "Test run in progress"} <LiveDot /></b>
              <span className="text-ink-2 text-[13px]">{jobProject?.name || "A project"}: watch every call as it happens.</span>
            </span>
            <span className="text-accent-ink font-semibold text-[13px] flex items-center gap-1">Watch live <Icon name="arrow" size={14} /></span>
          </a>
        )}

        <div className="grid gap-4 grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 stagger">
          <Kpi icon="bot" label="Agents under test" value={<CountUp to={t.projects} />}
            foot={`${t.client_projects} of yours · ${t.examples} practice agent${t.examples === 1 ? "" : "s"}`} />
          <Kpi icon="layers" label="Test runs" value={<CountUp to={t.runs} />} foot={`${t.runs_7d} in the last 7 days · ${t.decided} with a decision`} />
          <Kpi icon="shield" label="Release GO rate" tone={t.go_rate == null ? "accent" : t.go_rate >= 0.8 ? "good" : t.go_rate >= 0.5 ? "warn" : "bad"}
            value={t.go_rate == null ? "—" : <CountUp to={t.go_rate * 100} suffix="%" />}
            foot={t.decided ? `${t.go} GO · ${t.no_go} NO-GO${t.insufficient ? ` · ${t.insufficient} not enough evidence` : ""}${t.bug_runs ? ` · ${t.bug_runs} bug test${t.bug_runs === 1 ? "" : "s"} not counted` : ""}` : "after your first run"} />
          <Kpi icon="phone" label="Test calls" value={<CountUp to={t.calls} />} foot={`${t.scenarios} scenarios · ${money(t.cost_usd)} spent`} />
        </div>

        <div className="grid gap-4 items-start grid-cols-1 xl:grid-cols-[minmax(0,1fr)_380px]">
          <Card>
            <CardHead level={2} title="Activity" sub="runs per day, last 14 days" />
            <ActivityChart days={data.activity} />
          </Card>
          <Card>
            <CardHead level={2} title="Agent types" />
            <div className="grid grid-cols-2 gap-2.5 mb-4">
              <div className="flex items-center gap-2.5 p-3 rounded-xl bg-surface-2">
                <span className="size-8 rounded-[10px] grid place-items-center bg-agent-soft text-agent"><Icon name="mic" /></span>
                <div><b className="text-lg tabular-nums leading-none">{voice}</b><div className="text-muted text-xs">voice</div></div>
              </div>
              <div className="flex items-center gap-2.5 p-3 rounded-xl bg-surface-2">
                <span className="size-8 rounded-[10px] grid place-items-center bg-caller-soft text-caller"><Icon name="text" /></span>
                <div><b className="text-lg tabular-nums leading-none">{text}</b><div className="text-muted text-xs">text</div></div>
              </div>
            </div>
            <div className="text-[12.5px] text-ink-2 font-semibold mb-2.5">Projects by industry</div>
            <DomainBars rows={data.agent_types.domains} />
          </Card>
        </div>

        <Card flush>
          <div className="flex items-center gap-3 flex-wrap px-[18px] pt-4 pb-3">
            <h2 className="font-semibold text-base flex-1">Projects</h2>
            <Seg id="show" value={show} onChange={setShow}
              options={[{ value: "all", label: `All ${data.projects.length}` }, { value: "mine", label: "Your agents" }, { value: "practice", label: "Practice" }]} />
          </div>
          {rows.length ? (
            <Table head={["Project", "Type", "Last result", "Recent decisions", "GO rate", "Runs", "Last run"]}>
              {rows.map((p) => (
                <tr key={p.key} className={rowLink} data-project={p.key} onClick={() => navigate(projectHref(p.key))}>
                  <td>
                    <div className="flex items-center gap-3 min-w-[220px]">
                      <ProjectAvatar name={p.name} k={p.key} size={36} />
                      <div className="min-w-0">
                        <b className="block truncate">{p.name}</b>
                        <span className="text-muted text-xs">{p.stats.scenarios} scenario{p.stats.scenarios === 1 ? "" : "s"}{p.example ? " · practice agent" : ""}</span>
                      </div>
                    </div>
                  </td>
                  <td>
                    <div className="flex flex-wrap gap-1.5">
                      <Chip>{domainLabel(p.domain)}</Chip>
                      {p.channels.map((c) => <Chip key={c} tone={c === "voice" ? "agent" : "caller"} icon={c === "voice" ? "mic" : "text"}>{modeLabel(c)}</Chip>)}
                    </div>
                  </td>
                  <td>{p.stats.last_decided ? <Badge status={p.stats.last_decided.decision} /> : p.ready ? <span className="text-muted text-[12.5px]">not run yet</span> : <Chip tone="warn" icon="alert">to set up</Chip>}</td>
                  <td><MiniHistory items={p.stats.history} empty="—" /></td>
                  <td className="tabular-nums">{pct(p.stats.go_rate)}</td>
                  <td className="tabular-nums">{p.stats.runs}</td>
                  <td className="text-muted"><When iso={p.stats.last_run?.started_at} /></td>
                </tr>
              ))}
            </Table>
          ) : (
            <EmptyState icon="bot" title={show === "mine" ? "No agents of yours yet" : "No projects"}
              text="Create a project for each voice agent you test." action={<Button variant="primary" icon="plus" href="#/projects/new">New project</Button>} />
          )}
        </Card>

        <Card flush>
          <div className="px-[18px] pt-4 pb-3"><h2 className="font-semibold text-base">Recent runs</h2></div>
          {data.recent_runs.length ? (
            <Table head={["Decision", "Project", "Channel", "Calls", "Pass / fail", "When"]}>
              {data.recent_runs.map((r) => (
                <tr key={r.run_id} className={rowLink} onClick={() => navigate(runHref(r))}>
                  <td><Badge status={r.decision || r.status} /></td>
                  <td>
                    <div className="flex items-center gap-2.5">
                      {r.project_key && <ProjectAvatar name={r.project_name || ""} k={r.project_key} size={26} />}
                      <b className="truncate">{r.project_name || r.profile_name || "Removed project"}</b>
                    </div>
                  </td>
                  <td>{modeLabel(r.mode)}{r.offline ? <span className="text-muted"> · dry run</span> : null}</td>
                  <td className="tabular-nums">{r.calls_started}{r.planned ? `/${r.planned}` : ""}</td>
                  <td className="tabular-nums whitespace-nowrap"><span className="text-good">{r.counts.PASS || 0}</span> / <span className="text-bad">{r.counts.FAIL || 0}</span></td>
                  <td className="text-muted"><When iso={r.started_at} /></td>
                </tr>
              ))}
            </Table>
          ) : <EmptyState title="No runs yet" text="Open a project and start a run: results show up here." icon="play" />}
        </Card>
      </div>
    </>
  );
}
