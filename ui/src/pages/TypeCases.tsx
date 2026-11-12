// A test type's own test cases: the list, a form to add or change one, and Claude's drafts for this type.
// Functional, Conversation & timing, Speech & accents and Resilience each own their cases (a case's
// test_type, or where src/vatf/modes.py type_of places an older file).
import { useState } from "react";
import { useToast } from "../components/feedback";
import { CoverageCard, VariationsCard } from "../components/functional";
import { Help } from "../components/form";
import { Icon } from "../components/Icon";
import { Button, Card, EmptyState, Notice, Skeleton, Table, rowLink } from "../components/ui";
import { qs } from "../lib/api";
import { useApp, useLoad } from "../lib/app";
import { cx } from "../lib/format";
import { mainProfile, projectHref } from "../lib/links";
import { MODE_TITLE } from "../lib/modes";
import { navigate } from "../lib/router";
import { setupApi } from "../lib/setup";
import type { Project, Scenario, TestModeId } from "../lib/types";
import { PlanWriterCard } from "./setup/PlanWriterCard";
import { ScenarioForm } from "./setup/ScenarioForm";

export type CaseType = "functional" | "timing" | "speech" | "resilience";
export const CASE_TYPES: CaseType[] = ["functional", "timing", "speech", "resilience"];

// What a good case of each type looks like, for the empty list and the page's hint.
const ABOUT: Record<CaseType, { empty: string; adds?: string }> = {
  functional: { empty: "Start with the agent's main job (the happy path), then a caller who leaves out a detail, one who changes their mind, and a request it must refuse." },
  timing: { empty: "A case where the caller cuts in while the agent talks, says \"mm-hm\" in the middle, or goes quiet. Each one sets what must happen and how fast.",
    adds: "also times every Functional case, so you see answer speed on normal calls too" },
  speech: { empty: "A case on a hard line: background noise, a phone line, lost audio, or a caller with an accent spelling a name or a number.",
    adds: "also runs every Functional case with the harder callers you pick on Run" },
  resilience: { empty: "A case where one of the agent's tools fails (times out, errors or is slow) and the agent must recover or say honestly that it could not finish.",
    adds: "can also drill the Functional cases that end in a booking or order: their tool fails on purpose, one failure at a time" },
};

const EXPECTED: Record<string, string> = {
  success: "Gets it done", honest_failure: "Says it can't", recover_or_honest: "Recovers or is honest", no_action: "Saves nothing",
};

function what(s: Scenario, type: CaseType): string {
  if (type === "resilience" && s.faults.length) return s.faults.join(", ");
  if (type === "timing" && s.audio?.barge_in) return "Caller cuts in";
  if (type === "speech" && s.audio?.noise && s.audio.noise !== "none") return `${s.audio.noise[0].toUpperCase()}${s.audio.noise.slice(1)} noise${s.audio.snr_db != null ? `, ${s.audio.snr_db} dB` : ""}`;
  return s.expected ? EXPECTED[s.expected] || s.expected : `${s.checks} check${s.checks === 1 ? "" : "s"}`;
}

export function TypeCases({ project, type, edit }: { project: Project; type: CaseType; edit: string | null }) {
  const { reloadProjects } = useApp();
  const toast = useToast();
  const main = mainProfile(project);
  const [drafting, setDrafting] = useState(false);
  const [focus, setFocus] = useState("");
  const [varying, setVarying] = useState<Scenario | null>(null);
  const [coverKey, setCoverKey] = useState(0);
  const here = projectHref(project.key, `/test/${type}`);
  const { data: detail, error, reload } = useLoad(() => setupApi.profile(project.path, main?.file || "profile.yaml"), [project.path, main?.file]);
  const { data: doc } = useLoad(() => edit && edit !== "new" ? setupApi.doc(project.path, edit) : Promise.resolve(null), [project.path, edit]);
  if (error) return <Notice tone="bad">{error.message}</Notice>;
  if (!detail || !main) return <Skeleton height={240} />;
  const all = main.scenarios;
  const mine = all.filter((s) => s.test_type === type);
  const functional = all.filter((s) => s.test_type === "functional");
  const drillable = functional.filter((s) => s.drill);
  const reloadAll = () => { reload(); void reloadProjects(); setCoverKey((k) => k + 1); };

  if (edit) {
    if (edit !== "new" && !doc) return <Skeleton height={420} />;
    return (
      <div className="grid gap-3">
        <div><Button size="sm" variant="ghost" icon="back" onClick={() => navigate(here)}>{MODE_TITLE[type]} test cases</Button></div>
        <ScenarioForm key={edit} detail={detail} file={edit === "new" ? null : edit} initial={doc?.doc || null} testType={type}
          onDone={(file) => { if (file) reloadAll(); navigate(here); }} />
        {edit !== "new" && <Help>Advanced: <a href={`${projectHref(project.key, "/files")}?${qs({ file: edit })}`}>open the raw editor</a>.</Help>}
      </div>
    );
  }

  const remove = async (s: Scenario) => {
    if (!confirm(`Delete "${s.title}"? This can't be undone.`)) return;
    try { await setupApi.deleteDoc(project.path, s.file); toast(`Deleted ${s.title}`); reloadAll(); }
    catch (e) { toast((e as Error).message, "bad"); }
  };
  const about = ABOUT[type];
  const extra = type === "resilience" ? drillable.length : type === "timing" || type === "speech" ? functional.length : 0;

  return (
    <div className="grid gap-4" id="type-cases">
      <div className="flex items-center gap-3 flex-wrap">
        <p className="flex-1 min-w-[240px] text-[13px] text-ink-2">
          {mine.length ? `${mine.length} ${MODE_TITLE[type]} test case${mine.length === 1 ? "" : "s"}.` : `No ${MODE_TITLE[type]} test cases yet.`}
          {about.adds && extra > 0 && <> A run {about.adds} ({extra}).</>}
        </p>
        <Button id="draft-cases" icon="sparkles" onClick={() => { setFocus(""); setDrafting(!drafting); }}>{drafting ? "Close drafts" : "Draft with AI"}</Button>
        <Button id="new-case" variant="primary" icon="plus" href={`${here}?${qs({ edit: "new" })}`}>New test case</Button>
      </div>

      {type === "functional" && !drafting && (
        <CoverageCard key={coverKey} projectKey={project.key} file={main.file || "profile.yaml"}
          onWrite={(f) => { setFocus(f); setDrafting(true); }} />
      )}

      {drafting && <PlanWriterCard detail={detail} reload={reloadAll} projectKey={project.key} testType={type} focus={focus} />}

      {varying && (
        <VariationsCard key={varying.id} projectKey={project.key} file={main.file || "profile.yaml"} scenarioId={varying.id} title={varying.title}
          onClose={() => setVarying(null)} onAdded={() => { setVarying(null); reloadAll(); }} />
      )}

      <Card flush>
        {mine.length ? (
          <Table head={["Test case", "Caller", "What it tests", "Matters", "Runs in", ""]}>
            {mine.map((s) => (
              <tr key={s.id} className={rowLink} data-scenario={s.id} onClick={() => navigate(`${here}?${qs({ edit: s.file })}`)}>
                <td className="min-w-[260px]"><b className="font-medium block">{s.title}</b><span className="text-muted text-[13px] font-mono">{s.id}</span></td>
                <td className="text-ink-2 whitespace-nowrap">{s.style === "scripted" ? "Scripted lines" : s.persona || "—"}</td>
                <td className="text-ink-2">{what(s, type)}</td>
                <td className={cx("whitespace-nowrap", s.criticality === "critical" ? "text-ink font-medium" : "text-ink-2")}>{s.criticality === "critical" ? "Critical" : s.criticality === "exploratory" ? "Exploratory" : "Normal"}</td>
                <td className="text-ink-2 whitespace-nowrap">{s.modes.length === 2 ? "Voice and text" : s.modes[0] === "voice" ? "Voice" : "Text"}</td>
                <td className="w-0 whitespace-nowrap">
                  {type === "functional" && s.style === "simulated" && (
                    <Button size="sm" variant="ghost" iconOnly icon="sparkles" aria-label={`Caller variations of ${s.title}`} title="Caller variations"
                      onClick={(e) => { e.stopPropagation(); setVarying(s); window.scrollTo({ top: 0, behavior: "smooth" }); }} />
                  )}
                  <Button size="sm" variant="ghost" iconOnly icon="x" aria-label={`Delete ${s.title}`} onClick={(e) => { e.stopPropagation(); void remove(s); }} />
                </td>
              </tr>
            ))}
          </Table>
        ) : (
          <EmptyState icon="target" title={`No ${MODE_TITLE[type]} test cases yet`} text={about.empty}
            action={<div className="flex gap-2 justify-center"><Button icon="sparkles" onClick={() => setDrafting(true)}>Draft with AI</Button>
              <Button variant="primary" icon="plus" href={`${here}?${qs({ edit: "new" })}`}>New test case</Button></div>} />
        )}
      </Card>
      {type === "resilience" && drillable.length > 0 && (
        <Help><Icon name="bolt" size={13} className="inline -mt-0.5 mr-1" />Can be drilled: {drillable.map((s) => s.title).join(" · ")}. Pick the failures on Run.</Help>
      )}
    </div>
  );
}

export const isCaseType = (m: TestModeId): m is CaseType => (CASE_TYPES as string[]).includes(m);
