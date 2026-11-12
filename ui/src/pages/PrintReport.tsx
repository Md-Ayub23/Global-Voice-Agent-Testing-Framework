// The release readiness report laid out for paper: every section open, nothing to click. "Save as PDF"
// opens the browser's print window, where the person picks "Save as PDF" (or a printer).
import { useEffect } from "react";
import { Button, Notice, Skeleton } from "../components/ui";
import { ConditionsCard, CoverageCard, FailuresCard, ExecutiveSummary, FindingsList, MethodCard, MetricsCard, Scorecard, VerdictBanner, isReadiness } from "../components/readiness";
import { api } from "../lib/api";
import { useCrumbs, useLoad } from "../lib/app";
import { fmtDate } from "../lib/format";
import { projectHref } from "../lib/links";
import { modeTitle } from "../lib/modes";
import type { Project, RunDetail } from "../lib/types";

export function PrintReport({ project, runId }: { project: Project; runId: string }) {
  useCrumbs([{ label: project.name, href: projectHref(project.key) },
    { label: "Results", href: projectHref(project.key, "/runs") }, { label: "Run", href: projectHref(project.key, `/runs/${runId}`) },
    { label: "Report" }]);
  const { data: run, error } = useLoad(() => api.get<RunDetail>(`/api/runs/${runId}`), [runId]);
  const rd = run && isReadiness(run.readiness) ? run.readiness : null;

  // The PDF's file name comes from the page title.
  useEffect(() => {
    if (!rd) return;
    const old = document.title;
    document.title = `${project.name} - ${rd.mode.title} report - ${runId}`;
    return () => { document.title = old; };
  }, [rd, project.name, runId]);

  if (error) return <Notice tone="bad" className="mt-5">{error.message}</Notice>;
  if (!run) return <Skeleton />;
  const back = projectHref(project.key, `/runs/${runId}`);
  if (!rd) {
    const why = run.summary.status === "running" ? "The run is still going. The report is ready when it finishes."
      : (run.readiness as { error?: string } | null)?.error || "This run has no report.";
    return <Notice tone="warn" className="mt-5">{why} <a href={back}>Back to the run</a></Notice>;
  }

  const sm = run.summary;
  const bugs = sm.agent_options?.bugs || "";
  return (
    <div id="print-report" className="grid gap-5 pt-4 print:pt-0 print:gap-4 max-w-[1040px]">
      <div className="flex flex-wrap items-center gap-2.5 print:hidden">
        <Button variant="ghost" icon="back" href={back}>Back to the run</Button>
        <span className="flex-1" />
        <span className="text-[13px] text-muted">Pick "Save as PDF" as the printer in the next window.</span>
        <Button variant="primary" icon="printer" id="save-pdf" onClick={() => window.print()}>Save as PDF</Button>
      </div>

      <header className="flex flex-wrap items-end gap-x-6 gap-y-2 pb-4 border-b border-line">
        <div className="flex items-center gap-3 flex-1 min-w-[260px]">
          <img src="/static/logo.svg" alt="" className="size-9 rounded-[9px]" />
          <div>
            <div className="text-[13px] font-medium text-muted">Vocto · Release readiness report</div>
            <h1 className="text-[22px] font-bold tracking-[-.02em] leading-tight">{project.name} · {modeTitle(rd.mode.id)} test</h1>
          </div>
        </div>
        <dl className="grid grid-cols-[auto_auto] gap-x-3 gap-y-0.5 text-[13px]">
          <dt className="text-muted">Run</dt><dd className="font-mono">{runId}</dd>
          <dt className="text-muted">Tested</dt><dd>{fmtDate(sm.started_at)} · {rd.channel === "voice" ? "Voice" : "Text"}</dd>
        </dl>
      </header>

      <VerdictBanner r={rd} note={bugs ? (
        <Notice tone="warn">This run had planted bugs switched on ({bugs}), to prove the tests catch them. It is not a real release check.</Notice>
      ) : undefined} />
      <ExecutiveSummary r={rd} skipVerdict />
      <section className="break-inside-avoid"><Scorecard r={rd} /></section>
      <ConditionsCard r={rd} />
      <FailuresCard r={rd} />
      <FindingsList r={rd} print />
      <MetricsCard r={rd} />
      <CoverageCard r={rd} />
      <MethodCard r={rd} />
      <footer className="text-[13px] text-muted pt-2 border-t border-line">
        Made by Vocto from run {runId}. Every finding links back to the calls it came from in the Vocto dashboard.
      </footer>
    </div>
  );
}
