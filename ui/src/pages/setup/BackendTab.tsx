// The backend (source of truth) and the probes that read it after each call.
import { useState } from "react";
import { Area, Block, Check, Grid, Help, KeyValue, Problems, SaveBar, Select, Text, TestResult, type Obj, type Patch, type TestOutcome } from "../../components/form";
import { Button, Card, Field, Notice, Seg } from "../../components/ui";
import { binds, owner, setupApi } from "../../lib/setup";
import { asList, asObj, str, useDraft, type TabProps } from "./common";

type Access = "none" | "sql" | "rest";
const PLACEHOLDER: Record<Access, string> = {
  none: "",
  sql: "postgresql://vocto_reader:${PG_PASSWORD}@db.example.com:5432/bookings   or   sqlite:///path/to/file.db",
  rest: "https://api.example.com",
};

function ProbeTest({ detail, probe, query }: { detail: TabProps["detail"]; probe: string; query: string }) {
  const names = binds(query);
  const [values, setValues] = useState<Obj>({});
  const [result, setResult] = useState<TestOutcome | null>(null);
  const [busy, setBusy] = useState(false);
  const file = owner(detail, "backend");
  const run = async () => {
    setBusy(true);
    try { setResult(await setupApi.testBackend(detail.project, file, probe, values)); }
    catch (e) { setResult({ ok: false, message: (e as Error).message }); }
    finally { setBusy(false); }
  };
  return (
    <div className="grid gap-2">
      {names.length > 0 && (
        <Grid cols={3}>
          {names.map((n) => (
            <Field key={n} label={<span className="font-mono">:{n}</span>}>
              <Text value={str(values[n])} placeholder={n === "test_id" ? "t-20261029-0001-ab12" : n.includes("_at") ? "2026-10-29T10:00:00Z" : ""}
                onChange={(v) => setValues({ ...values, [n]: v })} />
            </Field>
          ))}
        </Grid>
      )}
      <div><Button size="sm" icon="db" busy={busy} disabled={busy} data-probe-test={probe} onClick={() => void run()}>Run this probe now</Button></div>
      <TestResult result={result} />
    </div>
  );
}

export function BackendTab({ detail, reload }: TabProps) {
  const file = owner(detail, "backend");
  const d = useDraft({ backend: asObj(detail.merged.backend) }, file, detail.project, reload);
  const [conn, setConn] = useState<TestOutcome | null>(null);
  const [busy, setBusy] = useState(false);
  const backend = asObj(d.draft.backend);
  const access = (str(backend.access) || "none") as Access;
  const probes = asList<Obj>(backend.probes);
  const set = (patch: Patch) => {
    const next = { ...backend, ...patch } as Obj;
    for (const [k, v] of Object.entries(next)) if (v === undefined || v === "") delete next[k];
    d.setDraft({ backend: next });
  };
  const setProbe = (i: number, patch: Patch) => set({
    probes: probes.map((p, j) => {
      if (j !== i) return p;
      const out = { ...p, ...patch } as Obj;
      for (const [k, v] of Object.entries(out)) if (v === undefined || v === "" || v === false) delete out[k];
      return out;
    }),
  });
  const savedProbes = new Set(asList<Obj>(asObj(detail.merged.backend).probes).map((p) => str(p.id)));

  const testConn = async () => {
    if (d.dirty && !(await d.save())) return;
    setBusy(true);
    try { setConn(await setupApi.testBackend(detail.project, file)); }
    catch (e) { setConn({ ok: false, message: (e as Error).message }); }
    finally { setBusy(false); }
  };

  return (
    <Card>
      <Block title="Backend access" sub={`Where the truth lives. After every call Vocto reads it to see what really changed: that beats anything the agent says.`}>
        <Seg id="backend-access" value={access} onChange={(v) => set({ access: v, connection: v === "none" ? undefined : str(backend.connection) })}
          options={[{ value: "none", label: "No access" }, { value: "sql", label: "Database (SQL)" }, { value: "rest", label: "REST API" }]} />
        {access === "none" && <Notice tone="warn">Without backend access, checks that need the stored record are NOT VERIFIED (never PASS), so a release can't get a full GO on them.</Notice>}
        {access !== "none" && (
          <>
            <Field label={access === "sql" ? "Database address" : "API base address"}
              hint={access === "sql" ? "Postgres (postgresql://…) or SQLite (sqlite:///…). Use a read-only user: Vocto only reads. Passwords as ${NAME}, set in Settings › Secrets."
                : "Probes are GET requests below this address."}>
              <Text id="backend-connection" mono value={str(backend.connection)} placeholder={PLACEHOLDER[access]} onChange={(v) => set({ connection: v })} />
            </Field>
            {access === "rest" && (
              <Field label="Headers for every probe" hint="e.g. Authorization = Bearer ${ACME_API_TOKEN}.">
                <KeyValue key={`bh${d.version}`} value={asObj(backend.headers)} onChange={(v) => set({ headers: Object.keys(v).length ? v : undefined })} keyPh="Header" valPh="Value or ${SECRET_NAME}" />
              </Field>
            )}
            <div className="flex items-center gap-2 flex-wrap">
              <Button id="test-backend" icon="db" busy={busy} disabled={busy} onClick={() => void testConn()}>Test backend</Button>
              <Help>Saves, then connects read-only.</Help>
            </div>
            <TestResult result={conn} />
          </>
        )}
      </Block>

      {access !== "none" && (
        <Block title="Probes: what to look up after a call" sub="Scenarios point at these by id. Bind values: :test_id, :call_started_at, :call_ended_at, and any caller fact by name (:full_name).">
          {probes.map((p, i) => (
            <div key={`${i}-${d.version}`} data-probe={str(p.id)} className="grid gap-3 p-3.5 border border-line rounded-xl bg-surface-2/40">
              <div className="flex items-center gap-2">
                <b className="font-mono flex-1">{str(p.id) || "new probe"}</b>
                <Button size="sm" variant="ghost" icon="x" onClick={() => set({ probes: probes.filter((_, j) => j !== i) })}>Remove</Button>
              </div>
              <Grid>
                <Field label="Id"><Text mono value={str(p.id)} onChange={(v) => setProbe(i, { id: v.trim() })} placeholder="bookings_for_test" /></Field>
                <Field label="Returns"><Select value={str(p.returns) || "rows"} options={[["rows", "rows: every record found"], ["one", "one: the first record"], ["count", "count: a number"]]}
                  onChange={(v) => setProbe(i, { returns: v === "rows" ? undefined : v })} /></Field>
              </Grid>
              <Field label="What it finds"><Text value={str(p.description)} onChange={(v) => setProbe(i, { description: v })} /></Field>
              <Field label={access === "sql" ? "SQL query" : "Request"} hint={access === "sql" ? "Find the call's records by :test_id if the backend stores it, otherwise by caller facts inside the call's time window."
                : 'e.g. GET /bookings?test_id=:test_id'}>
                <Area mono rows={access === "sql" ? 4 : 1} value={str(p.query)} onChange={(v) => setProbe(i, { query: v })}
                  placeholder={access === "sql" ? "SELECT id, status, amount FROM orders WHERE test_id = :test_id" : "GET /bookings?test_id=:test_id"} />
              </Field>
              {access === "rest" && (
                <Grid>
                  <Field label="Records field" hint="Where the list is in the answer, e.g. data.items. Empty: the answer itself.">
                    <Text mono value={str(p.rows_field)} onChange={(v) => setProbe(i, { rows_field: v })} />
                  </Field>
                  <div className="self-end pb-1.5"><Check checked={!!p.empty_on_404} onChange={(v) => setProbe(i, { empty_on_404: v })} label="404 means no record" hint="Otherwise a 404 is a probe error (NOT VERIFIED)." /></div>
                </Grid>
              )}
              {savedProbes.has(str(p.id)) && !d.dirty ? <ProbeTest detail={detail} probe={str(p.id)} query={str(p.query)} /> : <Help>Save to try this probe.</Help>}
            </div>
          ))}
          <div><Button id="add-probe" icon="plus" onClick={() => set({ probes: [...probes, { id: "", query: access === "rest" ? "GET /" : "" }] })}>Add probe</Button></div>
        </Block>
      )}
      <Problems items={d.problems} />
      <SaveBar id="save-backend" dirty={d.dirty} busy={d.busy} onSave={() => void d.save()} onRevert={d.revert}
        note={file !== detail.file ? "Shared by the voice and text setups of this project." : undefined} />
    </Card>
  );
}
