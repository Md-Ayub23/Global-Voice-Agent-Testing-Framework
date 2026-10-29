// A run's report: the decision and why, the scorecard, reliability per scenario, response times, every call.
import { useEffect, useState, type ReactNode } from "react";
import { LatencyDots, Ring } from "../components/charts";
import { Tip } from "../components/feedback";
import { Icon } from "../components/Icon";
import { EvRef, Hero, OptionChips, heroToneOf } from "../components/runbits";
import { Badge, Button, Card, CardHead, Chip, EmptyState, Legend, Notice, Skeleton, Table, rowLink } from "../components/ui";
import { api } from "../lib/api";
import { useCrumbs, useLoad } from "../lib/app";
import { modeLabel, projectHref, runHref } from "../lib/links";
import { fmtDate, fmtDuration, fmtMs, money, pct, shortScenario } from "../lib/format";
import { navigate } from "../lib/router";
import { decisionTitle } from "../lib/status";
import type { CallBrief, Project, RunDetail } from "../lib/types";

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
        <div className="text-ink-2 font-semibold text-[12.5px]">{label}</div>
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
            {c.headline && <div className="text-[12.5px] text-ink-2">{c.headline}</div>}
            {c.failed_checks?.length > 0 && (
              <div className="flex flex-wrap gap-1 mt-1">{c.failed_checks.map((x) => <Chip key={x} tone="bad">{x}</Chip>)}</div>
            )}
          </td>
          <td className="tabular-nums">{c.metrics?.turns ?? "—"}</td>
          <td className="tabular-nums whitespace-nowrap">{fmtDuration(c.metrics?.duration_ms)}</td>
          {voice && <td className="tabular-nums whitespace-nowrap">{fmtMs(c.metrics?.latency_p50_ms)}</td>}
          <td className="tabular-nums">{money(c.cost_usd)}</td>
          <td className="font-mono text-[11.5px] text-muted">{c.test_id}</td>
        </tr>
      ))}
    </Table>
  );
}

export function RunReport({ project, runId }: { project: Project; runId: string }) {
  useCrumbs([{ label: "Projects", href: "#/projects" }, { label: project.name, href: projectHref(project.key) },
    { label: "Run history", href: projectHref(project.key, "/runs") }, { label: "Report" }]);
  const { data: run, error } = useLoad(() => api.get<RunDetail>(`/api/runs/${runId}`), [runId]);
  const [dotsBox, dotsWidth] = useWidth<HTMLDivElement>();
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
  const timed = voice && finals.some((c) => c.metrics?.latency_turns?.length);

  const reasons = (d?.reasons || []).map((r, i) => (
    <span key={i}>
      {r.message}{" "}
      <span className="inline-flex flex-wrap gap-1 align-middle">
        {(r.refs || []).slice(0, 8).map((ref) => {
          const [test, check] = ref.split("/");
          return <EvRef key={ref} href={callLink(test)}>{test.slice(-9)}{check ? ` · ${check}` : ""}</EvRef>;
        })}
        {(r.refs || []).length > 8 && <span className="text-muted text-[12.5px]">+{r.refs!.length - 8} more</span>}
      </span>
    </span>
  ));

  return (
    <>
      <div className="flex items-end gap-4 mt-2 mb-5 flex-wrap">
        <div className="flex-1 min-w-0">
          <div className="flex flex-wrap items-center gap-2.5">
            <h1 className="text-2xl font-bold">{modeLabel(sm.mode)} run report</h1>
            <OptionChips run={sm} />
          </div>
          <div className="mt-1 text-ink-2 flex flex-wrap items-center gap-2">
            <span className="font-mono text-[12.5px]">{runId}</span><span className="text-muted">·</span>{fmtDate(sm.started_at)}
            {took != null && <><span className="text-muted">·</span>took {fmtDuration(took)}</>}
            <span className="text-muted">·</span>{finals.length} call(s)
            <span className="text-muted">·</span>framework cost {money(sm.cost_usd)}
            {sm.pii_masked === false && <Chip tone="bad" icon="alert">not masked</Chip>}
          </div>
        </div>
        <div className="flex flex-wrap gap-2.5">
          {info && <Button href={projectHref(project.key, `/new?from=${runId}`)} icon="refresh">Run again</Button>}
          {run.files["transcript.txt"] && <Button href={`/runs/${runId}/transcript.txt`} target="_blank" icon="file">Transcript</Button>}
          {run.files["report.html"] && <Button href={`/runs/${runId}/report.html`} target="_blank" icon="ext">HTML report</Button>}
        </div>
      </div>

      <div className="flex flex-col gap-4">
        {d ? (
          <Hero
            tone={heroToneOf(d.decision)}
            eyebrow="Release decision"
            title={<>{decisionTitle(d.decision)}{d.decision === "NO_GO_INSUFFICIENT_EVIDENCE" && <span className="text-lg font-semibold"> · not enough evidence</span>}</>}
            actions={bugs ? (
              <Notice tone="warn" icon="info" className="max-w-[360px]">Bug switches were on ({bugs}), so NO-GO here means Vocto caught them.</Notice>
            ) : undefined}
            reasons={reasons}
          />
        ) : running ? (
          <Notice icon="wave"><b>This run is still going.</b> <a href={projectHref(project.key, "/live")}>Watch it live</a>.</Notice>
        ) : (
          <Notice tone="warn"><b>This run has no decision.</b> It was {sm.status} before the end. The calls that finished are below.</Notice>
        )}

        {d && (
          <div className="grid gap-4 grid-cols-[repeat(auto-fit,minmax(200px,1fr))] stagger">
            {Object.entries(d.scorecard).map(([name, s]) => (
              <ScoreCard key={name} ring={<Ring value={s.score} />} label={name[0].toUpperCase() + name.slice(1)}>
                <div className="text-[12.5px] text-ink-2">{s.passed} passed · {s.failed} failed</div>
                <div className="text-[11.5px] text-muted">coverage {pct(s.coverage)} of {s.total} check(s)</div>
              </ScoreCard>
            ))}
            <ScoreCard label="Coverage" ring={<Ring value={d.coverage.ratio} toneName={d.coverage.ratio >= d.coverage.min_required ? "good" : "warn"} />}>
              <div className="text-[12.5px] text-ink-2">{d.coverage.verified_calls} of {d.coverage.planned_calls} calls verified</div>
              <div className="text-[11.5px] text-muted">needs {pct(d.coverage.min_required)}{d.coverage.error_calls ? ` · ${d.coverage.error_calls} Vocto error(s)` : ""}</div>
            </ScoreCard>
            {voice && (
              <Card className="grid gap-1">
                <div className="flex items-center gap-2 text-ink-2 font-semibold text-[12.5px]"><Icon name="clock" />Response time</div>
                <div className="text-[28px] font-extrabold tracking-[-.02em] tabular-nums leading-[1.15]">
                  {fmtMs(d.latency.p50_ms)}<small className="text-sm font-semibold text-muted ml-1">p50</small>
                </div>
                <div className="text-muted text-xs">p95 {fmtMs(d.latency.p95_ms)} · limits {fmtMs(d.latency.p50_limit_ms)} / {fmtMs(d.latency.p95_limit_ms)}</div>
                <div className="flex items-center gap-2 mt-1.5">
                  {d.latency.within_limits == null ? <Badge status="NOT_VERIFIED" label="not timed" />
                    : d.latency.within_limits ? <Badge status="PASS" label="within limits" /> : <Badge status="FAIL" label="too slow" />}
                  <span className="text-muted text-[11.5px]">{d.latency.turns_measured} answers timed</span>
                </div>
              </Card>
            )}
          </div>
        )}

        {d && d.reliability.length > 0 && (
          <Card>
            <CardHead level={2} title="Reliability" sub="pass^k: a critical scenario must pass every repeat" />
            <Table head={["Scenario", "Criticality", "Repeats", "Passed", "All passed"]}>
              {d.reliability.map((r) => {
                const calls = finals.filter((c) => c.scenario_id === r.scenario_id).sort((a, b) => a.repeat - b.repeat);
                return (
                  <tr key={r.scenario_id}>
                    <td><b>{shortScenario(r.scenario_id)}</b><div className="text-muted text-[11.5px] font-mono">{r.scenario_id}</div></td>
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

        {timed && (
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

        <Card flush>
          <CardHead level={2} title="Calls" sub="click a call to see the conversation, the checks and the evidence" className="px-[18px] pt-4 mb-2" />
          {finals.length ? <CallsTable calls={finals} runId={runId} voice={voice} running={running} />
            : <EmptyState title="No calls" text="No call finished in this run." icon="phone" />}
        </Card>

        {reruns.length > 0 && (
          <Card flush>
            <CardHead level={2} title="Reruns after Vocto errors" sub="Vocto's own failures: rerun automatically, never blamed on the agent" className="px-[18px] pt-4 mb-2" />
            <CallsTable calls={reruns} runId={runId} voice={voice} running={running} />
          </Card>
        )}

        {info && (
          <details className="bg-surface border border-line rounded-[14px] shadow-soft p-[18px] group">
            <summary className="cursor-pointer font-semibold flex items-center gap-2 list-none">
              <Icon name="arrow" className="transition-transform group-open:rotate-90" />How this run was set up
            </summary>
            <dl className="mt-3 grid gap-x-6 gap-y-2.5 grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 text-[13px]">
              {([
                ["Channel", modeLabel(info.mode)],
                ["Caller", info.offline ? "Rules (free dry run)" : `Claude${info.caller_model ? ` · ${info.caller_model}` : ""}`],
                ["Scenarios", info.scenario_ids.length ? info.scenario_ids.map(shortScenario).join(", ") : "All"],
                ["Repeats", info.repeats ? String(info.repeats) : "From test settings"],
                ["Agent options", Object.entries(info.agent_options || {}).map(([k, v]) => `${k} = ${v}`).join(" · ") || "Defaults"],
                ...(info.speech ? [["Caller's speech", info.speech.engine === "local" ? `Real speech · hears with ${info.speech.ears_model}` : "Test tones"]] : []),
                ["Personal data", info.pii_masked ? "Masked" : "Not masked"],
                ["Vocto version", info.framework_version],
              ] as [string, string][]).map(([k, v]) => (
                <div key={k}><dt className="text-muted text-[12px]">{k}</dt><dd className="font-medium break-words">{v}</dd></div>
              ))}
            </dl>
          </details>
        )}
      </div>
    </>
  );
}
