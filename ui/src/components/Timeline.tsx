// The conversation of one call, from its events (runs/<run>/calls/<test>/events.jsonl).
// Used by the live view and the call page. Items are keyed by event number, so when new events
// stream in only they are added (and animate in); a tool answer fills in its request card, and
// a second listen is attached to the agent turn it re-checked.
import { useImperativeHandle, useMemo, useRef, type ReactNode, type Ref } from "react";
import { cx, fmtDuration, fmtMs, fmtSecs } from "../lib/format";
import type { EventData, Limits, TraceEvent } from "../lib/types";
import { Icon, type IconName } from "./Icon";
import { Chip, Spinner, Table } from "./ui";

export interface TimelineHandle { flash: (seq: number) => boolean }
export type Typing = "agent" | "caller" | "connecting" | "judging" | null;

type Item =
  | { kind: "sys"; seq: number; tone: "" | "good" | "bad"; icon: IconName; text: ReactNode }
  | { kind: "agent"; seq: number; e: TraceEvent; second: EventData | null }
  | { kind: "caller"; seq: number; e: TraceEvent }
  | { kind: "tool"; seq: number; e: TraceEvent | null; tool: string; fault: EventData | null; resp: TraceEvent | null }
  | { kind: "db"; seq: number; e: TraceEvent };

function value(v: unknown): string {
  if (Array.isArray(v)) return v.length ? v.map(value).join(", ") : "[] (empty)";
  if (v && typeof v === "object") return Object.entries(v).map(([k, x]) => `${k}=${value(x)}`).join(", ");
  return String(v);
}

// Turns the event list into what is drawn, and which drawn item shows each event.
function build(events: TraceEvent[]): { items: Item[]; owner: Map<number, number> } {
  const items: Item[] = [];
  const owner = new Map<number, number>();         // event seq -> item seq showing it
  const cards = new Map<string, Item & { kind: "tool" }>();
  const agents = new Map<number, Item & { kind: "agent" }>();
  const add = (it: Item) => { items.push(it); owner.set(it.seq, it.seq); };
  for (const e of events) {
    const d = e.data || {};
    switch (e.type) {
      case "call_started":
        add({ kind: "sys", seq: e.seq, tone: "", icon: "phone", text: `Call started · ${d.mode || ""} mode` });
        break;
      case "agent_turn": {
        const it = { kind: "agent" as const, seq: e.seq, e, second: null };
        agents.set(e.seq, it);
        add(it);
        break;
      }
      case "caller_turn":
        add({ kind: "caller", seq: e.seq, e });
        break;
      case "tool_request": {
        const it = { kind: "tool" as const, seq: e.seq, e, tool: d.tool, fault: null, resp: null };
        cards.set(d.call_id, it);
        add(it);
        break;
      }
      case "fault_injected": {
        const card = cards.get(d.call_id);
        if (card) { card.fault = d.fault || {}; owner.set(e.seq, card.seq); }
        else {
          const f = d.fault || {};
          add({ kind: "sys", seq: e.seq, tone: "bad", icon: "bolt", text: `Vocto injected ${f.type}${f.delay_ms ? ` (${fmtMs(f.delay_ms)})` : ""}` });
        }
        break;
      }
      case "tool_response": {
        const card = cards.get(d.call_id);
        if (card) { card.resp = e; owner.set(e.seq, card.seq); }
        else add({ kind: "tool", seq: e.seq, e: null, tool: d.tool, fault: null, resp: e });
        break;
      }
      case "stt_text": {
        const target = agents.get(d.agent_turn);
        if (target) { target.second = d; owner.set(e.seq, target.seq); }
        else add({ kind: "sys", seq: e.seq, tone: "", icon: "ear", text: <><b>Second listen</b> ({d.model || ""}): {d.text || ""}</> });
        break;
      }
      case "probe_result":
        if (!d.ok) add({ kind: "sys", seq: e.seq, tone: "bad", icon: "db", text: `Database check ${d.probe} failed: ${d.error || "unknown error"}` });
        else add({ kind: "db", seq: e.seq, e });
        break;
      case "validator_result":
        add(d.on_script
          ? { kind: "sys", seq: e.seq, tone: "good", icon: "check", text: "Validator: the simulated caller stayed on script" }
          : { kind: "sys", seq: e.seq, tone: "bad", icon: "alert", text: `Validator: the simulated caller went off script: ${(d.problems || []).join("; ")}` });
        break;
      case "harness_event":
        add({ kind: "sys", seq: e.seq, tone: d.level === "warning" ? "" : "bad", icon: "alert",
          text: `Vocto ${d.level || "error"}: ${d.code}: ${d.message} (not blamed on the agent)` });
        break;
      case "call_ended":
        add({ kind: "sys", seq: e.seq, tone: "", icon: "hang",
          text: `Call ended: ${d.reason || ""} (by ${d.ended_by || "?"}) at ${fmtSecs(e.t_ms)}${d.recording ? ` · recording ${fmtDuration(d.recording.duration_ms)}` : ""}` });
        break;
    }
  }
  return { items, owner };
}

export function whoIsNext(events: TraceEvent[]): Typing {
  if (!events.length) return "connecting";
  if (events.some((e) => e.type === "call_ended")) return "judging";
  const last = [...events].reverse().find((e) => ["caller_turn", "agent_turn", "call_ended", "tool_request"].includes(e.type));
  if (!last || last.type === "caller_turn" || last.type === "tool_request") return "agent";
  return "caller";
}

const TYPING_TEXT = { agent: "Agent is answering", caller: "Caller is thinking", connecting: "Dialling the agent", judging: "Checking the database and judging the call" };

export function Timeline({ events, limits, onSeek, typing, ref }: {
  events: TraceEvent[]; limits?: Limits | null; onSeek?: (ms: number) => void; typing?: Typing; ref?: Ref<TimelineHandle>;
}) {
  const { items, owner } = useMemo(() => build(events), [events]);
  const nodes = useRef(new Map<number, HTMLElement>());
  useImperativeHandle(ref, () => ({
    flash(seq: number) {
      const el = nodes.current.get(owner.get(seq) ?? seq);
      if (!el) return false;
      el.scrollIntoView({ behavior: "smooth", block: "center" });
      el.classList.remove("animate-flash");
      void el.offsetWidth;
      el.classList.add("animate-flash");
      return true;
    },
  }), [owner]);
  const keep = (seq: number) => (el: HTMLElement | null) => {
    if (el) nodes.current.set(seq, el);
    else nodes.current.delete(seq);
  };
  const play = (ms: number | null | undefined) =>
    ms == null ? null : (
      <button type="button" title="Play from here" onClick={() => onSeek?.(ms)}
        className="inline-flex items-center gap-1 cursor-pointer hover:text-accent-ink">
        <Icon name="play" size={11} />{fmtSecs(ms)}
      </button>
    );

  return (
    <div className="flex flex-col gap-2.5 px-0.5 py-1">
      {items.map((it) => {
        switch (it.kind) {
          case "sys":
            return (
              <div key={it.seq} ref={keep(it.seq)} id={`ev-${it.seq}`}
                className={cx("self-center inline-flex items-center gap-2 text-[12.5px] px-3 py-1 rounded-full border max-w-[92%] animate-rise",
                  it.tone === "bad" ? "text-bad bg-bad-soft border-transparent" : it.tone === "good" ? "text-good bg-good-soft border-transparent" : "text-muted bg-surface-2 border-line")}>
                <Icon name={it.icon} size={14} className="flex-none" /><span>{it.text}</span>
              </div>
            );
          case "agent": {
            const d = it.e.data;
            const slow = limits && d.latency_ms != null && d.latency_ms > limits.p95;
            const own = d.platform_transcript && d.platform_transcript.trim() !== (d.heard_text || "").trim() ? d.platform_transcript : null;
            return (
              <div key={it.seq} ref={keep(it.seq)} id={`ev-${it.seq}`} className="flex gap-2.5 items-end max-w-full md:max-w-[86%] animate-rise rounded-2xl">
                <div className="size-[30px] rounded-[10px] grid place-items-center text-white flex-none bg-agent"><Icon name="bot" /></div>
                <div className="rounded-2xl rounded-bl-[5px] px-3.5 py-2.5 bg-surface-2 border border-line min-w-0">
                  <div className="text-[11.5px] font-bold flex gap-2 items-center mb-0.5 flex-wrap text-agent">
                    Agent <span className="text-muted font-medium inline-flex gap-1">turn {it.e.turn ?? ""} · {d.audio ? play(d.audio.start_ms) : fmtSecs(it.e.t_ms)}</span>
                  </div>
                  <div className="whitespace-pre-wrap break-words">{d.silent ? <span className="text-muted">(said nothing)</span> : d.heard_text || ""}</div>
                  {(d.latency_ms != null || d.interrupted) && (
                    <div className="flex gap-1.5 flex-wrap mt-[7px]">
                      {d.latency_ms != null && (
                        <Chip tone={d.latency_ms < 0 ? "warn" : slow ? "bad" : "default"} icon="clock">
                          {d.latency_ms < 0 ? "talked over the caller" : `answered after ${fmtMs(d.latency_ms)}`}
                        </Chip>
                      )}
                      {d.interrupted && <Chip tone="warn" icon="hang">interrupted · stopped {fmtMs(d.stopped_after_ms)} after the caller cut in</Chip>}
                    </div>
                  )}
                  {own && <div className="mt-2 pt-[7px] border-t border-dashed border-line-strong text-ink-2 text-[12.5px]"><b>Agent's own text:</b> {own}</div>}
                  {it.second && (
                    <div className="mt-2 pt-[7px] border-t border-dashed border-line-strong text-ink-2 text-[12.5px] animate-rise">
                      <b className="inline-flex items-center gap-1"><Icon name="ear" size={13} /> Second listen</b> <span className="text-muted">({it.second.model || ""})</span>: {it.second.text || ""}
                    </div>
                  )}
                </div>
              </div>
            );
          }
          case "caller": {
            const d = it.e.data;
            return (
              <div key={it.seq} ref={keep(it.seq)} id={`ev-${it.seq}`} className="flex flex-row-reverse gap-2.5 items-end self-end max-w-full md:max-w-[86%] animate-rise rounded-2xl">
                <div className="size-[30px] rounded-[10px] grid place-items-center text-white flex-none bg-caller"><Icon name="user" /></div>
                <div className="rounded-2xl rounded-br-[5px] px-3.5 py-2.5 bg-caller-soft min-w-0">
                  <div className="text-[11.5px] font-bold flex gap-2 items-center mb-0.5 flex-wrap text-caller">
                    Caller <span className="text-muted font-medium inline-flex gap-1">turn {it.e.turn ?? ""} · {d.audio ? play(d.audio.start_ms) : fmtSecs(it.e.t_ms)}</span>
                  </div>
                  <div className="whitespace-pre-wrap break-words">{d.text || ""}</div>
                  {d.barge_in && <div className="flex gap-1.5 flex-wrap mt-[7px]"><Chip tone="warn" icon="bolt">cut in while the agent was talking</Chip></div>}
                </div>
              </div>
            );
          }
          case "tool": {
            const req = it.e?.data;
            const resp = it.resp?.data;
            const failed = resp && !resp.ok;
            return (
              <div key={it.seq} ref={keep(it.seq)} id={`ev-${it.seq}`}
                className={cx("self-stretch md:mx-10 border border-line border-l-[3px] rounded-xl px-3 py-2.5 text-[13px] animate-rise",
                  it.fault ? "border-l-bad bg-[color-mix(in_srgb,var(--bad-soft)_45%,var(--surface))]" : failed ? "border-l-bad bg-surface" : "border-l-tool bg-surface")}>
                <div className="flex items-center gap-2 flex-wrap">
                  <Icon name="tool" className="text-tool" />
                  {req && <span className="text-muted text-[12.5px]">Agent uses tool</span>}
                  <b className="font-mono text-[13px]">{it.tool}</b>
                  <span className="flex-1" />
                  {it.e && <span className="text-muted text-[11.5px]">turn {it.e.turn ?? ""} · #{it.e.seq}</span>}
                </div>
                {req && <KV obj={req.args} />}
                {it.fault && (
                  <div className="flex gap-1.5 flex-wrap mt-2">
                    <Chip tone="bad" icon="bolt">
                      Vocto injected {it.fault.type}{it.fault.delay_ms ? ` (${fmtMs(it.fault.delay_ms)})` : ""}{it.fault.forward ? " · still reaches the backend" : " · never reaches the backend"}
                    </Chip>
                  </div>
                )}
                <div className="mt-2 pt-2 border-t border-dashed border-line">
                  {resp ? (
                    <>
                      <div className="flex items-center gap-2 flex-wrap text-[12.5px]">
                        <Chip tone={resp.ok ? "good" : "bad"} icon={resp.ok ? "check" : "x"}>{resp.ok ? "OK" : "Failed"} {resp.status}</Chip>
                        <span className="text-muted">{fmtMs(resp.duration_ms)}{resp.reached_backend ? "" : " · never reached the database"} · #{it.resp!.seq}</span>
                      </div>
                      <KV obj={resp.body} />
                    </>
                  ) : (
                    <span className="flex items-center gap-2 text-[12.5px] text-muted"><Spinner />waiting for the answer…</span>
                  )}
                </div>
              </div>
            );
          }
          case "db": {
            const d = it.e.data;
            const rows: Record<string, unknown>[] | null = d.rows || null;
            return (
              <div key={it.seq} ref={keep(it.seq)} id={`ev-${it.seq}`} className="self-stretch md:mx-10 border border-line border-l-[3px] border-l-tool rounded-xl bg-surface px-3 py-2.5 text-[13px] animate-rise">
                <div className="flex items-center gap-2 flex-wrap">
                  <Icon name="db" className="text-tool" /><span className="text-muted text-[12.5px]">Database after the call</span>
                  <b className="font-mono text-[13px]">{d.probe}</b><span className="flex-1" />
                  <span className="text-muted text-[11.5px]">{rows ? `${rows.length} row(s)` : ""} · #{it.seq}</span>
                </div>
                {rows ? (
                  rows.length ? <div className="mt-2"><RowsTable rows={rows} /></div>
                    : <div className="text-muted text-[12.5px] mt-1.5">No rows: nothing was stored by this call.</div>
                ) : <div className="font-mono text-[12.5px] mt-1.5">{value(d.value)}</div>}
              </div>
            );
          }
        }
      })}
      {typing && (
        <div className={cx("inline-flex gap-2 items-center text-muted text-[12.5px] pt-1.5", typing === "caller" ? "self-end pr-10" : "self-start pl-10")}>
          <span className="inline-flex gap-[3px]">
            {[0, 0.15, 0.3].map((d) => <i key={d} className="size-1.5 rounded-full bg-current animate-dot" style={{ animationDelay: `${d}s` }} />)}
          </span>
          {TYPING_TEXT[typing]}…
        </div>
      )}
    </div>
  );
}

function KV({ obj }: { obj: Record<string, unknown> | null | undefined }) {
  const entries = Object.entries(obj || {});
  if (!entries.length) return null;
  return (
    <div className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-[3px] mt-2 text-[12.5px]">
      {entries.map(([k, v]) => (
        <div key={k} className="contents">
          <span className="text-muted">{k}</span>
          <span className="font-mono break-words min-w-0">{value(v)}</span>
        </div>
      ))}
    </div>
  );
}

export function RowsTable({ rows }: { rows: Record<string, unknown>[] }) {
  return (
    <Table compact head={Object.keys(rows[0])}>
      {rows.map((r, i) => (
        <tr key={i}>{Object.values(r).map((v, j) => <td key={j} className="font-mono text-[12.5px]">{String(v)}</td>)}</tr>
      ))}
    </Table>
  );
}
