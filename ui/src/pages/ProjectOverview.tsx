// One project's home: who the agent is, what's left to set up, its latest release decision, its
// numbers and recent runs.
import { CountUp } from "../components/CountUp";
import { ActivityChart, Kpi, MiniHistory, ProjectAvatar, When } from "../components/dash";
import { Icon, type IconName } from "../components/Icon";
import { Hero, heroToneOf } from "../components/runbits";
import { Badge, Button, Card, CardHead, Chip, EmptyState, Notice, Skeleton, Table, rowLink } from "../components/ui";
import { api } from "../lib/api";
import { useApp, useCrumbs, useLoad } from "../lib/app";
import { cx, fmtMs, money, pct } from "../lib/format";
import { domainLabel, modeLabel, projectHref, projectOfJob, runHref } from "../lib/links";
import { navigate } from "../lib/router";
import { decisionTitle } from "../lib/status";
import type { Project, ProjectOverview, ProjectStats } from "../lib/types";

interface Step { key: string; title: string; icon: IconName; done: boolean; need: "required" | "recommended" | "optional"; text: string; href: string; problem?: boolean }

function stepsOf(p: Project, stats: ProjectStats): Step[] {
  const main = p.profiles.find((x) => x.file === "profile.yaml") || p.profiles[0];
  const k = p.key;
  const tools = main?.tools?.length ?? 0;
  const conn = !!main && main.ok && !!(main.connection.target || main.connection.launch);
  return [
    { key: "agent", title: "Connect the agent", icon: "plug", need: "required", href: projectHref(k, "/setup/agent"), done: conn, problem: !!main && !main.ok,
      text: conn ? `${p.channels.map(modeLabel).join(" and ")} connection set` : main && !main.ok ? `${main.problems.length} thing(s) to fix` : "Add the agent's address" },
    { key: "tools", title: "Tools", icon: "tool", need: "recommended", href: projectHref(k, "/setup/tools"), done: tools > 0,
      text: tools ? `${tools} tool${tools === 1 ? "" : "s"} watched` : "See and break the agent's API calls" },
    { key: "backend", title: "Backend & database", icon: "db", need: "recommended", href: projectHref(k, "/setup/backend"), done: !!main?.backend && main.backend !== "none",
      text: main?.backend && main.backend !== "none" ? (main.backend === "sql" ? "Database connected" : "API connected") : "Check what really got saved" },
    { key: "logs", title: "Logs", icon: "logs", need: "optional", href: projectHref(k, "/setup/logs"), done: !!main?.logs && main.logs !== "none",
      text: main?.logs && main.logs !== "none" ? "Agent logs collected" : "Keep the agent's logs with each call" },
    { key: "plan", title: "Test plan", icon: "plan", need: "required", href: projectHref(k, "/setup/plan"), done: stats.scenarios > 0,
      text: stats.scenarios ? `${stats.scenarios} scenario${stats.scenarios === 1 ? "" : "s"} · ${stats.personas} persona${stats.personas === 1 ? "" : "s"}` : "Add scenarios and callers" },
    { key: "run", title: "First run", icon: "play", need: "required", href: projectHref(k, "/new"), done: stats.runs > 0,
      text: stats.runs ? `${stats.runs} run${stats.runs === 1 ? "" : "s"} so far` : "Start a test run" },
  ];
}

function SetupSteps({ steps }: { steps: Step[] }) {
  const done = steps.filter((s) => s.done).length;
  const required = steps.filter((s) => s.need === "required");
  const ready = required.every((s) => s.done);
  return (
    <Card>
      <CardHead level={2} title={ready ? "Setup" : "Get this agent ready"} sub={`${done} of ${steps.length} done`}>
        <div className="w-[140px] h-1.5 rounded-full bg-surface-3 overflow-hidden" aria-hidden="true">
          <div className="h-full rounded-full bg-accent transition-[width] duration-700" style={{ width: `${(done / steps.length) * 100}%` }} />
        </div>
      </CardHead>
      <div className="grid gap-2.5 grid-cols-1 sm:grid-cols-2 xl:grid-cols-3" id="setup-steps">
        {steps.map((s, i) => (
          <a key={s.key} href={s.href} data-step={s.key}
            className={cx("group flex items-start gap-3 p-3 rounded-xl border transition-colors hover:no-underline text-ink",
              s.done ? "border-line bg-surface hover:bg-surface-2" : s.problem ? "border-[color-mix(in_srgb,var(--warn)_40%,transparent)] bg-warn-soft/50 hover:bg-warn-soft"
                : "border-dashed border-line-strong hover:border-accent hover:bg-accent-soft/40")}>
            <span className={cx("size-8 rounded-full grid place-items-center flex-none text-[13px] font-bold",
              s.done ? "bg-good text-white" : s.problem ? "bg-warn text-white" : "bg-surface-3 text-ink-2")}>
              {s.done ? <Icon name="check" size={15} strokeWidth={3} /> : s.problem ? <Icon name="alert" size={15} /> : i + 1}
            </span>
            <span className="flex-1 min-w-0">
              <span className="flex items-center gap-2 flex-wrap">
                <b className="text-[13.5px]">{s.title}</b>
                {!s.done && s.need !== "required" && <span className="text-[11px] text-muted font-semibold uppercase tracking-[.05em]">{s.need}</span>}
              </span>
              <span className="block text-[12.5px] text-ink-2 mt-0.5">{s.text}</span>
            </span>
            <Icon name="chevron" size={16} className="text-muted mt-2 transition-transform group-hover:translate-x-0.5" />
          </a>
        ))}
      </div>
    </Card>
  );
}

export function ProjectOverviewPage({ project }: { project: Project }) {
  useCrumbs([{ label: "Projects", href: "#/projects" }, { label: project.name }]);
  const { activeJob, projects } = useApp();
  const { data, error } = useLoad(() => api.get<ProjectOverview>(`/api/project/${project.key}`), [project.key, activeJob?.status]);
  if (error) return <Notice tone="bad" className="mt-5">{error.message}</Notice>;
  if (!data) return <Skeleton height={520} />;
  const s = data.stats;
  const steps = stepsOf(data.project, s);
  const last = s.last_decided;
  const running = activeJob && projectOfJob(activeJob, projects)?.key === project.key;
  const lat = s.latency, lim = s.latency_limits;
  const latTone = lat && lim?.p95_ms && lat.p95_ms != null ? (lat.p95_ms <= lim.p95_ms ? "good" : "bad") : "accent";

  return (
    <>
      <div className="flex items-start gap-4 mt-2 mb-6 flex-wrap">
        <ProjectAvatar name={project.name} k={project.key} size={56} />
        <div className="flex-1 min-w-[240px]">
          <h1 className="text-2xl font-bold tracking-[-.02em]">{project.name}</h1>
          <div className="flex flex-wrap items-center gap-1.5 mt-1.5">
            <Chip>{domainLabel(project.domain)}</Chip>
            {project.channels.map((c) => <Chip key={c} tone={c === "voice" ? "agent" : "caller"} icon={c === "voice" ? "mic" : "text"}>{modeLabel(c)}</Chip>)}
            {project.example && <Chip tone="accent" icon="flag">practice agent</Chip>}
            {project.ready ? <Chip tone="good" icon="check">ready to run</Chip> : <Chip tone="warn" icon="alert">needs setup</Chip>}
          </div>
          {project.description && <p className="text-ink-2 mt-2 max-w-[760px]">{project.description}</p>}
        </div>
        <div className="flex gap-2 flex-wrap">
          <Button icon="settings" href={projectHref(project.key, "/setup/agent")}>Set up</Button>
          {running
            ? <Button variant="primary" icon="activity" href={projectHref(project.key, "/live")}>Watch live run</Button>
            : <Button id="run-tests" variant="primary" icon="play" href={projectHref(project.key, "/new")}>Run tests</Button>}
        </div>
      </div>

      <div className="flex flex-col gap-4">
        {last ? (
          <Hero tone={heroToneOf(last.decision)} eyebrow={<>Latest release decision · <When iso={last.started_at} /></>}
            title={<>{decisionTitle(last.decision)}{last.decision === "NO_GO_INSUFFICIENT_EVIDENCE" && <span className="text-lg font-semibold"> · not enough evidence</span>}</>}
            actions={<Button href={runHref(last)} icon="file">Open report</Button>}>
            <div className="flex flex-wrap items-center gap-2 mt-1.5 text-ink-2">
              {modeLabel(last.mode)} run{last.offline ? " (free dry run)" : ""} · {last.calls_started} calls ·
              <span className="text-good font-semibold">{last.counts.PASS || 0} passed</span>
              {(last.counts.FAIL || 0) > 0 && <span className="text-bad font-semibold">{last.counts.FAIL} failed</span>}
            </div>
          </Hero>
        ) : null}

        <SetupSteps steps={steps} />

        {s.runs > 0 && <div className="grid gap-4 grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 stagger">
          <Kpi icon="layers" label="Test runs" value={<CountUp to={s.runs} />} foot={`${s.decided} with a decision${s.bug_runs ? ` · ${s.bug_runs} with bugs on` : ""}`} />
          <Kpi icon="shield" label="GO rate" tone={s.go_rate == null ? "accent" : s.go_rate >= 0.8 ? "good" : s.go_rate >= 0.5 ? "warn" : "bad"}
            value={s.go_rate == null ? "—" : pct(s.go_rate)} foot={<MiniHistory items={s.history} empty="after your first run" />} />
          <Kpi icon="phone" label="Test calls" value={<CountUp to={s.calls} />} foot={`${money(s.cost_usd)} spent`} />
          <Kpi icon="clock" label="Voice response time" tone={latTone}
            value={lat ? fmtMs(lat.p95_ms) : "—"}
            foot={lim ? (lat ? <>p95 · p50 {fmtMs(lat.p50_ms)} · limit {fmtMs(lim.p95_ms)}</> : <>no voice run yet{lim.p95_ms ? <> · p95 limit {fmtMs(lim.p95_ms)}</> : null}</>) : "text-only agent"} />
        </div>}

        <div className={cx("grid gap-4 items-start grid-cols-1", s.runs > 0 && "xl:grid-cols-[minmax(0,1fr)_380px]")}>
          <Card flush>
            <div className="flex items-center px-[18px] pt-4 pb-3"><h2 className="font-semibold text-base flex-1">Recent runs</h2>
              <Button href={projectHref(project.key, "/runs")} variant="ghost" size="sm">Run history <Icon name="arrow" /></Button></div>
            {data.recent_runs.length ? (
              <Table head={["Decision", "Channel", "Calls", "Pass / fail", "p95", "When"]}>
                {data.recent_runs.map((r) => (
                  <tr key={r.run_id} className={rowLink} onClick={() => navigate(runHref(r))}>
                    <td><Badge status={r.decision || r.status} /></td>
                    <td>{modeLabel(r.mode)}{r.offline ? <span className="text-muted"> · dry run</span> : null}{r.agent_options?.bugs ? <span className="text-bad text-xs font-semibold"> · bugs on</span> : null}</td>
                    <td className="tabular-nums">{r.calls_started}{r.planned ? `/${r.planned}` : ""}</td>
                    <td className="tabular-nums whitespace-nowrap"><span className="text-good">{r.counts.PASS || 0}</span> / <span className="text-bad">{r.counts.FAIL || 0}</span></td>
                    <td className="tabular-nums">{r.mode === "voice" ? fmtMs(r.p95_ms) : <span className="text-muted">—</span>}</td>
                    <td className="text-muted"><When iso={r.started_at} /></td>
                  </tr>
                ))}
              </Table>
            ) : (
              <EmptyState icon="play" title="No runs yet" text="Run the test plan against the agent to get its first release decision."
                action={project.ready ? <Button variant="primary" icon="play" href={projectHref(project.key, "/new")}>Run tests</Button> : undefined} />
            )}
          </Card>
          {s.runs > 0 && (
            <Card>
              <CardHead level={2} title="Activity" sub="last 14 days" />
              <ActivityChart days={data.activity} />
            </Card>
          )}
        </div>
      </div>
    </>
  );
}
