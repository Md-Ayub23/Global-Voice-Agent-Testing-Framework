// Set up > CI & alerts: where messages go after a run (Slack, Discord, any webhook), health checks on a
// schedule, and a ready CI file for this agent (GitHub Actions, GitLab CI, Jenkins).
import { useState } from "react";
import { useCopy, useToast } from "../../components/feedback";
import { Block, Check, Grid, Num, Problems, SaveBar, Select, Text, type Obj } from "../../components/form";
import { Icon } from "../../components/Icon";
import { OptionChips } from "../../components/runbits";
import { Badge, Button, Card, Chip, Choice, Field, Notice, Seg, Skeleton, Switch, Table } from "../../components/ui";
import { api, qs } from "../../lib/api";
import { useLoad } from "../../lib/app";
import { ago } from "../../lib/format";
import { runHref } from "../../lib/links";
import type { AlertTarget, CiFile, OpsPage } from "../../lib/types";
import { asList, asObj, useDraft, type TabProps } from "./common";

const EVENT_WORDS: Record<string, [string, string]> = {
  no_go: ["Not ready", "The release is blocked."],
  regression: ["Worse than the baseline", "Any new failure or change for the worse, even one that doesn't block."],
  insufficient: ["Not enough evidence", "Too few calls could be verified (usually the test setup, not the agent)."],
  recovered: ["Ready again", "Ready after the agent's last run was not."],
  monitor: ["Real-call alert", "A rule on the agent's real calls was broken (Quality › Real calls › Settings)."],
  monitor_recovered: ["Real calls back to normal", "Every real-call alert has cleared."],
};
const TYPE_WORDS: [string, string][] = [["slack", "Slack (also Google Chat, Mattermost)"], ["discord", "Discord"], ["webhook", "Any webhook (JSON)"]];
const HEALTH_DEFAULTS = { enabled: false, every_minutes: 60, scenarios: [], repeats: 1, conditions: "as_written", judges: false, start_agent: false };
const ENV = /\$\{([A-Za-z_][A-Za-z0-9_]*)\}/g;

export function OpsTab({ detail, reload, projectKey }: TabProps & { projectKey: string }) {
  const { data: ops, error, reload: reloadOps } = useLoad(
    () => api.get<OpsPage>(`/api/project/${projectKey}/ops?${qs({ file: detail.file })}`), [projectKey, detail.file]);
  const d = useDraft({ alerts: asList(detail.raw.alerts) as unknown as Obj, health_checks: asObj(detail.raw.health_checks) } as Obj,
    detail.file, detail.project, () => { reload(); reloadOps(); });
  if (error) return <Notice tone="bad">{error.message}</Notice>;
  if (!ops) return <Skeleton height={300} />;
  const alerts = asList<AlertTarget>(d.draft.alerts);
  const setAlerts = (list: AlertTarget[]) => d.setDraft({ ...d.draft, alerts: list as unknown as Obj });
  // What a run sees: Vocto's defaults, then a profile this one extends, then this file (the draft).
  const hc = { ...HEALTH_DEFAULTS, ...asObj(detail.base.health_checks), ...asObj(d.draft.health_checks) } as OpsPage["health"];
  const setHc = (patch: Partial<OpsPage["health"]>) => {
    const next = { ...asObj(d.draft.health_checks), ...patch } as Obj;
    for (const [k, v] of Object.entries(patch)) if (v === undefined) delete next[k];
    d.setDraft({ ...d.draft, health_checks: next });
  };
  const voice = ops.mode === "voice";
  const chosen = asList<string>(asObj(d.draft.health_checks).scenarios);

  return (
    <div className="flex flex-col gap-4">
      {!ops.committed_with_project && (
        <Notice tone="warn" icon="info">This agent's files are kept on this computer only (not committed), so CI can't run its tests until
          you commit the agent's folder. Alerts and health checks from this dashboard work either way.</Notice>
      )}
      <Card>
        <Block icon="bell" title="Alerts" sub="A message after a run, to the places your team already looks. Runs with bug switches never alert. Keep the webhook address as a secret: save it under Settings › Secrets and write ${NAME} here.">
          {alerts.length === 0 && <div className="text-muted text-[13px]">No alerts yet.</div>}
          {alerts.map((a, i) => {
            const set = (patch: Partial<AlertTarget>) => setAlerts(alerts.map((x, j) => (j === i ? { ...x, ...patch } : x)));
            const events = a.events || ops.default_events;
            const names = [...(a.url || "").matchAll(ENV)].map((m) => m[1]);
            return (
              <div key={i} className="border border-line rounded-[12px] p-3.5 grid gap-3" data-alert={i}>
                <Grid cols={3}>
                  <Field label="Name"><Text id={`alert-${i}-name`} value={a.name} placeholder={`alert ${i + 1}`} onChange={(v) => set({ name: v || undefined })} /></Field>
                  <Field label="Send to"><Select id={`alert-${i}-type`} value={a.type || "webhook"} options={TYPE_WORDS} onChange={(v) => set({ type: v as AlertTarget["type"] })} /></Field>
                  <Field label="Webhook address" hint={names.length ? names.map((n) => `${n}: ${ops.secrets[n] ? "saved" : "not saved yet (Settings › Secrets)"}`).join(" · ") : "Prefer ${NAME} over the address itself"}>
                    <Text id={`alert-${i}-url`} mono value={a.url} placeholder="${SLACK_WEBHOOK_URL}" onChange={(v) => set({ url: v })} />
                  </Field>
                </Grid>
                <div className="grid gap-2 sm:grid-cols-2">
                  {ops.alert_events.map((e) => (
                    <Check key={e} checked={events.includes(e)} label={EVENT_WORDS[e]?.[0] || e} hint={EVENT_WORDS[e]?.[1]}
                      onChange={(on) => set({ events: on ? [...events, e] : events.filter((x) => x !== e) })} />
                  ))}
                </div>
                <div className="flex items-center gap-2.5 flex-wrap">
                  <label><Switch id={`alert-${i}-on`} checked={a.enabled !== false} label="On" onChange={(v) => set({ enabled: v ? undefined : false })} /></label>
                  <span className="flex-1" />
                  <TestAlert projectKey={projectKey} target={a} />
                  <Button size="sm" icon="x" onClick={() => setAlerts(alerts.filter((_, j) => j !== i))}>Remove</Button>
                </div>
              </div>
            );
          })}
          <div><Button id="add-alert" size="sm" icon="plus" onClick={() => setAlerts([...alerts, { name: "", type: "slack", url: "" }])}>Add an alert</Button></div>
        </Block>

        <Block icon="activity" title="Health checks" sub="A short run on a schedule, to catch an agent that broke between releases: a provider outage, a changed prompt, a slow backend. They run while this dashboard is open; for round-the-clock checks, use the schedule in the CI file below.">
          <label><Switch id="health-on" checked={!!hc.enabled} label="Run health checks" onChange={(v) => setHc({ enabled: v || undefined })} /></label>
          <Grid cols={3}>
            <Field label="Every (minutes)" hint="At least 5. A check waits if another run is going."><Num id="health-every" min={5} value={asObj(d.draft.health_checks).every_minutes as number} placeholder={String({ ...HEALTH_DEFAULTS, ...asObj(detail.base.health_checks) }.every_minutes)} onChange={(v) => setHc({ every_minutes: v })} /></Field>
            <Field label="Repeats per test" hint="1 keeps checks quick and cheap."><Num id="health-repeats" min={1} max={10} value={asObj(d.draft.health_checks).repeats as number} placeholder={String({ ...HEALTH_DEFAULTS, ...asObj(detail.base.health_checks) }.repeats)} onChange={(v) => setHc({ repeats: v })} /></Field>
            {voice && <Field label="Caller conditions"><Select id="health-conditions" value={hc.conditions} options={ops.conditions.map((c) => [c, c.replace("_", " ")] as [string, string])} onChange={(v) => setHc({ conditions: v === "as_written" ? undefined : v })} /></Field>}
          </Grid>
          <div className="flex flex-wrap gap-x-6 gap-y-2">
            <label><Switch id="health-judges" checked={!!hc.judges} label="AI judges too (costs money on every check)" onChange={(v) => setHc({ judges: v || undefined })} /></label>
            {ops.agent_process && <label><Switch id="health-start" checked={!!hc.start_agent} label="Start the agent for each check" onChange={(v) => setHc({ start_agent: v || undefined })} /></label>}
          </div>
          <div>
            <div className="font-medium text-[13px] mb-1.5">Tests to run <span className="text-muted font-normal">· none picked = the critical tests ({ops.tests.filter((t) => t.criticality === "critical").length || "all"})</span></div>
            <div className="grid gap-2 sm:grid-cols-2">
              {ops.tests.map((t) => (
                <Choice key={t.id} on={chosen.includes(t.id)} data-test={t.id}
                  onClick={() => setHc({ scenarios: chosen.includes(t.id) ? chosen.filter((x) => x !== t.id) : [...chosen, t.id] })}>
                  <b className="text-[13px]">{t.title}</b>{t.criticality === "critical" && <span className="ml-1.5"><Chip tone="bad" icon="flag">critical</Chip></span>}
                </Choice>
              ))}
            </div>
          </div>
          {ops.health_runs.length > 0 && (
            <Table compact head={["Last checks", "", ""]}>
              {ops.health_runs.slice(0, 5).map((r) => (
                <tr key={r.run_id}>
                  <td><Badge status={r.decision || r.status} /></td>
                  <td><a href={runHref(r.project_key ? r : { ...r, project_key: projectKey })} className="font-mono text-[13px]">{r.run_id}</a> <OptionChips run={{ mode: r.mode }} /></td>
                  <td className="text-muted text-[13px]">{ago(r.started_at)}</td>
                </tr>
              ))}
            </Table>
          )}
        </Block>
        <Problems items={d.problems} />
        <SaveBar id="save-ops" dirty={d.dirty} busy={d.busy} onSave={() => void d.save()} onRevert={d.revert} />
      </Card>
      <CiCard ops={ops} projectKey={projectKey} file={detail.file} />
    </div>
  );
}

function TestAlert({ projectKey, target }: { projectKey: string; target: AlertTarget }) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const go = async () => {
    setBusy(true);
    try {
      const r = await api.post<{ sent: boolean; status?: number; error?: string }>(`/api/project/${projectKey}/alerts/test`, { target });
      toast(r.sent ? "Test message sent: check the channel" : `Not sent: ${r.error || `answer ${r.status}`}`, r.sent ? undefined : "bad");
    } catch (e) {
      toast((e as Error).message, "bad");
    } finally {
      setBusy(false);
    }
  };
  return <Button size="sm" icon="bell" busy={busy} disabled={!target.url} onClick={() => void go()}>Send test</Button>;
}

// The CI file is made here, not saved: it belongs in the agent's repository, which Vocto doesn't touch.
function CiCard({ ops, projectKey, file }: { ops: OpsPage; projectKey: string; file: string }) {
  const copy = useCopy();
  const [o, setO] = useState({ provider: "github", offline: false, safety: false, conditions: "as_written", start_agent: false, branch: "main", schedule: true });
  const params = { file, provider: o.provider, offline: o.offline ? "1" : "0", safety: o.safety ? "1" : "0", conditions: o.conditions,
    start_agent: o.start_agent ? "1" : "0", branch: o.branch || "main", schedule: o.schedule ? "1" : "0" };
  const { data: ci, error } = useLoad(() => api.get<CiFile>(`/api/project/${projectKey}/ci?${qs(params)}`), [projectKey, JSON.stringify(params)]);
  const where = { github: "Repository secrets (Settings › Secrets and variables › Actions)", gitlab: "masked CI/CD variables (Settings › CI/CD › Variables)",
    jenkins: "Secret text credentials, with the ids shown in the file" }[o.provider];
  return (
    <Card>
      <Block icon="plug" title="CI file" sub="Runs this agent's tests on every pull request and push. “Not ready” or “Not enough evidence” fails the build, and the report and run are kept with it. Runs are compared with the baseline when one is committed with the agent's folder.">
        <Seg id="ci-provider" value={o.provider} onChange={(provider) => setO({ ...o, provider })}
          options={Object.entries(ops.providers).map(([value, label]) => ({ value, label }))} />
        <Grid cols={3}>
          <Field label="Branch to check on push"><Text id="ci-branch" value={o.branch} onChange={(branch) => setO({ ...o, branch })} /></Field>
          {ops.mode === "voice" && <Field label="Caller conditions"><Select id="ci-conditions" value={o.conditions} options={ops.conditions.map((c) => [c, c.replace("_", " ")] as [string, string])} onChange={(conditions) => setO({ ...o, conditions })} /></Field>}
        </Grid>
        <div className="flex flex-wrap gap-x-6 gap-y-2">
          <Check checked={o.schedule} label="Daily health check" hint="06:00 UTC, the health check tests" onChange={(schedule) => setO({ ...o, schedule })} />
          <Check checked={o.safety} label="Include safety tests" onChange={(safety) => setO({ ...o, safety })} />
          <Check checked={o.offline} label="Free dry run" hint="Rule-based caller, no Claude key: only checks the setup" onChange={(offline) => setO({ ...o, offline })} />
          {ops.agent_process && <Check checked={o.start_agent} label="Start the agent in CI" hint="Vocto starts and stops it (agent_process)" onChange={(start_agent) => setO({ ...o, start_agent })} />}
        </div>
        {error && <Notice tone="bad">{error.message}</Notice>}
        {ci && (
          <>
            <div className="flex items-center gap-2 flex-wrap text-[13px]">
              <Icon name="file" size={14} className="text-muted" />Save it in the agent's repository as <code className="font-mono text-[13px]">{ci.path}</code>
              <span className="flex-1" />
              <Button id="ci-copy" size="sm" icon="copy" onClick={() => void copy(ci.text)}>Copy</Button>
            </div>
            <pre id="ci-text" className="min-w-0 w-full max-h-[420px] overflow-auto text-[13px] leading-[1.55] font-mono bg-surface-2 border border-line rounded-[12px] p-3.5 whitespace-pre">{ci.text}</pre>
            <div className="text-[13px] text-ink-2">
              {ci.secrets.length ? <>Add {ci.secrets.map((s, i) => <span key={s}>{i ? ", " : ""}<code className="font-mono">{s}</code></span>)} as {where}.</> : "No secrets needed."}
            </div>
          </>
        )}
      </Block>
    </Card>
  );
}
