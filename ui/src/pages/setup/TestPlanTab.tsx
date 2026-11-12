// The test plan, as two set-up pages: test cases (scenarios: the situations to test, plus "Write tests for me")
// and callers (personas: who calls). Lists, then forms.
import { useEffect, useState } from "react";
import { useToast } from "../../components/feedback";
import { Help, type Obj } from "../../components/form";
import { Icon } from "../../components/Icon";
import { Button, Card, CardHead, Chip, EmptyState, Notice, Skeleton } from "../../components/ui";
import { qs } from "../../lib/api";
import { navigate } from "../../lib/router";
import { setupApi } from "../../lib/setup";
import { pl } from "../../lib/links";
import { str, type TabProps } from "./common";
import { PersonaForm } from "./PersonaForm";
import { PersonaLibrary, PlanWriterCard } from "./PlanWriterCard";
import { ScenarioForm } from "./ScenarioForm";

interface Item { file: string; doc: Obj }

export function TestPlanTab({ part, detail, reload, edit, projectKey }: TabProps & { part: "cases" | "callers"; edit: string | null; projectKey: string }) {
  const toast = useToast();
  const [items, setItems] = useState<Item[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [library, setLibrary] = useState(false);
  const files = [...detail.scenarios, ...detail.persona_files].map((f) => f.file);

  useEffect(() => {
    let live = true;
    Promise.all(files.map((f) => setupApi.doc(detail.project, f).then((r) => ({ file: f, doc: r.doc }))))
      .then((xs) => live && setItems(xs), (e: Error) => live && setError(e.message));
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detail]);

  const page = (e: string | null) => pl(`/setup/${e && (e === "new-persona" || e.startsWith("personas/")) ? "callers" : e ? "cases" : part}`);
  const go = (e: string | null) => navigate(e ? `${page(e)}?${qs({ edit: e })}` : page(null));
  const done = (file?: string) => { if (file) reload(); go(null); };

  if (error) return <Notice tone="bad">{error}</Notice>;
  if (!items) return <Skeleton height={240} />;
  if (edit) {
    const isNew = edit === "new-scenario" || edit === "new-persona";
    const item = items.find((i) => i.file === edit);
    if (!isNew && !item) return <Notice tone="bad">{edit} not found.</Notice>;
    const Form = edit === "new-persona" || edit.startsWith("personas/") ? PersonaForm : ScenarioForm;
    return (
      <div className="grid gap-3">
        <div><Button size="sm" variant="ghost" icon="back" onClick={() => go(null)}>{part === "callers" ? "All callers" : "All test cases"}</Button></div>
        <Form key={edit} detail={detail} file={isNew ? null : edit} initial={item ? item.doc : null} onDone={done} />
        {!isNew && <Help>Advanced: <a href={`${pl("/files")}?${qs({ file: edit })}`}>open the raw editor</a>.</Help>}
      </div>
    );
  }

  const remove = async (file: string) => {
    const what = items?.find((i) => i.file === file)?.doc;
    const label = str(what?.title) || str(what?.id) || "this item";
    if (!confirm(`Delete "${label}"? This can't be undone.`)) return;
    try { await setupApi.deleteDoc(detail.project, file); toast(`Deleted ${label}`); reload(); }
    catch (e) { toast((e as Error).message, "bad"); }
  };
  const scenarios = items.filter((i) => i.file.startsWith("scenarios/"));
  const personas = items.filter((i) => i.file.startsWith("personas/"));
  const modes = (d: Obj) => (Array.isArray(d.modes) ? (d.modes as string[]) : ["text", "voice"]);

  return (
    <div className="grid gap-4">
      {part === "cases" && <>
      {!scenarios.length && <PlanWriterCard detail={detail} reload={reload} projectKey={projectKey} />}
      <Card>
        <CardHead level={2} title={`Scenarios (${scenarios.length})`} sub="each one is a situation the agent must handle">
          <Button id="new-scenario" variant="primary" size="sm" icon="plus" onClick={() => go("new-scenario")}>New scenario</Button>
        </CardHead>
        {!scenarios.length ? (
          <EmptyState icon="target" title="No scenarios yet" text="Start with the agent's main job (the happy path), then what happens when its tools fail, then hard callers." />
        ) : (
          <div className="grid gap-2">
            {scenarios.map(({ file, doc }) => {
              const outcome = (doc.outcome || null) as Obj | null;
              return (
                <div key={file} data-scenario={str(doc.id)} className="flex items-center gap-3 flex-wrap p-3 border border-line rounded-[12px] hover:bg-surface-2/60 transition-colors">
                  <Icon name="target" className="text-muted" />
                  <div className="flex-1 min-w-[200px]">
                    <a className="font-semibold" href={`${page(file)}?${qs({ edit: file })}`}>{str(doc.title) || str(doc.id)}</a>
                    <div className="text-muted text-[13px] font-mono">{str(doc.id)}</div>
                  </div>
                  <Chip tone={doc.criticality === "critical" ? "accent" : "default"}>{str(doc.criticality)}</Chip>
                  <Chip>{doc.style === "scripted" ? "scripted" : `persona: ${str((doc.caller as Obj | undefined)?.persona)}`}</Chip>
                  {outcome && <Chip tone="tool">{str(outcome.expected).replace("_", " ")}</Chip>}
                  {modes(doc).map((m) => <Chip key={m} icon={m === "voice" ? "mic" : "text"}>{m}</Chip>)}
                  <Button size="sm" variant="ghost" iconOnly icon="x" aria-label={`Delete ${str(doc.title) || str(doc.id)}`} onClick={() => void remove(file)} />
                </div>
              );
            })}
          </div>
        )}
      </Card>
      {scenarios.length > 0 && <PlanWriterCard detail={detail} reload={reload} projectKey={projectKey} />}
      <Help>Advanced: <a href={pl("/files")}>open the raw file editor</a> to edit any file of this agent as text.</Help>
      </>}
      {part === "callers" && <Card>
        <CardHead level={2} title={`Personas (${personas.length})`} sub="who calls">
          <div className="flex gap-2">
            <Button id="persona-library-toggle" size="sm" icon="layers" onClick={() => setLibrary(!library)}>{library ? "Close the library" : "Add from library"}</Button>
            <Button id="new-persona" size="sm" icon="plus" onClick={() => go("new-persona")}>New persona</Button>
          </div>
        </CardHead>
        {library && <div className="mb-4"><PersonaLibrary projectKey={projectKey} detail={detail} onAdded={() => { setLibrary(false); reload(); }} /></div>}
        <div className="grid gap-2 sm:grid-cols-2">
          {personas.map(({ file, doc }) => (
            <div key={file} data-persona={str(doc.id)} className="flex items-start gap-3 p-3 border border-line rounded-[12px]">
              <Icon name="user" className="text-muted mt-0.5" />
              <div className="flex-1 min-w-0">
                <a className="font-semibold" href={`${page(file)}?${qs({ edit: file })}`}>{str(doc.id)}</a>
                <div className="text-ink-2 text-[13px] mt-0.5">{str(doc.description)}</div>
              </div>
              <Button size="sm" variant="ghost" iconOnly icon="x" aria-label={`Delete ${str(doc.title) || str(doc.id)}`} onClick={() => void remove(file)} />
            </div>
          ))}
        </div>
        {!personas.length && <EmptyState icon="user" title="No callers yet" text="Add a few from the library: a calm caller, an impatient one and one who interrupts cover most agents." />}
      </Card>}
    </div>
  );
}
