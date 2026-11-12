// Quality > Compare: any two runs of this agent side by side, with what got worse and what got better.
// The earlier run defaults to the baseline, the later one to the newest run. An A/B test runs two
// versions of the agent on the same tests and shows version B against version A.
import { useState } from "react";
import { AB_DIRECTION, ChangesList, CompareCounts, DIRECTION, LikeForLike } from "../components/compare";
import { useToast } from "../components/feedback";
import { KeyValue, Num, Select } from "../components/form";
import { Badge, Button, Card, CardHead, Chip, EmptyState, Field, Notice, PageHead, Seg, Skeleton, Table } from "../components/ui";
import { api, qs } from "../lib/api";
import { useApp, useCrumbs, useLoad } from "../lib/app";
import { fmtDate, fmtMs, moneyIn, pct, shortScenario, plural } from "../lib/format";
import { channelLabel, modeLabel, projectHref, runHref } from "../lib/links";
import { navigate } from "../lib/router";
import type { BaselinePage, CompareResult, Job, Options, Project, RunSummary } from "../lib/types";

const label = (r: RunSummary, base?: string | null) =>
  `${fmtDate(r.started_at)} · ${modeLabel(r.mode)} · ${r.decision === "NO_GO_INSUFFICIENT_EVIDENCE" ? "NOT ENOUGH EVIDENCE" : r.decision?.replace("_", "-")}`
  + `${r.ab ? ` · A/B version ${r.ab.side}` : ""}${r.run_id === base ? " · baseline" : ""}${r.agent_options?.bugs ? ` · bugs: ${r.agent_options.bugs}` : ""}${r.offline ? " · dry run" : ""}`;

export function ComparePage({ project, query }: { project: Project; query: URLSearchParams }) {
  useCrumbs([{ label: project.name, href: projectHref(project.key) }, { label: "Compare runs" }]);
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
        <PageHead title="Compare runs" sub="What changed between two runs of this agent, or how two versions of it compare." />
        <ABCard project={project} />
        <Card><EmptyState icon="compare" title="Two finished runs needed" text="Run the tests twice (or set a baseline and run again) to compare, or test two versions above." /></Card>
      </>
    );
  }
  const opts = (exclude: string) => runs.filter((r) => r.run_id !== exclude).map((r) => [r.run_id, label(r, bases[fileOf(r)])] as [string, string]);
  const tests = cmp ? Array.from(new Set([...Object.keys(cmp.snapshots.a.tests), ...Object.keys(cmp.snapshots.b.tests)])) : [];
  const dirOf = (sid: string) => cmp?.changes.find((c) => c.kind === "test" && c.id === sid)?.direction;
  const ab = cmp?.ab || null;
  const cost = (k: "a" | "b") => cmp?.snapshots[k].cost;

  return (
    <>
      <PageHead title="Compare runs" sub="Pick an earlier and a later run of this agent (the later one is judged by the Regression gate in Set up › Pass rules), or test two versions of the agent side by side." />
      <ABCard project={project} />
      <Card className="mb-4">
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label={ab ? "Version A" : "Earlier run (before)"}><Select id="cmp-a" value={a} options={opts(b)} onChange={(v) => go({ a: v })} placeholder="Pick a run" /></Field>
          <Field label={ab ? "Version B" : "Later run (now)"}><Select id="cmp-b" value={b} options={opts(a)} onChange={(v) => go({ b: v })} /></Field>
        </div>
      </Card>
      {cmpError && <Notice tone="bad">{cmpError.message}</Notice>}
      {!a && <Notice>Pick the earlier run to compare with.</Notice>}
      {cmp && (
        <div className="flex flex-col gap-4">
          {ab && (
            <Notice icon="compare" tone="info">
              <b>A/B test.</b> The same tests with version A ({ab.labels.A}) and version B ({ab.labels.B}), one after the other. Below is version B against version A; nothing here blocks a release.
            </Notice>
          )}
          {!ab && !cmp.same_agent && <Notice tone="warn">These runs used different profiles, so tests and limits may not match.</Notice>}
          <LikeForLike cmp={cmp} />
          <CompareCounts cmp={cmp} ab={!!ab} />
          <Card flush>
            <CardHead level={2} title={ab ? "Where the versions differ" : "What changed"}
              sub={ab ? "tests that passed with one version and failed with the other, and numbers that differ by more than their limit"
                : cmp.blocking ? `${plural(cmp.blocking, "change")} would block a release` : "Nothing set to block got worse"} className="px-[18px] pt-4 mb-2" />
            <ChangesList cmp={cmp} ab={!!ab} />
          </Card>
          <Card flush>
            <CardHead level={2} title="Side by side" className="px-[18px] pt-4 mb-2" />
            <Table head={ab ? ["", "Version A", "Version B"] : ["", "Before", "Now"]}>
              <tr>
                <td><b>Run</b></td>
                {[cmp.runs.a, cmp.runs.b].map((r) => (
                  <td key={r.run_id}><a href={runHref({ run_id: r.run_id, project_key: project.key })} className="font-mono text-[13px]">{r.run_id}</a>
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
              {(cost("a") || cost("b")) && (
                <tr id="cmp-cost"><td><b>Cost per completed task</b> <span className="text-muted">at your prices</span></td>
                  {(["a", "b"] as const).map((k) => {
                    const c = cost(k);
                    return <td key={k} className="tabular-nums">{c ? (c.per_task != null ? moneyIn(c.per_task, c.currency) : "no task done") : "no prices set"}
                      {c && <div className="text-muted text-[13px]">{c.tasks_done} of {c.task_calls} done · {moneyIn(c.per_call, c.currency)} per call</div>}</td>;
                  })}
                </tr>
              )}
              {tests.map((sid) => {
                const ta = cmp.snapshots.a.tests[sid], tb = cmp.snapshots.b.tests[sid];
                const d = dirOf(sid);
                return (
                  <tr key={sid} data-test={sid}>
                    <td className="min-w-[220px]"><b>{(tb || ta).title || shortScenario(sid)}</b>
                      {d && (d !== "same" || ab) && <span className="ml-2"><Chip tone={DIRECTION[d].tone}>{(ab && AB_DIRECTION[d]) || DIRECTION[d].label}</Chip></span>}
                      <div className="text-muted text-[13px] font-mono">{sid}</div></td>
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

// A/B test: two versions of the agent (two channels, or the same one with other agent options) run
// the same tests with the same settings, one after the other; then version B is compared with A.
function ABCard({ project }: { project: Project }) {
  const toast = useToast();
  const { setActiveJob, activeJob } = useApp();
  const ok = project.profiles.filter((p) => p.ok);
  const [open, setOpen] = useState(false);
  const [aFile, setA] = useState(ok[0]?.file || "");
  const aProf = ok.find((p) => p.file === aFile);
  const same = ok.filter((p) => p.mode === aProf?.mode);
  const [bFile, setB] = useState(same.find((p) => p.file !== aFile)?.file || aFile);
  const [bOpts, setBOpts] = useState<Options>({});
  const [repeats, setRepeats] = useState<number | undefined>(undefined);
  const [caller, setCaller] = useState<"real" | "free">("real");
  const [busy, setBusy] = useState(false);
  if (!ok.length) return null;
  const optionsSet = Object.keys(bOpts).length > 0;
  const sameVersion = bFile === aFile && !optionsSet;
  const start = async () => {
    setBusy(true);
    try {
      const job = await api.post<Job>("/api/jobs", {
        command: "run", project: project.path, profile: aFile, ab_with: bFile, ab_agent_options: bOpts,
        repeats: repeats || null, offline: caller === "free", scenarios: [],
        ...(aProf?.mode === "voice" ? { speech: "local" } : {}),
      });
      setActiveJob(job);
      navigate(projectHref(project.key, `/live/${job.id}`));
    } catch (e) { toast((e as Error).message, "bad"); setBusy(false); }
  };
  return (
    <Card className="mb-4" id="ab-card">
      <div className="flex items-center gap-3 flex-wrap">
        <div className="flex-1 min-w-[240px]">
          <div className="font-semibold">Test two versions (A/B)</div>
          <div className="text-ink-2 text-[13px]">Run the same tests on two versions of this agent, for example the live one and a new prompt or model, and see which does better: tests passed, answer times, call quality and cost per task.</div>
        </div>
        {!open && <Button id="ab-open" icon="compare" onClick={() => setOpen(true)}>Set up an A/B test</Button>}
      </div>
      {open && (
        <div className="grid gap-4 mt-4">
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Version A" hint="Usually the version you have now.">
              <Select id="ab-a" value={aFile} options={ok.map((p) => [p.file, channelLabel(p, project.profiles, project.example)] as [string, string])}
                onChange={(v) => { setA(v); const m = ok.find((p) => p.file === v)?.mode; if (ok.find((p) => p.file === bFile)?.mode !== m) setB(v); }} />
            </Field>
            <Field label="Version B" hint="Another channel set up for the new version (its own address), or the same one with other agent options below.">
              <Select id="ab-b" value={bFile} options={same.map((p) => [p.file, channelLabel(p, project.profiles, project.example)] as [string, string])} onChange={setB} />
            </Field>
          </div>
          <Field label="Agent options for version B (optional)" hint="Passed to version B only, e.g. model = the new model's name. For agents Vocto starts itself or reaches with connection options.">
            <KeyValue id="ab-b-options" value={bOpts} onChange={(v) => setBOpts(v as Options)} keyPh="model" valPh="value" />
          </Field>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Caller">
              <Seg id="ab-caller" value={caller} onChange={setCaller}
                options={[{ value: "real", label: "Real run (Claude plays the caller)" }, { value: "free", label: "Free dry run" }]} />
            </Field>
            <Field label="Repeats per test" hint="Empty: each channel's own. More repeats make a small difference easier to trust.">
              <Num id="ab-repeats" min={1} max={50} value={repeats} onChange={setRepeats} />
            </Field>
          </div>
          {sameVersion && <Notice tone="warn">Version B is the same as version A: pick another channel or set an agent option for B.</Notice>}
          <div className="flex gap-2.5 items-center flex-wrap">
            <Button id="ab-start" variant="primary" icon="play" disabled={busy || sameVersion || !!activeJob} onClick={() => void start()}>Run both versions</Button>
            <Button variant="ghost" onClick={() => setOpen(false)}>Cancel</Button>
            <span className="text-muted text-[13px]">Every test case of the channel runs with A, then with B. Then this page shows B against A.</span>
          </div>
        </div>
      )}
    </Card>
  );
}

