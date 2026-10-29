// One call: the conversation, the recording, how it was judged and every check with its evidence.
import { useEffect, useRef, useState, type ReactNode } from "react";
import { LatencyBars, Waveform } from "../components/charts";
import { Icon } from "../components/Icon";
import { ChecksList, OutcomeNotice } from "../components/runbits";
import { RowsTable, Timeline, type TimelineHandle } from "../components/Timeline";
import { Badge, Button, Card, CardHead, Chip, Legend, Notice, Seg, Skeleton, Table } from "../components/ui";
import { api } from "../lib/api";
import { useCrumbs, useLoad } from "../lib/app";
import { projectHref } from "../lib/links";
import { fmtDate, fmtDuration, fmtMs, money, shortScenario } from "../lib/format";
import type { CallDetail, Project, RunDetail } from "../lib/types";

function Kpi({ label, value, foot }: { label: string; value: ReactNode; foot?: ReactNode }) {
  return (
    <Card className="grid gap-1">
      <div className="text-ink-2 font-semibold text-[12.5px]">{label}</div>
      <div className="text-[26px] font-extrabold tracking-[-.02em] tabular-nums leading-[1.15]">{value}</div>
      {foot && <div className="text-muted text-xs">{foot}</div>}
    </Card>
  );
}

export function CallPage({ project, runId, testId }: { project: Project; runId: string; testId: string }) {
  const runAt = (path = "") => projectHref(project.key, `/runs/${runId}${path}`);
  useCrumbs([{ label: "Projects", href: "#/projects" }, { label: project.name, href: projectHref(project.key) },
    { label: "Run history", href: projectHref(project.key, "/runs") }, { label: "Report", href: runAt() }, { label: "Call" }]);
  const { data, error } = useLoad(
    () => Promise.all([api.get<CallDetail>(`/api/runs/${runId}/calls/${testId}`), api.get<RunDetail>(`/api/runs/${runId}`)]),
    [runId, testId],
  );
  const [view, setView] = useState<"chat" | "text">("chat");
  const [audio, setAudio] = useState<HTMLAudioElement | null>(null);
  const tl = useRef<TimelineHandle>(null);
  const pendingFlash = useRef<number | null>(null);

  // Stop the recording when leaving the call.
  useEffect(() => () => audio?.pause(), [audio]);
  // An evidence click from the plain-text view switches to the conversation first, then flashes.
  useEffect(() => {
    if (view === "chat" && pendingFlash.current != null) {
      tl.current?.flash(pendingFlash.current);
      pendingFlash.current = null;
    }
  }, [view]);

  if (error) return <Notice tone="bad" className="mt-5">{error.message}</Notice>;
  if (!data) return <Skeleton />;
  const [c, run] = data;
  const v = c.verdict, r = c.record, m = v?.metrics || {};
  const voice = r.mode === "voice";
  const limits = voice && run.decision?.latency ? { p50: run.decision.latency.p50_limit_ms, p95: run.decision.latency.p95_limit_ms } : null;
  const finals = run.calls.filter((x) => x.final !== false);
  const idx = finals.findIndex((x) => x.test_id === testId);
  const prev = idx > 0 ? finals[idx - 1] : undefined;
  const next = idx >= 0 ? finals[idx + 1] : undefined;
  const final = run.calls.find((x) => x.test_id === testId)?.final;
  const running = run.summary.status === "running";

  const onEvidence = (seq: number) => {
    if (view === "chat") tl.current?.flash(seq);
    else { pendingFlash.current = seq; setView("chat"); }
  };
  const onSeek = (ms: number) => {
    if (!audio) return;
    audio.currentTime = ms / 1000;
    void audio.play();
  };

  return (
    <>
      <div className="flex items-end gap-4 mt-2 mb-5 flex-wrap">
        <div className="flex-1 min-w-0">
          <div className="flex flex-wrap items-center gap-2.5">
            <h1 className="text-2xl font-bold">{shortScenario(r.scenario_id)}</h1>
            {v ? <Badge status={v.status} /> : <Badge status={running ? "running" : "interrupted"} />}
            {final === false && <Chip tone="warn" icon="refresh">attempt {r.attempt}: Vocto error, rerun · not counted</Chip>}
          </div>
          <div className="mt-1 text-ink-2 flex flex-wrap items-center gap-2">
            <span className="font-mono text-[12.5px]">{r.scenario_id}</span><span className="text-muted">·</span>repeat {r.repeat}
            {r.persona_id && <><span className="text-muted">·</span><Chip tone="caller" icon="user">{r.persona_id}</Chip></>}
            <Chip tone={voice ? "agent" : "caller"} icon={voice ? "mic" : "text"}>{r.mode}</Chip>
            {c.pii_masked ? <Chip icon="shield">personal data masked</Chip> : <Chip tone="bad" icon="alert">personal data NOT masked</Chip>}
          </div>
        </div>
        <div className="flex gap-2">
          {prev && <Button size="sm" iconOnly icon="back" href={runAt(`/calls/${prev.test_id}`)} title="Previous call" aria-label="Previous call" />}
          {next && <Button size="sm" iconOnly icon="arrow" href={runAt(`/calls/${next.test_id}`)} title="Next call" aria-label="Next call" />}
          <Button size="sm" icon="layers" href={runAt()}>Run report</Button>
        </div>
      </div>

      <OutcomeNotice verdict={v} />

      <div className="grid gap-4 grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 stagger mb-4">
        <Kpi label="Caller turns" value={m.turns ?? "—"} />
        <Kpi label="Call length" value={fmtDuration(m.duration_ms)} foot={fmtDate(r.started_at)} />
        <Kpi label={voice ? "Response time p50" : "Agent processing p50"} value={fmtMs(m.latency_p50_ms)}
          foot={voice ? `p95 ${fmtMs(m.latency_p95_ms)}${limits ? ` · limit ${fmtMs(limits.p95)}` : ""}` : "text mode: not gated"} />
        <Kpi label="Framework cost" value={money(r.cost_usd)} foot={`caller + validator${c.llm_logs ? ` · ${c.llm_logs} LLM log(s)` : ""}`} />
      </div>

      <div className="grid gap-4 items-start grid-cols-1 xl:grid-cols-[minmax(0,1fr)_400px]">
        <div className="flex flex-col gap-4 min-w-0">
          {c.recording && (
            <Card>
              <CardHead title="Recording" sub="left = caller, right = agent as the caller heard it · seconds match the timeline" />
              <div id="wave"><Waveform runId={runId} testId={testId} audio={audio} events={c.events} /></div>
              <audio id="audio" ref={setAudio} controls preload="metadata" src={c.recording.url} className="w-full mt-2.5" />
              <div className="mt-2 flex flex-wrap items-center gap-3.5">
                <Legend items={[["var(--caller)", "Caller"], ["var(--agent)", "Agent"]]} />
                <span className="text-muted text-xs">Click the waveform or a ▶ time to jump there.</span>
              </div>
            </Card>
          )}
          <Card>
            <CardHead title="Conversation and actions">
              <Seg id="view-mode" value={view} onChange={setView} options={[{ value: "chat", label: "Conversation" }, { value: "text", label: "Plain text" }]} />
            </CardHead>
            <div id="conv" className={view === "chat" ? "" : "hidden"}>
              <Timeline ref={tl} events={c.events} limits={limits} onSeek={c.recording ? onSeek : undefined} />
            </div>
            {view === "text" && (
              <div id="plain">
                <Table compact head={["#", "Who", "What happened"]}>
                  {c.story.map((row) => (
                    <tr key={row.seq}>
                      <td className="font-mono text-[11.5px] text-muted">#{row.seq}</td>
                      <td className="whitespace-nowrap"><b>{row.who}</b></td>
                      <td>{row.text}</td>
                    </tr>
                  ))}
                </Table>
                <div className="text-muted text-[11.5px] mt-2">The same lines as story.txt and the terminal output.</div>
              </div>
            )}
          </Card>
        </div>

        <div className="flex flex-col gap-4 min-w-0">
          {c.judgement.length > 0 && (
            <Card>
              <CardHead title="Said vs did vs stored" />
              <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3.5 gap-y-2 text-[13px]">
                {c.judgement.map((j) => (
                  <div key={j.label} className="contents">
                    <dt className="text-muted font-semibold">{j.label}</dt>
                    <dd className="m-0 min-w-0 break-words">{j.label === "Outcome" ? <b>{j.text}</b> : j.text}</dd>
                  </div>
                ))}
              </dl>
            </Card>
          )}
          <Card>
            <CardHead title="Checks" sub={v ? `${v.checks.filter((x) => x.result === "PASS").length} of ${v.checks.length} passed` : undefined} />
            <div id="checks">
              {v ? <ChecksList verdict={v} onEvidence={onEvidence} />
                : <div className="text-muted text-[12.5px]">{running ? "This call has no verdict yet." : "The run was stopped during this call, so it was never judged."}</div>}
            </div>
            <div className="text-muted text-[11.5px] mt-2">Click an event number to see it in the conversation.</div>
          </Card>
          {m.latency_turns && m.latency_turns.length > 0 && (
            <Card>
              <CardHead title="Response times" sub="end of the caller's words → first sound of the answer" />
              <LatencyBars rows={m.latency_turns} limits={limits} width={330} />
            </Card>
          )}
          {c.database.length > 0 && (
            <Card>
              <CardHead title="Database after the call" />
              <div className="flex flex-col gap-3">
                {c.database.map((d) => (
                  <div key={d.probe} className="min-w-0">
                    <div className="font-mono text-[11.5px] text-muted mb-1.5 break-all">{d.probe}</div>
                    {d.rows.length ? <RowsTable rows={d.rows} /> : <div className="text-muted text-[12.5px]">No rows: nothing was stored by this call.</div>}
                  </div>
                ))}
              </div>
            </Card>
          )}
          <Card>
            <CardHead title="Evidence downloads" sub={`${r.locked ? "sealed" : "still open"}${r.events_sha256 ? ` · sha256 ${r.events_sha256.slice(0, 10)}…` : ""}`} />
            <div className="flex flex-col gap-1.5">
              {c.files.map((f) => (
                <a key={f} className="flex items-center gap-2 text-[12.5px]" href={`/runs/${runId}/calls/${testId}/${f}`} target="_blank" rel="noreferrer">
                  <Icon name="file" /><span className="font-mono">{f}</span>
                </a>
              ))}
            </div>
          </Card>
        </div>
      </div>
    </>
  );
}
