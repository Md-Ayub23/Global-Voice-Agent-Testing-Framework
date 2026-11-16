// One agent's own page: how it is connected, its latest result in each test type, what to fix first, how it has
// done over its last runs and what is left to set up. Nothing about other agents lives here (that is Home).
import { KpiStrip, ProjectAvatar, RunLine, When } from "../components/dash";
import { Icon } from "../components/Icon";
import { Badge, Button, Card, CardHead, EmptyState, LiveDot, Notice, Skeleton, Table, rowLink, PageHead, Section } from "../components/ui";
import { useCanEdit } from "../lib/auth";
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
      {!quiet && <i className={cx("size-1.75 rounded-full", TONE_DOT[st.tone])} />}
      {m.status === "unavailable" ? unavailableWords(m) : st.label}
    </span>
  );
}

export function ProjectOverviewPage({ project }: { project: Project }) {
  useCrumbs([{ label: project.name }]);
  const { activeJob, projects } = useApp();
  const canEdit = useCanEdit();
  const { data, error } = useLoad(() => api.get<ProjectOverview>(`/api/project/${project.key}`), [project.key, activeJob?.status], { keep: true });
  if (error) return <Notice tone="bad" className="mt-5">{error.message}</Notice>;
  if (!data) return <Skeleton />;
  const k = project.key;
  const s = data.stats;
  const conn = data.connection;
  const byId = new Map(data.mode_summary.map((m) => [m.id, m]));
  const types = TEST_TYPES.map((id) => byId.get(id)).filter((m): m is ModeBrief => !!m);
  const v = verdict(types);
  const running = activeJob && projectOfJob(activeJob, projects)?.key === k ? activeJob : null;
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

  const todoCard = todo.length > 0 && canEdit && (
    <section id="setup-todo">
      <div className="rounded-3xl glass overflow-hidden">
        <div className="flex items-center gap-2 px-4 pt-3.5 pb-2">
          <Icon name="plan" size={16} className="text-accent" />
          <h2 className="card-title flex-1">Left to set up</h2>
          <span className="text-sm text-muted">{plural(todo.length, "step")}</span>
        </div>
        <div className="divide-y divide-line border-t border-line">
          {todo.map((t) => (
            <a key={t.key} href={t.href} data-step={t.key} className="flex items-center gap-3 px-4 py-2.5 hover:bg-surface-2 hover:no-underline text-ink">
              <i className={cx("size-1.75 rounded-full flex-none", t.need === "Needed" ? "bg-warn" : "bg-line-strong")} />
              <span className="flex-1 min-w-0 text-sm">{t.text}</span>
              <span className={cx("text-sm whitespace-nowrap", t.need === "Needed" ? "text-warn font-semibold" : "text-muted")}>{t.need}</span>
              <Icon name="chevron" size={14} className="text-muted flex-none" />
            </a>
          ))}
        </div>
      </div>
    </section>
  );

  return (
    <>
      <PageHead
        title={<div className="flex items-center gap-3.5" id="agent-head"><ProjectAvatar kind={conn.kind} size={44} /><h1 className="page-title">{project.name}</h1></div>}
        meta={<>
          <span>{domainLabel(project.domain)} · {conn.connected ? <>{conn.label}{conn.text_too ? " + text" : ""}</> : <span className="text-warn">Not connected</span>}
            {project.example && !conn.label.startsWith("Practice") ? " · practice agent" : ""}</span>
          <span id="verdict" className={cx("font-semibold inline-flex items-center gap-2", v.tone === "neutral" ? "text-ink-2" : TONE_TEXT[v.tone])}>
            <i className={cx("size-2 rounded-full", TONE_DOT[v.tone])} />{v.text}
          </span>
        </>}
      >
        {canEdit && (!conn.connected
          ? <Button id="connect" variant="primary" icon="plug" href={projectHref(k, "/setup/connection")}>Connect agent</Button>
          : <Button id="release-check" variant="primary" icon="play" href={projectHref(k, "/test/full/run")}>Run release check</Button>)}
      </PageHead>

      <div className="flex flex-col gap-8">
        {running && (
          <a href={projectHref(k, `/live/${running.id}`)} id="live-banner"
            className="flex items-center gap-3 px-4 py-3 rounded-2xl glass text-ink hover:no-underline">
            <LiveDot /><b className="font-semibold flex-1">{running.kind === "exit-check" ? "Exit check running" : "Test run in progress"}</b>
            <span className="text-accent-ink font-semibold text-sm flex items-center gap-1">Watch live <Icon name="arrow" size={14} /></span>
          </a>
        )}

        {todo.some((t) => t.need === "Needed") && todoCard}

        {s.runs > 0 && (
          <KpiStrip id="agent-kpis" items={[
            { label: "Test types passed", value: ran.length ? `${ran.filter((m) => m.status === "passed" || m.status === "watch").length} of ${ran.length}` : "—",
              foot: types.length - ran.length ? `${types.length - ran.length} not run yet` : "all run", tone: ran.some((m) => m.status === "failed") ? "bad" : ran.length ? "good" : undefined },
            { label: "Runs", value: s.runs, foot: `${s.calls} test calls` },
            { label: "Slowest answer (p95)", value: s.latency ? fmtMs(s.latency.p95_ms) : "—",
              foot: s.latency ? (lim ? `limit ${fmtMs(lim)}` : `typical ${fmtMs(s.latency.p50_ms)}`) : "after a voice run",
              tone: s.latency && lim && s.latency.p95_ms != null ? (s.latency.p95_ms <= lim ? "good" : "bad") : undefined },
            { label: "Test cases", value: s.scenarios, foot: plural(s.personas, "caller") },
            { label: "Spend", value: money(s.cost_usd), foot: "Claude, this agent" },
          ]} />
        )}

        <Section id="readiness" title="Results by test type" sub="the latest run of each · open a row for its test cases, runs and reports">
          <Card flush>
            <Table head={["Test type", "What it checks", "Result", "Latest finding", "Runs", "Last run"]}>
              {types.map((m) => (
                <tr key={m.id} className={rowLink} data-type={m.id} onClick={() => navigate(projectHref(k, `/test/${m.id}`))}>
                  <td className="whitespace-nowrap"><b className="font-semibold">{MODE_TITLE[m.id as TestModeId]}</b></td>
                  <td className="text-ink-2 min-w-55">{m.question}</td>
                  <td>{m.run_id
                    ? <a href={projectHref(k, `/runs/${m.run_id}`)} onClick={(e) => e.stopPropagation()} className="hover:no-underline" title="Open this run's report"><StatusWords m={m} /></a>
                    : <span title={m.why || undefined}><StatusWords m={m} /></span>}</td>
                  <td className="text-ink-2 max-w-85"><span className="line-clamp-2">{m.top_finding || m.headline || (m.status === "unavailable" ? m.why : "") || <span className="text-muted">—</span>}</span></td>
                  <td className="tabular-nums text-ink-2">{m.runs || <span className="text-muted">0</span>}</td>
                  <td className="text-muted whitespace-nowrap">{m.at ? <When iso={m.at} /> : "—"}</td>
                </tr>
              ))}
            </Table>
          </Card>
        </Section>

        {data.trend.length > 1 && (
          <Section id="trend" title="This agent over time" sub={`last ${data.trend.length} runs · click a dot to open its report`}>
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
          </Section>
        )}

        <div className={cx("grid gap-6 items-start grid-cols-1", todo.length > 0 && !todo.some((t) => t.need === "Needed") && canEdit && "xl:grid-cols-[minmax(0,1fr)_400px]")}>
          <Section title="Recent runs" action={<Button href={projectHref(k, "/runs")} variant="ghost" size="sm">All runs <Icon name="arrow" /></Button>}>
            <Card flush>
              {data.recent_runs.length ? (
                <Table head={["Result", "Test type", "Calls", "Passed / failed", "When"]}>
                  {data.recent_runs.slice(0, 6).map((r) => (
                    <tr key={r.run_id} className={rowLink} onClick={() => navigate(runHref(r))}>
                      <td><Badge status={r.decision || r.status} label={runLabel(r)} /></td>
                      <td>{modeTitle(r.test_mode)}{r.offline ? <span className="text-muted"> · dry run</span> : null}{r.agent_options?.bugs ? <span className="text-bad text-sm font-medium"> · bugs on</span> : null}</td>
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
          </Section>
          {!todo.some((t) => t.need === "Needed") && todoCard}
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
