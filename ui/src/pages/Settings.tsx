// Settings: each agent's test settings, the secrets projects use, the API key, speech models.
import { useState, type ReactNode } from "react";
import { ProjectAvatar } from "../components/dash";
import { useToast } from "../components/feedback";
import { Icon } from "../components/Icon";
import { Badge, Button, Card, CardHead, Chip, EmptyState, Field, Notice, PageHead, Seg, Skeleton, inputCls } from "../components/ui";
import { api } from "../lib/api";
import { useApp, useCrumbs, useLoad } from "../lib/app";
import { cx } from "../lib/format";
import { channelLabel, projectHref } from "../lib/links";
import { setupApi } from "../lib/setup";
import type { Secret, System } from "../lib/types";
import { TestSettingsTab } from "./setup/TestSettingsTab";

// The test settings of one agent (p50 / p95, repeats, limits...), the same form as on its project page.
function AgentSettings() {
  const { projects, reloadProjects } = useApp();
  const [key, setKey] = useState<string | null>(null);
  const [file, setFile] = useState("profile.yaml");
  const chosen = projects?.find((p) => p.key === key) ?? projects?.find((p) => !p.example) ?? projects?.[0] ?? null;
  const { data: detail, error, reload } = useLoad(() => (chosen ? setupApi.profile(chosen.path, file) : Promise.resolve(null)), [chosen?.path, file]);
  if (!projects) return <Skeleton height={200} />;
  if (!chosen) return <Card><EmptyState icon="bot" title="No projects yet" action={<Button variant="primary" icon="plus" href="#/projects/new">New project</Button>} /></Card>;
  return (
    <section className="grid gap-3" id="agent-settings">
      <Card>
        <CardHead level={2} title="Test settings per agent" sub="each agent has its own release gate: response times, repeats, coverage, budget" />
        <div className="flex gap-3 flex-wrap items-center">
          <div className="flex items-center gap-2.5">
            <ProjectAvatar name={chosen.name} k={chosen.key} size={34} />
            <select id="settings-project" className={cx(inputCls, "w-full sm:w-auto sm:min-w-[280px]")} value={chosen.key}
              onChange={(e) => { setKey(e.target.value); setFile("profile.yaml"); }}>
              {projects.map((p) => <option key={p.key} value={p.key}>{p.name}</option>)}
            </select>
          </div>
          {chosen.profiles.length > 1 && (
            <Seg id="settings-profile" value={file} onChange={setFile}
              options={chosen.profiles.map((p) => ({ value: p.file, label: channelLabel(p, chosen.profiles), icon: p.mode === "voice" ? "mic" : "text" }))} />
          )}
          <span className="flex-1" />
          <a href={projectHref(chosen.key)} className="text-[13px] inline-flex items-center gap-1">Open the project <Icon name="arrow" size={13} /></a>
        </div>
      </Card>
      {error && <Notice tone="bad">{error.message}</Notice>}
      {detail && <TestSettingsTab key={`${detail.project}/${detail.file}`} detail={detail} reload={() => { reload(); void reloadProjects(); }} />}
    </section>
  );
}

function SecretRow({ s, onSaved }: { s: Secret; onSaved: (list: Secret[]) => void }) {
  const toast = useToast();
  const [value, setValue] = useState("");
  const [open, setOpen] = useState(!s.set);
  const [busy, setBusy] = useState(false);
  const save = async () => {
    setBusy(true);
    try {
      onSaved(await api.post<Secret[]>("/api/settings/secrets", { name: s.name, value }));
      setValue(""); setOpen(false); toast(`${s.name} saved`);
    } catch (e) { toast((e as Error).message, "bad"); } finally { setBusy(false); }
  };
  return (
    <div data-secret={s.name} className="grid gap-2 py-3 border-b border-line last:border-b-0">
      <div className="flex items-center gap-2.5 flex-wrap">
        <Icon name="lock" className="text-muted" />
        <b className="font-mono text-[13px]">{s.name}</b>
        {s.set ? <Badge status="PASS" label={s.hint ? `set · ${s.hint}` : "set"} /> : <Badge status="NOT_VERIFIED" label="missing" />}
        <span className="text-muted text-[12px] flex-1 min-w-[140px]">used by {s.projects.join(", ")}</span>
        {!open && <Button size="sm" variant="ghost" onClick={() => setOpen(true)}>{s.set ? "Replace" : "Set"}</Button>}
      </div>
      {open && (
        <form className="flex gap-2 flex-wrap" onSubmit={(e) => { e.preventDefault(); void save(); }}>
          <input className={cx(inputCls, "font-mono flex-1 min-w-[200px]")} type="password" autoComplete="off" placeholder="Value"
            value={value} onChange={(e) => setValue(e.target.value)} aria-label={`Value of ${s.name}`} />
          <Button type="submit" variant="primary" icon="save" busy={busy} disabled={busy || !value.trim()}>Save</Button>
          {s.set && <Button variant="ghost" onClick={() => { setOpen(false); setValue(""); }}>Cancel</Button>}
        </form>
      )}
    </div>
  );
}

function Secrets() {
  const { data, error } = useLoad(() => api.get<Secret[]>("/api/settings/secrets"), []);
  const [list, setList] = useState<Secret[] | null>(null);
  const rows = list ?? data;
  return (
    <Card id="secrets">
      <CardHead level={2} title="Secrets" sub="passwords and tokens your projects refer to">
        {rows && rows.length > 0 && (rows.every((s) => s.set) ? <Badge status="PASS" label="all set" /> : <Badge status="NOT_VERIFIED" label={`${rows.filter((s) => !s.set).length} missing`} />)}
      </CardHead>
      <p className="text-ink-2 mb-1">
        Write a secret in any setup field as <span className="font-mono text-[12.5px] bg-surface-2 px-1 rounded">{"${NAME}"}</span>, for example a token in a header. Its value is kept here, on this computer only, and never stored in the project.
      </p>
      {error && <Notice tone="bad">{error.message}</Notice>}
      {!rows ? <Skeleton height={80} /> : rows.length ? rows.map((s) => <SecretRow key={s.name} s={s} onSaved={setList} />)
        : <div className="text-muted text-[12.5px] py-3">No project uses a secret yet.</div>}
    </Card>
  );
}

const mb = (b: number) => (b ? `${(b / 1e6).toFixed(0)} MB` : "0 MB");

function Row({ label, children, top }: { label: string; children: ReactNode; top?: boolean }) {
  return (
    <div className={cx("flex gap-3", top ? "items-start" : "items-center")}>
      <span className="text-muted w-[120px] flex-none">{label}</span>
      <div className="min-w-0 flex-1">{children}</div>
    </div>
  );
}

export function Settings() {
  useCrumbs([{ label: "Settings" }]);
  const { poll } = useApp();
  const { data: sys, error, reload } = useLoad(() => api.get<System>("/api/system"), []);
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const toast = useToast();

  if (error) return <Notice tone="bad" className="mt-5">{error.message}</Notice>;
  if (!sys) return <Skeleton />;
  const k = sys.api_key;
  const speechReady = sys.models.piper_voices.length > 0 && sys.models.whisper.length > 0;

  const saveKey = async () => {
    if (!key.trim()) { toast("Paste the key first", "bad"); return; }
    setBusy(true);
    try {
      await api.post("/api/settings/api-key", { key });
      setKey("");
      toast("Key saved. New runs use it.");
      reload();
      void poll();
    } catch (e) {
      toast((e as Error).message, "bad");
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <PageHead title="Settings" sub="Each agent's test settings, your secrets, and what Vocto needs on this computer." />
      <div className="grid gap-4">
        <AgentSettings />
        <div className="grid gap-4 grid-cols-1 lg:grid-cols-2 items-start">
          <Secrets />
          <Card>
            <CardHead level={2} title="Anthropic API key">{k.set ? <Badge status="PASS" label="set" /> : <Badge status="NOT_VERIFIED" label="not set" />}</CardHead>
            <p className="text-ink-2 mb-3">Real runs use Claude to play the caller and to check it stayed on script. Free dry runs need no key.</p>
            {k.set && (
              <div className="flex items-center gap-2 text-[12.5px] mb-3">
                <Icon name="key" /><span>Using a key ending <b className="font-mono">{k.hint || ""}</b>{k.source === "shell" ? " from this computer's environment" : ""}.</span>
              </div>
            )}
            <form onSubmit={(e) => { e.preventDefault(); void saveKey(); }}>
              <Field label={k.set ? "Replace the key" : "Paste your key"} hint="Kept on this computer only. It is sent only to Claude.">
                <input id="key" className={cx(inputCls, "font-mono")} type="password" placeholder="sk-ant-…" autoComplete="off"
                  value={key} onChange={(e) => setKey(e.target.value)} />
              </Field>
              <div className="mt-3">
                <Button id="save-key" type="submit" variant="primary" icon="save" busy={busy} disabled={busy}>Save key</Button>
              </div>
            </form>
          </Card>
          <Card>
            <CardHead level={2} title="Speech models">{speechReady ? <Badge status="PASS" label="ready" /> : <Badge status="NOT_APPLICABLE" label="not downloaded yet" />}</CardHead>
            <p className="text-ink-2 mb-3">Voice runs speak with Piper and listen with Whisper, on this computer. They download once on the first voice run (about 750 MB).</p>
            <div className="flex flex-col gap-2">
              <Row label="Voices" top>
                <div className="flex flex-wrap gap-1.5">
                  {sys.models.piper_voices.length ? sys.models.piper_voices.map((v) => <Chip key={v} icon="mic">{v}</Chip>) : <span className="text-muted text-[12.5px]">none yet</span>}
                </div>
              </Row>
              <Row label="Hearing" top>
                <div className="flex flex-wrap gap-1.5">
                  {sys.models.whisper.length ? sys.models.whisper.map((w) => <Chip key={w.name} icon="ear">{w.name} · {mb(w.bytes)}</Chip>) : <span className="text-muted text-[12.5px]">none yet</span>}
                </div>
              </Row>
              <Row label="Disk used">{mb(sys.models.bytes)}</Row>
            </div>
          </Card>
          <Card>
            <CardHead level={2} title="About" />
            <div className="flex flex-col gap-[9px]">
              <Row label="Vocto"><b>{sys.version}</b></Row>
              <Row label="Caller model">{sys.default_model}</Row>
              <Row label="System">Python {sys.python} · {sys.platform}</Row>
            </div>
          </Card>
        </div>
      </div>
    </>
  );
}
