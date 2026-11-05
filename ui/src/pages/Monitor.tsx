// Quality > Real calls (Phase 8): calls from real customers, with personal data removed before anything is
// saved, scored like test calls (metrics, real-call checks, AI judges on a sample), with trends and alerts.
// Tabs: Overview, Calls, Import, Settings. One call opens at #/p/<key>/calls/<call id>.
import { useRef, useState, type ReactNode } from "react";
import { Kpi, When } from "../components/dash";
import { useCopy, useToast } from "../components/feedback";
import { Check, Grid, Num, Problems, SaveBar, Text } from "../components/form";
import { Icon } from "../components/Icon";
import { CallsPerDay, CHECK_WORDS, FlagChip, flagLabel } from "../components/monitor";
import { QualityTiles } from "../components/quality";
import { Badge, Button, Card, CardHead, Chip, EmptyState, Field, LinkTabs, Notice, PageHead, Seg, Skeleton, Switch, Table, rowLink } from "../components/ui";
import { api, qs } from "../lib/api";
import { useCrumbs, useLoad } from "../lib/app";
import { cx, fmtMs } from "../lib/format";
import { projectHref } from "../lib/links";
import { navigate } from "../lib/router";
import type { MonitorPage as Page, MonitorRow, MonitorSettings, Project } from "../lib/types";

export type MonitorTab = "overview" | "calls" | "import" | "settings";

const DETECTOR_WORDS: Record<string, [string, string]> = {
  email: ["Email addresses", "jane@example.com"],
  bank: ["Bank account numbers", "IBANs"],
  card: ["Payment card numbers", "13-19 digits that pass the card check"],
  id_number: ["ID numbers", "SSN, Aadhaar, PAN, UK National Insurance"],
  phone: ["Phone numbers", "9 to 15 digits"],
  birth_date: ["Dates of birth", "a date near “born”, “birthday” or “DOB”"],
  address: ["Street addresses", "“221 Baker Street”, UK postcodes"],
  name: ["Names", "after “my name is”, “this is”, “I'm”, and everywhere else in the call"],
};
const FILTERS: { value: string; label: string }[] = [
  { value: "all", label: "All" }, { value: "flagged", label: "Flagged" }, { value: "review", label: "To review" },
  { value: "failed", label: "Failed" }, { value: "escalated", label: "Handed to a person" }, { value: "pending", label: "Waiting for judges" },
];
const SOURCE: Record<string, string> = { vapi: "Vapi", retell: "Retell", vocto: "Vocto JSON", transcript: "Transcript" };
const pctText = (v: number | null | undefined) => (v == null ? "—" : `${Number(v.toFixed(1))}%`);

function change(now: number | null, before: number | null, unit = "%", lowerBetter = false): ReactNode {
  if (now == null || before == null) return null;
  const d = Math.round((now - before) * 10) / 10;
  if (!d) return <span className="text-muted">same as last week</span>;
  const better = lowerBetter ? d < 0 : d > 0;
  return <span className={better ? "text-good" : "text-bad"}>{d > 0 ? "+" : ""}{unit === "ms" ? fmtMs(d) : `${d}${unit === "%" ? " pts" : ""}`} vs last week</span>;
}

export function MonitorPageView({ project, tab, query }: { project: Project; tab: MonitorTab; query: URLSearchParams }) {
  useCrumbs([{ label: "Agents", href: "#/projects" }, { label: project.name, href: projectHref(project.key) }, { label: "Real calls" }]);
  const { data, error, reload } = useLoad(() => api.get<Page>(`/api/project/${project.key}/monitor`), [project.key]);
  if (error) return <Notice tone="bad" className="mt-5">{error.message}</Notice>;
  if (!data) return <Skeleton />;
  const at = (p = "") => projectHref(project.key, `/calls${p}`);
  return (
    <>
      <PageHead title="Real calls" sub="Calls from real customers. Personal data is removed before anything is saved; each call is scored like a test call, and alerts go to your team." >
        {data.judging && <Chip tone="accent" icon="scale">AI judges at work</Chip>}
        {!data.settings.enabled && <Chip tone="warn" icon="alert">monitoring off</Chip>}
      </PageHead>
      <LinkTabs value={tab} tabs={[
        { value: "overview", label: "Overview", href: at(), icon: "overview" },
        { value: "calls", label: `Calls${data.totals.calls ? ` (${data.totals.calls})` : ""}`, href: at("/list"), icon: "phone" },
        { value: "import", label: "Import", href: at("/import"), icon: "plus" },
        { value: "settings", label: "Settings", href: at("/settings"), icon: "settings" },
      ]} />
      {tab === "overview" && <Overview page={data} project={project} reload={reload} />}
      {tab === "calls" && <CallsList page={data} project={project} query={query} />}
      {tab === "import" && <ImportTab page={data} project={project} reload={reload} />}
      {tab === "settings" && <SettingsTab page={data} project={project} reload={reload} />}
    </>
  );
}

// ---- overview ----------------------------------------------------------------------------------

function Overview({ page, project, reload }: { page: Page; project: Project; reload: () => void }) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const w = page.window, tw = page.week.this_week, lw = page.week.last_week;
  const hours = page.settings.alerts.window_hours;
  const active = Object.entries(page.alerts.active);
  if (!page.totals.calls) {
    return (
      <Card>
        <EmptyState icon="phone" title="No real calls yet"
          text="Import call exports from your voice platform, or let the platform send each finished call here. Personal data is removed before anything is saved."
          action={<div className="flex gap-2 justify-center flex-wrap">
            <Button variant="primary" icon="plus" href={projectHref(project.key, "/calls/import")}>Import calls</Button>
            <Button icon="settings" href={projectHref(project.key, "/calls/settings")}>Receive calls automatically</Button>
          </div>} />
      </Card>
    );
  }
  const checkNow = async () => {
    setBusy(true);
    try {
      const r = await api.post<{ active: Record<string, unknown>; sent: unknown[] }>(`/api/project/${project.key}/monitor/check`);
      toast(Object.keys(r.active).length ? `${Object.keys(r.active).length} alert(s) active` : "No alerts");
      reload();
    } catch (e) { toast((e as Error).message, "bad"); } finally { setBusy(false); }
  };
  return (
    <div className="flex flex-col gap-4">
      {active.length > 0 ? (
        <Notice tone="bad" icon="bell">
          <b>{active.length === 1 ? "1 alert" : `${active.length} alerts`} on real calls</b>
          <ul className="mt-1.5 grid gap-1">
            {active.map(([k, a]) => (
              <li key={k} data-alert={k}><b>{a.title}.</b> {a.message}
                {a.calls?.length > 0 && <> <a href={projectHref(project.key, `/calls/${a.calls[0]}`)}>Open a call</a></>}</li>
            ))}
          </ul>
        </Notice>
      ) : (
        <Notice tone="good" icon="check-circle">No alerts: every alert rule holds over the last {hours} hours.</Notice>
      )}
      {page.pending > 0 && (
        <Notice icon="scale">
          {page.pending} call(s) waiting for the AI judges.{" "}
          {page.have_key ? (page.judging ? "They are being judged now." : <Button size="sm" busy={busy} onClick={checkNow}>Judge them now</Button>)
            : "Add a Claude API key under Settings to judge them."}
        </Notice>
      )}
      <div className="grid gap-4 grid-cols-1 sm:grid-cols-2 xl:grid-cols-5 stagger">
        <Kpi icon="phone" label={`Calls, last ${hours} h`} value={w.calls} foot={`${tw.calls} this week · ${lw.calls} the week before`} />
        <Kpi icon="check-circle" label="Pass rate" tone={w.pass_rate == null ? "accent" : w.pass_rate >= (page.settings.alerts.pass_rate_below ?? 0) ? "good" : "bad"}
          value={pctText(w.pass_rate)} foot={change(tw.pass_rate, lw.pass_rate) || `${w.passed} passed · ${w.failed} failed`} />
        <Kpi icon="flag" label="Flagged" tone={w.flagged ? "warn" : "good"} value={pctText(w.flagged_pct)}
          foot={change(tw.flagged_pct, lw.flagged_pct, "%", true) || `${w.flagged} call(s) worth a look`} />
        <Kpi icon="clock" label="Response time p90" value={fmtMs(w.p90_ms)} foot={w.answers ? `${w.answers} timed answers` : "needs turn times in the import"} />
        <Kpi icon="user" label="Handed to a person" value={pctText(w.escalated_pct)} foot={`${w.repeat_callers} repeat caller(s)`} />
      </div>
      <Card>
        <CardHead level={2} title="Calls per day" sub="last 14 days · hover a day for its numbers">
          <Button size="sm" icon="refresh" busy={busy} onClick={checkNow}>Check alerts now</Button>
        </CardHead>
        <CallsPerDay days={page.daily} />
      </Card>
      {page.quality.length > 0 && <QualityTiles rows={page.quality} sub={`Real calls in the last ${hours} h · worked out from turn times · bands are this agent's (Set up › Pass rules)`} />}
      <div className="grid gap-4 grid-cols-1 xl:grid-cols-2">
        <Card flush>
          <CardHead level={2} title="Why calls were flagged" sub="most common first, recent calls" className="px-[18px] pt-4 mb-2" />
          {page.top_flags.length ? (
            <Table compact head={["Reason", "Calls", ""]}>
              {page.top_flags.map((f) => (
                <tr key={`${f.kind}:${f.id}`}>
                  <td><b>{flagLabel(f, page.metric_defs)}</b><div className="text-muted text-[11.5px]">{f.kind === "metric" ? "call quality" : f.kind === "check" ? "real-call check" : "AI judge"}</div></td>
                  <td className="tabular-nums">{f.calls}</td>
                  <td className="text-right"><a href={`${projectHref(project.key, "/calls/list")}?${qs({ filter: "flagged" })}`}>See calls</a></td>
                </tr>
              ))}
            </Table>
          ) : <div className="px-[18px] pb-4 text-muted text-[13px]">Nothing flagged.</div>}
        </Card>
        <Card flush>
          <CardHead level={2} title="By agent version" sub="when the platform says which version took the call" className="px-[18px] pt-4 mb-2" />
          {page.versions.length ? (
            <Table compact head={["Version", "Calls", "Pass rate", "Flagged", "p90"]}>
              {page.versions.map((v) => (
                <tr key={v.version} className={rowLink} onClick={() => navigate(`${projectHref(project.key, "/calls/list")}?${qs({ version: v.version })}`)}>
                  <td className="font-mono text-[12.5px]">{v.version}<div className="text-muted text-[11.5px] font-sans"><When iso={v.first_seen} /></div></td>
                  <td className="tabular-nums">{v.calls}</td>
                  <td className="tabular-nums">{pctText(v.pass_rate)}</td>
                  <td className="tabular-nums">{pctText(v.flagged_pct)}</td>
                  <td className="tabular-nums">{fmtMs(v.p90_ms)}</td>
                </tr>
              ))}
            </Table>
          ) : <div className="px-[18px] pb-4 text-muted text-[13px]">No version information in these calls. Send <span className="font-mono">agent_version</span> with each call to compare releases.</div>}
        </Card>
      </div>
      {page.alerts.history.length > 0 && (
        <Card flush>
          <CardHead level={2} title="Alert history" sub={page.alerts.history.length > 12 ? `latest 12 of ${page.alerts.history.length}` : undefined} className="px-[18px] pt-4 mb-2" />
          <Table compact head={["When", "", "What"]}>
            {page.alerts.history.slice(0, 12).map((h, i) => (
              <tr key={i}><td className="whitespace-nowrap"><When iso={h.at} /></td>
                <td>{h.event === "raised" ? <Chip tone="bad" icon="bell">raised</Chip> : <Chip tone="good" icon="check">cleared</Chip>}</td>
                <td>{h.message}</td></tr>
            ))}
          </Table>
        </Card>
      )}
    </div>
  );
}

// ---- the list of calls ---------------------------------------------------------------------------

function CallsList({ page, project, query }: { page: Page; project: Project; query: URLSearchParams }) {
  const filter = query.get("filter") || "all";
  const version = query.get("version") || "";
  const [q, setQ] = useState(query.get("q") || "");
  const [limit, setLimit] = useState(50);
  const params = { filter, limit: String(limit), ...(version ? { version } : {}), ...(query.get("q") ? { q: query.get("q")! } : {}) };
  const { data, error } = useLoad(() => api.get<{ total: number; rows: MonitorRow[] }>(`/api/project/${project.key}/monitor/calls?${qs(params)}`),
    [project.key, filter, version, query.get("q"), limit]);
  const go = (patch: Record<string, string>) => {
    const next = { ...(filter !== "all" ? { filter } : {}), ...(version ? { version } : {}), ...(query.get("q") ? { q: query.get("q")! } : {}), ...patch };
    navigate(`${projectHref(project.key, "/calls/list")}?${qs(Object.fromEntries(Object.entries(next).filter(([, v]) => v && v !== "all")))}`, true);
  };
  return (
    <Card flush>
      <div className="flex flex-wrap items-center gap-2.5 px-[18px] pt-4 pb-3">
        <Seg id="calls-filter" value={filter} options={FILTERS} onChange={(v) => go({ filter: v })} />
        {version && <Chip icon="tag">version {version} <button className="ml-1 cursor-pointer" aria-label="Show all versions" onClick={() => go({ version: "" })}><Icon name="x" size={12} /></button></Chip>}
        <form className="ml-auto flex gap-1.5" onSubmit={(e) => { e.preventDefault(); go({ q }); }}>
          <Text id="calls-search" value={q} onChange={setQ} placeholder="Search words or reasons" className="w-[220px]" />
          <Button size="sm" icon="search" type="submit">Search</Button>
        </form>
      </div>
      {error && <Notice tone="bad" className="mx-[18px] mb-4">{error.message}</Notice>}
      {!data ? <Skeleton height={200} /> : data.rows.length === 0 ? (
        <div className="px-[18px] pb-5 text-muted text-[13px]">No calls match.</div>
      ) : (
        <>
          <Table head={["When", "What the caller wanted", "Result", "Why", "Length"]}>
            {data.rows.map((r) => (
              <tr key={r.call_id} data-call={r.call_id} className={rowLink} onClick={() => navigate(projectHref(project.key, `/calls/${r.call_id}`))}>
                <td className="whitespace-nowrap"><When iso={r.started_at || r.imported_at} />
                  <div className="text-muted text-[11.5px]">{SOURCE[r.source] || r.source}{r.agent_version ? ` · ${r.agent_version}` : ""}</div></td>
                <td className="max-w-[380px]"><div className="truncate">{r.summary || <span className="text-muted">(the caller said nothing)</span>}</div>
                  {r.redacted > 0 && <div className="text-muted text-[11.5px]"><Icon name="shield" size={11} className="inline -mt-0.5" /> {r.redacted} personal detail(s) removed</div>}</td>
                <td><Badge status={r.status} />
                  {r.judge_state === "pending" && <div className="text-muted text-[11.5px] mt-0.5">judges waiting</div>}</td>
                <td><div className="flex flex-wrap gap-1">{r.flags.slice(0, 2).map((f, i) => <FlagChip key={i} f={f} defs={page.metric_defs} />)}
                  {r.flags.length > 2 && <Chip>+{r.flags.length - 2}</Chip>}
                  {r.escalated && <Chip icon="user">handed over</Chip>}</div></td>
                <td className="tabular-nums whitespace-nowrap">{r.duration_s ? `${r.duration_s} s` : "—"}</td>
              </tr>
            ))}
          </Table>
          <div className="px-[18px] py-3 flex items-center gap-3 text-muted text-[12.5px]">
            Showing {data.rows.length} of {data.total}
            {data.rows.length < data.total && <Button size="sm" onClick={() => setLimit(limit + 100)}>Show more</Button>}
          </div>
        </>
      )}
    </Card>
  );
}

// ---- import -------------------------------------------------------------------------------------

interface ImportResult { files: { name: string; added?: number; duplicates?: number; problems?: string[]; error?: string; first?: string | null }[];
  added: number; alerts: { active: Record<string, { title: string; message: string }>; raised: string[] }; judging: boolean }

const EXAMPLE = `{
  "id": "call-1042",
  "started_at": "2026-11-02T09:30:00Z",
  "agent_version": "v12",
  "caller": "+14155550123",
  "turns": [
    {"role": "agent",  "text": "Riverside Clinic, how can I help?", "start_ms": 400,  "end_ms": 2200},
    {"role": "caller", "text": "I'd like a checkup tomorrow.",      "start_ms": 2900, "end_ms": 4700},
    {"role": "agent",  "text": "You're booked for 9 am.",           "start_ms": 5600, "end_ms": 7000,
     "tools": [{"name": "book_appointment", "args": {"slot": "09:00"}, "result": {"ok": true}, "at_ms": 4900}]}
  ]
}`;

function ImportTab({ page, project, reload }: { page: Page; project: Project; reload: () => void }) {
  const toast = useToast();
  const input = useRef<HTMLInputElement>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ImportResult | null>(null);
  const [over, setOver] = useState(false);
  const send = async () => {
    setBusy(true);
    try {
      const body = { files: await Promise.all(files.map(async (f) => ({ name: f.name, text: await f.text() }))) };
      const r = await api.post<ImportResult>(`/api/project/${project.key}/monitor/import`, body);
      setResult(r);
      setFiles([]);
      toast(r.added ? `${r.added} call(s) imported` : "No new calls", r.added ? undefined : "bad");
      reload();
    } catch (e) { toast((e as Error).message, "bad"); } finally { setBusy(false); }
  };
  return (
    <div className="grid gap-4 items-start grid-cols-1 xl:grid-cols-[minmax(0,1fr)_420px]">
      <Card>
        <CardHead level={2} title="Import call files" sub="Personal data is removed in this step: only the cleaned copy is kept." />
        <div
          className={cx("border-2 border-dashed rounded-[10px] p-8 grid place-items-center text-center gap-2 transition-colors cursor-pointer",
            over ? "border-accent bg-accent-soft" : "border-line hover:border-line-strong")}
          onClick={() => input.current?.click()}
          onDragOver={(e) => { e.preventDefault(); setOver(true); }} onDragLeave={() => setOver(false)}
          onDrop={(e) => { e.preventDefault(); setOver(false); setFiles([...files, ...Array.from(e.dataTransfer.files)]); }}>
          <Icon name="file" size={26} className="text-muted" />
          <div className="font-semibold">Drop call files here, or click to choose</div>
          <div className="text-muted text-[12.5px]">JSON, JSON lines or plain-text transcripts · up to 25 MB each</div>
          <input ref={input} id="import-files" type="file" multiple accept=".json,.jsonl,.ndjson,.txt" className="hidden"
            onChange={(e) => { setFiles([...files, ...Array.from(e.target.files || [])]); e.target.value = ""; }} />
        </div>
        {files.length > 0 && (
          <div className="mt-3 grid gap-1.5">
            {files.map((f, i) => (
              <div key={i} className="flex items-center gap-2 text-[13px]"><Icon name="file" size={14} className="text-muted" />{f.name}
                <span className="text-muted">{(f.size / 1024).toFixed(1)} KB</span>
                <Button size="sm" variant="ghost" iconOnly icon="x" aria-label="Remove" onClick={() => setFiles(files.filter((_, j) => j !== i))} /></div>
            ))}
            <div><Button id="import-send" variant="primary" icon="plus" busy={busy} onClick={send}>Import {files.length} file(s)</Button></div>
          </div>
        )}
        {!page.settings.enabled && <Notice tone="warn" className="mt-3">Monitoring is switched off for this agent (Settings), so imports are refused.</Notice>}
        {result && (
          <div className="mt-4 grid gap-2" id="import-result">
            {result.files.map((f, i) => (
              <Notice key={i} tone={f.error ? "bad" : f.added ? "good" : "info"} icon={f.error ? "alert" : "check"}>
                <b>{f.name}:</b>{" "}
                {f.error ? f.error : <>{f.added} call(s) added{f.duplicates ? `, ${f.duplicates} already imported` : ""}.
                  {f.first && <> <a href={projectHref(project.key, `/calls/${f.first}`)}>Open the first</a></>}</>}
                {f.problems?.map((p, j) => <div key={j} className="text-[12.5px]">{p}</div>)}
              </Notice>
            ))}
            {result.judging && <Notice icon="scale">The AI judges are scoring the new calls in the background.</Notice>}
            {result.alerts.raised.length > 0 && <Notice tone="bad" icon="bell">New alert: {result.alerts.raised.map((k) => result.alerts.active[k]?.title).join(", ")}.</Notice>}
          </div>
        )}
      </Card>
      <Card>
        <CardHead title="What Vocto can read" />
        <ul className="grid gap-2 text-[13px]">
          <li><b>Vapi</b>: end-of-call reports, or calls from Vapi's API.</li>
          <li><b>Retell</b>: call_ended or call_analyzed webhooks, or call objects.</li>
          <li><b>Plain transcripts</b>: one turn per line, starting <span className="font-mono">Agent:</span> or <span className="font-mono">Caller:</span>.</li>
          <li><b>Vocto JSON</b> from any other platform, one call per file, a list, or one per line:</li>
        </ul>
        <pre className="mt-2 text-[11.5px] font-mono bg-surface-2 border border-line rounded-lg p-3 overflow-x-auto">{EXAMPLE}</pre>
        <div className="text-muted text-[12px] mt-2">Turn times (start_ms, end_ms) are optional. With them, response time, silences and talk share are measured too.</div>
      </Card>
    </div>
  );
}

// ---- settings -----------------------------------------------------------------------------------

type Draft = Partial<{ [K in keyof MonitorSettings]: MonitorSettings[K] extends object ? Partial<MonitorSettings[K]> : MonitorSettings[K] }>;

function SettingsTab({ page, project, reload }: { page: Page; project: Project; reload: () => void }) {
  const toast = useToast();
  const copy = useCopy();
  const [draft, setDraft] = useState<Draft>(() => JSON.parse(JSON.stringify(page.saved || {})));
  const [saved, setSaved] = useState(() => JSON.stringify(page.saved || {}));
  const [busy, setBusy] = useState(false);
  const [problems, setProblems] = useState<string[]>([]);
  const [token, setToken] = useState<{ name: string; token: string } | null>(null);
  const [confirmAll, setConfirmAll] = useState(false);
  const dirty = JSON.stringify(draft) !== saved;
  const eff = <K extends keyof MonitorSettings>(k: K): MonitorSettings[K] =>
    (typeof page.defaults[k] === "object" ? { ...(page.defaults[k] as object), ...((draft[k] as object) || {}) } : (draft[k] ?? page.defaults[k])) as MonitorSettings[K];
  const set = <K extends keyof MonitorSettings>(k: K, patch: Partial<MonitorSettings[K]> | MonitorSettings[K]) =>
    setDraft((d) => ({ ...d, [k]: typeof patch === "object" && patch !== null && !Array.isArray(patch) ? clean({ ...((d[k] as object) || {}), ...patch }) : patch }));
  const intake = eff("intake"), privacy = eff("privacy"), scoring = eff("scoring"), rules = eff("alerts");
  const save = async () => {
    setBusy(true);
    try {
      const r = await api.put<{ saved: boolean; problems: string[] }>(`/api/project/${project.key}/monitor`, { monitor: draft });
      if (r.saved) { setSaved(JSON.stringify(draft)); setProblems([]); toast("Saved"); reload(); } else setProblems(r.problems);
    } catch (e) { toast((e as Error).message, "bad"); } finally { setBusy(false); }
  };
  const newToken = async () => {
    try {
      const r = await api.post<{ saved: boolean; name: string; token: string; problems?: string[] }>(`/api/project/${project.key}/monitor/token`);
      if (r.saved) {
        // The profile now names the secret; keep that in the draft so a later Save doesn't drop it.
        const ref = "${" + r.name + "}";
        setDraft((d) => ({ ...d, intake: { ...(d.intake || {}), token: ref } }));
        setSaved((was) => { const o = JSON.parse(was); o.intake = { ...(o.intake || {}), token: ref }; return JSON.stringify(o); });
        setToken(r); reload();
      } else setProblems(r.problems || []);
    } catch (e) { toast((e as Error).message, "bad"); }
  };
  const deleteAll = async () => {
    try {
      const r = await api.post<{ deleted: number }>(`/api/project/${project.key}/monitor/delete`, { all: true });
      toast(`${r.deleted} real call(s) deleted`);
      setConfirmAll(false);
      reload();
    } catch (e) { toast((e as Error).message, "bad"); }
  };
  const url = page.intake.url || `http://127.0.0.1:${intake.port}/ingest/${project.key}`;
  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHead level={2} title="Monitoring" />
        <label><Switch id="mon-enabled" checked={eff("enabled") !== false} label="Monitor real calls for this agent" onChange={(v) => set("enabled", v)} /></label>
      </Card>

      <Card>
        <CardHead level={2} title="Receive calls automatically" sub="Your voice platform sends each finished call here, while this dashboard is open." />
        <div className="grid gap-3">
          <label><Switch id="mon-listen" checked={!!intake.listen} label="Receive calls from the platform" onChange={(v) => set("intake", { listen: v })} /></label>
          <Grid cols={3}>
            <Field label="Port" hint="One port for all agents; 8791 unless it is taken (8790 is the tool gateway)."><Num id="mon-port" min={1024} max={65535} value={draft.intake?.port} placeholder={page.defaults.intake.port} onChange={(v) => set("intake", { port: v })} /></Field>
            <Field label="Address to give the platform" hint={page.intake.running ? "Receiving now" : intake.listen ? "Save to start receiving" : "Switch on and save to start"}>
              <div className="flex gap-1.5"><Text id="mon-url" mono value={url} onChange={() => undefined} /><Button size="sm" iconOnly icon="copy" aria-label="Copy the address" onClick={() => copy(url)} /></div>
            </Field>
            <Field label="Token" hint={page.intake.token_set ? `Saved as a secret (${page.intake.token_name})` : "Needed: calls without it are refused"}>
              <Button id="mon-token" size="sm" icon="key" onClick={newToken}>{page.intake.token_set ? "Make a new token" : "Make a token"}</Button>
            </Field>
          </Grid>
          {token && (
            <Notice tone="good" icon="key">
              <b>Your new token</b> (shown only now; it is saved as the secret {token.name}):
              <div className="flex gap-1.5 mt-1.5"><Text id="mon-token-value" mono value={token.token} onChange={() => undefined} /><Button size="sm" icon="copy" onClick={() => copy(token.token)}>Copy</Button></div>
            </Notice>
          )}
          {page.intake.error && <Notice tone="bad">{page.intake.error}</Notice>}
          <div className="text-muted text-[12.5px] grid gap-1">
            <div><b className="text-ink-2">Vapi:</b> set the assistant's Server URL to this address and its server secret to the token.</div>
            <div><b className="text-ink-2">Retell:</b> set the webhook URL to this address followed by <span className="font-mono">?token=</span> and the token.</div>
            <div><b className="text-ink-2">Any other platform:</b> POST each call as Vocto JSON with the header <span className="font-mono">Authorization: Bearer</span> and the token.</div>
            <div>Platforms in the cloud can't reach this computer directly: put a tunnel such as cloudflared or ngrok in front of this address.</div>
          </div>
        </div>
      </Card>

      <Card>
        <CardHead level={2} title="Privacy" sub="Removed before anything is saved. The AI judges only ever see the cleaned text." />
        <div className="grid gap-2 sm:grid-cols-2">
          {page.detectors.map((d) => (
            <Check key={d} checked={privacy.redact.includes(d)} label={DETECTOR_WORDS[d]?.[0] || d} hint={DETECTOR_WORDS[d]?.[1]}
              onChange={(on) => set("privacy", { redact: on ? [...privacy.redact, d] : privacy.redact.filter((x) => x !== d) })} />
          ))}
        </div>
        <div className="text-muted text-[12.5px] mt-2">Always removed as well: personal values the agent sends to its tools (the tools' personal fields, email, phone, date of birth, names) and this agent's own patterns.</div>
        <div className="mt-3 grid gap-2">
          <div className="font-semibold text-[13px]">Your own patterns</div>
          {(privacy.patterns || []).map((p, i) => (
            <div key={i} className="flex gap-1.5 items-center">
              <Text id={`mon-pat-${i}-name`} mono value={p.name} placeholder="member_id" className="w-[34%]"
                onChange={(v) => set("privacy", { patterns: privacy.patterns.map((x, j) => (j === i ? { ...x, name: v } : x)) })} />
              <Text id={`mon-pat-${i}-regex`} mono value={p.regex} placeholder="MEM\d{6}"
                onChange={(v) => set("privacy", { patterns: privacy.patterns.map((x, j) => (j === i ? { ...x, regex: v } : x)) })} />
              <Button size="sm" variant="ghost" iconOnly icon="x" aria-label="Remove pattern" onClick={() => set("privacy", { patterns: privacy.patterns.filter((_, j) => j !== i) })} />
            </div>
          ))}
          <div><Button size="sm" icon="plus" onClick={() => set("privacy", { patterns: [...(privacy.patterns || []), { name: "", regex: "" }] })}>Add a pattern</Button></div>
        </div>
        <Grid cols={2} className="mt-3">
          <Field label="Keep calls for (days)" hint="Older calls are deleted automatically. 0 keeps them."><Num id="mon-retention" min={0} max={3650} value={draft.privacy?.retention_days} placeholder={page.defaults.privacy.retention_days} onChange={(v) => set("privacy", { retention_days: v })} /></Field>
          <Field label="Recording links"><label className="mt-1.5"><Switch id="mon-keep-links" checked={!!privacy.keep_recording_links} label="Keep the platform's recording link" onChange={(v) => set("privacy", { keep_recording_links: v })} /></label></Field>
        </Grid>
      </Card>

      <Card>
        <CardHead level={2} title="AI judges" sub="The agent's judge items (Quality › AI judges) on real calls. Trust comes from your labels, as for test calls." />
        <label><Switch id="mon-judges" checked={!!scoring.judges} label="Judge real calls" onChange={(v) => set("scoring", { judges: v })} /></label>
        <Grid cols={3} className="mt-3">
          <Field label="Share of calls judged (%)" hint="The same calls are always picked."><Num id="mon-share" min={0} max={100} value={draft.scoring?.judge_share_pct} placeholder={page.defaults.scoring.judge_share_pct} onChange={(v) => set("scoring", { judge_share_pct: v })} /></Field>
          <Field label="At most, per day (calls)"><Num id="mon-max" min={0} value={draft.scoring?.judge_max_per_day} placeholder={page.defaults.scoring.judge_max_per_day} onChange={(v) => set("scoring", { judge_max_per_day: v })} /></Field>
          <Field label="Budget per day ($)"><Num id="mon-budget" min={0} step={0.5} value={draft.scoring?.judge_budget_usd_per_day} placeholder={page.defaults.scoring.judge_budget_usd_per_day} onChange={(v) => set("scoring", { judge_budget_usd_per_day: v })} /></Field>
        </Grid>
        <div className="mt-3"><Check checked={!!scoring.always_judge_flagged} label="Always judge calls a check or metric flagged" hint="Within the daily limits." onChange={(v) => set("scoring", { always_judge_flagged: v })} /></div>
        {!page.have_key && <Notice tone="warn" className="mt-3">No Claude API key yet (Settings), so real calls are scored without the judges.</Notice>}
      </Card>

      <Card>
        <CardHead level={2} title="Alert rules" sub={<>Checked after every import and every few minutes. Alerts go to the places set in <a href={projectHref(project.key, "/setup/ops")}>Set up › CI & alerts</a>, once when raised and once when everything is back to normal.</>} />
        <Grid cols={4}>
          <Field label="Over the last (hours)"><Num id="mon-window" min={1} max={720} value={draft.alerts?.window_hours} placeholder={page.defaults.alerts.window_hours} onChange={(v) => set("alerts", { window_hours: v })} /></Field>
          <Field label="Only with at least (calls)" hint="For the rate rules."><Num id="mon-min" min={1} value={draft.alerts?.min_calls} placeholder={page.defaults.alerts.min_calls} onChange={(v) => set("alerts", { min_calls: v })} /></Field>
          <Field label="Pass rate below (%)"><Num id="mon-pass" min={0} max={100} value={draft.alerts?.pass_rate_below ?? undefined} placeholder={page.defaults.alerts.pass_rate_below ?? ""} onChange={(v) => set("alerts", { pass_rate_below: v })} /></Field>
          <Field label="Flagged above (%)"><Num id="mon-flagged" min={0} max={100} value={draft.alerts?.flagged_above ?? undefined} placeholder={page.defaults.alerts.flagged_above ?? ""} onChange={(v) => set("alerts", { flagged_above: v })} /></Field>
        </Grid>
        <div className="grid gap-2 sm:grid-cols-3 mt-3">
          <Check checked={!!rules.safety} label="Any safety failure" hint="A safety judge item failed, on any call." onChange={(v) => set("alerts", { safety: v })} />
          <Check checked={!!rules.false_success} label={CHECK_WORDS["real.false_success"]} hint="A tool failed and the agent said it was done." onChange={(v) => set("alerts", { false_success: v })} />
          <Check checked={!!rules.metrics_bad} label="Call quality in the bad band" hint="Bands from Set up › Pass rules." onChange={(v) => set("alerts", { metrics_bad: v })} />
        </div>
      </Card>

      <Card>
        <CardHead level={2} title="Delete real calls" sub="Removes every real call of this agent from this computer. This can't be undone." />
        {confirmAll ? (
          <div className="flex gap-2 items-center flex-wrap"><span className="text-[13px]">Delete all {page.totals.calls} real call(s)?</span>
            <Button id="mon-delete-yes" size="sm" variant="danger" icon="x" onClick={deleteAll}>Yes, delete them</Button>
            <Button size="sm" onClick={() => setConfirmAll(false)}>Cancel</Button></div>
        ) : <div><Button id="mon-delete-all" size="sm" icon="x" disabled={!page.totals.calls} onClick={() => setConfirmAll(true)}>Delete all real calls</Button></div>}
      </Card>

      <Problems items={problems} />
      <SaveBar dirty={dirty} busy={busy} onSave={save} onRevert={() => { setDraft(JSON.parse(saved)); setProblems([]); }}
        note="Saved in this agent's profile, monitor section." />
    </div>
  );
}

// Fields set back to empty are left out, so the default applies.
function clean<T extends object>(o: T): T {
  return Object.fromEntries(Object.entries(o).filter(([, v]) => v !== undefined && v !== "")) as T;
}
