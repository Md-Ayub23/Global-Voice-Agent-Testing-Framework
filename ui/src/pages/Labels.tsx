// Label calls: a person says what a careful reviewer would say about each judge item of a call.
// The judges' own answers stay hidden until the labels are saved, so they can't sway the person.
// Labels calibrate the judges (an item may block only once the judges catch your failures and pass your passes)
// and decide the call at once: a call the judges disagreed on stops holding its run.
import { useEffect, useMemo, useRef, useState } from "react";
import { useToast } from "../components/feedback";
import { Icon } from "../components/Icon";
import { Timeline, type TimelineHandle } from "../components/Timeline";
import { Badge, Button, Card, CardHead, Chip, EmptyState, Notice, PageHead, Skeleton } from "../components/ui";
import { api } from "../lib/api";
import { useCrumbs, useLoad } from "../lib/app";
import { ago, cx, shortScenario } from "../lib/format";
import { JUDGE_NAME, judgesApi, modelName, VOTE_LABEL } from "../lib/judges";
import { projectHref } from "../lib/links";
import { navigate } from "../lib/router";
import type { CallDetail, CallLabels, Check, Project, QueueItem, Vote } from "../lib/types";

const CHOICES: { v: Vote; icon: "thumbs-up" | "thumbs-down" | "minus"; on: string }[] = [
  { v: "PASS", icon: "thumbs-up", on: "bg-good text-white border-good" },
  { v: "FAIL", icon: "thumbs-down", on: "bg-bad text-white border-bad" },
  { v: "NOT_APPLICABLE", icon: "minus", on: "bg-ink-2 text-surface border-ink-2" },
];
const REASON_TONE: Record<string, "warn" | "bad" | "accent" | "default"> = {
  "the judges disagree": "warn", "a judge found a problem": "bad", "from a bug test": "accent", "a normal call": "default",
};

export function Labels({ project, runId, testId }: { project: Project; runId?: string; testId?: string }) {
  useCrumbs([{ label: "Projects", href: "#/projects" }, { label: project.name, href: projectHref(project.key) },
    { label: "AI judges", href: projectHref(project.key, "/judges") }, { label: "Label calls" }]);
  const [tick, setTick] = useState(0);
  const { data: queue, error } = useLoad(() => judgesApi.queue(project.key), [project.key, tick]);
  const current = runId && testId ? { run_id: runId, test_id: testId } : null;

  // With no call in the address, open the first one waiting.
  useEffect(() => {
    if (!current && queue?.length) navigate(projectHref(project.key, `/labels/${queue[0].run_id}/${queue[0].test_id}`), true);
  }, [current, queue, project.key]);

  if (error) return <Notice tone="bad" className="mt-5">{error.message}</Notice>;
  if (!queue) return <Skeleton />;
  const nextAfter = (id: string) => {
    const i = queue.findIndex((q) => q.test_id === id);
    return queue.slice(i + 1).find((q) => q.test_id !== id) || queue.find((q) => q.test_id !== id) || null;
  };

  return (
    <>
      <PageHead title="Label calls" sub={<>Say what a careful reviewer would say. Your labels show Vocto which judges to trust: until an item catches the failures you find and passes the calls you pass, its findings are only an opinion.</>}>
        <Button icon="scale" href={projectHref(project.key, "/judges")}>Judges and calibration</Button>
      </PageHead>
      {!queue.length && !current ? (
        <Card><EmptyState icon="check-circle" title="Nothing waiting for a label"
          text="Every judged call has your labels. New runs with the AI judges on bring new calls here."
          action={<Button variant="primary" icon="play" href={projectHref(project.key, "/new")}>Run tests</Button>} /></Card>
      ) : (
        <div className="grid gap-4 items-start grid-cols-1 xl:grid-cols-[300px_minmax(0,1fr)]">
          <Queue project={project} queue={queue} current={current?.test_id} />
          {current ? (
            <LabelCall key={current.test_id} project={project} runId={current.run_id} testId={current.test_id}
              next={nextAfter(current.test_id)} onSaved={() => setTick((t) => t + 1)} />
          ) : <Skeleton />}
        </div>
      )}
    </>
  );
}

function Queue({ project, queue, current }: { project: Project; queue: QueueItem[]; current?: string }) {
  return (
    <Card flush className="xl:sticky xl:top-[76px] max-h-[calc(100vh-100px)] flex flex-col">
      <div className="px-4 pt-3.5 pb-2.5 border-b border-line">
        <div className="font-semibold">Waiting for you</div>
        <div className="text-muted text-[12.5px]">{queue.length} call{queue.length === 1 ? "" : "s"} · most useful first</div>
      </div>
      <div className="overflow-y-auto scrollbar-thin p-1.5 flex xl:flex-col gap-1 max-xl:overflow-x-auto" id="queue">
        {queue.map((q) => (
          <a key={q.test_id} href={projectHref(project.key, `/labels/${q.run_id}/${q.test_id}`)} data-queue={q.test_id}
            className={cx("block rounded-lg px-2.5 py-2 hover:no-underline text-ink transition-colors max-xl:min-w-[220px]",
              q.test_id === current ? "bg-accent-soft" : "hover:bg-surface-2")}>
            <div className="flex items-center gap-2">
              <b className="text-[13px] truncate flex-1">{shortScenario(q.scenario_id)}</b>
              <span className="text-[11px] text-muted tabular-nums">{q.labelled}/{q.items}</span>
            </div>
            <div className="flex items-center gap-1.5 mt-1 flex-wrap">
              <Chip tone={REASON_TONE[q.reason] || "default"}>{q.reason}</Chip>
              {q.bugs && <Chip tone="bad" icon="alert">{q.bugs}</Chip>}
            </div>
            <div className="text-[11.5px] text-muted mt-1">repeat {q.repeat} · {ago(q.started_at)}</div>
          </a>
        ))}
        {!queue.length && <div className="text-muted text-[12.5px] p-2.5">Nothing else waiting.</div>}
      </div>
    </Card>
  );
}

function LabelCall({ project, runId, testId, next, onSaved }: { project: Project; runId: string; testId: string; next: QueueItem | null; onSaved: () => void }) {
  const toast = useToast();
  const { data, error } = useLoad(
    () => Promise.all([api.get<CallDetail>(`/api/runs/${runId}/calls/${testId}`), judgesApi.callLabels(runId, testId)]),
    [runId, testId],
  );
  const [choice, setChoice] = useState<Record<string, Vote>>({});
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [saved, setSaved] = useState<{ labels: CallLabels; checks: Check[]; decision: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [showQuiet, setShowQuiet] = useState(false);
  const tl = useRef<TimelineHandle>(null);

  useEffect(() => {
    if (!data) return;
    const given: Record<string, Vote> = {};
    for (const [k, v] of Object.entries(data[1].labels)) if (v) given[k] = v;
    setChoice(given);
  }, [data]);

  const items = useMemo(() => (data?.[0].verdict?.checks || []).filter((c) => c.origin === "judge" && c.judge?.votes?.length), [data]);
  if (error) return <Notice tone="bad">{error.message}</Notice>;
  if (!data) return <Skeleton />;
  const [call, labels] = data;
  const relevant = items.filter((c) => labels.relevant.includes(c.check_id));
  const quiet = items.filter((c) => !labels.relevant.includes(c.check_id));
  const missing = relevant.filter((c) => !choice[c.check_id]);
  const changed = Object.entries(choice).filter(([k, v]) => labels.labels[k] !== v);

  const save = async () => {
    setBusy(true);
    try {
      const payload = Object.fromEntries(changed) as Record<string, Vote>;
      const why = Object.fromEntries(Object.entries(notes).filter(([k, t]) => payload[k] && t.trim()));
      const r = await judgesApi.save(runId, testId, payload, why);
      setSaved({ labels: { labels: r.labels, relevant: r.relevant }, checks: r.verdict.checks, decision: r.decision });
      toast(`Saved ${changed.length} label${changed.length === 1 ? "" : "s"}`);
      onSaved();
    } catch (e) {
      toast((e as Error).message, "bad");
    } finally {
      setBusy(false);
    }
  };

  const v = call.verdict;
  const judgeOf = (id: string) => (saved?.checks || items).find((c) => c.check_id === id);
  const itemCard = (c: Check) => {
    const mine = choice[c.check_id];
    const after = saved ? judgeOf(c.check_id) : null;
    const votes = after?.judge?.votes ?? [];
    const agree = votes.filter((x) => x.result === mine).length;
    const agreeText = agree === votes.length ? (votes.length === 2 ? "Both judges agree with you." : "Every judge agrees with you.")
      : agree === 0 ? (votes.length === 2 ? "Neither judge agrees with you." : "No judge agrees with you.")
      : `${agree} of ${votes.length} judges agree with you.`;
    return (
      <div key={c.check_id} data-item={c.check_id} className="py-3 border-b border-line last:border-b-0">
        <div className="flex items-start gap-2 flex-wrap">
          <div className="flex-1 min-w-[200px]">
            <b className="text-[13.5px]">{c.judge?.title || c.check_id}</b>
            <div className="text-[13px] text-ink-2 mt-0.5">{c.judge?.question}</div>
          </div>
          <div className="flex gap-1" role="radiogroup" aria-label={c.judge?.title || c.check_id}>
            {CHOICES.map((o) => (
              <button key={o.v} type="button" role="radio" aria-checked={mine === o.v} data-choice={o.v} disabled={!!saved}
                onClick={() => setChoice({ ...choice, [c.check_id]: o.v })}
                className={cx("inline-flex items-center gap-1.5 h-8 px-2.5 rounded-lg border text-[12.5px] font-semibold transition-colors cursor-pointer disabled:cursor-default",
                  mine === o.v ? o.on : "bg-surface border-line-strong text-ink-2 hover:bg-surface-2 disabled:opacity-60")}>
                <Icon name={o.icon} size={14} />{VOTE_LABEL[o.v]}
              </button>
            ))}
          </div>
        </div>
        {mine && !saved && (
          <input type="text" maxLength={300} value={notes[c.check_id] || ""} placeholder="Why? (optional, helps teach the judges)"
            aria-label={`Why, for ${c.judge?.title || c.check_id}`}
            onChange={(e) => setNotes({ ...notes, [c.check_id]: e.target.value })}
            className="mt-2 w-full h-8 px-2.5 rounded-lg border border-line-strong bg-surface text-[12.5px] placeholder:text-muted focus:outline-none focus:border-accent" />
        )}
        {after?.judge && (
          <div className="mt-2 grid gap-1.5 sm:grid-cols-2 animate-rise">
            {after.judge.votes.map((x) => (
              <div key={x.judge} className={cx("text-[12.5px] rounded-lg px-2.5 py-1.5 border",
                x.result === mine ? "border-[color-mix(in_srgb,var(--good)_35%,transparent)] bg-good-soft/50" : "border-[color-mix(in_srgb,var(--warn)_40%,transparent)] bg-warn-soft/50")}>
                <div className="flex items-center gap-1.5 flex-wrap">
                  <Icon name={x.result === mine ? "check" : "alert"} size={13} className={x.result === mine ? "text-good" : "text-warn"} />
                  <b>{JUDGE_NAME[x.judge]}</b><span className="text-muted">{modelName(x.model)}</span><span className="font-semibold">{VOTE_LABEL[x.result]}</span>
                </div>
                {x.reason && <div className="text-ink-2 mt-0.5">{x.reason}</div>}
              </div>
            ))}
            <div className="sm:col-span-2 text-[11.5px] text-muted">{agreeText}</div>
          </div>
        )}
      </div>
    );
  };

  return (
    <div className="grid gap-4 items-start grid-cols-1 2xl:grid-cols-[minmax(0,1fr)_minmax(0,520px)]">
      <Card>
        <CardHead title={<span className="flex items-center gap-2 flex-wrap">{shortScenario(call.record.scenario_id)} {v && <Badge status={v.status} />}</span>}
          sub={`repeat ${call.record.repeat} · ${call.record.mode}`}>
          <Button size="sm" variant="ghost" href={projectHref(project.key, `/runs/${runId}/calls/${testId}`)} icon="ext">Full call</Button>
        </CardHead>
        <Timeline ref={tl} events={call.events} limits={null} />
      </Card>
      <Card className="2xl:sticky 2xl:top-[76px]">
        <CardHead title="Your labels" sub={saved ? "saved" : `${relevant.length - missing.length} of ${relevant.length} done`} />
        {!saved && (
          <Notice icon="eye-off" className="mb-1 text-[12.5px]">The judges' answers are hidden until you save, so they don't sway you. Judge only what the conversation shows.</Notice>
        )}
        <div id="label-items">{relevant.map(itemCard)}</div>
        {quiet.length > 0 && (
          <div className="pt-2">
            <button type="button" className="text-[12.5px] text-muted hover:text-ink cursor-pointer inline-flex items-center gap-1" onClick={() => setShowQuiet((s) => !s)}>
              <Icon name="chevron" size={13} className={cx("transition-transform", showQuiet && "rotate-90")} />
              {quiet.length} item(s) the judges said didn't come up {showQuiet ? "" : "(label them only if they did)"}
            </button>
            {showQuiet && <div className="mt-1">{quiet.map(itemCard)}</div>}
          </div>
        )}
        <div className="flex items-center gap-2.5 mt-3 flex-wrap">
          {saved ? (
            <>
              <span className="flex-1 text-[12.5px] text-muted">Run decision now: <b className="text-ink">{saved.decision.replace(/_/g, " ").replace("NO GO", "NO-GO")}</b></span>
              {next ? <Button id="next-call" variant="primary" icon="arrow" href={projectHref(project.key, `/labels/${next.run_id}/${next.test_id}`)}>Next call</Button>
                : <Button variant="primary" icon="scale" href={projectHref(project.key, "/judges")}>See calibration</Button>}
            </>
          ) : (
            <>
              <span className="flex-1 text-[12.5px] text-muted">{missing.length ? `${missing.length} item(s) still to label` : "All labelled"}</span>
              {next && <Button variant="ghost" href={projectHref(project.key, `/labels/${next.run_id}/${next.test_id}`)}>Skip</Button>}
              <Button id="save-labels" variant="primary" icon="save" busy={busy} disabled={busy || !changed.length} onClick={() => void save()}>Save labels</Button>
            </>
          )}
        </div>
      </Card>
    </div>
  );
}

