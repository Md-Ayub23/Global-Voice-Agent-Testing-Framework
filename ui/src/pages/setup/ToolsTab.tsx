// The agent's tools (its APIs) and the tool gateway its tool calls go through during tests.
import { useCopy } from "../../components/feedback";
import { Block, Csv, Grid, Help, KeyValue, Num, Problems, SaveBar, Select, Text, type Obj, type Patch } from "../../components/form";
import { Button, Card, Cmd, Field, Notice, Seg } from "../../components/ui";
import { owner } from "../../lib/setup";
import { asList, asObj, str, useDraft, type TabProps } from "./common";

export function ToolsTab({ detail, reload }: TabProps) {
  const copy = useCopy();
  const file = owner(detail, "tools");
  const d = useDraft({ tools: asList(detail.merged.tools), tool_gateway: asObj(detail.merged.tool_gateway) }, file, detail.project, reload);
  const tools = asList<Obj>(d.draft.tools);
  const gw = asObj(d.draft.tool_gateway);
  const setTool = (i: number, patch: Patch) => {
    const next = tools.map((t, j) => {
      if (j !== i) return t;
      const out = { ...t, ...patch } as Obj;
      for (const [k, v] of Object.entries(out)) if (v === undefined || v === "" || (Array.isArray(v) && !v.length)) delete out[k];
      return out;
    });
    d.setDraft({ ...d.draft, tools: next });
  };
  const port = Number(gw.port ?? detail.gateway.port);
  const host = str(gw.host) || detail.gateway.host;
  const urlFor = (name: string) => `http://${host}:${port}/tools/${name || "<name>"}`;
  const inRepo = !detail.gateway.used && (asObj(detail.merged.connection).type === "text_python" || !!asObj(detail.merged.connection).launch);

  return (
    <Card>
      <Block title="The agent's tools" sub={`The APIs the agent calls during a conversation. Write tools (bookings, payments, changes) are what the outcome check follows.`}>
        {tools.length === 0 && <Notice>No tools yet. Add each API the agent can call: Vocto records every call, can break it on purpose (faults), and checks it against the backend.</Notice>}
        {tools.map((t, i) => (
          <div key={`${i}-${d.version}`} data-tool={str(t.name)} className="grid gap-3 p-3.5 border border-line rounded-xl bg-surface-2/40">
            <div className="flex items-center gap-2 flex-wrap">
              <b className="font-mono">{str(t.name) || "new tool"}</b>
              <Seg value={str(t.effect) || "read"} onChange={(v) => setTool(i, { effect: v })}
                options={[{ value: "read", label: "Read (looks up)" }, { value: "write", label: "Write (changes data)" }]} />
              <span className="flex-1" />
              <Button size="sm" variant="ghost" icon="x" onClick={() => d.setDraft({ ...d.draft, tools: tools.filter((_, j) => j !== i) })}>Remove</Button>
            </div>
            <Grid>
              <Field label="Name" hint="The name the agent uses for it."><Text mono value={str(t.name)} onChange={(v) => setTool(i, { name: v.trim() })} /></Field>
              <Field label="What it does"><Text value={str(t.description)} onChange={(v) => setTool(i, { description: v })} /></Field>
            </Grid>
            <Grid>
              <Field label="Real API address (upstream)" hint="Where Vocto forwards the call: the agent's real (test) API.">
                <Text mono value={str(t.upstream)} placeholder="https://api.example.com/bookings" onChange={(v) => setTool(i, { upstream: v })} />
              </Field>
              <Field label="Method" hint="GET sends the arguments as the query string.">
                <Select value={str(t.method) || "POST"} options={["POST", "GET", "PUT", "PATCH", "DELETE"]} onChange={(v) => setTool(i, { method: v === "POST" ? undefined : v })} />
              </Field>
            </Grid>
            <Grid>
              <Field label="Same action if these arguments match" hint="Two successful calls with the same values = DUPLICATE_ACTION. e.g. patient_id, slot_start">
                <Csv value={asList<string>(t.idempotency_key)} onChange={(v) => setTool(i, { idempotency_key: v })} />
              </Field>
              <Field label="Personal-data arguments" hint="Masked in saved evidence. e.g. full_name, date_of_birth">
                <Csv value={asList<string>(t.pii_args)} onChange={(v) => setTool(i, { pii_args: v })} />
              </Field>
            </Grid>
            <Field label="Headers for the real API" hint="e.g. Authorization = Bearer ${ACME_API_TOKEN}; set its value in Settings › Secrets.">
              <KeyValue value={asObj(t.headers)} onChange={(v) => setTool(i, { headers: Object.keys(v).length ? v : undefined })} keyPh="Header" valPh="Value or ${SECRET_NAME}" />
            </Field>
            {detail.gateway.used && str(t.name) && (
              <Cmd action={<Button size="sm" variant="ghost" iconOnly icon="copy" aria-label="Copy" onClick={() => void copy(urlFor(str(t.name)))} />}>{urlFor(str(t.name))}</Cmd>
            )}
          </div>
        ))}
        <div><Button id="add-tool" icon="plus" onClick={() => d.setDraft({ ...d.draft, tools: [...tools, { name: "", effect: "read" }] })}>Add tool</Button></div>
      </Block>

      <Block title="Tool gateway" sub="How Vocto sees the tool calls of an agent that runs somewhere else.">
        {inRepo ? (
          <Help>This agent is started by Vocto and calls its tools through Vocto directly, so it needs no gateway.</Help>
        ) : (
          <>
            <Help>
              For test runs, point the agent's tool calls at Vocto instead of at the real API (one setting in the agent's test configuration).
              Vocto records each call, adds the test id header, injects the scenario's faults and forwards it to the real API above.
              The agent gets the real answer back. It runs while a test run is going.
            </Help>
            <Grid>
              <Field label="Gateway port" hint="On this computer. Change it if 8790 is in use."><Num value={gw.port as number} placeholder={8790} min={1} max={65535}
                onChange={(v) => d.setDraft({ ...d.draft, tool_gateway: v ? { ...gw, port: v } : (() => { const g = { ...gw }; delete g.port; return g; })() })} /></Field>
              <Field label="Base address to give the agent">
                <Cmd action={<Button size="sm" variant="ghost" iconOnly icon="copy" aria-label="Copy" onClick={() => void copy(`http://${host}:${port}/tools`)} />}>{`http://${host}:${port}/tools/<tool name>`}</Cmd>
              </Field>
            </Grid>
            <Help>An agent in the cloud can't reach this computer directly: use a tunnel (for example ngrok or cloudflared) to this port, or run Vocto next to the agent.</Help>
          </>
        )}
      </Block>
      <Problems items={d.problems} />
      <SaveBar id="save-tools" dirty={d.dirty} busy={d.busy} onSave={() => void d.save()} onRevert={d.revert}
        note={file !== detail.file ? "Shared by the voice and text setups of this project." : undefined} />
    </Card>
  );
}
