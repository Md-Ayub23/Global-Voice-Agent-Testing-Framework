// One real call (Phase 8): the conversation as the agent had it (personal data already removed), why it was flagged,
// the checks, the AI judges with a place to label them, the call quality, and a button to turn it into a test.
import { useRef, useState } from "react";
import { useToast } from "../components/feedback";
import { Icon } from "../components/Icon";
import { FlagChip } from "../components/monitor";
import { CallQuality } from "../components/quality";
import { ChecksList, JudgeList, OutcomeNotice } from "../components/runbits";
import { Timeline, type TimelineHandle } from "../components/Timeline";
import { Badge, Button, Card, CardHead, Chip, Notice, Seg, Skeleton, Table, Tabs } from "../components/ui";
import { api } from "../lib/api";
import { useCrumbs, useLoad } from "../lib/app";
import { cx, fmtDate, fmtDuration, fmtMs, money } from "../lib/format";
import { VOTE_LABEL } from "../lib/judges";
import { projectHref } from "../lib/links";
import { navigate } from "../lib/router";
import type { MonitorCallDetail, Project, Vote } from "../lib/types";

const SOURCE_WORDS: Record<string, string> = {
  vocto: "Vocto format", vapi: "Vapi", retell: "Retell", transcript: "Transcript",
};
const VIA_WORDS: Record<string, string> = { upload: "uploaded", webhook: "sent live", cli: "imported" };
const CHOICES: { v: Vote; icon: "thumbs-up" | "thumbs-down" | "minus"; on: string }[] = [
  { v: "PASS", icon: "thumbs-up", on: "bg-good text-white border-good" },
  { v: "FAIL", icon: "thumbs-down", on: "bg-bad text-white border-bad" },
  { v: "NOT_APPLICABLE", icon: "minus", on: "bg-ink-2 text-surface border-ink-2" },
];
const JUDGE_STATE: Record<string, string> = { done: "judged", pending: "waiting for the judges", skipped: "not judged" };

export function MonitorCallPage({ project, callId }: { project: Project; callId: string }) {
  const home = (path = "") => projectHref(project.key, `/calls${path}`);
  useCrumbs([{ label: "Agents", href: "#/projects" }, { label: project.name, href: projectHref(project.key) },
    { label: "Real calls", href: home() }, { label: "Calls", href: home("/list") }, { label: "Call" }]);
  const { data, error, reload } = useLoad(
    () => api.get<MonitorCallDetail>(`/api/project/${project.key}/monitor/calls/${callId}`), [project.key, callId]);
  const [view, setView] = useState<"chat" | "text">("chat");
  const [side, setSide] = useState("checks");
  const [choice, setChoice] = useState<Record<string, Vote>>({});
  const [busy, setBusy] = useState("");
  const tl = useRef<TimelineHandle>(null);
  const toast = useToast();

  if (error) return <Notice tone="bad" className="mt-5">{error.message}</Notice>;
  if (!data) return <Skeleton />;
  const r = data.record, v = data.verdict, m = v?.metrics || {};
  const own = (v?.checks || []).filter((x) => x.origin !== "judge");
  const judged = (v?.checks || []).filter((x) => x.origin === "judge");
  const judgedRan = judged.some((x) => x.judge?.votes?.length || x.judge?.human);
  const redacted = Object.entries(r.redacted || {}).filter(([, n]) => n > 0);
  const removed = redacted.reduce((a, [, n]) => a + n, 0);

  const sideTabs = [
    { value: "checks", label: "Checks" },
    ...(judged.length ? [{ value: "judges", label: "AI judges" }] : []),
    ...(m.quality && Object.keys(m.quality).length ? [{ value: "quality", label: "Quality" }] : []),
    { value: "about", label: "About" },
  ];
  const sideShown = sideTabs.some((t) => t.value === side) ? side : "checks";
  const onEvidence = (seq: number) => { setView("chat"); setTimeout(() => tl.current?.flash(seq), 30); };
  const changed = Object.entries(choice).filter(([k, val]) => judged.find((c) => c.check_id === k)?.judge?.human !== val);

  const act = async (what: string, run: () => Promise<void>) => {
    setBusy(what);
    try { await run(); } catch (e) { toast((e as Error).message, "bad"); } finally { setBusy(""); }
  };
  const saveLabels = () => act("labels", async () => {
    await api.post(`/api/project/${project.key}/monitor/calls/${callId}/labels`, { labels: Object.fromEntries(changed) });
    toast(`Saved ${changed.length} label${changed.length === 1 ? "" : "s"}`);
    setChoice({});
    reload();
  });
  const makeTest = () => act("test", async () => {
    const out = await api.post<{ saved: boolean; file: string; existed?: boolean; problems?: string[] }>(
      `/api/project/${project.key}/monitor/calls/${callId}/test`);
    if (!out.saved) { toast(out.problems?.[0] || "Couldn't make the test", "bad"); return; }
    toast(out.existed ? "This call is already a test case" : "Test case made from this call");
    navigate(projectHref(project.key, `/setup/cases?edit=${encodeURIComponent(out.file)}`));
  });
  const remove = () => act("delete", async () => {
    if (!confirm("Delete this call and everything Vocto kept about it? This can't be undone.")) return;
    await api.post(`/api/project/${project.key}/monitor/delete`, { call_ids: [callId] });
    toast("Call deleted");
    navigate(home("/list"), true);
  });

  return (
    <>
      <div className="flex items-end gap-4 mt-2 mb-5 flex-wrap">
        <div className="flex-1 min-w-0">
          <div className="flex flex-wrap items-center gap-2.5">
            <h1 className="text-[24px] font-semibold tracking-[-.025em]">Real call</h1>
            {v ? <Badge status={v.status} /> : <Badge status="interrupted" label="not scored" />}
            {v?.flagged && <Chip tone="warn" icon="flag">flagged</Chip>}
          </div>
          <div className="mt-1 text-ink-2 flex flex-wrap items-center gap-2">
            <span>{fmtDate(r.started_at || r.imported_at)}</span><span className="text-muted">·</span>
            <Chip icon="phone">{SOURCE_WORDS[r.source] || r.source}{r.received_via ? `, ${VIA_WORDS[r.received_via] || r.received_via}` : ""}</Chip>
            {r.agent_version && <Chip icon="tag">version {r.agent_version}</Chip>}
            <Chip tone={r.mode === "voice" ? "agent" : "caller"} icon={r.mode === "voice" ? "mic" : "text"}>{r.mode === "voice" ? "timed" : "no timings"}</Chip>
            <Chip icon="shield" title={redacted.map(([k, n]) => `${k.replace("_", " ")}: ${n}`).join(", ") || undefined}>
              {removed ? `${removed} personal detail${removed === 1 ? "" : "s"} removed` : "no personal data found"}
            </Chip>
          </div>
        </div>
        <div className="flex gap-2 flex-wrap">
          <Button size="sm" icon="plan" busy={busy === "test"} disabled={!!busy} onClick={() => void makeTest()} id="make-test">Make a test from this call</Button>
          <Button size="sm" variant="danger" icon="x" busy={busy === "delete"} disabled={!!busy} onClick={() => void remove()} id="delete-call">Delete</Button>
          <Button size="sm" icon="back" href={home("/list")}>All calls</Button>
        </div>
      </div>

      <OutcomeNotice verdict={v} />
      {v && v.flags.length > 0 && (
        <Card className="mb-4">
          <CardHead title="Why it was flagged" sub="a person should look at these" />
          <div className="flex flex-wrap gap-1.5" id="flags">
            {v.flags.map((f) => <FlagChip key={`${f.kind}:${f.id}`} f={f} defs={data.metric_defs} items={data.judge_items} />)}
          </div>
        </Card>
      )}

      <div className="grid gap-4 items-start grid-cols-1 xl:grid-cols-[minmax(0,1fr)_400px]">
        <Card className="min-w-0">
          <CardHead title="Conversation and actions" sub={removed ? "personal data was removed before saving" : undefined}>
            <Seg id="view-mode" value={view} onChange={setView} options={[{ value: "chat", label: "Conversation" }, { value: "text", label: "Plain text" }]} />
          </CardHead>
          <div id="conv" className={view === "chat" ? "" : "hidden"}>
            <Timeline ref={tl} events={data.events} limits={null} />
          </div>
          {view === "text" && (
            <Table compact head={["#", "Who", "What happened"]}>
              {data.story.map((row) => (
                <tr key={row.seq}>
                  <td className="font-mono text-[11.5px] text-muted">#{row.seq}</td>
                  <td className="whitespace-nowrap"><b>{row.who}</b></td>
                  <td>{row.text}</td>
                </tr>
              ))}
            </Table>
          )}
        </Card>

        <div className="flex flex-col gap-4 min-w-0">
          <Tabs value={sideShown} onChange={setSide} tabs={sideTabs} className="mb-0" />
          {sideShown === "checks" && (
            <Card className="animate-view-in">
              <CardHead title="Checks" sub={v ? `${own.filter((x) => x.result === "PASS").length} of ${own.length} passed` : undefined} />
              <div id="checks">{v ? <ChecksList verdict={v} onEvidence={onEvidence} /> : <div className="text-muted text-[12.5px]">This call has not been scored.</div>}</div>
              <div className="text-muted text-[11.5px] mt-2">Real calls have no script, so only checks that fit any call run here; the AI judges cover the rest.</div>
            </Card>
          )}
          {sideShown === "judges" && v && (
            <Card id="judges" className="animate-view-in">
              <CardHead title="AI judges" sub={JUDGE_STATE[v.judges?.state] || v.judges?.state}>
                <Button size="sm" variant="ghost" href={projectHref(project.key, "/judges")}>About</Button>
              </CardHead>
              {v.judges?.note && <Notice className="mb-3 text-[12.5px]">{v.judges.note}</Notice>}
              {judgedRan ? <JudgeList verdict={v} onEvidence={onEvidence} /> : <div className="text-muted text-[12.5px]">The judges haven't looked at this call.</div>}
              {judgedRan && (
                <div className="mt-3 pt-3 border-t border-line" id="label-items">
                  <div className="font-semibold text-[13px] mb-1">Your labels</div>
                  <div className="text-muted text-[12px] mb-2">Were the judges right? Your answer replaces theirs on this call and teaches them which items to trust.</div>
                  {judged.map((c) => {
                    const mine = choice[c.check_id] || (c.judge?.human as Vote | undefined);
                    return (
                      <div key={c.check_id} data-item={c.check_id} className="flex items-center gap-2 flex-wrap py-1.5">
                        <span className="flex-1 min-w-[140px] text-[13px]">{c.judge?.title || data.judge_items[c.check_id] || c.check_id}</span>
                        <div className="flex gap-1" role="radiogroup" aria-label={c.judge?.title || c.check_id}>
                          {CHOICES.map((o) => (
                            <button key={o.v} type="button" role="radio" aria-checked={mine === o.v} data-choice={o.v} title={VOTE_LABEL[o.v]}
                              onClick={() => setChoice({ ...choice, [c.check_id]: o.v })}
                              className={cx("inline-flex items-center justify-center size-8 rounded-lg border transition-colors cursor-pointer",
                                mine === o.v ? o.on : "bg-surface border-line-strong text-ink-2 hover:bg-surface-2")}>
                              <Icon name={o.icon} size={14} /><span className="sr-only">{VOTE_LABEL[o.v]}</span>
                            </button>
                          ))}
                        </div>
                      </div>
                    );
                  })}
                  <div className="flex justify-end mt-2">
                    <Button id="save-labels" size="sm" variant="primary" icon="save" busy={busy === "labels"} disabled={!!busy || !changed.length}
                      onClick={() => void saveLabels()}>Save labels</Button>
                  </div>
                </div>
              )}
            </Card>
          )}
          {sideShown === "quality" && m.quality && <CallQuality quality={m.quality} defs={data.metric_defs} />}
          {sideShown === "about" && (
            <Card className="animate-view-in">
              <CardHead title="About this call" />
              <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3.5 gap-y-2 text-[13px]">
                <dt className="text-muted font-semibold">Length</dt><dd className="m-0">{fmtDuration(r.duration_ms ?? m.duration_ms)}</dd>
                <dt className="text-muted font-semibold">Response p50</dt><dd className="m-0">{r.mode === "voice" ? fmtMs(m.latency_p50_ms) : "no timings in the file"}</dd>
                <dt className="text-muted font-semibold">How it ended</dt><dd className="m-0">{r.ended_reason || "not given"}</dd>
                <dt className="text-muted font-semibold">Caller</dt><dd className="m-0">{r.caller_hash ? <span title="The number itself is not kept">caller {r.caller_hash}</span> : "unknown"}</dd>
                <dt className="text-muted font-semibold">Their id</dt><dd className="m-0 font-mono text-[12px] break-all">{r.external_id || "none"}</dd>
                <dt className="text-muted font-semibold">Received</dt><dd className="m-0">{fmtDate(r.imported_at)}</dd>
                {v?.judges?.cost_usd ? <><dt className="text-muted font-semibold">Judge cost</dt><dd className="m-0">{money(v.judges.cost_usd)}</dd></> : null}
                {r.recording_url && <><dt className="text-muted font-semibold">Recording</dt>
                  <dd className="m-0"><a href={r.recording_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1">Open on the platform<Icon name="ext" size={13} /></a></dd></>}
              </dl>
            </Card>
          )}
        </div>
      </div>
    </>
  );
}
