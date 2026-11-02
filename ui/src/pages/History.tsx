// Results: the run going on now (if any) on top, then every run of this agent, filterable, plus its exit checks.
import { useEffect, useState } from "react";
import { Icon } from "../components/Icon";
import { OptionChips } from "../components/runbits";
import { Badge, Button, Card, CardHead, EmptyState, LiveDot, Notice, PageHead, Seg, Skeleton, Table, Tabs, inputCls, rowLink } from "../components/ui";
import { api } from "../lib/api";
import { useApp, useCrumbs, useLoad } from "../lib/app";
import { ago, cx, fmtDate, fmtMs, money } from "../lib/format";
import { navigate } from "../lib/router";
import { modeLabel, projectHref, projectOfJob, runHref } from "../lib/links";
import type { ExitCheckRecord, Project, RunSummary } from "../lib/types";

type Filter = "all" | "GO" | "NO_GO" | "NO_GO_INSUFFICIENT_EVIDENCE" | "none" | "text" | "voice";
const FILTERS: { value: Filter; label: string }[] = [
  { value: "all", label: "All" }, { value: "GO", label: "GO" }, { value: "NO_GO", label: "NO-GO" },
  { value: "NO_GO_INSUFFICIENT_EVIDENCE", label: "Not enough evidence" }, { value: "none", label: "No decision" },
  { value: "text", label: "Text" }, { value: "voice", label: "Voice" },
];
interface Saved { filter: Filter; q: string; tab: "runs" | "exit" }
const KEY = "vatf-history";

function restore(): Saved {
  const base: Saved = { filter: "all", q: "", tab: "runs" };
  try { return { ...base, ...JSON.parse(sessionStorage.getItem(KEY) || "{}") }; } catch { return base; }
}

function matches(r: RunSummary, filter: Filter, q: string) {
  if (filter === "none" && r.decision) return false;
  if ((filter === "GO" || filter === "NO_GO" || filter === "NO_GO_INSUFFICIENT_EVIDENCE") && r.decision !== filter) return false;
  if ((filter === "text" || filter === "voice") && r.mode !== filter) return false;
  if (q && ![r.run_id, r.agent_options?.bugs, r.mode, r.decision, r.offline ? "dry run" : ""].join(" ").toLowerCase().includes(q)) return false;
  return true;
}

export function History({ project }: { project: Project }) {
  useCrumbs([{ label: "Agents", href: "#/projects" }, { label: project.name, href: projectHref(project.key) }, { label: "Results" }]);
  const { activeJob, projects } = useApp();
  const live = activeJob && activeJob.status === "running" && projectOfJob(activeJob, projects)?.key === project.key ? activeJob : null;
  const { data, error } = useLoad(() => Promise.all([
    api.get<RunSummary[]>(`/api/runs?project=${encodeURIComponent(project.key)}`),
    api.get<ExitCheckRecord[]>(`/api/exit-checks?project=${encodeURIComponent(project.key)}`),
  ]), [project.key, live?.id]);
  const [st, setSt] = useState<Saved>(restore);
  useEffect(() => {
    try { sessionStorage.setItem(KEY, JSON.stringify(st)); } catch { /* storage blocked */ }
  }, [st]);

  if (error) return <Notice tone="bad" className="mt-5">{error.message}</Notice>;
  if (!data) return <Skeleton />;
  const [runs, checks] = data;
  const q = st.q.trim().toLowerCase();
  const list = runs.filter((r) => matches(r, st.filter, q));

  return (
    <>
      <PageHead title="Results" sub={<>Every test run of <b className="text-ink">{project.name}</b> and its release decision, newest first.</>} />
      {live && (
        <a href={projectHref(project.key, `/live/${live.id}`)} id="live-card"
          className="group flex items-center gap-4 mb-5 p-4 rounded-[10px] border border-[color-mix(in_srgb,var(--accent)_35%,var(--line))] bg-accent-soft/50 hover:bg-accent-soft hover:no-underline text-ink transition-colors animate-view-in">
          <span className="size-10 rounded-[10px] grid place-items-center bg-accent text-white flex-none"><Icon name="wave" size={20} /></span>
          <span className="flex-1 min-w-0">
            <span className="flex items-center gap-2"><LiveDot /><b className="text-[14px]">{live.kind === "exit-check" ? "Exit check running" : "Test run in progress"}</b></span>
            <span className="block text-[12.5px] text-ink-2 mt-0.5">Started {ago(live.started_at)} · watch the calls and listen in as they happen</span>
          </span>
          <span className="text-accent-ink font-semibold text-[13px] inline-flex items-center gap-1">Watch live <Icon name="arrow" size={15} className="transition-transform group-hover:translate-x-0.5" /></span>
        </a>
      )}
      <Tabs value={st.tab} onChange={(tab) => setSt({ ...st, tab })}
        tabs={[{ value: "runs", label: `Runs (${runs.length})` }, ...(checks.length || project.example ? [{ value: "exit" as const, label: `Exit checks (${checks.length})` }] : [])]} />

      {st.tab === "runs" ? (
        <div key="runs" className="animate-view-in">
          <div className="flex flex-wrap items-center gap-3 mb-3.5">
            <Seg id="filters" value={st.filter} options={FILTERS} onChange={(filter) => setSt({ ...st, filter })} />
            <span className="flex-1" />
            <div className="relative w-full sm:w-auto sm:min-w-[260px]">
              <Icon name="search" className="absolute left-[11px] top-2.5 text-muted pointer-events-none" />
              <input id="q" className={cx(inputCls, "pl-[34px]")} placeholder="Search runs…" value={st.q}
                onChange={(e) => setSt({ ...st, q: e.target.value })} />
            </div>
          </div>
          <Card flush>
            {list.length ? (
              <Table head={["Decision", "Run", "Settings", "Calls", "Pass / fail", "Response p50", "Cost", "Started"]}>
                {list.map((r) => (
                  <tr key={r.run_id} className={rowLink} data-href={runHref(r)} onClick={() => navigate(runHref(r))}>
                    <td><Badge status={r.decision || r.status} /></td>
                    <td><b>{modeLabel(r.mode)} run</b><div className="text-muted text-[11.5px] font-mono">{r.run_id}</div></td>
                    <td><div className="flex flex-wrap gap-1.5"><OptionChips run={r} /></div></td>
                    <td className="tabular-nums">{r.calls_started}{r.planned ? `/${r.planned}` : ""}</td>
                    <td className="tabular-nums whitespace-nowrap">
                      <span className="text-good">{r.counts.PASS || 0}</span> / <span className="text-bad">{r.counts.FAIL || 0}</span>
                      {r.counts.ERROR ? <span className="text-muted"> · {r.counts.ERROR} err</span> : null}
                    </td>
                    <td className="tabular-nums">{r.mode === "voice" ? fmtMs(r.p50_ms) : <span className="text-muted">—</span>}</td>
                    <td className="tabular-nums">{money(r.cost_usd)}</td>
                    <td className="whitespace-nowrap">{fmtDate(r.started_at)}<div className="text-muted text-[11.5px]">{ago(r.started_at)}</div></td>
                  </tr>
                ))}
              </Table>
            ) : (
              <EmptyState icon="search" title={runs.length ? "No runs match" : "No runs yet"}
                text={runs.length ? "Try another filter." : "Start the first one from Run tests."}
                action={runs.length ? undefined : <Button variant="primary" icon="play" href={projectHref(project.key, "/new")}>Run tests</Button>} />
            )}
          </Card>
        </div>
      ) : (
        <div key="exit" className="flex flex-col gap-4 animate-view-in">
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
                    <td><b>{r.label === "clean" ? "Clean agent" : r.label}</b>{r.title && r.label !== "clean" && <div className="text-muted text-[12px]">{r.title}</div>}</td>
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
                action={<Button href={projectHref(project.key, "/new")}>Set one up</Button>} />
            </Card>
          )}
        </div>
      )}
    </>
  );
}
