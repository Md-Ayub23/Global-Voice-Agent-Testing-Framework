// The raw editor: a project's profile, scenario and persona files by hand, checked by the same
// loader and schemas as a run before anything is saved. (The setup pages are the normal way.)
import { useCallback, useEffect, useRef, useState } from "react";
import { useToast } from "../components/feedback";
import { Icon, type IconName } from "../components/Icon";
import { Button, Card, CardHead, Chip, Divider, EmptyState, Kbd, Notice, PageHead, Skeleton } from "../components/ui";
import { api, qs } from "../lib/api";
import { useCrumbs } from "../lib/app";
import { pl, projectHref } from "../lib/links";
import { cx } from "../lib/format";
import { setLeaveGuard } from "../lib/router";
import type { CheckResult, Project, ProjectFile } from "../lib/types";

const KIND: [string, string, IconName][] = [["client_profile", "Profiles", "layers"], ["scenario", "Scenarios", "target"], ["persona", "Personas", "user"]];
const DISCARD = "Discard unsaved changes?";

function CheckNotice({ check }: { check: CheckResult | null }) {
  if (!check) return null;
  if (check.ok) {
    return <Notice tone="good"><b>All good.</b> Every profile in this folder loads with this file{check.saved ? ", and it is saved" : ""}.</Notice>;
  }
  return (
    <Notice tone={check.syntax ? "warn" : "bad"}>
      <b>{check.syntax ? (check.saved ? "Saved, but the project has problems:" : "Problems found:") : "Not saved: the YAML can't be read."}</b>
      <ul className="mt-1.5 pl-[18px] list-disc">
        {check.problems.map((p, i) => <li key={i} className="font-mono text-[12.5px] break-words">{p}</li>)}
      </ul>
    </Notice>
  );
}

function Editor({ project, initialFile }: { project: string; initialFile: string | null }) {
  const toast = useToast();
  const [proj] = useState(project);
  const [files, setFiles] = useState<ProjectFile[] | null>(null);
  const [file, setFile] = useState<string | null>(initialFile);
  const [text, setText] = useState("");
  const [saved, setSaved] = useState("");
  const [check, setCheck] = useState<CheckResult | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const ta = useRef<HTMLTextAreaElement>(null);
  const gutter = useRef<HTMLDivElement>(null);
  const dirty = text !== saved;
  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;

  // Leaving the page (in the app or closing the tab) with unsaved edits asks first.
  useEffect(() => {
    setLeaveGuard(() => !dirtyRef.current || confirm("You have unsaved changes. Leave without saving?"));
    const onUnload = (e: BeforeUnloadEvent) => { if (dirtyRef.current) e.preventDefault(); };
    window.addEventListener("beforeunload", onUnload);
    return () => { setLeaveGuard(null); window.removeEventListener("beforeunload", onUnload); };
  }, []);

  const openFile = useCallback(async (p: string, f: string | null) => {
    setCheck(null);
    if (!f) { setText(""); setSaved(""); return; }
    const r = await api.get<{ text: string }>(`/api/projects/file?${qs({ project: p, file: f })}`);
    setText(r.text);
    setSaved(r.text);
  }, []);

  // Loads a project's file list and opens `want` (or the first file); returns the list.
  const loadFiles = useCallback(async (p: string, want: string | null) => {
    try {
      const list = await api.get<ProjectFile[]>(`/api/projects/files?${qs({ project: p })}`);
      const f = list.some((x) => x.file === want) ? want : list[0]?.file ?? null;
      setFiles(list);
      setFile(f);
      await openFile(p, f);
      setLoadError(null);
    } catch (e) {
      setLoadError((e as Error).message);
    }
  }, [openFile]);

  useEffect(() => { void loadFiles(project, initialFile); }, [project, initialFile, loadFiles]);

  const lines = text.split("\n").length;
  const height = Math.max(520, lines * 20 + 40);   // grows with the file: one scroll bar, the page's

  const pickFile = async (f: string) => {
    if (f === file || (dirty && !confirm(DISCARD))) return;
    setFile(f);
    try { await openFile(proj, f); } catch (e) { toast((e as Error).message, "bad"); }
  };
  const save = async () => {
    if (!dirty || !file) return;
    try {
      const r = await api.put<CheckResult>("/api/projects/file", { project: proj, file, text });
      setCheck(r);
      if (r.saved) { setSaved(text); toast(r.ok ? "Saved" : "Saved, with problems to fix"); }
    } catch (e) { toast((e as Error).message, "bad"); }
  };
  const runCheck = async () => {
    try { setCheck(await api.post<CheckResult>("/api/projects/check", { project: proj, file, text })); }
    catch (e) { toast((e as Error).message, "bad"); }
  };
  const create = async (folder: string, kind: string) => {
    if (dirty && !confirm(DISCARD)) return;
    const name = prompt(`Name of the new ${kind} file (letters, numbers and dashes):`, `my-${kind}`);
    if (!name) return;
    const clean = name.trim().toLowerCase().replace(/\.ya?ml$/, "").replace(/[^a-z0-9-]+/g, "-").replace(/^-+|-+$/g, "");
    if (!clean) { toast("That name has no letters or numbers", "bad"); return; }
    const path = `${folder}/${clean}.yaml`;
    const list = files || [];
    const template = list.find((f) => f.kind === kind && f.file === file) || list.find((f) => f.kind === kind);
    try {
      let body = `schema_version: "0.1"\nkind: ${kind}\nid: ${clean}\n`;
      if (template) {
        body = (await api.get<{ text: string }>(`/api/projects/file?${qs({ project: proj, file: template.file })}`)).text;
        const newId = kind === "scenario" ? `${(template.id || "").split(".")[0] || "my"}.${clean}` : clean;
        body = body.replace(/^id:\s*.*$/m, `id: ${newId}`);
      }
      const r = await api.put<CheckResult>("/api/projects/file", { project: proj, file: path, text: body, create: true });
      toast(`Created ${path}`);
      await loadFiles(proj, path);
      setCheck(r);
    } catch (e) { toast((e as Error).message, "bad"); }
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Tab") {
      e.preventDefault();
      const el = e.currentTarget;
      const s = el.selectionStart;
      el.setRangeText("  ", s, el.selectionEnd, "end");
      setText(el.value);
    } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
      e.preventDefault();
      void save();
    }
  };

  const cur = files?.find((f) => f.file === file);
  const groups = new Map<string, ProjectFile[]>();
  for (const f of files || []) groups.set(f.kind, [...(groups.get(f.kind) || []), f]);
  const fileBtn = (f: ProjectFile, ic: IconName, label: string) => (
    <button key={f.file} type="button" data-file={f.file} title={f.title || ""} onClick={() => void pickFile(f.file)}
      className={cx("w-full flex items-center gap-2 px-2.5 py-[7px] rounded-lg text-left text-[13px] cursor-pointer transition-colors",
        f.file === file ? "bg-accent-soft text-accent-ink font-semibold" : "text-ink-2 hover:bg-surface-2 hover:text-ink")}>
      <Icon name={ic} className="flex-none" /><span className="truncate">{label}</span>
    </button>
  );

  return (
    <>
      <PageHead title="Raw editor"
        sub={<>For experts: the text behind the setup pages. Every save is checked the same way. <a href={pl("/setup/plan")}>Back to the test plan</a></>} />
      {loadError && <Notice tone="bad" className="mb-4">{loadError}</Notice>}
      <div className="grid gap-4 items-start grid-cols-1 lg:grid-cols-[260px_minmax(0,1fr)]">
        <Card className="p-2.5 lg:sticky lg:top-[70px]">
          {!files ? <Skeleton height={200} className="mt-0" /> : (
            <div className="flex flex-col gap-0.5">
              {KIND.map(([k, label, ic]) => groups.get(k)?.length ? (
                <div key={k}>
                  <div className="text-[11px] uppercase tracking-[.08em] text-muted px-2.5 pt-2.5 pb-1">{label}</div>
                  {groups.get(k)!.map((f) => fileBtn(f, ic, f.file.split("/").pop() || f.file))}
                </div>
              ) : null)}
              {groups.get("unreadable")?.length ? (
                <div>
                  <div className="text-[11px] uppercase tracking-[.08em] text-muted px-2.5 pt-2.5 pb-1">Can't read</div>
                  {groups.get("unreadable")!.map((f) => fileBtn(f, "alert", f.file))}
                </div>
              ) : null}
            </div>
          )}
          <Divider />
          <div className="flex flex-col gap-1.5">
            <Button size="sm" icon="plus" id="new-scenario" onClick={() => void create("scenarios", "scenario")}>New scenario</Button>
            <Button size="sm" icon="plus" id="new-persona" onClick={() => void create("personas", "persona")}>New persona</Button>
          </div>
          <div className="text-muted text-[11.5px] px-1 pt-2.5 pb-0.5">New items start as a copy of the selected one of that kind.</div>
        </Card>

        <div className="flex flex-col gap-4 min-w-0">
          <Card>
            <CardHead title={<span className="font-mono break-all">{file || ""}</span>}>
              <div className="flex items-center gap-2 flex-wrap">
                {dirty ? <Chip tone="warn">unsaved</Chip> : cur ? <Chip tone="good">saved</Chip> : null}
                <Button size="sm" id="revert" disabled={!dirty} onClick={() => { setText(saved); setCheck(null); }}>Revert</Button>
                <Button size="sm" id="check" icon="shield" disabled={!file} onClick={() => void runCheck()}>Check</Button>
                <Button size="sm" id="save" variant="primary" icon="save" disabled={!dirty} onClick={() => void save()}>Save</Button>
              </div>
            </CardHead>
            {files && !files.length ? <EmptyState icon="folder" title="No files in this project" /> : (
              <div className="flex border border-line rounded-[11px] overflow-hidden bg-surface-2 focus-within:border-accent transition-colors">
                <div id="gutter" ref={gutter} aria-hidden="true"
                  className="flex-none py-3 px-2.5 text-right font-mono text-[12.5px] leading-5 text-muted select-none whitespace-pre border-r border-line overflow-hidden"
                  style={{ height }}>
                  {Array.from({ length: lines }, (_, i) => i + 1).join("\n")}
                </div>
                <textarea id="text" ref={ta} spellCheck={false} autoComplete="off" autoCapitalize="off" value={text}
                  onChange={(e) => setText(e.target.value)} onKeyDown={onKeyDown}
                  onScroll={(e) => { if (gutter.current) gutter.current.scrollTop = e.currentTarget.scrollTop; }}
                  className="flex-1 min-w-0 py-3 px-3 font-mono text-[12.5px] leading-5 bg-surface text-ink outline-none resize-none whitespace-pre overflow-x-auto"
                  style={{ height, tabSize: 2 }} />
              </div>
            )}
            <div className="text-muted text-[11.5px] mt-2 flex flex-wrap items-center gap-1">
              <Kbd>Ctrl</Kbd>+<Kbd>S</Kbd> saves · <Kbd>Tab</Kbd> indents two spaces · the whole project is checked against the change
            </div>
          </Card>
          <div id="result"><CheckNotice check={check} /></div>
        </div>
      </div>
    </>
  );
}

export function Files({ project, query }: { project: Project; query: URLSearchParams }) {
  useCrumbs([{ label: "Projects", href: "#/projects" }, { label: project.name, href: projectHref(project.key) }, { label: "Raw editor" }]);
  return <Editor project={project.path} initialFile={query.get("file")} />;
}
