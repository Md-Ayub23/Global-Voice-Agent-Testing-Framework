// The agent and how to reach it: name, domain, its address (voice or text), headers, test id.
import { useState } from "react";
import { Area, Block, Check, Grid, Help, KeyValue, Num, Problems, SaveBar, Select, Text, TestResult, type Obj, type TestOutcome } from "../../components/form";
import { Button, Card, Choice, Field, Notice } from "../../components/ui";
import { owner, setupApi } from "../../lib/setup";
import { asList, asObj, str, useDraft, type TabProps } from "./common";

const DOMAINS = ["healthcare", "banking", "insurance", "trading", "retail", "travel", "telecom", "support", "general"];
type Kind = "websocket_audio" | "text_http" | "in_repo";

function Identity({ detail, reload }: TabProps) {
  const file = owner(detail, "name");
  const src = file === detail.file ? detail.raw : detail.base;
  const d = useDraft({ name: str(src.name), description: str(src.description), domain: str(src.domain) }, file, detail.project, reload);
  return (
    <Card>
      <Block title="The agent" sub={file !== detail.file ? "Shared by the voice and text setups of this project." : undefined}>
        <Grid>
          <Field label="Name"><Text id="agent-name" value={d.draft.name as string} onChange={(v) => d.setDraft({ ...d.draft, name: v })} /></Field>
          <Field label="Industry" hint="Shown on the dashboard as the type of agent.">
            <>
              <input list="domains" className="h-9 w-full px-[11px] rounded-[9px] border border-line-strong bg-surface text-ink outline-none font-normal focus:border-accent"
                value={d.draft.domain as string} onChange={(e) => d.setDraft({ ...d.draft, domain: e.target.value })} />
              <datalist id="domains">{DOMAINS.map((x) => <option key={x} value={x} />)}</datalist>
            </>
          </Field>
        </Grid>
        <Field label="What it does"><Area value={d.draft.description as string} onChange={(v) => d.setDraft({ ...d.draft, description: v })} rows={2} /></Field>
      </Block>
      <Problems items={d.problems} />
      <SaveBar id="save-identity" dirty={d.dirty} busy={d.busy} onSave={() => void d.save()} onRevert={d.revert} />
    </Card>
  );
}

function kindOf(conn: Obj): Kind {
  if (conn.type === "text_python" || conn.launch) return "in_repo";
  return conn.type === "text_http" ? "text_http" : "websocket_audio";
}
const VOICE_FORMATS: [string, string][] = [["twilio_media_stream", "Twilio Media Streams (phone agents)"], ["websocket_audio", "Vocto voice protocol (PCM16)"]];

function Connection({ detail, reload }: TabProps) {
  const conn0 = asObj(detail.merged.connection);
  const tid0 = asObj(detail.merged.test_id);
  const d = useDraft({ connection: conn0, test_id: tid0 }, detail.file, detail.project, reload);
  const [test, setTest] = useState<TestOutcome | null>(null);
  const [testing, setTesting] = useState(false);
  const conn = asObj(d.draft.connection);
  const options = asObj(conn.options);
  const tid = asObj(d.draft.test_id);
  const kind = kindOf(conn);
  const setConn = (patch: Obj) => d.setDraft({ ...d.draft, connection: { ...conn, ...patch } });
  const setOpt = (key: string, value: unknown) => {
    const next = { ...options } as Obj;
    if (value === undefined || value === "") delete next[key]; else next[key] = value as Obj[string];
    setConn({ options: next });
  };
  const prop = (Array.isArray(tid.propagation) ? tid.propagation : ["proxy_header"]) as string[];
  const toggleProp = (p: string, on: boolean) =>
    d.setDraft({ ...d.draft, test_id: { ...tid, propagation: on ? [...new Set([...prop, p])] : prop.filter((x) => x !== p) } });
  const otherOptions = Object.fromEntries(Object.entries(options).filter(([k, v]) =>
    !["headers", "reply_field", "end_field", "timeout_s", "account_sid", "jitter_buffer_ms"].includes(k) && (v == null || typeof v !== "object")));
  const twilio = conn.type === "twilio_media_stream";

  const runTest = async () => {
    if (d.dirty && !(await d.save())) return;
    setTesting(true);
    try { setTest(await setupApi.testConnection(detail.project, detail.file)); }
    catch (e) { setTest({ ok: false, message: (e as Error).message }); }
    finally { setTesting(false); }
  };

  const setFormat = (type: string) => setConn(type === "twilio_media_stream"
    ? { type, audio: undefined as never }
    : { type, audio: { sample_rate_hz: 16000, encoding: "pcm16" } });
  const pick = (k: Kind) => {
    if (k === kind) return;
    if (k === "websocket_audio") setConn({ type: "twilio_media_stream", launch: undefined as never, target: "", audio: undefined as never });
    if (k === "text_http") setConn({ type: "text_http", launch: undefined as never, target: "" });
  };
  const clean = (c: Obj): Obj => Object.fromEntries(Object.entries(c).filter(([, v]) => v !== undefined));

  return (
    <Card>
      <Block title="How Vocto reaches the agent" sub="The address Vocto calls, and how it talks to the agent.">
        <div className="grid gap-2 sm:grid-cols-3">
          <Choice radio on={kind === "websocket_audio"} onClick={() => pick("websocket_audio")} data-kind="websocket_audio">
            <b>Voice (phone-like)</b><div className="text-muted text-[12.5px]">Audio over a WebSocket. The caller really speaks and listens.</div>
          </Choice>
          <Choice radio on={kind === "text_http"} onClick={() => pick("text_http")} data-kind="text_http">
            <b>Text over HTTP</b><div className="text-muted text-[12.5px]">Fast, cheap runs against the same agent: no speech.</div>
          </Choice>
          <Choice radio on={kind === "in_repo"} off={kind !== "in_repo"} data-kind="in_repo" title="For Vocto's own practice agents">
            <b>Built into Vocto</b><div className="text-muted text-[12.5px]">Vocto's own practice agents, run inside Vocto.</div>
          </Choice>
        </div>
        {kind === "in_repo" ? (
          <Grid>
            <Field label={conn.launch ? "Launch (module:function)" : "Agent (module:function)"}>
              <Text mono value={str(conn.launch || conn.target)} onChange={(v) => setConn(conn.launch ? { launch: v } : { target: v })} />
            </Field>
            <Help className="self-end">Vocto starts this agent itself for every call and passes it the tool proxy directly.</Help>
          </Grid>
        ) : (
          <>
            {kind === "websocket_audio" && (
              <Field label="Voice format" hint={twilio ? "Vocto plays Twilio: connected, start (with your custom parameters), 20 ms μ-law audio, marks and clear. Nothing in the agent changes."
                : "The agent speaks Vocto's own small protocol."}>
                <Select id="voice-format" value={str(conn.type)} options={VOICE_FORMATS} onChange={setFormat} />
              </Field>
            )}
            <Field label={kind === "websocket_audio" ? "Agent's voice address" : "Agent's text address"}
              hint={kind === "websocket_audio" ? (twilio ? "ws:// or wss://: the url of the <Stream> in the agent's TwiML. The details are in docs/twilio-protocol.md."
                : "ws:// or wss://. The protocol is in docs/voice-protocol.md (start message, 20 ms PCM16 frames both ways).")
                : "http:// or https://. The protocol is in docs/text-http-protocol.md (start / message / stop, JSON)."}>
              <Text id="conn-target" mono value={str(conn.target)} placeholder={kind === "websocket_audio" ? "wss://agent.example.com/call" : "https://agent.example.com/vocto"}
                onChange={(v) => setConn({ target: v })} />
            </Field>
            {twilio && (
              <>
                <Field label="Custom parameters" hint="What the agent's TwiML would pass as <Parameter>s; the agent reads them from the start message. Vocto adds test_id and the scenario's time itself.">
                  <KeyValue key={`p${d.version}`} value={asObj(options.custom_parameters)} onChange={(v) => setOpt("custom_parameters", Object.keys(v).length ? v : undefined)} keyPh="Parameter" valPh="Value or ${SECRET_NAME}" />
                </Field>
                <Grid>
                  <Field label="Account SID" hint="Optional: only if the agent checks it. Usually ${TWILIO_ACCOUNT_SID}.">
                    <Text mono value={str(options.account_sid)} placeholder="AC… (made up when empty)" onChange={(v) => setOpt("account_sid", v)} />
                  </Field>
                  <Field label="Jitter buffer (ms)" hint="Agent audio that arrives this late still counts as one unbroken sentence. Default 60.">
                    <Num value={options.jitter_buffer_ms as number} placeholder={60} onChange={(v) => setOpt("jitter_buffer_ms", v)} />
                  </Field>
                </Grid>
              </>
            )}
            {kind === "websocket_audio" && !twilio && (
              <Grid>
                <Field label="Sample rate">
                  <Select value={String(asObj(conn.audio).sample_rate_hz ?? 16000)} options={["8000", "16000", "24000", "48000"]}
                    onChange={(v) => setConn({ audio: { ...asObj(conn.audio), sample_rate_hz: Number(v), encoding: "pcm16" } })} />
                </Field>
                <Field label="Encoding" hint="PCM16 mono little-endian."><Text value="pcm16" onChange={() => {}} disabled /></Field>
              </Grid>
            )}
            {kind === "text_http" && (
              <Grid cols={3}>
                <Field label="Reply field" hint='Where the words are in its answer. Default "reply".'>
                  <Text mono value={str(options.reply_field)} placeholder="reply" onChange={(v) => setOpt("reply_field", v)} />
                </Field>
                <Field label="Hang-up field" hint='Default "end".'><Text mono value={str(options.end_field)} placeholder="end" onChange={(v) => setOpt("end_field", v)} /></Field>
                <Field label="Wait for an answer (s)"><Num value={options.timeout_s as number} placeholder={60} onChange={(v) => setOpt("timeout_s", v)} /></Field>
              </Grid>
            )}
            <Field label="Headers sent to the agent" hint="e.g. Authorization = Bearer ${ACME_AGENT_TOKEN}. Write a secret as ${NAME} and set its value in Settings › Secrets.">
              <KeyValue key={`h${d.version}`} value={asObj(options.headers)} onChange={(v) => setOpt("headers", Object.keys(v).length ? v : undefined)} keyPh="Header" valPh="Value or ${SECRET_NAME}" />
            </Field>
          </>
        )}
        {Object.keys(otherOptions).length > 0 && (
          <Field label="Other agent options" hint="Passed to the agent as connection.options.">
            <KeyValue key={`o${d.version}`} value={otherOptions} typed onChange={(v) => setConn({ options: { ...Object.fromEntries(Object.entries(options).filter(([k]) => !(k in otherOptions))), ...v } })} />
          </Field>
        )}
        <div className="flex items-center gap-2 flex-wrap">
          <Button id="test-connection" icon="phone" busy={testing} disabled={testing} onClick={() => void runTest()}>Test connection</Button>
          <Help>Saves, then opens one short session the way a test call does{kind === "websocket_audio" ? " and listens for the greeting" : ""}.</Help>
        </div>
        <TestResult result={test} />
      </Block>

      <Block title="Linking calls to tool calls and records" sub="Every test call has a test id. The more places it reaches, the more exactly Vocto can find what the call did.">
        <Check checked={prop.includes("proxy_header")} onChange={(v) => toggleProp("proxy_header", v)}
          label="Tool calls carry it as a header" hint={`Vocto's tool gateway adds ${str(tid.header_name) || "X-Test-Id"} to every request it forwards to the agent's tools, so the backend can store it.`} />
        <Check checked={prop.includes("session_metadata")} onChange={(v) => toggleProp("session_metadata", v)}
          label="The agent is told it when the call starts" hint="Sent in the start message's metadata (test_id), so the agent can write it in its logs." />
        <Field label="Header name" className="max-w-[320px]">
          <Text mono value={str(tid.header_name)} placeholder="X-Test-Id" onChange={(v) => d.setDraft({ ...d.draft, test_id: { ...tid, header_name: v } })} />
        </Field>
      </Block>
      {!str(conn.target) && kind !== "in_repo" && <Notice tone="warn">The agent's address is not set yet: runs can't start until it is.</Notice>}
      <Problems items={d.problems} />
      <SaveBar id="save-connection" dirty={d.dirty} busy={d.busy} onSave={() => void d.save({ connection: clean(conn), test_id: d.draft.test_id })} onRevert={d.revert} />
    </Card>
  );
}

// Starting the agent (agent_process) and the bugs planted in it, for agents that run on this computer.
function StartAgent({ detail, reload }: TabProps) {
  const file = owner(detail, "agent_process");
  const src = file === detail.file ? detail.raw : detail.base;
  const ap0 = asObj(src.agent_process);
  const d = useDraft({
    on: !!src.agent_process,
    command: asList<string>(ap0.command).join("\n"), cwd: str(ap0.cwd), ready_url: str(ap0.ready_url),
    ready_timeout_s: (ap0.ready_timeout_s as number | undefined) ?? null, planted_bug_env: str(ap0.planted_bug_env), env: asObj(ap0.env),
    bugs: asList(src.planted_bugs).map((b) => ({ id: str(b.id), title: str(b.title), scenario: str(asObj(b.caught_by).scenario), judge: str(asObj(b.caught_by).judge) })),
  }, file, detail.project, reload);
  const x = d.draft;
  const set = (patch: Partial<typeof x>) => d.setDraft({ ...x, ...patch });
  const bugs = x.bugs as { id: string; title: string; scenario: string; judge: string }[];
  const setBug = (i: number, patch: Partial<(typeof bugs)[number]>) => set({ bugs: bugs.map((b, j) => (j === i ? { ...b, ...patch } : b)) });
  const save = () => d.save(x.on ? {
    agent_process: {
      command: String(x.command).split("\n").map((l) => l.trim()).filter(Boolean), cwd: x.cwd, ready_url: x.ready_url,
      ready_timeout_s: x.ready_timeout_s, planted_bug_env: x.planted_bug_env, env: x.env,
    },
    planted_bugs: bugs.filter((b) => b.id.trim()).map((b) => ({ id: b.id.trim(), title: b.title, caught_by: { scenario: b.scenario, judge: b.judge } })),
  } : { agent_process: null, planted_bugs: null });
  const scenarioIds = detail.scenarios.map((s) => s.id).filter(Boolean) as string[];
  return (
    <Card>
      <Block title="Starting the agent" sub="For an agent that runs on this computer (an open-source agent, or your own on a test machine). Vocto can start it for a run, and the exit check restarts it with each planted bug.">
        <Check checked={!!x.on} onChange={(v) => set({ on: v })} label="Vocto can start and stop this agent"
          hint="Off: the agent is already running somewhere and Vocto only calls it." />
        {x.on && (
          <>
            <Field label="Command" hint="One part per line, as you would type it, run from the Vocto folder. The first line is the program.">
              <Area id="ap-command" mono rows={3} value={x.command as string} onChange={(v) => set({ command: v })} placeholder={"uv\nrun\npython\nagent/server.py"} />
            </Field>
            <Grid cols={3}>
              <Field label="Ready address" hint="Vocto waits until this answers. Default: the agent's address.">
                <Text mono value={x.ready_url as string} placeholder="http://127.0.0.1:7860/health" onChange={(v) => set({ ready_url: v })} />
              </Field>
              <Field label="Wait up to (s)" hint="The first start may download models."><Num value={x.ready_timeout_s as number | null} placeholder={120} onChange={(v) => set({ ready_timeout_s: v ?? null })} /></Field>
              <Field label="Folder" hint="Where it runs. Default: the Vocto folder."><Text mono value={x.cwd as string} placeholder="." onChange={(v) => set({ cwd: v })} /></Field>
            </Grid>
            <Field label="Environment" hint="Extra settings for the agent. Write a secret as ${NAME} and set it in Settings › Secrets.">
              <KeyValue key={`e${d.version}`} value={asObj(x.env)} onChange={(v) => set({ env: v })} keyPh="NAME" valPh="value or ${SECRET}" />
            </Field>
          </>
        )}
      </Block>
      {x.on && (
        <Block title="Planted bugs" sub="Known bugs the agent can start with, one at a time, to prove Vocto catches them. The agent reads the bug's name from the variable below and changes its own code or prompts.">
          <Field label="Variable that names the bug" className="max-w-[360px]">
            <Text mono value={x.planted_bug_env as string} placeholder="AGENT_PLANTED_BUG" onChange={(v) => set({ planted_bug_env: v })} />
          </Field>
          <datalist id="ap-scenarios">{scenarioIds.map((id) => <option key={id} value={id} />)}</datalist>
          {bugs.map((b, i) => (
            <div key={i} className="grid gap-2 grid-cols-1 md:grid-cols-[1fr_2fr_1.4fr_1fr_auto] items-end" data-planted={b.id}>
              <Field label={i ? "" : "Name"}><Text mono value={b.id} placeholder="skip_confirmation" onChange={(v) => setBug(i, { id: v })} /></Field>
              <Field label={i ? "" : "What it does"}><Text value={b.title} placeholder="Places the order without confirming it" onChange={(v) => setBug(i, { title: v })} /></Field>
              <Field label={i ? "" : "Caught by scenario"}>
                <input list="ap-scenarios" className="h-9 w-full px-[11px] rounded-[9px] border border-line-strong bg-surface text-ink outline-none font-mono text-[12.5px] focus:border-accent"
                  value={b.scenario} onChange={(e) => setBug(i, { scenario: e.target.value })} />
              </Field>
              <Field label={i ? "" : "Judge item (optional)"}><Text mono value={b.judge} placeholder="polite-professional-tone" onChange={(v) => setBug(i, { judge: v })} /></Field>
              <Button size="sm" variant="ghost" icon="x" onClick={() => set({ bugs: bugs.filter((_, j) => j !== i) })}>Remove</Button>
            </div>
          ))}
          <div><Button size="sm" icon="plus" id="ap-add-bug" onClick={() => set({ bugs: [...bugs, { id: "", title: "", scenario: "", judge: "" }] })}>Add a planted bug</Button></div>
          <Help>A bug with a judge item is caught when that AI judge flags every repeat (tone, honesty...); without one, its scenario must fail every repeat. Safety tests count too (e.g. safety.secrets.persistent).</Help>
        </Block>
      )}
      <Problems items={d.problems} />
      <SaveBar id="save-agent-process" dirty={d.dirty} busy={d.busy} onSave={() => void save()} onRevert={d.revert} />
    </Card>
  );
}

export function AgentTab(props: TabProps) {
  const conn = asObj(props.detail.merged.connection);
  return (
    <div className="grid gap-4">
      <Identity {...props} />
      <Connection {...props} />
      {!conn.launch && conn.type !== "text_python" && <StartAgent {...props} />}
    </div>
  );
}
