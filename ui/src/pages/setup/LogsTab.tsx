// The agent's own logs: where they are, and which lines belong to a call.
import { useState } from "react";
import { Block, Grid, Help, KeyValue, Num, Problems, SaveBar, Select, Text, TestResult, type Obj, type Patch, type TestOutcome } from "../../components/form";
import { Button, Card, Field, Seg } from "../../components/ui";
import { owner, setupApi } from "../../lib/setup";
import { asObj, str, useDraft, type TabProps } from "./common";

export function LogsTab({ detail, reload }: TabProps) {
  const file = owner(detail, "logs");
  const d = useDraft({ logs: asObj(detail.merged.logs) }, file, detail.project, reload);
  const [result, setResult] = useState<TestOutcome | null>(null);
  const [busy, setBusy] = useState(false);
  const logs = asObj(d.draft.logs);
  const source = str(logs.source) || "none";
  const set = (patch: Patch) => {
    const next = { ...logs, ...patch } as Obj;
    for (const [k, v] of Object.entries(next)) if (v === undefined || v === "") delete next[k];
    d.setDraft({ logs: next });
  };
  const test = async () => {
    if (d.dirty && !(await d.save())) return;
    setBusy(true);
    try { setResult(await setupApi.testLogs(detail.project, file)); }
    catch (e) { setResult({ ok: false, message: (e as Error).message }); }
    finally { setBusy(false); }
  };

  return (
    <Card>
      <Block title="The agent's logs" sub={`After every call Vocto keeps the agent's log lines for that call next to the conversation and tool calls, and scenarios can check them ("must not log an ERROR").`}>
        <Seg id="logs-source" value={source} onChange={(v) => set({ source: v })}
          options={[{ value: "none", label: "No logs" }, { value: "file", label: "A log file" }, { value: "http", label: "A log search URL" }]} />
        {source === "file" && (
          <Grid>
            <Field label="Log file" hint="The full path of the agent's log file on this computer.">
              <Text id="logs-path" mono value={str(logs.path)} placeholder="C:\agent\logs\agent.log" onChange={(v) => set({ path: v })} />
            </Field>
            <Field label="Which lines belong to a call">
              <Select value={str(logs.match) || "during_call"} onChange={(v) => set({ match: v === "during_call" ? undefined : v })}
                options={[["during_call", "Lines written while the call ran"], ["test_id", "Only lines containing the test id"]]} />
            </Field>
          </Grid>
        )}
        {source === "http" && (
          <>
            <Field label="Log search URL" hint="Use {test_id}, {started_at} and {ended_at}; Vocto fills them in for each call. Works with most log tools' HTTP search.">
              <Text id="logs-url" mono value={str(logs.url)} placeholder="https://logs.example.com/api/search?q={test_id}&from={started_at}&to={ended_at}" onChange={(v) => set({ url: v })} />
            </Field>
            <Field label="Headers" hint="e.g. Authorization = Bearer ${LOGS_TOKEN}.">
              <KeyValue key={`lh${d.version}`} value={asObj(logs.headers)} onChange={(v) => set({ headers: Object.keys(v).length ? v : undefined })} keyPh="Header" valPh="Value or ${SECRET_NAME}" />
            </Field>
            <Grid>
              <Field label="Lines field" hint="For a JSON answer: where the list of lines is (e.g. data.results). Empty: the answer itself.">
                <Text mono value={str(logs.lines_field)} onChange={(v) => set({ lines_field: v })} />
              </Field>
              <Field label="Message field" hint='When each line is an object: the text field. Default "message".'>
                <Text mono value={str(logs.message_field)} placeholder="message" onChange={(v) => set({ message_field: v })} />
              </Field>
            </Grid>
          </>
        )}
        {source !== "none" && (
          <>
            <Grid>
              <Field label="Wait after the call (ms)" hint="So the last lines are written before Vocto reads. Log services may need a few seconds.">
                <Num value={logs.settle_ms as number} placeholder={500} min={0} onChange={(v) => set({ settle_ms: v })} />
              </Field>
              <Field label="Keep at most (lines per call)"><Num value={logs.max_lines as number} placeholder={300} min={1} onChange={(v) => set({ max_lines: v })} /></Field>
            </Grid>
            <div className="flex items-center gap-2 flex-wrap">
              <Button id="test-logs" icon="file" busy={busy} disabled={busy} onClick={() => void test()}>Read recent lines</Button>
              <Help>Saves, then reads the last lines (or searches the last 10 minutes).</Help>
            </div>
            <TestResult result={result} />
          </>
        )}
      </Block>
      <Problems items={d.problems} />
      <SaveBar id="save-logs" dirty={d.dirty} busy={d.busy} onSave={() => void d.save()} onRevert={d.revert}
        note={file !== detail.file ? "Shared by the voice and text setups of this project." : undefined} />
    </Card>
  );
}
