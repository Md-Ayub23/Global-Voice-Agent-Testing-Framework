// Home: every agent under test at a glance. What needs attention, how many agents and of which kinds,
// how the runs went, recent activity, and each agent's latest result.
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
  useCrumbs([{ label: "Home" }]);
  const { system, activeJob, projects } = useApp();
  const { data, error } = useLoad(() => api.get<DashboardData>("/api/dashboard"), [activeJob?.id, activeJob?.status]);
  const [show, setShow] = useState<Show>("all");
  if (error) return <Notice tone="bad" className="mt-5">{error.message}</Notice>;
  if (!data) return <Skeleton height={520} />;
  const t = data.totals;
  const jobProject = projectOfJob(activeJob, projects);
  const rows = data.projects.filter((p) => show === "all" || (show === "mine" ? !p.example : p.example));
  const voice = data.agent_types.channels.voice, text = data.agent_types.channels.text;
  // What to look at first: agents whose latest decided run was NO-GO, then your agents that still need setting up.
  const attention = [
    ...data.projects.filter((p) => p.stats.last_decided && p.stats.last_decided.decision !== "GO").map((p) => ({
      key: p.key, name: p.name, tone: "bad" as const, href: runHref(p.stats.last_decided!), action: "Open the run",
      what: p.stats.last_decided!.decision === "NO_GO" ? "Latest run was NO-GO" : "Latest run didn't have enough evidence",
    })),
    ...data.projects.filter((p) => !p.ready && !p.example).map((p) => ({
      key: p.key, name: p.name, tone: "warn" as const, href: projectHref(p.key), action: "Finish set up", what: "Not ready to run yet",
    })),
  ].slice(0, 6);

  return (
    <>
      <PageHead title="Home" sub="Every voice agent you test, and how its releases are doing.">
        <Button icon="bot" href="#/projects">All agents</Button>
        <Button id="new-project" variant="primary" icon="plus" href="#/projects/new">New agent</Button>
      </PageHead>

      <div className="flex flex-col gap-4">
        {system && !system.api_key.set && (
          <Notice tone="warn" icon="key">
            <b>Add your Anthropic API key to run real tests.</b> Free dry runs work without it. <a href="#/settings">Open Settings</a>
          </Notice>
        )}
        {activeJob && (
          <a href={jobProject ? projectHref(jobProject.key, `/live/${activeJob.id}`) : "#/"} id="live-banner"
            className="flex items-center gap-3.5 p-4 rounded-[10px] border border-[color-mix(in_srgb,var(--accent)_30%,transparent)] bg-accent-soft text-ink hover:no-underline animate-rise">
            <span className="size-9 rounded-full bg-accent text-white grid place-items-center flex-none"><Icon name="activity" /></span>
            <span className="flex-1 min-w-0">
              <b className="flex items-center gap-2">{activeJob.kind === "exit-check" ? "Exit check running" : "Test run in progress"} <LiveDot /></b>
              <span className="text-ink-2 text-[13px]">{jobProject?.name || "An agent"}: watch every call as it happens.</span>
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

        {attention.length > 0 && (
          <Card flush id="attention">
            <div className="px-5 pt-4 pb-2"><CardHead level={2} title="Needs attention" sub={`${attention.length} agent${attention.length === 1 ? "" : "s"}`} className="mb-0" /></div>
            <div className="divide-y divide-line border-t border-line stagger">
              {attention.map((a) => (
                <a key={a.key + a.what} href={a.href} data-attention={a.key}
                  className="group flex items-center gap-3 px-5 py-3 hover:bg-surface-2/70 hover:no-underline text-ink transition-colors">
                  <span className={a.tone === "bad" ? "size-2 rounded-full bg-bad flex-none" : "size-2 rounded-full bg-warn flex-none"} />
                  <b className="text-[13.5px] truncate">{a.name}</b>
                  <span className="text-ink-2 text-[13px] flex-1 min-w-0 truncate">{a.what}</span>
                  <span className="text-accent-ink text-[12.5px] font-semibold whitespace-nowrap inline-flex items-center gap-1">{a.action}<Icon name="arrow" size={14} className="transition-transform group-hover:translate-x-0.5" /></span>
                </a>
              ))}
            </div>
          </Card>
        )}

        <div className="grid gap-4 items-start grid-cols-1 xl:grid-cols-[minmax(0,1fr)_380px]">
          <Card>
            <CardHead level={2} title="Activity" sub="runs per day, last 14 days" />
            <ActivityChart days={data.activity} />
          </Card>
          <Card>
            <CardHead level={2} title="Agent types" />
            <div className="grid grid-cols-2 gap-2.5 mb-4">
              <div className="flex items-center gap-2.5 p-3 rounded-[10px] bg-surface-2">
                <span className="size-8 rounded-lg grid place-items-center bg-agent-soft text-agent"><Icon name="mic" /></span>
                <div><b className="text-lg tabular-nums leading-none">{voice}</b><div className="text-muted text-xs">voice</div></div>
              </div>
              <div className="flex items-center gap-2.5 p-3 rounded-[10px] bg-surface-2">
                <span className="size-8 rounded-lg grid place-items-center bg-caller-soft text-caller"><Icon name="text" /></span>
                <div><b className="text-lg tabular-nums leading-none">{text}</b><div className="text-muted text-xs">text</div></div>
              </div>
            </div>
            <div className="text-[12.5px] text-ink-2 font-semibold mb-2.5">Agents by industry</div>
            <DomainBars rows={data.agent_types.domains} />
          </Card>
        </div>

        <Card flush>
          <div className="flex items-center gap-3 flex-wrap px-[18px] pt-4 pb-3">
            <h2 className="font-semibold text-base flex-1">Agents</h2>
            <Seg id="show" value={show} onChange={setShow}
              options={[{ value: "all", label: `All ${data.projects.length}` }, { value: "mine", label: "Your agents" }, { value: "practice", label: "Practice" }]} />
          </div>
          {rows.length ? (
            <Table head={["Agent", "Type", "Last result", "Recent decisions", "GO rate", "Runs", "Last run"]}>
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
            <EmptyState icon="bot" title={show === "mine" ? "No agents of yours yet" : "No agents"}
              text="Add each voice agent you want to test." action={<Button variant="primary" icon="plus" href="#/projects/new">New agent</Button>} />
          )}
        </Card>

        <Card flush>
          <div className="px-[18px] pt-4 pb-3"><h2 className="font-semibold text-base">Recent runs</h2></div>
          {data.recent_runs.length ? (
            <Table head={["Decision", "Agent", "Channel", "Calls", "Pass / fail", "When"]}>
              {data.recent_runs.map((r) => (
                <tr key={r.run_id} className={rowLink} onClick={() => navigate(runHref(r))}>
                  <td><Badge status={r.decision || r.status} /></td>
                  <td>
                    <div className="flex items-center gap-2.5">
                      {r.project_key && <ProjectAvatar name={r.project_name || ""} k={r.project_key} size={26} />}
                      <b className="truncate">{r.project_name || r.profile_name || "Removed agent"}</b>
                    </div>
                  </td>
                  <td>{modeLabel(r.mode)}{r.offline ? <span className="text-muted"> · dry run</span> : null}</td>
                  <td className="tabular-nums">{r.calls_started}{r.planned ? `/${r.planned}` : ""}</td>
                  <td className="tabular-nums whitespace-nowrap"><span className="text-good">{r.counts.PASS || 0}</span> / <span className="text-bad">{r.counts.FAIL || 0}</span></td>
                  <td className="text-muted"><When iso={r.started_at} /></td>
                </tr>
              ))}
            </Table>
          ) : <EmptyState title="No runs yet" text="Open an agent and run its tests: results show up here." icon="play" />}
        </Card>
      </div>
    </>
  );
}
