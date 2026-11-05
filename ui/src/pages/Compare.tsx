// Quality > Compare: any two runs of this agent side by side, with what got worse and what got better.
// The earlier run defaults to the baseline, the later one to the newest run.
import { ChangesList, CompareCounts, DIRECTION, LikeForLike } from "../components/compare";
import { Select } from "../components/form";
import { Badge, Card, CardHead, Chip, EmptyState, Field, Notice, PageHead, Skeleton, Table } from "../components/ui";
import { api, qs } from "../lib/api";
import { useCrumbs, useLoad } from "../lib/app";
import { fmtDate, fmtMs, pct, shortScenario } from "../lib/format";
import { modeLabel, projectHref, runHref } from "../lib/links";
import { navigate } from "../lib/router";
import type { BaselinePage, CompareResult, Project, RunSummary } from "../lib/types";

const label = (r: RunSummary, base?: string | null) =>
  `${fmtDate(r.started_at)} · ${modeLabel(r.mode)} · ${r.decision === "NO_GO_INSUFFICIENT_EVIDENCE" ? "NOT ENOUGH EVIDENCE" : r.decision?.replace("_", "-")}`
  + `${r.run_id === base ? " · baseline" : ""}${r.agent_options?.bugs ? ` · bugs: ${r.agent_options.bugs}` : ""}${r.offline ? " · dry run" : ""}`;

export function ComparePage({ project, query }: { project: Project; query: URLSearchParams }) {
  useCrumbs([{ label: "Agents", href: "#/projects" }, { label: project.name, href: projectHref(project.key) }, { label: "Compare runs" }]);
  const { data, error } = useLoad(() => Promise.all([
    api.get<RunSummary[]>(`/api/runs?project=${encodeURIComponent(project.key)}`),
    Promise.all(project.profiles.map((p) => api.get<BaselinePage>(`/api/project/${project.key}/baseline?file=${encodeURIComponent(p.file)}`).catch(() => null))),
  ]), [project.key]);
  const runs = (data?.[0] || []).filter((r) => r.decision);
  const bases = Object.fromEntries((data?.[1] || []).filter(Boolean).map((p) => [p!.file, p!.baseline?.run_id || null]));
  const b = query.get("b") || runs[0]?.run_id || "";
  const bRun = runs.find((r) => r.run_id === b);
  const fileOf = (r?: RunSummary) => r?.profile_file || "profile.yaml";
  const baseFor = bases[fileOf(bRun)] || null;
  const a = query.get("a") || (baseFor && baseFor !== b ? baseFor : runs.find((r) => r.run_id < b && fileOf(r) === fileOf(bRun))?.run_id) || "";
  const { data: cmp, error: cmpError } = useLoad(
    () => (a && b && a !== b ? api.get<CompareResult>(`/api/compare?${qs({ a, b })}`) : Promise.resolve(null)), [a, b]);
  const go = (next: { a?: string; b?: string }) => navigate(`${projectHref(project.key, "/compare")}?${qs({ a: next.a ?? a, b: next.b ?? b })}`, true);

  if (error) return <Notice tone="bad" className="mt-5">{error.message}</Notice>;
  if (!data) return <Skeleton />;
  if (runs.length < 2) {
    return (
      <>
        <PageHead title="Compare runs" sub="What changed between two runs of this agent." />
        <Card><EmptyState icon="compare" title="Two finished runs needed" text="Run the tests twice (or set a baseline and run again) to compare." /></Card>
      </>
    );
  }
  const opts = (exclude: string) => runs.filter((r) => r.run_id !== exclude).map((r) => [r.run_id, label(r, bases[fileOf(r)])] as [string, string]);
  const tests = cmp ? Array.from(new Set([...Object.keys(cmp.snapshots.a.tests), ...Object.keys(cmp.snapshots.b.tests)])) : [];
  const dirOf = (sid: string) => cmp?.changes.find((c) => c.kind === "test" && c.id === sid)?.direction;

  return (
    <>
      <PageHead title="Compare runs" sub="Pick an earlier and a later run of this agent. The later run is judged by this agent's Regression gate (Set up › Pass rules)." />
      <Card className="mb-4">
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Earlier run (before)"><Select id="cmp-a" value={a} options={opts(b)} onChange={(v) => go({ a: v })} placeholder="Pick a run" /></Field>
          <Field label="Later run (now)"><Select id="cmp-b" value={b} options={opts(a)} onChange={(v) => go({ b: v })} /></Field>
        </div>
      </Card>
      {cmpError && <Notice tone="bad">{cmpError.message}</Notice>}
      {!a && <Notice>Pick the earlier run to compare with.</Notice>}
      {cmp && (
        <div className="flex flex-col gap-4 animate-view-in">
          {!cmp.same_agent && <Notice tone="warn">These runs used different profiles, so tests and limits may not match.</Notice>}
          <LikeForLike cmp={cmp} />
          <CompareCounts cmp={cmp} />
          <Card flush>
            <CardHead level={2} title="What changed" sub={cmp.blocking ? `${cmp.blocking} change(s) would block a release` : "Nothing set to block got worse"} className="px-[18px] pt-4 mb-2" />
            <ChangesList cmp={cmp} />
          </Card>
          <Card flush>
            <CardHead level={2} title="Side by side" className="px-[18px] pt-4 mb-2" />
            <Table head={["", "Before", "Now"]}>
              <tr>
                <td><b>Run</b></td>
                {[cmp.runs.a, cmp.runs.b].map((r) => (
                  <td key={r.run_id}><a href={runHref({ run_id: r.run_id, project_key: project.key })} className="font-mono text-[12.5px]">{r.run_id}</a>
                    <div className="mt-1"><Badge status={r.decision} /></div></td>
                ))}
              </tr>
              {Array.from(new Set([...Object.keys(cmp.snapshots.a.scores), ...Object.keys(cmp.snapshots.b.scores)])).map((k) => (
                <tr key={k}><td><b>{k[0].toUpperCase() + k.slice(1)}</b></td><td className="tabular-nums">{pct(cmp.snapshots.a.scores[k])}</td><td className="tabular-nums">{pct(cmp.snapshots.b.scores[k])}</td></tr>
              ))}
              {(cmp.snapshots.a.latency || cmp.snapshots.b.latency) && (
                <tr><td><b>Response time</b> <span className="text-muted">p50 / p95</span></td>
                  {[cmp.snapshots.a.latency, cmp.snapshots.b.latency].map((l, i) => (
                    <td key={i} className="tabular-nums">{l ? `${fmtMs(l.p50_ms)} / ${fmtMs(l.p95_ms)}` : "—"}</td>
                  ))}</tr>
              )}
              {tests.map((sid) => {
                const ta = cmp.snapshots.a.tests[sid], tb = cmp.snapshots.b.tests[sid];
                const d = dirOf(sid);
                return (
                  <tr key={sid} data-test={sid}>
                    <td className="min-w-[220px]"><b>{(tb || ta).title || shortScenario(sid)}</b>
                      {d && d !== "same" && <span className="ml-2"><Chip tone={DIRECTION[d].tone}>{DIRECTION[d].label}</Chip></span>}
                      <div className="text-muted text-[11.5px] font-mono">{sid}</div></td>
                    <td className="tabular-nums">{ta ? `${ta.passed}/${ta.k} passed` : "—"}</td>
                    <td className="tabular-nums">{tb ? `${tb.passed}/${tb.k} passed` : "—"}</td>
                  </tr>
                );
              })}
            </Table>
          </Card>
        </div>
      )}
    </>
  );
}
