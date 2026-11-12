// One agent's own page: how it is connected, its latest result in each test type, what to fix first, how it has
// done over its last runs and what is left to set up. Nothing about other agents lives here (that is Home).
import { KpiStrip, ProjectAvatar, RunLine, When } from "../components/dash";
import { Icon } from "../components/Icon";
import { Badge, Button, Card, CardHead, EmptyState, LiveDot, Notice, Skeleton, Table, rowLink } from "../components/ui";
import { api } from "../lib/api";
import { useApp, useCrumbs, useLoad } from "../lib/app";
import { cx, fmtMs, money, pct, plural } from "../lib/format";
import { domainLabel, mainProfile, projectHref, projectOfJob, runHref } from "../lib/links";
import { MODE_TITLE, TEST_TYPES, TONE_DOT, TONE_TEXT, TYPE_STATUS, modeTitle, runLabel, unavailableWords } from "../lib/modes";
import { navigate } from "../lib/router";
import type { ModeBrief, Project, ProjectOverview, TestModeId } from "../lib/types";

// The one line under the agent's name that says where it stands.
function verdict(modes: ModeBrief[]): { tone: "good" | "bad" | "warn" | "neutral"; text: string } {
  const types = modes.filter((m) => TEST_TYPES.includes(m.id));
  const failed = types.filter((m) => m.status === "failed");
  const ran = types.filter((m) => !["not_run", "unavailable"].includes(m.status));
  if (!ran.length) return { tone: "neutral", text: "Not tested yet" };
  if (failed.length) return { tone: "bad", text: `${failed.length} test type${failed.length === 1 ? "" : "s"} failed: ${failed.map((m) => MODE_TITLE[m.id]).join(", ")}` };
  const gaps = ran.filter((m) => m.status === "evidence");
  if (gaps.length) return { tone: "warn", text: `No failures, but ${gaps.map((m) => MODE_TITLE[m.id]).join(", ")} need more evidence` };
  return { tone: "good", text: `Passed all ${ran.length} test type${ran.length === 1 ? "" : "s"} it ran` };
}

function StatusWords({ m }: { m: ModeBrief }) {
  const st = TYPE_STATUS[m.status];
  const quiet = m.status === "not_run" || m.status === "unavailable";
  return (
    <span className={cx("inline-flex items-center gap-1.5 whitespace-nowrap", quiet ? "text-muted" : TONE_TEXT[st.tone])}>
      {!quiet && <i className={cx("size-[7px] rounded-full", TONE_DOT[st.tone])} />}
      {m.status === "unavailable" ? unavailableWords(m) : st.label}
    </span>
  );
}

export function ProjectOverviewPage({ project }: { project: Project }) {
  useCrumbs([{ label: project.name }]);
  const { activeJob, projects } = useApp();
  const { data, error } = useLoad(() => api.get<ProjectOverview>(`/api/project/${project.key}`), [project.key, activeJob?.status]);
  if (error) return <Notice tone="bad" className="mt-5">{error.message}</Notice>;
  if (!data) return <Skeleton height={520} />;
  const k = project.key;
  const s = data.stats;
  const conn = data.connection;
  const byId = new Map(data.mode_summary.map((m) => [m.id, m]));
  const types = TEST_TYPES.map((id) => byId.get(id)).filter((m): m is ModeBrief => !!m);
  const v = verdict(types);
  const running = activeJob && projectOfJob(activeJob, projects)?.key === k ? activeJob : null;
  const findings = types.filter((m) => (m.status === "failed" || m.status === "watch" || m.status === "evidence") && m.top_finding && m.run_id);
  const main = mainProfile(data.project);
  const voiceTrend = data.trend.some((t) => t.p95_ms != null);
  const lim = s.latency_limits?.p95_ms ?? null;
  const ran = types.filter((m) => !["not_run", "unavailable"].includes(m.status));
  const j = data.judges;

  // What is left to set up, only the things not done (done items need no space).
  const todo: { key: string; text: string; href: string; need: string }[] = [];
  if (!conn.connected) todo.push({ key: "connect", text: main && !main.ok ? `Fix the connection: ${plural(main.problems.length, "thing")} to fix` : "Connect the agent so Vocto can call it", href: projectHref(k, "/setup/connection"), need: "Needed" });
  if (!s.scenarios) todo.push({ key: "cases", text: "Add test cases, or let Claude draft them", href: projectHref(k, "/test/functional"), need: "Needed" });
  if (main && !main.tools?.length) todo.push({ key: "tools", text: "Add the agent's tools, so tests can check and break its API calls", href: projectHref(k, "/setup/tools"), need: "Recommended" });
  if (main && (!main.backend || main.backend === "none")) todo.push({ key: "backend", text: "Connect its database or API, so tests check what really got saved", href: projectHref(k, "/setup/backend"), need: "Recommended" });
  if (!s.personas) todo.push({ key: "callers", text: "Add callers from the library", href: projectHref(k, "/setup/callers"), need: "Recommended" });
  if (s.runs && !main?.baseline) todo.push({ key: "baseline", text: "Mark a good run as the baseline, so Regression can compare with it", href: projectHref(k, "/runs"), need: "Recommended" });
  if (j && j.items > 0 && !j.labelled_calls) todo.push({ key: "judges", text: "Label a few calls, so the AI judges can be trusted to fail a run", href: projectHref(k, "/judges"), need: "Optional" });

  return (
    <>
      <div className="flex items-start gap-4 mt-2 mb-5 flex-wrap" id="agent-head">
        <ProjectAvatar kind={conn.kind} size={44} />
        <div className="flex-1 min-w-[240px]">
          <h1 className="text-[21px] font-semibold tracking-[-.015em] leading-tight">{project.name}</h1>
          <div className="text-ink-2 mt-1 text-[13px]">
            {domainLabel(project.domain)} · {conn.connected ? <>{conn.label}{conn.text_too ? " + text" : ""}</> : <span className="text-warn">Not connected</span>}
            {project.example && !conn.label.startsWith("Practice") ? " · practice agent" : ""}
          </div>
          <div id="verdict" className={cx("mt-2 text-[13.5px] font-medium inline-flex items-center gap-2", v.tone === "neutral" ? "text-ink-2" : TONE_TEXT[v.tone])}>
            <i className={cx("size-2 rounded-full", TONE_DOT[v.tone])} />{v.text}
          </div>
        </div>
        <div className="flex gap-2 flex-wrap">
          {!conn.connected
            ? <Button id="connect" variant="primary" icon="plug" href={projectHref(k, "/setup/connection")}>Connect agent</Button>
            : <Button id="release-check" variant="primary" icon="play" href={projectHref(k, "/test/full")}>Release check</Button>}
        </div>
      </div>

      <div className="flex flex-col gap-6">
        {running && (
          <a href={projectHref(k, `/live/${running.id}`)} id="live-banner"
            className="flex items-center gap-3 px-4 py-3 rounded-[12px] border border-line bg-surface text-ink hover:no-underline hover:bg-surface-2">
            <LiveDot /><b className="font-medium flex-1">{running.kind === "exit-check" ? "Exit check running" : "Test run in progress"}</b>
            <span className="text-accent-ink font-medium text-[13px] flex items-center gap-1">Watch live <Icon name="arrow" size={14} /></span>
          </a>
        )}

        {s.runs > 0 && (
          <KpiStrip id="agent-kpis" items={[
            { label: "Test types passed", value: ran.length ? `${ran.filter((m) => m.status === "passed" || m.status === "watch").length} of ${ran.length}` : "—",
              foot: `${types.length - ran.length} not run yet`, tone: ran.some((m) => m.status === "failed") ? "bad" : ran.length ? "good" : undefined },
            { label: "Runs", value: s.runs, foot: `${s.calls} test calls` },
            { label: "Slowest answer (p95)", value: s.latency ? fmtMs(s.latency.p95_ms) : "—",
              foot: s.latency ? (lim ? `limit ${fmtMs(lim)}` : `typical ${fmtMs(s.latency.p50_ms)}`) : "after a voice run",
              tone: s.latency && lim && s.latency.p95_ms != null ? (s.latency.p95_ms <= lim ? "good" : "bad") : undefined },
            { label: "Test cases", value: s.scenarios, foot: `${s.personas} caller${s.personas === 1 ? "" : "s"}` },
            { label: "Spend", value: money(s.cost_usd), foot: "Claude, this agent" },
          ]} />
        )}

        <section id="readiness">
          <div className="flex items-baseline gap-3 mb-2.5">
            <h2 className="font-semibold text-[15px] flex-1">Results by test type</h2>
            <span className="text-muted text-[13px] hidden sm:inline">latest run of each</span>
          </div>
          <Card flush>
            <Table head={["Test type", "Checks", "Result", "Latest finding", "Runs", "Last run"]}>
              {types.map((m) => (
                <tr key={m.id} className={rowLink} data-type={m.id} onClick={() => navigate(projectHref(k, `/test/${m.id}`))}>
                  <td className="whitespace-nowrap"><b className="font-medium">{MODE_TITLE[m.id as TestModeId]}</b></td>
                  <td className="text-ink-2 min-w-[220px]">{m.question}</td>
                  <td>{m.run_id
                    ? <a href={projectHref(k, `/runs/${m.run_id}`)} onClick={(e) => e.stopPropagation()} className="hover:no-underline"><StatusWords m={m} /></a>
                    : <span title={m.why || undefined}><StatusWords m={m} /></span>}</td>
                  <td className="text-ink-2 max-w-[340px]"><span className="line-clamp-2">{m.top_finding || m.headline || (m.status === "unavailable" ? m.why : "") || <span className="text-muted">—</span>}</span></td>
                  <td className="tabular-nums text-ink-2">{m.runs || <span className="text-muted">0</span>}</td>
                  <td className="text-muted whitespace-nowrap">{m.at ? <When iso={m.at} /> : "—"}</td>
                </tr>
              ))}
            </Table>
          </Card>
        </section>

        {findings.length > 0 && (
          <section id="findings">
            <CardHead level={2} title="Findings" sub="the top finding of each test type that did not pass cleanly" className="mb-2.5" />
            <div className=" rounded-[20px] glass divide-y divide-line overflow-hidden">
              {findings.map((m) => (
                <a key={m.id} href={projectHref(k, `/runs/${m.run_id}`)} className="flex items-center gap-3 px-4 py-2.5 hover:bg-surface-2 hover:no-underline text-ink">
                  <i className={cx("size-[7px] rounded-full flex-none", TONE_DOT[TYPE_STATUS[m.status].tone === "good" ? "warn" : TYPE_STATUS[m.status].tone])} />
                  <b className="font-medium whitespace-nowrap">{MODE_TITLE[m.id]}</b>
                  <span className="text-ink-2 flex-1 min-w-0 truncate">{m.top_finding}</span>
                  <span className="text-accent-ink text-[13px] font-medium whitespace-nowrap inline-flex items-center gap-1">Open report<Icon name="arrow" size={13} /></span>
                </a>
              ))}
            </div>
          </section>
        )}

        {data.trend.length > 1 && (
          <section id="trend">
            <CardHead level={2} title="This agent over time" sub={`last ${data.trend.length} runs · click a dot to open its report`} className="mb-2.5" />
            <div className={cx("grid gap-4 grid-cols-1", voiceTrend && "lg:grid-cols-2")}>
              <Card>
                <CardHead title="Test calls passed" />
                <RunLine aria="Share of test calls passed in each run" max={1} fmt={pct} onOpen={(id) => navigate(projectHref(k, `/runs/${id}`))}
                  points={data.trend.map((t) => ({ run_id: t.run_id, at: t.at, decision: t.decision, value: t.pass_rate, label: modeTitle(t.test_mode) }))} />
              </Card>
              {voiceTrend && (
                <Card>
                  <CardHead title="Slowest answer (p95)" />
                  <RunLine aria="The slowest answer time (p95) in each voice run" fmt={(x) => fmtMs(x)} tick={(x) => `${+(x / 1000).toFixed(1)} s`} limit={lim} onOpen={(id) => navigate(projectHref(k, `/runs/${id}`))}
                    points={data.trend.map((t) => ({ run_id: t.run_id, at: t.at, decision: t.decision, value: t.p95_ms, label: modeTitle(t.test_mode) }))} />
                </Card>
              )}
            </div>
          </section>
        )}

        <div className={cx("grid gap-6 items-start grid-cols-1", todo.length > 0 && "xl:grid-cols-[minmax(0,1fr)_400px]")}>
          <section>
            <div className="flex items-center mb-2.5"><h2 className="font-semibold text-[15px] flex-1">Recent runs</h2>
              <Button href={projectHref(k, "/runs")} variant="ghost" size="sm">All runs <Icon name="arrow" /></Button></div>
            <Card flush>
              {data.recent_runs.length ? (
                <Table head={["Result", "Test type", "Calls", "Passed / failed", "When"]}>
                  {data.recent_runs.slice(0, 6).map((r) => (
                    <tr key={r.run_id} className={rowLink} onClick={() => navigate(runHref(r))}>
                      <td><Badge status={r.decision || r.status} label={runLabel(r)} /></td>
                      <td>{modeTitle(r.test_mode)}{r.offline ? <span className="text-muted"> · dry run</span> : null}{r.agent_options?.bugs ? <span className="text-bad text-[13px] font-medium"> · bugs on</span> : null}</td>
                      <td className="tabular-nums">{r.calls_started}{r.planned ? `/${r.planned}` : ""}</td>
                      <td className="tabular-nums whitespace-nowrap"><span className="text-good">{r.counts.PASS || 0}</span> / <span className="text-bad">{r.counts.FAIL || 0}</span></td>
                      <td className="text-muted whitespace-nowrap"><When iso={r.started_at} /></td>
                    </tr>
                  ))}
                </Table>
              ) : (
                <EmptyState icon="play" title="No runs yet"
                  text={conn.connected ? "Open a test type in the sidebar to see its test cases and run them, or run the Release check." : "Connect the agent first, then run its tests."}
                  action={conn.connected ? <Button variant="primary" icon="play" href={projectHref(k, "/test/functional")}>Open Functional tests</Button>
                    : <Button variant="primary" icon="plug" href={projectHref(k, "/setup/connection")}>Connect agent</Button>} />
              )}
            </Card>
          </section>
          {todo.length > 0 && (
            <section id="setup-todo">
              <CardHead level={2} title="Left to set up" sub={`${todo.length}`} className="mb-2.5" />
              <div className=" rounded-[20px] glass divide-y divide-line overflow-hidden">
                {todo.map((t) => (
                  <a key={t.key} href={t.href} data-step={t.key} className="flex items-start gap-3 px-4 py-2.5 hover:bg-surface-2 hover:no-underline text-ink">
                    <span className="flex-1 min-w-0 text-[13px]">{t.text}</span>
                    <span className={cx("text-[13px] whitespace-nowrap", t.need === "Needed" ? "text-warn font-medium" : "text-muted")}>{t.need}</span>
                  </a>
                ))}
              </div>
            </section>
          )}
        </div>

        {j && j.to_review > 0 && (
          <Notice tone="warn" icon="eye">
            <b>The AI judges disagree on {plural(j.to_review, "call")}.</b> Your label settles each one and teaches Vocto which judge to trust.{" "}
            <a href={projectHref(k, "/labels")}>Label calls</a>
          </Notice>
        )}
      </div>
    </>
  );
}
