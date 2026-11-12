// Results: the run going on now (if any) on top, then every run of this agent, filterable, plus its exit checks.
import { useEffect, useState } from "react";
import { Icon } from "../components/Icon";
import { OptionChips } from "../components/runbits";
import { BaselineIcon } from "../components/compare";
import { useToast } from "../components/feedback";
import { Badge, Button, Card, CardHead, Chip, EmptyState, LiveDot, Notice, PageHead, Seg, Skeleton, Table, Tabs, inputCls, rowLink } from "../components/ui";
import { api } from "../lib/api";
import { useApp, useCrumbs, useLoad } from "../lib/app";
import { ago, cx, fmtDate, fmtMs, money, plural } from "../lib/format";
import { navigate } from "../lib/router";
import { SHAPE_LABEL } from "../lib/load";
import { modeLabel, projectHref, projectOfJob, runHref } from "../lib/links";
import { decisionTitle } from "../lib/status";
import { MODE_ICON, modeTitle, runLabel } from "../lib/modes";
import type { BaselinePage, ExitCheckRecord, Project, RunSummary, TestModeId } from "../lib/types";

type Filter = "all" | "GO" | "NO_GO" | "NO_GO_INSUFFICIENT_EVIDENCE" | "none" | "text" | "voice" | "health";
const FILTERS: { value: Filter; label: string }[] = [
  { value: "all", label: "All" }, { value: "GO", label: "Ready" }, { value: "NO_GO", label: "Not ready" },
  { value: "NO_GO_INSUFFICIENT_EVIDENCE", label: "Not enough evidence" }, { value: "none", label: "No result" },
  { value: "text", label: "Text" }, { value: "voice", label: "Voice" }, { value: "health", label: "Health checks" },
];
interface Saved { filter: Filter; q: string; tab: "runs" | "exit"; mode: string }
const KEY = "vatf-history";

function restore(): Saved {
  const base: Saved = { filter: "all", q: "", tab: "runs", mode: "" };
  try { return { ...base, ...JSON.parse(sessionStorage.getItem(KEY) || "{}") }; } catch { return base; }
}

function matches(r: RunSummary, filter: Filter, q: string, mode: string) {
  if (mode && (r.test_mode || "functional") !== mode) return false;
  if (filter === "none" && r.decision) return false;
  if ((filter === "GO" || filter === "NO_GO" || filter === "NO_GO_INSUFFICIENT_EVIDENCE") && r.decision !== filter) return false;
  if ((filter === "text" || filter === "voice") && r.mode !== filter) return false;
  if (filter === "health" && r.trigger !== "health") return false;
  if (q && ![r.run_id, r.agent_options?.bugs, r.mode, r.decision, decisionTitle(r.decision), modeTitle(r.test_mode), r.offline ? "dry run" : "", r.trigger, r.kind === "load" ? "load test" : ""].join(" ").toLowerCase().includes(q)) return false;
  return true;
}

// `mode` set: the Reports tab of one test mode (only its runs, no baseline card or exit checks).
export function History({ project, mode }: { project: Project; mode?: TestModeId }) {
  useCrumbs(mode ? null : [{ label: project.name, href: projectHref(project.key) }, { label: "All runs" }]);
  const { activeJob, projects } = useApp();
  const live = activeJob && activeJob.status === "running" && projectOfJob(activeJob, projects)?.key === project.key ? activeJob : null;
  const { data, error } = useLoad(() => Promise.all([
    api.get<RunSummary[]>(`/api/runs?project=${encodeURIComponent(project.key)}`),
    api.get<ExitCheckRecord[]>(`/api/exit-checks?project=${encodeURIComponent(project.key)}`),
  ]), [project.key, live?.id]);
  const files = project.profiles.map((p) => p.file);
  const { data: bases, reload: reloadBases } = useLoad(() => Promise.all(files.map((f) =>
    api.get<BaselinePage>(`/api/project/${project.key}/baseline?file=${encodeURIComponent(f)}`).catch(() => null))), [project.key, files.join(",")]);
  const [st, setSt] = useState<Saved>(restore);
  useEffect(() => {
    try { sessionStorage.setItem(KEY, JSON.stringify(st)); } catch { /* storage blocked */ }
  }, [st]);

  if (error) return <Notice tone="bad" className="mt-5">{error.message}</Notice>;
  if (!data) return <Skeleton />;
  const [runs, checks] = data;
  const q = st.q.trim().toLowerCase();
  const modeFilter = mode || st.mode;
  const list = runs.filter((r) => matches(r, st.filter, q, modeFilter));
  const modesSeen = [...new Set(runs.map((r) => r.test_mode || "functional"))];
  const baselineIds = new Set((bases || []).map((b) => b?.baseline?.run_id).filter(Boolean) as string[]);

  return (
    <>
      {!mode && <PageHead title="Results" sub={<>Every test run of <b className="text-ink">{project.name}</b> and its release readiness, newest first.</>} />}
      {live && !mode && (
        <a href={projectHref(project.key, `/live/${live.id}`)} id="live-card"
          className="group flex items-center gap-4 mb-5 p-4 rounded-[12px] border border-[color-mix(in_srgb,var(--accent)_35%,var(--line))] bg-accent-soft/50 hover:bg-accent-soft hover:no-underline text-ink transition-colors">
          <span className="size-10 rounded-[12px] grid place-items-center bg-accent text-white flex-none"><Icon name="wave" size={20} /></span>
          <span className="flex-1 min-w-0">
            <span className="flex items-center gap-2"><LiveDot /><b className="text-[14px]">{live.kind === "exit-check" ? "Exit check running" : live.kind === "load" ? "Load test in progress" : "Test run in progress"}</b></span>
            <span className="block text-[13px] text-ink-2 mt-0.5">Started {ago(live.started_at)} · watch the calls and listen in as they happen</span>
          </span>
          <span className="text-accent-ink font-semibold text-[13px] inline-flex items-center gap-1">Watch live <Icon name="arrow" size={15} className="transition-transform group-hover:translate-x-0.5" /></span>
        </a>
      )}
      {(!mode || mode === "regression") && <BaselineCard project={project} pages={bases} reload={reloadBases} />}
      {!mode && <Tabs value={st.tab} onChange={(tab) => setSt({ ...st, tab })}
        tabs={[{ value: "runs", label: `Runs (${runs.length})` }, ...(checks.length || project.example ? [{ value: "exit" as const, label: `Exit checks (${checks.length})` }] : [])]} />}

      {st.tab === "runs" || mode ? (
        <div key="runs" >
          <div className="flex flex-wrap items-center gap-3 mb-3.5">
            <Seg id="filters" value={st.filter} options={FILTERS} onChange={(filter) => setSt({ ...st, filter })} />
            {!mode && (
              <select id="mode-filter" aria-label="Test mode" className={cx(inputCls, "w-auto min-w-[170px]")} value={st.mode} onChange={(e) => setSt({ ...st, mode: e.target.value })}>
                <option value="">All test modes</option>
                {modesSeen.map((m) => <option key={m} value={m}>{modeTitle(m)}</option>)}
              </select>
            )}
            <span className="flex-1" />
            <div className="relative w-full sm:w-auto sm:min-w-[260px]">
              <Icon name="search" className="absolute left-[11px] top-2.5 text-muted pointer-events-none" />
              <input id="q" className={cx(inputCls, "pl-[34px]")} placeholder="Search runs…" value={st.q}
                onChange={(e) => setSt({ ...st, q: e.target.value })} />
            </div>
          </div>
          <Card flush>
            {list.length ? (
              <Table head={["Readiness", "Run", "Settings", "Calls", "Pass / fail", "Response p50", "Cost", "Started"]}>
                {list.map((r) => (
                  <tr key={r.run_id} className={rowLink} data-href={runHref(r)} onClick={() => navigate(runHref(r))}>
                    <td><Badge status={r.decision || r.status} label={runLabel(r)} /></td>
                    <td>
                      <b className="inline-flex items-center gap-1.5"><Icon name={MODE_ICON[r.test_mode || "functional"] || "play"} size={14} className="text-muted" />{modeTitle(r.test_mode)} · {modeLabel(r.mode)}{baselineIds.has(r.run_id) && <span className="inline-flex items-center gap-1 text-accent-ink text-[13px] font-semibold" title="The baseline: every new run is compared with it"><BaselineIcon />baseline</span>}</b>
                      <div className="text-muted text-[13px] font-mono">{r.run_id}</div>
                      {r.vs_baseline && (r.vs_baseline.regressions + r.vs_baseline.worse > 0) && (
                        <div className="text-[13px] text-bad">{r.vs_baseline.regressions ? `${plural(r.vs_baseline.regressions, "new failure")}` : ""}{r.vs_baseline.regressions && r.vs_baseline.worse ? " · " : ""}{r.vs_baseline.worse ? `${r.vs_baseline.worse} worse` : ""} vs baseline</div>
                      )}
                    </td>
                    <td><div className="flex flex-wrap gap-1.5">{r.kind === "load" && <Chip tone="accent" icon="activity">load test{r.load ? ` · ${SHAPE_LABEL[r.load.shape].toLowerCase()} to ${r.load.calls_at_once}` : ""}</Chip>}<OptionChips run={r} /></div></td>
                    <td className="tabular-nums">{r.calls_started}{r.planned ? `/${r.planned}` : ""}</td>
                    <td className="tabular-nums whitespace-nowrap">
                      <span className="text-good">{r.counts.PASS || 0}</span> / <span className="text-bad">{r.counts.FAIL || 0}</span>
                      {r.counts.ERROR ? <span className="text-muted"> · {r.counts.ERROR} {r.kind === "load" ? "didn't finish" : "err"}</span> : null}
                      {r.kind === "load" && r.load?.capacity != null && <div className="text-muted text-[13px]">capacity {r.load.capacity} at once</div>}
                    </td>
                    <td className="tabular-nums">{r.mode === "voice" ? fmtMs(r.p50_ms) : <span className="text-muted">—</span>}</td>
                    <td className="tabular-nums">{money(r.cost_usd)}</td>
                    <td className="whitespace-nowrap">{fmtDate(r.started_at)}<div className="text-muted text-[13px]">{ago(r.started_at)}</div></td>
                  </tr>
                ))}
              </Table>
            ) : (
              <EmptyState icon="search" title={runs.length ? "No runs match" : "No runs yet"}
                text={runs.length ? "Try another filter." : mode ? "Start one from the Run tab." : "Start the first one from Test modes."}
                action={runs.length || mode ? undefined : <Button variant="primary" icon="play" href={projectHref(project.key, "/test")}>Test modes</Button>} />
            )}
          </Card>
        </div>
      ) : (
        <div key="exit" className="flex flex-col gap-4">
          {checks.length ? checks.map((c) => (
            <Card key={c.job_id}>
              <CardHead title={`${c.mode === "voice" ? "Voice" : "Text"} exit check${c.planted ? " with planted bugs" : ""}`}
                sub={c.started_at ? fmtDate(c.started_at) : undefined}>
                {!c.finished ? <Badge status="stopped" label="did not finish" /> : <Badge status={c.passed ? "passed" : "not_passed"} />}
              </CardHead>
              <Table compact>
                {c.rows.map((r) => (
                  <tr key={r.run_id + r.label}>
                    <td className="w-[90px]"><Badge status={r.ok ? "PASS" : "FAIL"} label={r.ok ? "OK" : "MISS"} /></td>
                    <td><b>{r.label === "clean" ? "Clean agent" : r.label}</b>{r.title && r.label !== "clean" && <div className="text-muted text-[13px]">{r.title}</div>}</td>
                    <td className="text-ink-2">{r.detail}</td>
                    <td><a href={runHref({ run_id: r.run_id, project_key: project.key })} className="hover:no-underline"><Badge status={r.decision} /></a></td>
                  </tr>
                ))}
              </Table>
            </Card>
          )) : (
            <Card>
              <EmptyState icon="shield" title="No exit checks yet"
                text="An exit check runs the clean agent and each bug switch, and passes when every bug is caught."
                action={<Button href={projectHref(project.key, "/test/full")}>Set one up</Button>} />
            </Card>
          )}
        </div>
      )}
    </>
  );
}

// The agent's baseline (one per channel): the known-good run every new run is compared with.
function BaselineCard({ project, pages, reload }: { project: Project; pages: (BaselinePage | null)[] | null; reload: () => void }) {
  const toast = useToast();
  if (!pages) return null;
  const set = pages.filter((p): p is BaselinePage => !!p?.baseline);
  const clear = async (file: string) => {
    try {
      await api.post(`/api/project/${project.key}/baseline/clear`, { file });
      toast("Baseline removed: runs are no longer compared");
      reload();
    } catch (e) { toast((e as Error).message, "bad"); }
  };
  const channel = (file: string) => project.profiles.length > 1 ? (project.profiles.find((p) => p.file === file)?.mode === "voice" ? "Voice" : "Text") : "";
  return (
    <Card className="mb-5" id="baseline-card">
      <div className="flex items-start gap-3 flex-wrap">
        <span className="size-9 rounded-[12px] grid place-items-center bg-accent-soft text-accent flex-none"><Icon name="anchor" size={18} /></span>
        <div className="flex-1 min-w-[240px]">
          <b className="text-[14px]">Baseline</b>
          {set.length ? set.map((p) => (
            <div key={p.file} className="text-[13px] text-ink-2 mt-0.5 flex flex-wrap items-center gap-x-2">
              {channel(p.file) && <span className="text-muted">{channel(p.file)}:</span>}
              <a className="font-mono text-[13px]" href={runHref({ run_id: p.baseline!.run_id, project_key: project.key })}>{p.baseline!.run_id}</a>
              <Badge status={p.baseline!.decision} />
              <span className="text-muted">set {ago(p.baseline!.set_at)}{p.baseline!.note ? ` · ${p.baseline!.note}` : ""}</span>
              <button type="button" className="text-muted hover:text-bad text-[13px] underline-offset-2 hover:underline cursor-pointer" onClick={() => void clear(p.file)}>Remove</button>
            </div>
          )) : (
            <div className="text-[13px] text-ink-2 mt-0.5">None yet. Open a good run and click <b>Set as baseline</b>: every new run is then compared with it, and a test that passed there and fails later blocks the release.</div>
          )}
          {set.length > 0 && !set[0].committed_with_project && (
            <div className="text-[13px] text-muted mt-1">This agent's files are kept on this computer only, so CI can't see the baseline unless you commit the agent's folder.</div>
          )}
        </div>
        {set.length > 0 && <Button size="sm" icon="compare" href={projectHref(project.key, "/compare")}>Compare runs</Button>}
      </div>
    </Card>
  );
}
