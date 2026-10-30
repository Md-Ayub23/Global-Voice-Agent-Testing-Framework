// Live: a run as it happens. The server streams the job's output and every call's files as they change.
import { useEffect, useMemo, useRef, useState } from "react";
import { LatencyBars } from "../components/charts";
import { Tip, useToast } from "../components/feedback";
import { Icon } from "../components/Icon";
import { ChecksList, Hero, heroToneOf } from "../components/runbits";
import { Timeline, whoIsNext, type TimelineHandle } from "../components/Timeline";
import { Badge, Button, Card, CardHead, Chip, EmptyState, Notice, Progress, Skeleton, Spinner, Table } from "../components/ui";
import { api } from "../lib/api";
import { useApp, useCrumbs, useLoad } from "../lib/app";
import { cx, fmtDuration, fmtMs, shortScenario } from "../lib/format";
import { decisionTitle } from "../lib/status";
import { LiveListener } from "../lib/listen";
import { modeLabel, pl, projectHref, projectOfJob, runHref } from "../lib/links";
import type { CallDetail, Job, Limits, Project, RunDetail } from "../lib/types";

export function Live({ project, jobId }: { project: Project; jobId: string | null }) {
  const { activeJob, projects } = useApp();
  const { data, error } = useLoad<Job | Job[]>(() => (jobId ? api.get<Job>(`/api/jobs/${jobId}`) : api.get<Job[]>("/api/jobs")), [jobId]);
  const base = [{ label: "Projects", href: "#/projects" }, { label: project.name, href: projectHref(project.key) }];
  useCrumbs(jobId ? [...base, { label: "Live run", href: projectHref(project.key, "/live") }, { label: data && !Array.isArray(data) && data.kind === "exit-check" ? "Exit check" : "Test run" }]
    : [...base, { label: "Live run" }]);

  if (!jobId) {
    if (error) return <Notice tone="bad" className="mt-5">{error.message}</Notice>;
    if (!data) return <Skeleton />;
    const jobs = (data as Job[]).filter((j) => j.request?.project === project.path);
    const pick = jobs.find((j) => j.status === "running") || jobs[0];
    if (pick) { location.replace(projectHref(project.key, `/live/${pick.id}`)); return null; }
    const other = projectOfJob(activeJob, projects);
    return (
      <>
        <div className="mt-2 mb-5"><h1 className="text-2xl font-bold">Live run</h1><div className="mt-1 text-ink-2">Watch a run of {project.name} as it happens: every turn, tool call and check.</div></div>
        {activeJob && other && other.key !== project.key && (
          <Notice className="mb-4" icon="activity">A run of <b>{other.name}</b> is in progress. <a href={projectHref(other.key, `/live/${activeJob.id}`)}>Watch it</a></Notice>
        )}
        <Card><EmptyState title="Nothing is running" text="Start a run and it appears here live." icon="wave"
          action={<Button href={projectHref(project.key, "/new")} variant="primary" icon="play">New run</Button>} /></Card>
      </>
    );
  }
  if (error) {
    return <Card><EmptyState title="This run isn't live any more" text="Vocto was restarted since. Finished runs are all in Run history." icon="help"
      action={<Button href={projectHref(project.key, "/runs")}>Run history</Button>} /></Card>;
  }
  if (!data) return <Skeleton />;
  if (Array.isArray(data)) return <Skeleton />;   // the list from before the address changed
  return <LiveJob initial={data} project={project} />;
}

interface RunState extends RunDetail { limits?: Limits | null }

function LiveJob({ initial, project }: { initial: Job; project: Project }) {
  const { setActiveJob, poll } = useApp();
  const toast = useToast();
  const exit = initial.kind === "exit-check";
  const [job, setJob] = useState<Job>(initial);
  const [lines, setLines] = useState<string[]>([]);
  const [runs, setRuns] = useState<Map<string, RunState>>(new Map());
  const [calls, setCalls] = useState<Map<string, CallDetail>>(new Map());
  const [selected, setSelected] = useState<string | null>(null);
  const [follow, setFollow] = useState(true);
  const [viewRun, setViewRun] = useState<string | null>(null);
  const [ended, setEnded] = useState(false);
  const [now, setNow] = useState(Date.now());
  const [stopping, setStopping] = useState(false);
  const limitsFor = useRef(new Map<string, Limits | null>());

  // ---- the stream ----
  useEffect(() => {
    const es = new EventSource(`/api/jobs/${initial.id}/stream`);
    let closed = false;
    es.addEventListener("log", (e) => {
      const d = JSON.parse((e as MessageEvent).data) as { from: number; lines: string[] };
      setLines((xs) => [...xs.slice(0, d.from), ...d.lines]);
    });
    es.addEventListener("job", (e) => {
      const j = JSON.parse((e as MessageEvent).data) as Job;
      setJob(j);
      setActiveJob(j.status === "running" ? j : null);
    });
    es.addEventListener("run", (e) => {
      const d = JSON.parse((e as MessageEvent).data) as RunDetail;
      const id = d.summary.run_id;
      setRuns((m) => new Map(m).set(id, { ...d, limits: limitsFor.current.get(id) ?? m.get(id)?.limits ?? null }));
      if (d.info && !limitsFor.current.has(id)) {
        limitsFor.current.set(id, null);
        const info = d.info;
        api.get<Project[]>("/api/projects").then((ps) => {
          const prof = ps.find((p) => p.path === info.project)?.profiles.find((f) => f.file === info.profile_file);
          if (prof?.mode !== "voice") return;
          const lim = { p50: prof.thresholds?.latency_p50_ms ?? 800, p95: prof.thresholds?.latency_p95_ms ?? 1500 };
          limitsFor.current.set(id, lim);
          setRuns((m) => { const r = m.get(id); return r ? new Map(m).set(id, { ...r, limits: lim }) : m; });
        }).catch(() => {});
      }
    });
    es.addEventListener("call", (e) => {
      const d = JSON.parse((e as MessageEvent).data) as CallDetail;
      setCalls((m) => new Map(m).set(d.record.test_id, d));
    });
    es.addEventListener("end", () => { closed = true; es.close(); setEnded(true); poll(); });
    es.onerror = () => { if (closed) es.close(); };
    return () => { closed = true; es.close(); };
  }, [initial.id, setActiveJob, poll]);

  useEffect(() => {
    if (job.status !== "running") return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [job.status]);

  const callsOf = (runId: string | undefined) =>
    [...calls.values()].filter((c) => c.record.run_id === runId).sort((a, b) => a.record.test_id.localeCompare(b.record.test_id));
  const lastRun = job.run_ids[job.run_ids.length - 1];
  const currentRunId = viewRun || lastRun;

  // Follow the newest call of the newest run unless the user picked one. A call can arrive before
  // the job lists its run, so this re-runs on both.
  useEffect(() => {
    if (!follow) return;
    const list = callsOf(lastRun);
    const running = list.filter((c) => !c.verdict);
    const pick = running[running.length - 1] || list[list.length - 1];
    if (pick) setSelected(pick.record.test_id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [follow, calls, lastRun]);

  const runMode = [...runs.values()].find((r) => r.info)?.info?.mode || null;
  const finished = job.status !== "running";
  const sameCall = (c: CallDetail, p: { scenario_id: string; repeat: number }) => c.record.scenario_id === p.scenario_id && c.record.repeat === p.repeat;
  const planDone = (runId: string): [number, number] => {
    const run = runs.get(runId);
    const plan = run?.info?.plan || [];
    if (run?.decision) return [plan.length, plan.length];
    const list = callsOf(runId);
    const done = plan.filter((p) => {
      const a = list.filter((c) => sameCall(c, p));
      return a.some((c) => c.verdict && c.verdict.status !== "ERROR") || (a.length >= 3 && a.every((c) => c.verdict));
    }).length;
    return [done, plan.length];
  };
  let doneAll = 0, planAll = 0;
  for (const r of job.run_ids) { const [d, n] = planDone(r); doneAll += d; planAll += n; }
  if (exit && runMode) planAll = (runMode === "voice" ? 5 : 4) * (runs.get(job.run_ids[0])?.info?.plan?.length || 0);
  const ratio = finished && job.status === "finished" ? 1 : planAll ? Math.min(1, doneAll / planAll) : 0;

  const run = currentRunId ? runs.get(currentRunId) : undefined;
  const plan = run?.info?.plan || [];
  const list = callsOf(currentRunId);
  const active = list.find((c) => !c.verdict);
  const lastVatfLine = [...lines].reverse().find((l) => l.startsWith("vatf:"));
  const phase = !finished && !active && lastVatfLine ? lastVatfLine.replace(/^vatf:\s*/, "") : "";
  const progressText = finished
    ? ({ finished: "Finished", stopped: "Stopped", failed: "Vocto stopped with an error" } as Record<string, string>)[job.status] || job.status
    : active ? `Calling: ${shortScenario(active.record.scenario_id)} · repeat ${active.record.repeat}${active.record.attempt > 1 ? ` · retry ${active.record.attempt - 1}` : ""}`
      : run ? (list.length && list.length >= plan.length ? "Deciding…" : "Getting ready…") : "Starting…";

  const sel = selected ? calls.get(selected) : undefined;
  const elapsed = (job.finished_at ? new Date(job.finished_at).getTime() : now) - new Date(job.started_at).getTime();

  const stop = async () => {
    if (!confirm("Stop this run? Calls already finished stay saved; there will be no decision.")) return;
    setStopping(true);
    try { await api.post(`/api/jobs/${job.id}/stop`); toast("Stopping…"); }
    catch (e) { toast((e as Error).message, "bad"); setStopping(false); }
  };

  return (
    <>
      <div className="flex items-end gap-4 mt-2 mb-5 flex-wrap">
        <div className="flex-1 min-w-0">
          <div className="flex flex-wrap items-center gap-2.5"><h1 className="text-2xl font-bold">{exit ? "Exit check" : "Test run"}</h1><span id="status"><Badge status={job.status} /></span></div>
          <div className="mt-2 flex flex-wrap items-center gap-1.5 text-ink-2">
            <b className="text-ink">{project.name}</b>
            {runMode && <Chip tone={runMode === "voice" ? "agent" : "caller"} icon={runMode === "voice" ? "mic" : "text"}>{modeLabel(runMode)}</Chip>}
            {job.request?.offline && <Chip>free dry run</Chip>}
          </div>
        </div>
        <div className="flex items-center gap-2.5">
          <span className="text-muted tabular-nums" id="elapsed">{fmtDuration(elapsed)}</span>
          {job.status === "running" && runMode === "voice" && <ListenLive jobId={job.id} />}
          {job.status === "running" && <Button id="stop" variant="danger" icon="stop" disabled={stopping} onClick={stop}>Stop</Button>}
        </div>
      </div>

      <div id="result"><Result job={job} ended={ended} exit={exit} /></div>

      <Card className="mb-4">
        {exit && <ExitSteps job={job} runMode={runMode} onPick={(r) => {
          setViewRun(r); setFollow(false);
          setSelected(callsOf(r)[0]?.record.test_id || null);
        }} />}
        <div className="flex items-center gap-2.5 mb-2"><b id="prog-text">{progressText}</b><span className="flex-1" /><span className="text-muted text-[12.5px] tabular-nums" id="prog-num">{planAll ? `${doneAll} of ${planAll} calls` : ""}</span></div>
        <Progress value={ratio} running={!finished} />
        <div className="text-muted text-[12.5px] mt-2 min-h-[18px]">{phase && <span className="flex items-center gap-2"><Spinner />{phase}</span>}</div>
      </Card>

      <div className="grid gap-4 items-start grid-cols-1 lg:grid-cols-[260px_minmax(0,1fr)] 2xl:grid-cols-[280px_minmax(0,1fr)_340px]">
        <Card className="p-3">
          <CardHead title="Calls" sub={<span id="calls-sub">{plan.length ? `${list.filter((c) => c.verdict).length}/${plan.length}` : ""}</span>} className="mx-1 mt-1 mb-2.5" />
          <div className="flex flex-col gap-1" id="calls">
            {plan.length ? plan.map((p) => {
              const attempts = list.filter((c) => sameCall(c, p));
              const c = attempts[attempts.length - 1];
              return <CallRow key={`${p.scenario_id}-${p.repeat}`} p={p} c={c} attempts={attempts.length} selected={!!c && c.record.test_id === selected} live={job.status === "running"}
                onClick={() => { if (c) { setSelected(c.record.test_id); setFollow(false); } }} />;
            }) : <div className="text-muted text-[12.5px] p-2">Planning the calls…</div>}
          </div>
        </Card>
        <Conversation call={sel} limits={run?.limits ?? null} follow={follow} ended={ended} running={job.status === "running"}
          onFollow={() => { setFollow(true); setViewRun(null); }} />
        <div className="grid gap-4 items-start lg:col-span-2 lg:grid-cols-2 2xl:col-span-1 2xl:grid-cols-1" id="side">{sel && <SidePanel call={sel} limits={run?.limits ?? null} live={job.status === "running"} />}</div>
      </div>

      <Console lines={lines} open={finished && job.status !== "finished" && job.status !== "stopped"} />
    </>
  );
}

const statusText = (s: string) => ({ PASS: "passed", FAIL: "failed", ERROR: "Vocto error, rerun", NOT_VERIFIED: "not verified", NEEDS_REVIEW: "needs you" } as Record<string, string>)[s] || s;
// A call with no verdict is still going while the job runs; once the job has ended it never will be.
const callState = (c: CallDetail, live: boolean) => (c.verdict ? c.verdict.status : !live ? "interrupted" : c.record.locked ? "evaluating" : "running");

// Off until clicked. The run sends its audio whether or not anyone listens, so listening changes nothing
// about the call or its response times; the page unmounting or the run ending stops it.
function ListenLive({ jobId }: { jobId: string }) {
  const toast = useToast();
  const [on, setOn] = useState(false);
  const [hearing, setHearing] = useState("");
  const ref = useRef<LiveListener | null>(null);
  useEffect(() => () => ref.current?.stop(), []);
  const toggle = () => {
    if (ref.current) { ref.current.stop(); return; }
    const l = new LiveListener((label) => setHearing(label.split(" ")[1] || ""), (error) => {
      ref.current = null; setOn(false); setHearing("");
      if (error) toast(error, "bad");
    });
    ref.current = l;
    setOn(true);
    void l.start(jobId);
  };
  return (
    <>
      {on && <span id="hearing" className="text-[12.5px] text-muted inline-flex items-center gap-1.5">
        <span className="size-2 rounded-full bg-bad animate-pulse" aria-hidden="true" />
        {hearing ? <>Hearing <b className="text-ink">{shortScenario(hearing)}</b></> : "Waiting for the call audio…"}
      </span>}
      <Tip content={on ? "Stop playing the call" : "Play this voice call as it happens: the caller and the agent, as the caller hears them. It doesn't affect the call or its timing."}>
        <Button id="listen" icon={on ? "mute" : "speaker"} variant={on ? "primary" : undefined} onClick={toggle} aria-pressed={on}>
          {on ? "Stop listening" : "Listen live"}
        </Button>
      </Tip>
    </>
  );
}

function CallRow({ p, c, attempts, selected, live, onClick }: { p: { scenario_id: string; repeat: number }; c: CallDetail | undefined; attempts: number; selected: boolean; live: boolean; onClick: () => void }) {
  const s = c ? callState(c, live) : "queued";
  const ring = {
    PASS: "bg-good-soft text-good", FAIL: "bg-bad-soft text-bad", ERROR: "bg-warn-soft text-warn", NOT_VERIFIED: "bg-warn-soft text-warn",
    NEEDS_REVIEW: "bg-warn-soft text-warn", interrupted: "bg-surface-2 text-muted", running: "text-accent", evaluating: "text-accent", queued: "border-[1.5px] border-dashed border-line-strong",
  }[s] || "";
  const sub = s === "queued" ? (live ? "waiting" : "not run") : s === "interrupted" ? "interrupted" : s === "running" ? "on the call…" : s === "evaluating" ? "judging…" : statusText(s);
  return (
    <div data-test={c?.record.test_id || ""} onClick={onClick}
      className={cx("flex gap-2.5 items-center px-2.5 py-[9px] rounded-[10px] border transition-colors duration-150",
        c ? "cursor-pointer" : "cursor-default",
        selected ? "bg-accent-soft border-[color-mix(in_srgb,var(--accent)_30%,transparent)]" : "border-transparent hover:bg-surface-2")}>
      <span className={cx("size-[22px] rounded-full grid place-items-center flex-none", ring)}>
        {s === "running" || s === "evaluating" ? <Spinner size={13} /> : s === "queued" ? null : <Icon name={s === "PASS" ? "check" : s === "FAIL" ? "x" : s === "interrupted" ? "stop" : "alert"} size={s === "interrupted" ? 10 : 13} strokeWidth={2.6} />}
      </span>
      <div className="flex-1 min-w-0">
        <div className="font-semibold text-[13px] truncate">{shortScenario(p.scenario_id)}</div>
        <div className="text-muted text-[11.5px]">repeat {p.repeat} · {sub}{attempts > 1 ? ` · ${attempts} attempts` : ""}</div>
      </div>
    </div>
  );
}

function Conversation({ call, limits, follow, ended, running, onFollow }: {
  call: CallDetail | undefined; limits: Limits | null; follow: boolean; ended: boolean; running: boolean; onFollow: () => void;
}) {
  const box = useRef<HTMLDivElement>(null);
  const tl = useRef<TimelineHandle>(null);
  const testId = call?.record.test_id;
  const count = call?.events.length || 0;
  const sealed = !!call?.record.locked;
  const prev = useRef<string | undefined>(undefined);
  // Keep the newest line in view: jump on a new call, glide while following or already at the bottom.
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const fresh = prev.current !== testId;
    prev.current = testId;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
    if (fresh) el.scrollTop = el.scrollHeight;
    else if (follow || atBottom) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [testId, count, sealed, follow]);

  const s = call ? callState(call, running) : null;
  return (
    <Card>
      <CardHead title={<span id="conv-title">{call ? `${shortScenario(call.record.scenario_id)} · repeat ${call.record.repeat}${call.record.attempt > 1 ? ` · attempt ${call.record.attempt}` : ""}` : "Conversation"}</span>}>
        {s && <span id="conv-badge">{s === "running" ? <Badge status="running" label="On the call" /> : s === "evaluating" ? <Badge status="running" label="Judging" /> : <Badge status={s} />}</span>}
        {!follow && !ended && <Button size="sm" icon="wave" id="follow" onClick={onFollow}>Follow live</Button>}
        {(call?.record.locked || s === "interrupted") && call && <Button size="sm" variant="ghost" icon="ext" id="open-call" href={runHref({ run_id: call.record.run_id }, `/calls/${call.record.test_id}`)}>Open</Button>}
      </CardHead>
      <div ref={box} id="conv" className="max-h-[68vh] overflow-auto pr-1 scrollbar-thin">
        {call ? (
          <Timeline key={testId} ref={tl} events={call.events} limits={limits} typing={call.verdict || !running ? null : whoIsNext(call.events)} />
        ) : (
          <div className="text-muted text-[12.5px] p-[30px] text-center">
            {running ? <span className="flex items-center justify-center gap-2"><Spinner />Waiting for the first call…</span> : "No calls."}
          </div>
        )}
      </div>
    </Card>
  );
}

function SidePanel({ call, limits, live }: { call: CallDetail; limits: Limits | null; live: boolean }) {
  const ev = call.events;
  const tools = ev.filter((e) => e.type === "tool_request").length;
  const faults = ev.filter((e) => e.type === "fault_injected").length;
  const turns = ev.filter((e) => e.type === "caller_turn").length;
  const last = ev[ev.length - 1];
  const m = call.verdict?.metrics;
  const stat = (label: string, value: React.ReactNode) => (
    <div className="grid gap-1"><div className="text-ink-2 font-semibold text-[12.5px]">{label}</div><div className="text-[22px] font-extrabold tabular-nums leading-[1.15]">{value}</div></div>
  );
  return (
    <>
      <Card className="lg:col-start-1 2xl:col-start-auto">
        <CardHead title="This call" />
        <div className="grid grid-cols-2 gap-2.5">
          {stat("Caller turns", turns)}
          {stat("Tool calls", <>{tools}{faults > 0 && <small className="text-sm font-semibold text-muted ml-1">{faults} broken</small>}</>)}
          {stat("Call time", fmtDuration(m?.duration_ms ?? last?.t_ms ?? 0))}
          <div className="grid gap-1"><div className="text-ink-2 font-semibold text-[12.5px]">Persona</div><div className="text-[15px] font-bold pt-1.5">{call.record.persona_id || "scripted"}</div></div>
        </div>
        {call.live && call.pii_masked && (
          <div className="text-muted text-[11.5px] mt-2.5 flex items-center gap-1.5"><Icon name="shield" size={13} /> Personal data is masked here, the same way it is saved.</div>
        )}
      </Card>
      {m?.latency_turns && (
        <Card className="lg:col-start-1 2xl:col-start-auto">
          <CardHead title="Response times" sub={`p50 ${fmtMs(m.latency_p50_ms)} · p95 ${fmtMs(m.latency_p95_ms)}`} />
          <div className="max-w-[460px]"><LatencyBars rows={m.latency_turns} limits={limits} width={310} /></div>
        </Card>
      )}
      <Card className="lg:col-start-2 lg:row-start-1 lg:row-span-2 2xl:col-start-auto 2xl:row-start-auto 2xl:row-span-1">
        <CardHead title="Checks">{call.verdict && <Badge status={call.verdict.status} />}</CardHead>
        {call.verdict ? <ChecksList verdict={call.verdict} /> : !live ? (
          <div className="text-muted text-[12.5px]">The run was stopped during this call, so it was never judged.</div>
        ) : (
          <div className="text-muted text-[12.5px] flex items-center gap-2"><Spinner />{call.record.locked ? "Judging the call…" : "Checks run when the call ends."}</div>
        )}
      </Card>
    </>
  );
}

const EXIT_LABELS = (mode: string | null) => ["clean", "lie_on_failure", "skip_confirmation", "double_booking", ...(mode === "voice" ? ["misread_reference"] : [])];

function ExitSteps({ job, runMode, onPick }: { job: Job; runMode: string | null; onPick: (runId: string) => void }) {
  const rows = job.summary?.rows || [];
  const runFor = Object.fromEntries(Object.entries(job.labels).map(([r, l]) => [l, r]));
  return (
    <div className="flex gap-2 flex-wrap mb-3.5" id="steps">
      {EXIT_LABELS(runMode).map((l, i) => {
        const row = rows.find((r) => r.label === l);
        const run = runFor[l];
        const cur = !row && run && job.status === "running";
        return (
          <Tip key={l} content={row?.detail}>
            <button type="button" data-run={run || ""} disabled={!run} onClick={() => run && onPick(run)}
              className={cx("inline-flex items-center gap-2 pl-2 pr-3 py-1.5 rounded-full border bg-surface font-semibold text-[13px] transition-colors",
                cur ? "border-accent" : "border-line", run ? "cursor-pointer hover:bg-surface-2" : "cursor-default")}>
              <span className={cx("size-5 rounded-full grid place-items-center text-[11px]",
                row ? (row.ok ? "bg-good text-white" : "bg-bad text-white") : cur ? "bg-accent text-white" : "bg-surface-3 text-ink-2")}>
                {row ? <Icon name={row.ok ? "check" : "x"} size={12} strokeWidth={3} /> : i + 1}
              </span>
              {l === "clean" ? "Clean agent" : l}
            </button>
          </Tip>
        );
      })}
    </div>
  );
}

function Result({ job, ended, exit }: { job: Job; ended: boolean; exit: boolean }) {
  const runId = job.run_ids[0];
  const done = job.status !== "running";
  const { data: detail } = useLoad(() => (done && !exit && runId ? api.get<RunDetail>(`/api/runs/${runId}`) : Promise.resolve(null)), [done, exit, runId, ended]);
  if (!done) return null;
  if (exit) {
    const sum = job.summary;
    const passed = !!(sum?.finished && sum.passed);
    const t = job.status !== "finished" ? "warn" : passed ? "good" : "bad";
    return (
      <Hero tone={t} className="mb-4"
        footer={sum?.rows?.length ? (
          <Table head={["", "Agent", "Result", "Run"]}>
            {sum.rows.map((r) => (
              <tr key={r.label}>
                <td><Badge status={r.ok ? "PASS" : "FAIL"} label={r.ok ? "OK" : "MISS"} /></td>
                <td><b>{r.label === "clean" ? "Clean agent" : r.label}</b>{r.scenario_id && <div className="text-muted text-[11.5px] font-mono">caught by {r.scenario_id}</div>}</td>
                <td>{r.detail}</td>
                <td><a href={runHref({ run_id: r.run_id })}><Badge status={r.decision} /></a></td>
              </tr>
            ))}
          </Table>
        ) : undefined}
        title={job.status !== "finished" ? "Exit check did not finish" : passed ? "Exit check PASSED" : "Exit check NOT PASSED"}>
        <div className="text-ink-2">
          {passed ? "The clean agent got GO, and every bug switch was caught in every repeat." : job.status !== "finished" ? "It was stopped or Vocto failed; the activity log below says why." : "At least one row below missed. Open its run to see why."}
        </div>
      </Hero>
    );
  }
  if (!runId) return <Notice tone="bad" className="mb-4"><b>The run didn't start.</b> The activity log below says why.</Notice>;
  if (!detail) return null;
  const d = detail.decision;
  if (!d) {
    return job.status === "stopped"
      ? <Notice icon="stop" className="mb-4"><b>Run stopped.</b> Finished calls are saved; there is no decision.</Notice>
      : <Notice tone="bad" className="mb-4"><b>Vocto stopped with an error.</b> Finished calls are saved; there is no decision. The activity log below says why.</Notice>;
  }
  return (
    <Hero tone={heroToneOf(d.decision)} className="mb-4" eyebrow="Release decision"
      title={<>{decisionTitle(d.decision)}{d.decision === "NO_GO_INSUFFICIENT_EVIDENCE" && <span className="text-lg"> · not enough evidence</span>}</>}
      actions={<><Button href={pl(`/new?from=${runId}`)} icon="refresh">Run again</Button><Button href={runHref({ run_id: runId })} variant="primary" icon="file">Open report</Button></>}
      reasons={d.reasons.map((r) => r.message)} />
  );
}

function Console({ lines, open }: { lines: string[]; open: boolean }) {
  const box = useRef<HTMLDivElement>(null);
  const [isOpen, setOpen] = useState(open);
  useEffect(() => { if (open) setOpen(true); }, [open]);
  useEffect(() => {
    const el = box.current;
    if (el && el.scrollHeight - el.scrollTop - el.clientHeight < 60) el.scrollTop = el.scrollHeight;
  }, [lines, isOpen]);
  const cls = (l: string) => /FAIL|NO_GO|NOT PASSED|MISS|error/i.test(l) ? "text-[#ff8a80]" : /PASS|GO\b|OK/.test(l) ? "text-[#5fd08a]"
    : /warning|harness/i.test(l) ? "text-[#f2b84b]" : /^vatf/.test(l) ? "text-[#7c869a]" : "";
  const rendered = useMemo(() => lines.map((l, i) => <div key={i} className={cls(l)}>{l || " "}</div>), [lines]);
  return (
    <details className="bg-surface border border-line rounded-[14px] shadow-soft p-[18px] mt-4" id="console-box" open={isOpen}
      onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}>
      <summary className="flex items-center cursor-pointer list-none">
        <Icon name="arrow" size={14} className={cx("mr-2 transition-transform", isOpen && "rotate-90")} />
        <h3 className="flex-1 font-semibold text-sm">Activity log</h3><span className="text-muted text-[12.5px]">technical details</span>
      </summary>
      <div ref={box} id="console" className="mt-3 font-mono text-xs leading-[1.55] bg-[#0b0f17] text-[#c9d1e3] rounded-xl px-3.5 py-3 max-h-[260px] overflow-auto whitespace-pre-wrap break-words">
        {rendered}
      </div>
    </details>
  );
}
