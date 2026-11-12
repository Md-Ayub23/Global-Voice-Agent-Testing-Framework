// Home: every agent under test, side by side. Key numbers, what needs attention, each agent's latest result in
// every test type, runs per day and the latest runs. Nothing about one agent's set-up lives here: that is the
// agent's own Overview.
import { useState } from "react";
import { ActivityChart, KpiStrip, ProjectAvatar, When } from "../components/dash";
import { Icon } from "../components/Icon";
import { Badge, Button, Card, CardHead, EmptyState, LiveDot, Notice, PageHead, Seg, Skeleton, Table, rowLink } from "../components/ui";
import { api } from "../lib/api";
import { useApp, useCrumbs, useLoad } from "../lib/app";
import { cx, money } from "../lib/format";
import { domainLabel, projectHref, projectOfJob, runHref } from "../lib/links";
import { MODE_TITLE, SHORT_TITLE, TEST_TYPES, TONE_DOT, TONE_TEXT, TYPE_STATUS, modeTitle, runLabel, unavailableWords } from "../lib/modes";
import { navigate } from "../lib/router";
import type { Dashboard as DashboardData, ModeBrief } from "../lib/types";

type Show = "all" | "mine" | "practice";
type Row = DashboardData["projects"][number];

// One cell of the agents × test types grid.
function TypeCell({ m, k }: { m: ModeBrief | undefined; k: string }) {
  if (!m) return <span className="text-muted">—</span>;
  const st = TYPE_STATUS[m.status];
  const words = m.status === "unavailable" ? unavailableWords(m) : m.status === "watch" ? "Passed" : st.label;
  const body = (
    <span className={cx("inline-flex items-center gap-1.5 text-[13px] whitespace-nowrap", m.status === "not_run" || m.status === "unavailable" ? "text-muted" : TONE_TEXT[st.tone])}>
      {m.status !== "not_run" && m.status !== "unavailable" && <i className={cx("size-[7px] rounded-full", TONE_DOT[st.tone])} />}
      {words}{m.status === "watch" && <span className="text-muted">· notes</span>}
    </span>
  );
  return m.run_id
    ? <a href={projectHref(k, `/runs/${m.run_id}`)} onClick={(e) => e.stopPropagation()} title={m.top_finding || m.headline || st.label} className="hover:no-underline">{body}</a>
    : <span title={m.why || undefined}>{body}</span>;
}

// Agents that ran and are fine in everything they ran.
const passing = (p: Row) => {
  const ran = p.modes.filter((m) => TEST_TYPES.includes(m.id) && m.status !== "not_run" && m.status !== "unavailable");
  return ran.length > 0 && ran.every((m) => m.status === "passed" || m.status === "watch");
};

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
  const tested = data.projects.filter((p) => p.modes.some((m) => m.status !== "not_run" && m.status !== "unavailable"));
  // What to look at first: each agent's failed test types, then your agents that can't run yet.
  const attention = [
    ...data.projects.flatMap((p) => p.modes.filter((m) => m.status === "failed" && m.run_id).map((m) => ({
      key: `${p.key}:${m.id}`, name: p.name, tone: "bad" as const, href: projectHref(p.key, `/runs/${m.run_id}`), action: "Open report",
      what: `${MODE_TITLE[m.id]} failed${m.top_finding ? `: ${m.top_finding}` : ""}`,
    }))),
    ...data.projects.filter((p) => !p.connection.connected && !p.example).map((p) => ({
      key: `${p.key}:connect`, name: p.name, tone: "warn" as const, href: projectHref(p.key, "/setup/connection"), action: "Connect", what: "Not connected yet",
    })),
  ].slice(0, 6);

  return (
    <>
      <PageHead title="Voice agent quality" sub="Release readiness for every voice agent you test, at a glance.">
        <Button id="new-project" variant="primary" icon="plus" href="#/projects/new">New agent</Button>
      </PageHead>

      <div className="flex flex-col gap-6">
        {system && !system.api_key.set && (
          <Notice tone="warn" icon="key">
            <b>Add your Anthropic API key to run real tests.</b> Free dry runs work without it. <a href="#/settings">Open Settings</a>
          </Notice>
        )}
        {activeJob && (
          <a href={jobProject ? projectHref(jobProject.key, `/live/${activeJob.id}`) : "#/"} id="live-banner"
            className="flex items-center gap-3 px-4 py-3 rounded-[12px] border border-line bg-surface text-ink hover:no-underline hover:bg-surface-2 animate-rise">
            <LiveDot />
            <span className="flex-1 min-w-0"><b className="font-medium">{activeJob.kind === "exit-check" ? "Exit check running" : "Test run in progress"}</b>
              <span className="text-ink-2"> · {jobProject?.name || "An agent"}</span></span>
            <span className="text-accent-ink font-medium text-[13px] flex items-center gap-1">Watch live <Icon name="arrow" size={14} /></span>
          </a>
        )}

        <KpiStrip id="home-kpis" items={[
          { label: "Agents", value: t.projects, foot: `${t.client_projects} yours · ${t.examples} practice` },
          { label: "Passing what they ran", value: tested.length ? `${tested.filter(passing).length} of ${tested.length}` : "—",
            foot: tested.length ? "agents with no failed test type" : "after the first run",
            tone: tested.length ? (tested.every(passing) ? "good" : "bad") : undefined },
          { label: "Runs, last 7 days", value: t.runs_7d, foot: `${t.runs} in all` },
          { label: "Test calls", value: t.calls, foot: `${t.scenarios} test cases` },
          { label: "Spend", value: money(t.cost_usd), foot: "Claude, all runs" },
        ]} />

        {attention.length > 0 && (
          <section id="attention">
            <CardHead level={2} title="Needs attention" sub={`${attention.length}`} className="mb-2.5" />
            <div className=" rounded-[20px] glass divide-y divide-line overflow-hidden">
              {attention.map((a) => (
                <a key={a.key} href={a.href} data-attention={a.key}
                  className="group flex items-center gap-3 px-4 py-3 hover:bg-[var(--glass-strong)] hover:no-underline text-ink transition-colors">
                  <i className={cx("size-[7px] rounded-full flex-none", a.tone === "bad" ? "bg-bad" : "bg-warn")} />
                  <b className="font-semibold truncate min-w-0 max-sm:flex-1 sm:whitespace-nowrap sm:shrink-0">{a.name}</b>
                  <span className="text-ink-2 flex-1 min-w-0 truncate max-sm:hidden">{a.what}</span>
                  <span className="text-accent-ink text-[13px] font-medium whitespace-nowrap inline-flex items-center gap-1">{a.action}<Icon name="arrow" size={13} /></span>
                </a>
              ))}
            </div>
          </section>
        )}

        <section>
          <div className="flex items-center gap-3 flex-wrap mb-2.5">
            <h2 className="font-semibold text-[15px] flex-1">Agents by test type</h2>
            <span className="text-muted text-[13px] hidden sm:inline">latest result of each kind of test</span>
            <Seg id="show" value={show} onChange={setShow}
              options={[{ value: "all", label: `All ${data.projects.length}` }, { value: "mine", label: "Yours" }, { value: "practice", label: "Practice" }]} />
          </div>
          <Card flush>
            {rows.length ? (
              <Table compact head={["Agent", ...TEST_TYPES.map((id) => SHORT_TITLE[id] || MODE_TITLE[id]), "Last run"]}>
                {rows.map((p) => {
                  const byId = new Map(p.modes.map((m) => [m.id, m]));
                  return (
                    <tr key={p.key} className={rowLink} data-project={p.key} onClick={() => navigate(projectHref(p.key))}>
                      <td>
                        <div className="flex items-center gap-2.5 min-w-[200px]">
                          <ProjectAvatar kind={p.connection.kind} size={26} />
                          <div className="min-w-0">
                            <b className="block truncate font-medium">{p.name}</b>
                            <span className="text-muted text-[13px]">{domainLabel(p.domain)} · {p.connection.connected
                              ? <>{p.connection.label}{p.connection.text_too ? " + text" : ""}</> : <span className="text-warn">Not connected</span>}{p.example && !p.connection.label.startsWith("Practice") ? " · practice" : ""}</span>
                          </div>
                        </div>
                      </td>
                      {TEST_TYPES.map((id) => <td key={id}><TypeCell m={byId.get(id)} k={p.key} /></td>)}
                      <td className="text-muted whitespace-nowrap"><When iso={p.stats.last_run?.started_at} /></td>
                    </tr>
                  );
                })}
              </Table>
            ) : (
              <EmptyState icon="bot" title={show === "mine" ? "No agents of yours yet" : "No agents"}
                text="Add each voice agent you want to test." action={<Button variant="primary" icon="plus" href="#/projects/new">New agent</Button>} />
            )}
          </Card>
        </section>

        <div className="grid gap-6 items-start grid-cols-1 xl:grid-cols-[minmax(0,1fr)_400px]">
          <section>
            <CardHead level={2} title="Runs per day" sub="last 14 days, all agents" className="mb-2.5" />
            <Card><ActivityChart days={data.activity} /></Card>
          </section>
          <section>
            <CardHead level={2} title="Recent runs" className="mb-2.5" />
            <Card flush>
              {data.recent_runs.length ? (
                <div className="divide-y divide-line">
                  {data.recent_runs.slice(0, 6).map((r) => (
                    <a key={r.run_id} href={runHref(r)} className="flex items-center gap-3 px-4 py-2.5 hover:bg-surface-2 hover:no-underline text-ink">
                      <Badge status={r.decision || r.status} label={runLabel(r)} />
                      <span className="flex-1 min-w-0 truncate">{r.project_name || r.profile_name || "Removed agent"} <span className="text-muted">· {modeTitle(r.test_mode)}</span></span>
                      <span className="text-muted text-[13px] whitespace-nowrap"><When iso={r.started_at} /></span>
                    </a>
                  ))}
                </div>
              ) : <EmptyState title="No runs yet" text="Open an agent and run its tests: results show up here." icon="play" />}
            </Card>
          </section>
        </div>
      </div>
    </>
  );
}
