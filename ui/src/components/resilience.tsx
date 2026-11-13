// Resilience › Test cases: how this agent's own tools say no, for the business refusal drills (already taken,
// payment declined, out of stock). Claude can draft answers that sound like the real API (errorwriter.py);
// the person checks them here and saves them to the profile (resilience.refusals). Nothing is saved unasked.
import { useState } from "react";
import { useToast } from "./feedback";
import { Area, type Obj } from "./form";
import { Button, Card, CardHead, Chip, Notice, Skeleton } from "./ui";
import { useLoad } from "../lib/app";
import { api, qs } from "../lib/api";
import { setupApi } from "../lib/setup";

interface Kind { id: string; title: string; about: string; status: number; body: unknown }
interface Refusal { tool?: string; kind: string; status?: number; body: unknown; from?: string }
interface Page { tools: string[]; kinds: Kind[]; refusals: Refusal[] }
interface Draft { refusal: Refusal; problems: string[] }
interface Edit { status: number; body: string; from?: string; problems?: string[] }

const page = (key: string, file: string) => api.get<Page>(`/api/project/${key}/resilience?${qs({ file })}`);
const write = (key: string, file: string) => api.post<{ drafts: Draft[]; cost_usd: number }>(`/api/project/${key}/resilience/write`, { file });

const show = (body: unknown) => typeof body === "string" ? body : JSON.stringify(body, null, 2);

export function RefusalsCard({ projectKey, project, file }: { projectKey: string; project: string; file: string }) {
  const toast = useToast();
  const { data, error, reload } = useLoad(() => page(projectKey, file), [projectKey, file]);
  const [edits, setEdits] = useState<Record<string, Edit>>({});
  const [busy, setBusy] = useState<"" | "write" | "save">("");
  if (error) return <Notice tone="bad">{error.message}</Notice>;
  if (!data) return <Skeleton height={120} />;
  if (!data.tools.length) return null;
  const saved = (tool: string, kind: string) => data.refusals.find((r) => r.kind === kind && (r.tool === tool || r.tool === "*" || !r.tool));
  const keyOf = (tool: string, kind: string) => `${tool}|${kind}`;
  const current = (tool: string, k: Kind): Edit | null => {
    const e = edits[keyOf(tool, k.id)];
    if (e) return e;
    const s = saved(tool, k.id);
    return s ? { status: s.status || k.status, body: show(s.body), from: s.from } : null;
  };
  const dirty = Object.keys(edits).length > 0;

  const draft = async () => {
    setBusy("write");
    try {
      const r = await write(projectKey, file);
      const next = { ...edits };
      for (const d of r.drafts) {
        if (!d.refusal.tool || !data.tools.includes(d.refusal.tool)) continue;
        next[keyOf(d.refusal.tool, d.refusal.kind)] = { status: d.refusal.status || 409, body: show(d.refusal.body), from: "ai-writer", problems: d.problems };
      }
      setEdits(next);
      toast(`${r.drafts.length} answer${r.drafts.length === 1 ? "" : "s"} drafted ($${r.cost_usd.toFixed(3)}). Check them, then Save.`);
    } catch (e) { toast((e as Error).message, "bad"); }
    finally { setBusy(""); }
  };

  const save = async () => {
    const out: Obj[] = [];
    for (const tool of data.tools) {
      for (const k of data.kinds) {
        const c = current(tool, k);
        if (!c) continue;
        let body: unknown = c.body;
        try { body = JSON.parse(c.body); } catch { /* kept as text: some APIs answer in plain words */ }
        out.push({ tool, kind: k.id, status: c.status, body: body as Obj, ...(c.from ? { from: c.from } : {}) });
      }
    }
    setBusy("save");
    try {
      const r = await setupApi.saveProfile(project, file, { resilience: { refusals: out } });
      if (!r.saved) { toast(r.problems.join("; ") || "Not saved", "bad"); return; }
      setEdits({}); reload(); toast("Saved: the refusal drills now answer this way.");
    } catch (e) { toast((e as Error).message, "bad"); }
    finally { setBusy(""); }
  };

  return (
    <Card id="refusals-card">
      <CardHead level={2} title="How your tools say no" sub="used by the Already taken, Payment declined and Out of stock drills on Run">
        <Button size="sm" icon="sparkles" busy={busy === "write"} onClick={draft} id="refusals-write">Write with AI</Button>
      </CardHead>
      <p className="text-[13px] text-ink-2 mb-3">A refusal drill makes the tool answer with a business “no” instead of crashing. Answers shaped like your real API test the agent better than Vocto's plain one. Claude drafts them from each tool's description; check them before you save. Untouched cards use Vocto's plain answer.</p>
      <div className="grid gap-3">
        {data.tools.map((tool) => (
          <div key={tool} className="grid gap-2">
            <div className="font-mono text-[13px] font-semibold">{tool}</div>
            <div className="grid gap-2 grid-cols-1 lg:grid-cols-3">
              {data.kinds.map((k) => {
                const c = current(tool, k);
                const set = (patch: Partial<Edit>) =>
                  setEdits({ ...edits, [keyOf(tool, k.id)]: { status: c?.status || k.status, body: c?.body ?? show(k.body), ...patch } });
                const problems = edits[keyOf(tool, k.id)]?.problems || [];
                return (
                  <div key={k.id} className="rounded-lg border border-line p-2.5 grid gap-1.5 content-start" data-refusal={`${tool}:${k.id}`}>
                    <div className="flex items-center gap-2 flex-wrap">
                      <b className="text-[13px] font-semibold flex-1">{k.title}</b>
                      {c?.from === "ai-writer" && <Chip tone="accent" icon="sparkles">AI draft</Chip>}
                      {!c && <Chip>Vocto's plain answer</Chip>}
                    </div>
                    <label className="text-[13px] text-muted flex items-center gap-2">Status
                      <input type="number" className="w-20 rounded-md border border-line bg-surface px-1.5 py-0.5 text-ink" value={c?.status || k.status} min={400} max={599}
                        onChange={(e) => set({ status: Number(e.target.value) || k.status })} />
                    </label>
                    <Area mono rows={4} value={c?.body ?? show(k.body)} onChange={(v) => set({ body: v })} />
                    {problems.length > 0 && <Notice tone="warn">{problems.join("; ")}</Notice>}
                  </div>
                );
              })}
            </div>
          </div>
        ))}
      </div>
      <div className="flex gap-2 mt-3">
        <Button variant="primary" busy={busy === "save"} disabled={!dirty} onClick={save} id="refusals-save">Save</Button>
        {dirty && <Button variant="ghost" onClick={() => setEdits({})}>Undo changes</Button>}
      </div>
    </Card>
  );
}
