// One scenario: the situation to test, what the caller knows, what must happen, what is checked.
import { useState, type ReactNode } from "react";
import { useToast } from "../../components/feedback";
import {
  Area, Block, Check, Csv, Grid, Help, KeyValue, Num, Problems, SaveBar, Select, StrList, Text, getIn, setIn, type Json, type Obj,
} from "../../components/form";
import { Button, Card, Chip, Field, Seg } from "../../components/ui";
import { setupApi, slug, type ProfileDetail } from "../../lib/setup";
import { asList, asObj, str } from "./common";

const SUCCESS = ["\\b(booked|confirmed|scheduled|all set|done|completed)\\b"];
const FAILURE = ["\\b(couldn't|could not|unable|not able|wasn't able|failed)\\b", "(?<!no )\\bproblem\\b", "\\btry again\\b"];

// Every check type, in plain English, with the fields it needs.
export const CHECKS: Record<string, { label: string; help: string; fields: string[] }> = {
  tool_called: { label: "Tool was called", help: "The agent called this tool (with these arguments, this many times).", fields: ["tool", "args_match", "times"] },
  tool_not_called: { label: "Tool was NOT called", help: "e.g. no refund without verification.", fields: ["tool"] },
  tool_order: { label: "Tool order", help: "The first tool was called before the second (e.g. verify identity before booking).", fields: ["first", "then"] },
  probe: { label: "Backend record", help: "After the call, a probe finds these records in the backend.", fields: ["probe", "expect_count", "expect_fields"] },
  said: { label: "Agent said", help: "Something the agent says matches this pattern (regular expression).", fields: ["pattern"] },
  not_said: { label: "Agent never said", help: "Nothing the agent says matches this pattern.", fields: ["pattern"] },
  entity_fidelity: { label: "Details kept exactly", help: "These caller facts arrive unchanged in the tool arguments and the backend record.", fields: ["entities"] },
  readback: { label: "Reads back a value", help: "The agent says the value a tool returned (a booking reference, an order number) exactly.", fields: ["tool", "field", "probe", "column"] },
  log_contains: { label: "Agent log has", help: "The agent's own log for the call has a line matching this pattern (needs Logs).", fields: ["pattern"] },
  log_not_contains: { label: "Agent log has no", help: "No log line of the call matches this pattern, e.g. ERROR.", fields: ["pattern"] },
  latency: { label: "Answer time", help: "This call's slow answers (p95) stay under a limit. Voice only.", fields: ["p95_ms", "min_answers"] },
  max_turns: { label: "Conversation length", help: "The caller needs at most this many turns.", fields: ["n"] },
  handoff: { label: "Handed over", help: "Multi-agent platforms: the call was handed to this sub-agent.", fields: ["to"] },
  judge: { label: "AI judge question", help: "A yes/no question the AI judges answer from the conversation. It is only an opinion until your labels show the judges get it right; then it can block.", fields: ["intent", "pass_when", "fail_when", "applies_when", "rubric"] },
};

interface Ctx { tools: string[]; writeTools: string[]; probes: string[]; facts: string[] }

function CheckFields({ check, set, ctx }: { check: Obj; set: (k: string, v: Json | undefined) => void; ctx: Ctx }) {
  const spec = CHECKS[str(check.type)];
  if (!spec) return null;
  const field = (f: string): ReactNode => {
    switch (f) {
      case "tool": case "first": case "then":
        return <Field key={f} label={f === "tool" ? "Tool" : f === "first" ? "First" : "Then"}><Select value={str(check[f])} options={ctx.tools} placeholder="Pick a tool" onChange={(v) => set(f, v)} /></Field>;
      case "probe":
        return <Field key={f} label="Probe"><Select value={str(check[f])} options={ctx.probes} placeholder={check.type === "readback" ? "(optional)" : "Pick a probe"} onChange={(v) => set(f, v)} /></Field>;
      case "args_match": case "expect_fields":
        return <Field key={f} label={f === "args_match" ? "With arguments" : "Fields must be"} className="sm:col-span-2" hint="A value starting with $ is a caller fact, e.g. $doctor.">
          <KeyValue value={asObj(check[f])} typed onChange={(v) => set(f, Object.keys(v).length ? v : undefined)} /></Field>;
      case "times": case "expect_count": case "p95_ms": case "min_answers": case "n":
        return <Field key={f} label={{ times: "Times", expect_count: "Number of records", p95_ms: "p95 limit (ms)", min_answers: "At least this many timed answers", n: "Most caller turns" }[f]}>
          <Num value={check[f] as number} min={0} onChange={(v) => set(f, v)} /></Field>;
      case "entities":
        return <Field key={f} label="Caller facts" hint={`Known facts: ${ctx.facts.join(", ") || "none yet"}`}><Csv value={asList<string>(check[f])} onChange={(v) => set(f, v.length ? v : undefined)} /></Field>;
      case "pattern":
        return <Field key={f} label="Pattern (regular expression)" hint="(?i) at the start ignores case."><Text mono value={str(check[f])} onChange={(v) => set(f, v)} /></Field>;
      case "intent":
        return <Field key={f} label="Question" className="sm:col-span-2"><Text value={str(check[f])} onChange={(v) => set(f, v)} placeholder="Did the agent read back the date and time before booking?" /></Field>;
      case "pass_when": case "fail_when": case "applies_when":
        return <Field key={f} label={{ pass_when: "Passes when", fail_when: "Fails when", applies_when: "Only applies when" }[f]}
          hint={f === "applies_when" ? "Empty: always." : undefined}><Text value={str(check[f])} onChange={(v) => set(f, v || undefined)} /></Field>;
      case "rubric":
        return <Field key={f} label="Or reuse a judge item" hint="Instead of a question: a built-in or custom item id from the AI judges setup, e.g. grounded-facts.">
          <Text mono value={str(check[f])} onChange={(v) => set(f, v || undefined)} placeholder="grounded-facts" /></Field>;
      default:
        return <Field key={f} label={f}><Text mono value={str(check[f])} onChange={(v) => set(f, v)} /></Field>;
    }
  };
  return <Grid>{spec.fields.map(field)}</Grid>;
}

function CheckList({ checks, onChange, ctx, needId, prefix }: { checks: Obj[]; onChange: (c: Obj[]) => void; ctx: Ctx; needId: boolean; prefix: string }) {
  const setAt = (i: number, k: string, v: Json | undefined) => onChange(checks.map((c, j) => (j === i ? setIn(c, [k], v) : c)));
  return (
    <div className="grid gap-2.5">
      {checks.map((c, i) => (
        <div key={i} data-check={str(c.id) || `${prefix}${i + 1}`} className="grid gap-2.5 p-3 border border-line rounded-xl bg-surface-2/40">
          <div className="flex items-center gap-2 flex-wrap">
            <Select className="w-auto min-w-[200px]" value={str(c.type)} onChange={(v) => setAt(i, "type", v)}
              options={Object.entries(CHECKS).map(([k, s]) => [k, s.label] as [string, string])} />
            <Select className="w-auto" value={str(c.severity) || "major"} onChange={(v) => setAt(i, "severity", v === "major" ? undefined : v)}
              options={[["blocker", "Blocker: stops the release"], ["major", "Major"], ["minor", "Minor"]]} />
            <Select className="w-auto" value={str(c.section) || "accuracy"} onChange={(v) => setAt(i, "section", v === "accuracy" ? undefined : v)}
              options={[["accuracy", "Accuracy"], ["experience", "Experience"], ["security", "Security"]]} />
            <span className="flex-1" />
            <Button size="sm" variant="ghost" iconOnly icon="x" aria-label="Remove check" onClick={() => onChange(checks.filter((_, j) => j !== i))} />
          </div>
          <Help>{CHECKS[str(c.type)]?.help}</Help>
          {needId && (
            <Field label="Check id" className="max-w-[320px]"><Text mono value={str(c.id)} onChange={(v) => setAt(i, "id", slug(v).replace(/-+/g, "-"))} /></Field>
          )}
          <CheckFields check={c} ctx={ctx} set={(k, v) => setAt(i, k, v)} />
          <Field label="Why it matters (optional)"><Text value={str(c.description)} onChange={(v) => setAt(i, "description", v)} /></Field>
        </div>
      ))}
      <div>
        <Button size="sm" icon="plus" data-add-check={prefix} onClick={() => onChange([...checks, { type: "said", ...(needId ? { id: `check-${checks.length + 1}` } : {}) }])}>Add check</Button>
      </div>
    </div>
  );
}

export function ScenarioForm({ detail, file, initial, onDone }: { detail: ProfileDetail; file: string | null; initial: Obj | null; onDone: (file?: string) => void }) {
  const toast = useToast();
  const pid = str(detail.merged.id);
  const blank: Obj = {
    id: "", title: "", profile: pid, criticality: "critical", style: "simulated",
    caller: { persona: detail.personas[0] || "", goal: "", facts: {} },
  };
  const [doc, setDoc] = useState<Obj>(initial || blank);
  const [saved, setSaved] = useState(JSON.stringify(initial || blank));
  const [busy, setBusy] = useState(false);
  const [problems, setProblems] = useState<string[]>([]);
  const dirty = JSON.stringify(doc) !== saved;
  const put = (path: (string | number)[], v: Json | undefined) => setDoc((d) => setIn(d, path, v));
  const at = (...p: (string | number)[]) => getIn(doc, p);

  const tools = asList<Obj>(detail.merged.tools);
  const ctx: Ctx = {
    tools: tools.map((t) => str(t.name)),
    writeTools: tools.filter((t) => t.effect === "write").map((t) => str(t.name)),
    probes: asList<Obj>(asObj(detail.merged.backend).probes).map((p) => str(p.id)),
    facts: Object.keys(asObj(at("caller", "facts"))),
  };
  const scripted = doc.style === "scripted";
  const outcome = asObj(doc.outcome);
  const hasOutcome = !!doc.outcome;
  const modes = asList<string>(doc.modes);
  const voiceOn = !modes.length || modes.includes("voice");

  const save = async () => {
    const title = str(doc.title);
    if (!title.trim()) { toast("Give the scenario a title", "bad"); return; }
    const id = str(doc.id) || `${pid}.${slug(title)}`;
    const target = file || `scenarios/${slug(id.replace(`${pid}.`, "")) || "scenario"}.yaml`;
    const out = { ...doc, id, profile: pid };
    setBusy(true);
    try {
      const r = await setupApi.saveDoc(detail.project, target, out, !file);
      if (!r.saved) { setProblems(r.problems); toast("Not saved: see the problems below", "bad"); return; }
      setProblems([]);
      setDoc(out);
      setSaved(JSON.stringify(out));
      toast(`Saved ${target}`);
      onDone(target);
    } catch (e) { toast((e as Error).message, "bad"); } finally { setBusy(false); }
  };

  const faults = asList<Obj>(at("setup", "faults"));
  const setFault = (i: number, k: string, v: Json | undefined) => put(["setup", "faults"], faults.map((f, j) => (j === i ? setIn(f, [k], v) : f)));
  const barge = asList<Obj>(at("audio", "barge_in"));
  const setBarge = (i: number, k: string, v: Json | undefined) => put(["audio", "barge_in"], barge.map((f, j) => (j === i ? setIn(f, [k], v) : f)));
  const turns = asList<Obj>(doc.turns);

  return (
    <Card>
      <Block title={file ? str(doc.title) || file : "New scenario"} sub="One situation to test. Each scenario runs several times (repeats); critical ones must pass every time.">
        <Grid>
          <Field label="Title"><Text id="scenario-title" value={str(doc.title)} onChange={(v) => put(["title"], v)} placeholder="Customer cancels an order that has already shipped" /></Field>
          <Field label="Id" hint={file ? "Fixed once saved." : `Made from the title: ${pid}.<title>`}>
            <Text mono value={str(doc.id)} disabled={!!file} placeholder={`${pid}.${slug(str(doc.title)) || "..."}`} onChange={(v) => put(["id"], v.toLowerCase().replace(/[^a-z0-9._-]/g, ""))} />
          </Field>
        </Grid>
        <Field label="Description (optional)"><Area value={str(doc.description)} rows={2} onChange={(v) => put(["description"], v)} /></Field>
        <div className="flex flex-wrap gap-x-6 gap-y-3 items-end">
          <Field label="How much it matters">
            <Seg value={str(doc.criticality) || "critical"} onChange={(v) => put(["criticality"], v)}
              options={[{ value: "critical", label: "Critical (gates release)" }, { value: "normal", label: "Normal" }, { value: "exploratory", label: "Exploratory" }]} />
          </Field>
          <Field label="Style">
            <Seg id="scenario-style" value={scripted ? "scripted" : "simulated"} onChange={(v) => {
              if (v === "scripted") setDoc((d) => ({ ...setIn(d, ["style"], "scripted"), turns: d.turns || [{ caller: "", expect: [] }] }));
              else setDoc((d) => setIn(setIn(d, ["style"], "simulated"), ["turns"], undefined));
            }} options={[{ value: "simulated", label: "Simulated caller (AI)" }, { value: "scripted", label: "Scripted lines" }]} />
          </Field>
          <Field label="Runs in">
            <div className="flex gap-3 h-9 items-center">
              {(["text", "voice"] as const).map((m) => (
                <Check key={m} label={m === "text" ? "Text runs" : "Voice runs"} checked={!modes.length || modes.includes(m)} onChange={(on) => {
                  const cur = modes.length ? modes : ["text", "voice"];
                  const next = on ? [...new Set([...cur, m])] : cur.filter((x) => x !== m);
                  put(["modes"], next.length === 2 || !next.length ? undefined : next);
                }} />
              ))}
            </div>
          </Field>
          <Field label="Repeats" hint="Empty: the profile's."><Num value={doc.repeats as number} min={1} onChange={(v) => put(["repeats"], v)} /></Field>
        </div>
        <Field label="Tags (optional)"><Csv value={asList<string>(doc.tags)} onChange={(v) => put(["tags"], v.length ? v : undefined)} placeholder="booking, happy-path" /></Field>
      </Block>

      <Block title="The caller" sub={scripted ? "Fixed lines, each followed by checks on the agent's reply. Facts are the personal values the lines contain (so they are masked)." : "An AI caller with a persona, a goal and a fact sheet has a free conversation with the agent."}>
        {!scripted && (
          <>
            <Grid>
              <Field label="Persona"><Select id="scenario-persona" value={str(at("caller", "persona"))} options={detail.personas} placeholder="Pick a persona" onChange={(v) => put(["caller", "persona"], v)} /></Field>
              <Field label="Most caller turns" hint="Empty: the profile's limit."><Num value={at("caller", "max_turns") as number} min={1} onChange={(v) => put(["caller", "max_turns"], v)} /></Field>
            </Grid>
            <Field label="Goal" hint="What the caller wants, in plain words."><Area id="scenario-goal" rows={2} value={str(at("caller", "goal"))} onChange={(v) => put(["caller", "goal"], v)} placeholder="Cancel order 4417 and get a refund to the original card." /></Field>
          </>
        )}
        <Field label="Facts the caller knows" hint="The caller uses only these. Checks can refer to them as $name, and probes as :name.">
          <KeyValue id="scenario-facts" value={asObj(at("caller", "facts"))} onChange={(v) => put(["caller", "facts"], v)} keyPh="full_name" valPh="Maria Lopez" />
        </Field>
        {!scripted && (
          <>
            <Field label="Extra rules for the caller (optional)"><StrList value={asList<string>(at("caller", "rules"))} onChange={(v) => put(["caller", "rules"], v.length ? v : undefined)} placeholder="Do not give your date of birth until asked." /></Field>
            <Field label="End the call when"><Text value={str(at("caller", "end_when"))} onChange={(v) => put(["caller", "end_when"], v)} placeholder="The agent confirms the cancellation, or says it can't." /></Field>
          </>
        )}
        {scripted && (
          <div className="grid gap-3">
            {turns.map((t, i) => (
              <div key={i} className="grid gap-2.5 p-3 border border-line rounded-xl">
                <div className="flex items-center gap-2">
                  <Chip tone="caller">Line {i + 1}</Chip>
                  <span className="flex-1" />
                  <Button size="sm" variant="ghost" iconOnly icon="x" aria-label="Remove line" onClick={() => put(["turns"], turns.filter((_, j) => j !== i))} />
                </div>
                <Field label="Caller says"><Text value={str(t.caller)} onChange={(v) => put(["turns"], turns.map((x, j) => (j === i ? { ...x, caller: v } : x)))} /></Field>
                <Field label="Then check the agent's reply">
                  <CheckList checks={asList<Obj>(t.expect)} ctx={ctx} needId={false} prefix={`turn${i + 1}.`}
                    onChange={(c) => put(["turns"], turns.map((x, j) => (j === i ? { ...x, expect: c.length ? c : undefined } as Obj : x)))} />
                </Field>
              </div>
            ))}
            <div><Button size="sm" icon="plus" onClick={() => put(["turns"], [...turns, { caller: "" }])}>Add line</Button></div>
          </div>
        )}
      </Block>

      <Block title="What must happen (outcome)" sub="The heart of a test: what the agent said vs what its tools did vs what the backend stored."
        aside={<Check checked={hasOutcome} label="Check a business action" onChange={(on) => put(["outcome"], on ? {
          expected: "success", action: { tool: ctx.writeTools[0] || "" }, claim: { method: "patterns", success_patterns: SUCCESS, failure_patterns: FAILURE },
        } : undefined)} />}>
        {hasOutcome ? (
          <>
            <Grid>
              <Field label="Expected result">
                <Select value={str(outcome.expected)} onChange={(v) => put(["outcome", "expected"], v)} options={[
                  ["success", "Success: the action happens and the agent says so"],
                  ["honest_failure", "Honest failure: it can't succeed, and the agent must say so"],
                  ["no_action", "No action: nothing may be written"]]} />
              </Field>
              <Field label="The action (a write tool)"><Select value={str(getIn(outcome, ["action", "tool"]))} options={ctx.writeTools} placeholder="Pick a write tool" onChange={(v) => put(["outcome", "action", "tool"], v)} /></Field>
            </Grid>
            <Field label="Called with" hint="Expected argument values. $fact means a caller fact.">
              <KeyValue value={asObj(getIn(outcome, ["action", "args_match"]))} typed onChange={(v) => put(["outcome", "action", "args_match"], Object.keys(v).length ? v : undefined)} keyPh="argument" valPh="$doctor" />
            </Field>
            <Grid>
              <Field label="Record in the backend (probe)"><Select value={str(getIn(outcome, ["record", "probe"]))} options={ctx.probes} placeholder="No backend check"
                onChange={(v) => put(["outcome", "record"], v ? { ...asObj(outcome.record), probe: v } : undefined)} /></Field>
              {!!getIn(outcome, ["record", "probe"]) && (
                <Field label="Number of records"><Num value={getIn(outcome, ["record", "expect_count"]) as number} min={0} onChange={(v) => put(["outcome", "record", "expect_count"], v)} /></Field>
              )}
            </Grid>
            {!!getIn(outcome, ["record", "probe"]) && (
              <Field label="Record fields must be"><KeyValue value={asObj(getIn(outcome, ["record", "expect_fields"]))} typed keyPh="column" valPh="$full_name or booked"
                onChange={(v) => put(["outcome", "record", "expect_fields"], Object.keys(v).length ? v : undefined)} /></Field>
            )}
            <Grid>
              <Field label='The agent said it worked if it matches' hint="Regular expressions on its last replies.">
                <StrList mono value={asList<string>(getIn(outcome, ["claim", "success_patterns"]))} onChange={(v) => put(["outcome", "claim", "success_patterns"], v)} />
              </Field>
              <Field label="The agent said it failed if it matches">
                <StrList mono value={asList<string>(getIn(outcome, ["claim", "failure_patterns"]))} onChange={(v) => put(["outcome", "claim", "failure_patterns"], v)} />
              </Field>
            </Grid>
          </>
        ) : <Help>No business action: only the checks below are used.</Help>}
      </Block>

      <Block title="Checks" sub="Hard checks on the whole call. Each one cites the events it rests on.">
        <CheckList checks={asList<Obj>(doc.checks)} ctx={ctx} needId prefix="" onChange={(c) => put(["checks"], c.length ? c : undefined)} />
      </Block>

      <Block title="Before the call" sub="Fixed time, test data, and faults Vocto injects into the agent's tools.">
        <Grid>
          <Field label='"Now" for the agent and caller' hint='So "tomorrow" means the same day in every run. ISO time, e.g. 2026-11-02T09:00:00+00:00.'>
            <Text mono value={str(at("setup", "now"))} onChange={(v) => put(["setup", "now"], v)} placeholder="2026-11-02T09:00:00+00:00" />
          </Field>
          <Field label="Test data (SQL files or statements)" hint="Loaded before every call. SQL backends only; use a test database.">
            <StrList mono value={asList<string>(at("setup", "fixtures"))} onChange={(v) => put(["setup", "fixtures"], v.length ? v : undefined)} placeholder="fixtures/base.sql" />
          </Field>
        </Grid>
        <Field label="Faults" hint="Honesty under failure: the tool breaks and the agent must not pretend it worked.">
          <div className="grid gap-2">
            {faults.map((f, i) => (
              <div key={i} className="flex gap-2 flex-wrap items-end p-2.5 border border-line rounded-xl">
                <Field label="Tool"><Select value={str(f.tool)} options={ctx.tools} placeholder="Pick" onChange={(v) => setFault(i, "tool", v)} /></Field>
                <Field label="Fault"><Select value={str(f.type)} onChange={(v) => setFault(i, "type", v)} options={[["timeout", "Times out"], ["http_error", "HTTP error"], ["slow", "Slow"], ["malformed_response", "Garbled answer"]]} /></Field>
                <Field label="On call no." className="w-[110px]"><Num value={f.on_call as number} min={1} placeholder="every" onChange={(v) => setFault(i, "on_call", v)} /></Field>
                {f.type === "http_error" && <Field label="Status" className="w-[100px]"><Num value={f.status as number} placeholder={500} onChange={(v) => setFault(i, "status", v)} /></Field>}
                {(f.type === "slow" || f.type === "timeout") && <Field label="Delay (ms)" className="w-[120px]"><Num value={f.delay_ms as number} placeholder={10000} onChange={(v) => setFault(i, "delay_ms", v)} /></Field>}
                <div className="pb-2"><Check checked={!!f.forward} onChange={(v) => setFault(i, "forward", v || undefined)} label="Still reaches the backend" /></div>
                <Button size="sm" variant="ghost" iconOnly icon="x" aria-label="Remove fault" onClick={() => put(["setup", "faults"], faults.filter((_, j) => j !== i).length ? faults.filter((_, j) => j !== i) : undefined)} />
              </div>
            ))}
            <div><Button size="sm" icon="plus" onClick={() => put(["setup", "faults"], [...faults, { tool: ctx.writeTools[0] || ctx.tools[0] || "", type: "timeout" }])}>Add fault</Button></div>
          </div>
        </Field>
      </Block>

      {voiceOn && (
        <Block title="Audio (voice runs)" sub="A harder line: background noise, and the caller cutting in.">
          <Grid cols={3}>
            <Field label="Background noise"><Select value={str(at("audio", "noise")) || "none"} options={["none", "office", "street", "cafe", "car"]} onChange={(v) => put(["audio", "noise"], v === "none" ? undefined : v)} /></Field>
            <Field label="Speech-to-noise (dB)" hint="Lower is noisier. Default 15."><Num value={at("audio", "snr_db") as number} placeholder={15} onChange={(v) => put(["audio", "snr_db"], v)} /></Field>
          </Grid>
          <Field label="Caller cuts in">
            <div className="grid gap-2">
              {barge.map((b, i) => (
                <div key={i} className="flex gap-2 flex-wrap items-end p-2.5 border border-line rounded-xl">
                  <Field label="Agent's turn no." className="w-[120px]"><Num value={b.agent_turn as number} min={1} onChange={(v) => setBarge(i, "agent_turn", v)} /></Field>
                  <Field label="After (ms)" className="w-[110px]"><Num value={b.offset_ms as number} min={0} onChange={(v) => setBarge(i, "offset_ms", v)} /></Field>
                  <Field label="Caller says" className="flex-1 min-w-[180px]"><Text value={str(b.say)} onChange={(v) => setBarge(i, "say", v)} /></Field>
                  <Field label="Must stop within (ms)" className="w-[150px]"><Num value={b.stop_within_ms as number} placeholder={1000} onChange={(v) => setBarge(i, "stop_within_ms", v)} /></Field>
                  <Button size="sm" variant="ghost" iconOnly icon="x" aria-label="Remove" onClick={() => put(["audio", "barge_in"], barge.filter((_, j) => j !== i).length ? barge.filter((_, j) => j !== i) : undefined)} />
                </div>
              ))}
              <div><Button size="sm" icon="plus" onClick={() => put(["audio", "barge_in"], [...barge, { agent_turn: 2, offset_ms: 700, say: "Sorry, can I just ask something?" }])}>Add cut-in</Button></div>
            </div>
          </Field>
        </Block>
      )}

      <Problems items={problems} title="Not saved. Problems to fix:" />
      <SaveBar id="save-scenario" dirty={dirty || !file} busy={busy} onSave={() => void save()} onRevert={() => onDone()} revertLabel="Back to the list" />
    </Card>
  );
}
