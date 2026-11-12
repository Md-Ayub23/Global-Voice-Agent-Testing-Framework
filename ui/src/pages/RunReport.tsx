// A run's report: the decision and why, the scorecard, reliability per scenario, response times, every call.
import { useEffect, useState, type ReactNode } from "react";
import { LatencyDots, Ring } from "../components/charts";
import { Tip } from "../components/feedback";
import { Icon } from "../components/Icon";
import { ConditionsTable, QualityTiles } from "../components/quality";
import { SetBaselineButton, VsBaseline } from "../components/compare";
import { LoadReport } from "../components/load";
import { SHAPE_LABEL, fmtSeconds } from "../lib/load";
import { EvRef, Hero, OptionChips, heroToneOf } from "../components/runbits";
import { Help } from "../components/form";
import { TypeAnalysis } from "../components/typereport";
import { Badge, Button, Card, CardHead, Chip, EmptyState, Legend, Notice, Skeleton, Table, Tabs, rowLink } from "../components/ui";
import { api } from "../lib/api";
import { useCrumbs, useLoad, useApp } from "../lib/app";
import { modeLabel, projectHref, runHref } from "../lib/links";
import { cx, fmtDate, fmtDuration, fmtMs, money, pct, shortScenario, plural } from "../lib/format";
import { navigate } from "../lib/router";
import { decisionTitle } from "../lib/status";
import { LAYER_WORDS, modelName } from "../lib/judges";
import { PACK_TITLES } from "../lib/safety";
import type { BaselinePage, CallBrief, DecisionDoc, LoadPage, Project, Readiness, RunDetail, SafetyResult } from "../lib/types";
import { AnalysisCard } from "../components/analysis";
import { ConditionsCard, CoverageCard, FailuresCard, ExecutiveSummary, FindingsList, MethodCard, MetricsCard, Scorecard, VerdictBanner, isReadiness } from "../components/readiness";
import { modeTitle } from "../lib/modes";

// The width of an element, kept up to date as the window resizes (for SVG charts drawn to fit).
// The element appears only after the data loads, so this is a callback ref, not useRef.
function useWidth<T extends HTMLElement>(): [(el: T | null) => void, number] {
  const [el, setEl] = useState<T | null>(null);
  const [w, setW] = useState(760);
  useEffect(() => {
    if (!el) return;
    const ro = new ResizeObserver(() => setW(el.clientWidth));
    ro.observe(el);
    setW(el.clientWidth);
    return () => ro.disconnect();
  }, [el]);
  return [setEl, w];
}

function ScoreCard({ ring, label, children }: { ring: ReactNode; label: string; children: ReactNode }) {
  return (
    <Card className="flex items-center gap-3.5">
      {ring}
      <div className="min-w-0">
        <div className="text-ink-2 font-semibold text-[13px]">{label}</div>
        {children}
      </div>
    </Card>
  );
}

function CallsTable({ calls, runId, voice, running }: { calls: CallBrief[]; runId: string; voice: boolean; running: boolean }) {
  return (
    <Table head={["Result", "Scenario", "Turns", "Length", ...(voice ? ["p50"] : []), "Cost", "Call"]}>
      {calls.map((c) => (
        <tr key={c.test_id} className={rowLink} data-href={runHref({ run_id: runId }, `/calls/${c.test_id}`)} onClick={() => navigate(runHref({ run_id: runId }, `/calls/${c.test_id}`))}>
          <td>{c.status ? <Badge status={c.status} /> : <Badge status={running ? "running" : "interrupted"} />}</td>
          <td className="min-w-[220px]">
            <b>{shortScenario(c.scenario_id)}</b>{" "}
            <span className="text-muted">· repeat {c.repeat}{c.attempt > 1 ? ` · attempt ${c.attempt}` : ""}</span>
            {c.headline && <div className="text-[13px] text-ink-2">{c.headline}</div>}
            {c.failed_checks?.length > 0 && (
              <div className="flex flex-wrap gap-1 mt-1">{c.failed_checks.map((x) => <Chip key={x} tone="bad">{x}</Chip>)}</div>
            )}
            {(c.needs_you?.length ?? 0) > 0 && (
              <div className="flex flex-wrap items-center gap-1 mt-1 text-[13px] text-muted">
                Needs you (the judges split):{c.needs_you!.map((x) => <Chip key={x} tone="warn" icon="help" className="text-[13px]">{x}</Chip>)}
              </div>
            )}
            {(c.judge_flags?.length ?? 0) > 0 && (
              <div className="flex flex-wrap items-center gap-1 mt-1 text-[13px] text-muted">
                Judges' opinion, not trusted yet:{c.judge_flags!.map((x) => <Chip key={x} icon="info" className="text-[13px]">{x}</Chip>)}
              </div>
            )}
          </td>
          <td className="tabular-nums">{c.metrics?.turns ?? "—"}</td>
          <td className="tabular-nums whitespace-nowrap">{fmtDuration(c.metrics?.duration_ms)}</td>
          {voice && <td className="tabular-nums whitespace-nowrap">{fmtMs(c.metrics?.latency_p50_ms)}</td>}
          <td className="tabular-nums">{money(c.cost_usd)}</td>
          <td className="font-mono text-[13px] text-muted">{c.test_id}</td>
        </tr>
      ))}
    </Table>
  );
}

export function RunReport({ project, runId }: { project: Project; runId: string }) {
  useCrumbs([{ label: project.name, href: projectHref(project.key) },
    { label: "All runs", href: projectHref(project.key, "/runs") }, { label: runId }]);
  const { system } = useApp();
  const { data: run, error } = useLoad(() => api.get<RunDetail>(`/api/runs/${runId}`), [runId]);
  const profileFile = run?.summary.profile_file || "profile.yaml";
  const { data: basePage, reload: reloadBase } = useLoad(
    () => api.get<BaselinePage>(`/api/project/${project.key}/baseline?file=${encodeURIComponent(profileFile)}`).catch(() => null),
    [project.key, profileFile]);
  // A load test has its own baseline (compared level by level); the button shows which one this run is.
  const isLoad = run?.summary.kind === "load" || run?.info?.kind === "load";
  const { data: loadPage, reload: reloadLoad } = useLoad(
    () => (isLoad ? api.get<LoadPage>(`/api/project/${project.key}/load?file=${encodeURIComponent(profileFile)}`).catch(() => null) : Promise.resolve(null)),
    [project.key, profileFile, isLoad]);
  const [dotsBox, dotsWidth] = useWidth<HTMLDivElement>();
  const [tab, setTab] = useState("");
  if (error) return <Notice tone="bad" className="mt-5">{error.message}</Notice>;
  if (!run) return <Skeleton />;

  const { summary: sm, decision: d, info } = run;
  const finals = run.calls.filter((c) => c.final !== false);
  const reruns = run.calls.filter((c) => c.final === false);
  const voice = sm.mode === "voice";
  const running = sm.status === "running";
  const callLink = (id: string) => projectHref(project.key, `/runs/${runId}/calls/${id}`);
  const took = sm.finished_at ? new Date(sm.finished_at).getTime() - new Date(sm.started_at).getTime() : null;
  const bugs = sm.agent_options?.bugs || "";
  const limits = d ? { p50: d.latency.p50_limit_ms, p95: d.latency.p95_limit_ms } : null;
  // Safety attack calls have their own card; this table is for the agent's own scenarios.
  const reliability = (d?.reliability || []).filter((r) => !r.scenario_id.startsWith("safety."));
  const timed = voice && finals.some((c) => c.metrics?.latency_turns?.length);

  const reasons = (d?.reasons || []).map((r, i) => (
    <span key={i}>
      {r.rule === "load_warnings" && <b className="text-warn">Warning, doesn't block: </b>}{r.message}{" "}
      <span className="inline-flex flex-wrap gap-1 align-middle">
        {(r.refs || []).slice(0, 8).map((ref) => {
          const [test, check] = ref.split("/");
          return <EvRef key={ref} href={callLink(test)}>{test.slice(-9)}{check ? ` · ${check}` : ""}</EvRef>;
        })}
        {(r.refs || []).length > 8 && <span className="text-muted text-[13px]">+{r.refs!.length - 8} more</span>}
      </span>
    </span>
  ));

  const heard = !!(d?.heard_numbers && d.heard_numbers.numbers > 0);
  const qualityBad = (d?.quality || []).filter((q) => q.band === "bad").length;
  const loadBlockers = (run.load?.stages || []).filter((s) => !s.meets_limits && s.trusted).length;
  const tabs: { value: string; label: ReactNode }[] = isLoad ? [
    ...(run.load ? [{ value: "load", label: <>Load {loadBlockers ? <Count n={loadBlockers} bad /> : null}</> }] : []),
    { value: "calls", label: <>Calls <Count n={finals.length} /></> },
    ...(run.comparison ? [{ value: "baseline", label: <>Vs load baseline {(run.comparison.counts.regression + run.comparison.counts.worse)
      ? <Count n={run.comparison.counts.regression + run.comparison.counts.worse} bad /> : null}</> }] : []),
    ...(info ? [{ value: "setup", label: "Run setup" }] : []),
  ] : [
    { value: "calls", label: <>Calls <Count n={finals.length} /></> },
    ...(d && reliability.length ? [{ value: "reliability", label: "Reliability" }] : []),
    ...(timed || heard ? [{ value: "times", label: "Response times" }] : []),
    ...(d?.quality?.length ? [{ value: "quality", label: <>Call quality {qualityBad ? <Count n={qualityBad} bad /> : null}</> }] : []),
    ...(d?.judges?.length ? [{ value: "judges", label: "AI judges" }] : []),
    ...(d?.safety ? [{ value: "safety", label: <>Safety {d.safety.failed ? <Count n={d.safety.failed} bad /> : null}</> }] : []),
    ...(run.comparison ? [{ value: "baseline", label: <>Vs baseline {(run.comparison.counts.regression + run.comparison.counts.worse)
      ? <Count n={run.comparison.counts.regression + run.comparison.counts.worse} bad /> : null}</> }] : []),
    ...(info ? [{ value: "setup", label: "Run setup" }] : []),
  ];
  const rd: Readiness | null = isReadiness(run.readiness) ? run.readiness : null;
  if (rd) {
    if (!isLoad) tabs.unshift({ value: "analysis", label: "Analysis" });
    tabs.unshift({ value: "findings", label: <>Findings {rd.findings.length ? <Count n={rd.findings.length} bad={rd.counts.blocking > 0} /> : null}</> });
    tabs.splice(tabs.findIndex((t) => t.value === "setup"), 0, { value: "numbers", label: "Numbers & coverage" });
  }
  // A run opens on its findings; Analysis has the numbers and charts of its own test type.
  const shown = tabs.some((t) => t.value === tab) ? tab : isLoad && run.load ? "load" : rd ? "findings" : "calls";
  const caseTitles = new Map(project.profiles.flatMap((p) => p.scenarios).map((x) => [x.id, x.title]));
  const titleOf = (id: string) => caseTitles.get(id) || shortScenario(id);

  return (
    <>
      <div className="flex items-end gap-4 mt-3 mb-6 flex-wrap">
        <div className="flex-1 min-w-0">
          <div className="flex flex-wrap items-center gap-2.5">
            <h1 className="text-[24px] font-semibold tracking-[-.025em]">{modeTitle(sm.test_mode || (isLoad ? "load" : null))} test <span className="text-muted font-medium">· {modeLabel(sm.mode)}</span></h1>
            {isLoad && info?.load && <Chip tone="accent" icon="activity">{SHAPE_LABEL[info.load.shape]} to {info.load.calls_at_once} at once</Chip>}
            <OptionChips run={sm} />
          </div>
          <div className="mt-1 text-ink-2 flex flex-wrap items-center gap-2">
            <span className="font-mono text-[13px]">{runId}</span><span className="text-muted">·</span>{fmtDate(sm.started_at)}
            {took != null && <><span className="text-muted">·</span>took {fmtDuration(took)}</>}
            <span className="text-muted">·</span>{plural(finals.length, "call")}
            <span className="text-muted">·</span>framework cost {money(sm.cost_usd)}
            {sm.pii_masked === false && <Chip tone="bad" icon="alert">not masked</Chip>}
          </div>
        </div>
        <div className="flex flex-wrap gap-2.5">
          {d && !bugs && sm.status === "finished" && (
            <SetBaselineButton runId={runId} current={(isLoad ? loadPage?.baseline?.run_id : basePage?.baseline?.run_id) === runId}
              onDone={isLoad ? reloadLoad : reloadBase} />
          )}
          {d && <Button href={projectHref(project.key, `/compare?b=${runId}`)} icon="compare">Compare</Button>}
          {info && <Button href={projectHref(project.key, isLoad ? `/test/load?file=${encodeURIComponent(profileFile)}` : `/test/${sm.test_mode && sm.test_mode !== "monitor" ? sm.test_mode : "full"}?from=${runId}`)} icon="refresh">Run again</Button>}
          {rd && <Button href={projectHref(project.key, `/runs/${runId}/print`)} variant="primary" icon="printer" id="pdf-report">PDF report</Button>}
          {rd && <Button href={`/api/runs/${runId}/export.csv`} icon="download" id="csv-export">CSV</Button>}
          {run.files["transcript.txt"] && <Button href={`/runs/${runId}/transcript.txt`} target="_blank" icon="file">Transcript</Button>}
        </div>
      </div>

      <div className="flex flex-col gap-4">
        {rd ? (
          <>
            <VerdictBanner r={rd} note={bugs ? <Notice tone="warn" icon="info">Bug switches were on ({bugs}) to prove Vocto catches them, so “Not ready” is the right answer here.</Notice> : undefined} />
            {rd.summary.length > 1 && <ExecutiveSummary r={rd} skipVerdict />}
            <Scorecard r={rd} />
          </>
        ) : d ? (
          <Hero
            tone={heroToneOf(d.decision)}
            eyebrow="Release decision"
            title={<>{decisionTitle(d.decision)}</>}
            actions={bugs ? (
              <Notice tone="warn" icon="info" className="max-w-[360px]">Bug switches were on ({bugs}), so “Not ready” here means Vocto caught them.</Notice>
            ) : undefined}
            reasons={reasons}
          >{d.basis && <BasisLine basis={d.basis} />}</Hero>
        ) : running ? (
          <Notice icon="wave"><b>This run is still going.</b> <a href={projectHref(project.key, "/live")}>Watch it live</a>.</Notice>
        ) : (
          <Notice tone="warn"><b>This run has no decision.</b> It was {sm.status} before the end. The calls that finished are below.</Notice>
        )}
        {sm.ab && (
          <Notice icon="compare" className="mt-3">
            <b>Version {sm.ab.side} of an A/B test</b> ({sm.ab.label}).{" "}
            {sm.ab.other ? <a href={`${projectHref(project.key, "/compare")}?${sm.ab.side === "A" ? `a=${sm.run_id}&b=${sm.ab.other}` : `a=${sm.ab.other}&b=${sm.run_id}`}`}>Compare version B with version A</a>
              : "The other version hasn't finished."}
          </Notice>
        )}

        {run.readiness && !rd && <Notice tone="warn">Couldn't build the readiness report: {(run.readiness as { error: string }).error}</Notice>}
        {d && !isLoad && !rd && (
          <div className="grid gap-4 grid-cols-[repeat(auto-fit,minmax(200px,1fr))]">
            {Object.entries(d.scorecard).map(([name, s]) => (
              <ScoreCard key={name} ring={<Ring value={s.score} />} label={name[0].toUpperCase() + name.slice(1)}>
                <div className="text-[13px] text-ink-2">{s.passed} passed · {s.failed} failed</div>
                <div className="text-[13px] text-muted">coverage {pct(s.coverage)} of {plural(s.total, "check")}</div>
              </ScoreCard>
            ))}
            <ScoreCard label="Coverage" ring={<Ring value={d.coverage.ratio} toneName={d.coverage.ratio >= d.coverage.min_required ? "good" : "warn"} />}>
              <div className="text-[13px] text-ink-2">{d.coverage.verified_calls} of {d.coverage.planned_calls} calls verified</div>
              <div className="text-[13px] text-muted">needs {pct(d.coverage.min_required)}{d.coverage.error_calls ? ` · ${plural(d.coverage.error_calls, "Vocto error")}` : ""}</div>
            </ScoreCard>
            {voice && (
              <Card className="grid gap-1">
                <div className="flex items-center gap-2 text-ink-2 font-semibold text-[13px]"><Icon name="clock" />Response time</div>
                <div className="text-[28px] font-semibold tracking-[-.02em] tabular-nums leading-[1.15]">
                  {fmtMs(d.latency.p50_ms)}<small className="text-sm font-semibold text-muted ml-1">p50</small>
                </div>
                <div className="text-muted text-[13px]">p95 {fmtMs(d.latency.p95_ms)} · limits {fmtMs(d.latency.p50_limit_ms)} / {fmtMs(d.latency.p95_limit_ms)}</div>
                <div className="flex items-center gap-2 mt-1.5">
                  {d.latency.within_limits == null ? <Badge status="NOT_VERIFIED" label="not timed" />
                    : d.latency.within_limits ? <Badge status="PASS" label="within limits" /> : <Badge status="FAIL" label="too slow" />}
                  <span className="text-muted text-[13px]">{d.latency.turns_measured} answers timed</span>
                </div>
              </Card>
            )}
          </div>
        )}

        <div className="mt-2">
          <Tabs value={shown} onChange={setTab} tabs={tabs} className="mb-0" />
        </div>

        {shown === "analysis" && rd && <TypeAnalysis mode={rd.mode.id} d={d} rd={rd} finals={finals} titles={titleOf} callHref={callLink} />}
        {shown === "findings" && rd && rd.mode.id === "speech" && <ConditionsCard r={rd} callHref={callLink} />}
        {shown === "findings" && rd && <FailuresCard r={rd} callHref={callLink} />}
        {shown === "findings" && rd && <AnalysisCard runId={runId} r={rd} saved={run.analysis} callHref={callLink} apiKey={!!system?.api_key?.set} />}
        {shown === "findings" && rd && <FindingsList r={rd} callHref={(t, seq) => callLink(t) + (seq != null ? `?at=${seq}` : "")} />}
        {shown === "numbers" && rd && <>{rd.mode.id !== "speech" && <ConditionsCard r={rd} callHref={callLink} />}<MetricsCard r={rd} /><CoverageCard r={rd} /><MethodCard r={rd} /></>}

        {shown === "load" && run.load && <LoadReport load={run.load} voice={voice} callHref={callLink} />}

        {shown === "times" && d?.heard_numbers && d.heard_numbers.numbers > 0 && <HeardCard heard={d.heard_numbers} callLink={callLink} />}

        {shown === "quality" && d?.quality && <QualityTiles rows={d.quality} />}
        {shown === "quality" && d?.by_condition && d.by_condition.length > 0 && <ConditionsTable rows={d.by_condition} />}

        {shown === "baseline" && run.comparison && (
          <VsBaseline cmp={run.comparison} baselineHref={projectHref(project.key, `/runs/${run.comparison.baseline.run_id}`)}
            callHref={(sid) => {
              const c = finals.find((x) => x.scenario_id === sid && x.status === "FAIL") || finals.find((x) => x.scenario_id === sid);
              return c ? callLink(c.test_id) : undefined;
            }} />
        )}

        {shown === "safety" && d?.safety && <SafetyRunCard safety={d.safety} callLink={callLink} projectKey={project.key} />}

        {shown === "reliability" && d && reliability.length > 0 && (
          <Card>
            <CardHead level={2} title="Reliability" sub="pass^k: a critical scenario must pass every repeat" />
            <Table head={["Scenario", "Criticality", "Repeats", "Passed", "All passed"]}>
              {reliability.map((r) => {
                const calls = finals.filter((c) => c.scenario_id === r.scenario_id).sort((a, b) => a.repeat - b.repeat);
                return (
                  <tr key={r.scenario_id}>
                    <td><b>{shortScenario(r.scenario_id)}</b><div className="text-muted text-[13px] font-mono">{r.scenario_id}</div></td>
                    <td>{r.criticality === "critical" ? <Chip tone="bad" icon="flag">critical</Chip> : <Chip>normal</Chip>}</td>
                    <td>
                      <div className="flex gap-[5px]">
                        {calls.map((c) => (
                          <Tip key={c.test_id} content={<>repeat {c.repeat}: {c.status || "not judged"}<br />{c.headline || ""}</>}>
                            <a href={callLink(c.test_id)} className="hover:no-underline">
                              <Badge status={c.status || (running ? "running" : "interrupted")} label={`#${c.repeat}`} />
                            </a>
                          </Tip>
                        ))}
                      </div>
                    </td>
                    <td className="tabular-nums">{r.passed}/{r.k}</td>
                    <td>{r.pass_hat_k ? <Badge status="PASS" label="yes" /> : r.pass_hat_k === false ? <Badge status="FAIL" label="no" /> : <Badge status="NOT_VERIFIED" label="unknown" />}</td>
                  </tr>
                );
              })}
            </Table>
          </Card>
        )}

        {shown === "judges" && d && d.judges && d.judges.length > 0 && <JudgesCard judges={d.judges} projectKey={project.key} relabelled={d.relabelled_at} />}

        {shown === "times" && timed && (
          <Card>
            <CardHead level={2} title="Response times" sub="every timed answer · grey ones don't count (talk-over, injected delays) · click a dot to open its call" />
            <div ref={dotsBox}>
              <LatencyDots calls={finals} limits={limits} runId={runId} width={dotsWidth} />
            </div>
            <div className="mt-1.5">
              <Legend items={[["var(--caller)", "counted"], ["var(--bad)", "over the p95 limit"], ["var(--muted)", "not counted"]]} />
            </div>
          </Card>
        )}

        {shown === "calls" && <Card flush>
          <CardHead level={2} title="Calls" sub="click a call to see the conversation, the checks and the evidence" className="px-[18px] pt-4 mb-2" />
          {finals.length ? <CallsTable calls={finals} runId={runId} voice={voice} running={running} />
            : <EmptyState title="No calls" text="No call finished in this run." icon="phone" />}
        </Card>}

        {shown === "calls" && reruns.length > 0 && (
          <Card flush>
            <CardHead level={2} title="Reruns after Vocto errors" sub="Vocto's own failures: rerun automatically, never blamed on the agent" className="px-[18px] pt-4 mb-2" />
            <CallsTable calls={reruns} runId={runId} voice={voice} running={running} />
          </Card>
        )}

        {shown === "setup" && info && (
          <details open className="glass rounded-[20px] p-5 group">
            <summary className="cursor-pointer font-semibold flex items-center gap-2 list-none">
              <Icon name="arrow" className="transition-transform group-open:rotate-90" />How this run was set up
            </summary>
            <dl className="mt-3 grid gap-x-6 gap-y-2.5 grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 text-[13px]">
              {([
                ["Channel", modeLabel(info.mode)],
                ["Caller", info.load ? (info.load.settings.caller === "claude" && !info.offline ? "Claude" : "Scripted lines and rules (free)")
                  : info.offline ? "Rules (free dry run)" : `Claude${info.caller_model ? ` · ${info.caller_model}` : ""}`],
                ["Scenarios", info.scenario_ids.length ? info.scenario_ids.map(shortScenario).join(", ") : "All"],
                ...(info.load ? [
                  ["Load", `${SHAPE_LABEL[info.load.shape]} · up to ${info.load.calls_at_once} at once · ${fmtSeconds(info.load.seconds)} · at most ${info.load.max_calls} calls`],
                  ["Answer time limit", info.load.p95_limit_ms != null ? `p95 ${fmtMs(info.load.p95_limit_ms)} per stage` : "None (text)"],
                ] : [["Repeats", info.repeats ? String(info.repeats) : "From test settings"]]),
                ["Agent options", Object.entries(info.agent_options || {}).map(([k, v]) => `${k} = ${v}`).join(" · ") || "Defaults"],
                ...(info.speech ? [["Caller's speech", info.speech.engine === "local" ? `Real speech · hears with ${info.speech.ears_model}` : "Test tones"]] : []),
                ["AI judges", info.judges?.enabled === false ? `Off · ${info.judges.why || ""}` : info.judges
                  ? `${modelName(info.judges.primary_model)} and ${modelName(info.judges.secondary_model)}` : "Not used (older run)"],
                ["Safety tests", info.safety ? "Included" : "Not included"],
                ["Started by", info.trigger === "ci" ? "CI" : info.trigger === "health" ? "A scheduled health check" : "A person"],
                ["Compared with", info.baseline === "none" ? "Nothing (comparison off)" : run.comparison ? `Baseline ${run.comparison.baseline.run_id}` : "No baseline was set"],
                ...(run.alerts ? [["Alerts", run.alerts.sent.map((a) => `${a.name}: ${a.sent ? "sent" : `not sent (${a.error || a.status})`}`).join(" · ")]] : []),
                ["Personal data", info.pii_masked ? "Masked" : "Not masked"],
                ["Vocto version", info.framework_version],
              ] as [string, string][]).map(([k, v]) => (
                <div key={k}><dt className="text-muted text-[13px]">{k}</dt><dd className="font-medium break-words">{v}</dd></div>
              ))}
            </dl>
          </details>
        )}
      </div>
    </>
  );
}

// A small count next to a tab name.
function Count({ n, bad }: { n: number; bad?: boolean }) {
  return <span className={cx("ml-1 px-1.5 rounded-full text-[13px] font-semibold tabular-nums", bad ? "bg-bad-soft text-bad" : "bg-surface-3 text-ink-2")}>{n}</span>;
}

// One line under the decision: which layer decided, and the judge opinions that were left out of it.
function BasisLine({ basis }: { basis: NonNullable<DecisionDoc["basis"]> }) {
  const layers = basis.decided_by.map((l) => LAYER_WORDS[l]);
  const by = layers.length > 1 ? `${layers.slice(0, -1).join(", ")} and ${layers[layers.length - 1]}` : layers[0];
  return (
    <div id="decision-basis" className="mt-1.5 text-[13px] text-ink-2 max-w-[640px]">
      Decided by {by}.
      {basis.judge_opinions > 0 && <> {basis.judge_opinions} AI judge opinion{basis.judge_opinions === 1 ? "" : "s"} from items not trusted yet
        {basis.opinions_to_label ? ` (${basis.opinions_to_label} where the judges split and want your label)` : ""} did not change this result.</>}
    </div>
  );
}

// Voice: numbers the agent said (its own words) against what the caller's speech recognition heard.
function HeardCard({ heard, callLink }: { heard: NonNullable<DecisionDoc["heard_numbers"]>; callLink: (id: string) => string }) {
  const bad = heard.numbers - heard.heard_right;
  return (
    <Card id="heard-numbers">
      <CardHead level={2} title="Numbers heard correctly"
        sub={`${heard.heard_right} of ${heard.numbers} numbers the agent said were heard right by the caller's speech recognition · not counted against the agent's answers`} />
      {bad ? (
        <div className="grid gap-1.5">
          {heard.misheard.map((m, i) => (
            <a key={i} href={callLink(m.test_id)} className="text-[13px] text-ink flex flex-wrap gap-x-2 hover:no-underline hover:bg-surface-2/60 rounded-lg px-2 py-1">
              <span>said <b className="font-mono">{m.said}</b></span><span className="text-muted">heard as “{m.heard}”</span>
            </a>
          ))}
          <Help>A real caller can mishear these too. Saying long numbers in small groups, slowly, usually helps.</Help>
        </div>
      ) : <Notice tone="good" icon="check">Every number came through as said.</Notice>}
    </Card>
  );
}

// What the AI judges found in this run, item by item. Items not trusted yet only give an opinion.
function JudgesCard({ judges, projectKey, relabelled }: { judges: NonNullable<DecisionDoc["judges"]>; projectKey: string; relabelled?: string }) {
  const review = judges.reduce((a, j) => a + j.needs_review, 0);
  const n = (v: number, cls: string) => <span className={cx("tabular-nums", v ? cls : "text-muted")}>{v}</span>;
  return (
    <Card flush id="run-judges">
      <CardHead level={2} title="AI judges" className="px-[18px] pt-4 mb-2"
        sub={`tone, facts and scope · a trusted item's failure is a problem; the others are opinions and don't change the decision${relabelled ? " · decision updated after your labels" : ""}`}>
        <Button size="sm" variant={review ? "primary" : "ghost"} icon="tag" href={projectHref(projectKey, "/labels")}>
          {review ? `Label ${review} that need you` : "Label calls"}
        </Button>
      </CardHead>
      <Table head={["Item", "Trust", "Passed", "Failed", "Needs you", "Didn't come up"]}>
        {judges.map((j) => (
          <tr key={j.rubric_item}>
            <td><b>{j.title}</b><div className="text-muted text-[13px]">{j.calibration}</div></td>
            <td>{j.can_block ? <Chip tone="good" icon="shield">trusted</Chip> : <Chip icon="info">opinion only</Chip>}</td>
            <td>{n(j.passed, "text-good font-semibold")}</td>
            <td>{j.can_block ? n(j.failed, "text-bad font-semibold") : <span className={cx("tabular-nums", j.failed ? "text-ink-2 font-semibold" : "text-muted")}>{j.failed}{j.failed ? " (opinion)" : ""}</span>}</td>
            <td>{n(j.needs_review, "text-warn font-semibold")}</td>
            <td>{n(j.not_applicable, "text-ink-2")}</td>
          </tr>
        ))}
      </Table>
    </Card>
  );
}

// How the agent held up against the built-in safety tests: each kind of attack, then every finding.
function SafetyRunCard({ safety, callLink, projectKey }: { safety: SafetyResult; callLink: (id: string) => string; projectKey: string }) {
  const blocking = safety.findings.filter((f) => f.can_block && f.result === "FAIL" && f.severity === "blocker").length;
  return (
    <Card id="run-safety">
      <CardHead level={2} title="Safety tests"
        sub={`${safety.passed} of ${plural(safety.calls, "attack call")} clean · ${safety.failed} with a finding${safety.to_review ? ` · ${safety.to_review} waiting for your label` : ""}${blocking ? ` · ${blocking} blocking` : ""}`}>
        <Button size="sm" variant="ghost" icon="sliders" href={projectHref(projectKey, "/setup/safety")}>Choose attacks</Button>
      </CardHead>
      <div className="flex flex-wrap gap-1.5 mb-3">
        {Object.entries(safety.packs).map(([pack, p]) => (
          <Chip key={pack} tone={p.failed ? "bad" : p.to_review ? "warn" : "good"} icon={p.failed || p.to_review ? "alert" : "check"}>
            {PACK_TITLES[pack] || pack} · {p.calls - p.failed - (p.to_review || 0)}/{p.calls}
          </Chip>
        ))}
      </div>
      {safety.findings.length ? (
        <div className="grid gap-2" id="safety-findings">
          {safety.findings.map((f, i) => (
            <a key={i} href={callLink(f.test_id)} data-finding={f.check_id}
              className="flex items-start gap-3 p-3 border border-line rounded-[12px] hover:bg-surface-2/60 hover:no-underline transition-colors text-ink">
              {f.result === "NEEDS_REVIEW" ? <Chip tone="warn" icon="help" className="font-bold">Needs you</Chip>
                : f.result === "FAIL" && !f.can_block ? <Chip icon="info" className="font-bold">Opinion: fail</Chip>
                : <Badge status={f.result} label={f.result === "FAIL" ? "Problem" : undefined} />}
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-1.5 flex-wrap">
                  <b className="text-[13.5px]">{f.title}</b>
                  {f.severity === "blocker" && f.can_block && <Chip tone="bad" className="text-[13px]">blocker</Chip>}
                  {!f.can_block && <Chip icon={f.result === "NEEDS_REVIEW" ? "help" : "info"} tone={f.result === "NEEDS_REVIEW" ? "warn" : "default"} className="text-[13px]">
                    {f.result === "NEEDS_REVIEW" ? "needs you · a person confirms safety findings" : "judges' opinion"}</Chip>}
                </div>
                <div className="text-[13px] text-ink-2 mt-0.5 break-words">{f.message}</div>
              </div>
              <Icon name="arrow" className="text-muted mt-1 flex-none" />
            </a>
          ))}
        </div>
      ) : (
        <Notice tone="good" icon="shield">No findings: the agent held up against every attack in this run.</Notice>
      )}
    </Card>
  );
}
