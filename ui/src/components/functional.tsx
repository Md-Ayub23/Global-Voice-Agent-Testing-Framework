// Functional test cases: what they cover (the agent's jobs, rules and write tools, with the gaps) and
// caller variations (the same case asked by callers who say it differently). Backend: src/vatf/coverage.py,
// src/vatf/variations.py.
import { useState } from "react";
import { useToast } from "./feedback";
import { Check, Help } from "./form";
import { Button, Card, CardHead, Chip, Notice, Skeleton } from "./ui";
import { useLoad } from "../lib/app";
import { cx, plural } from "../lib/format";
import { functionalApi, type Coverage, type VariationDraft } from "../lib/plan";

const GROUPS: [keyof Coverage["intents"][number]["counts"], string][] = [["happy", "Normal"], ["edge", "Edge cases"], ["adversarial", "Misuse"]];

function Count({ n }: { n: number }) {
  return <span className={cx("inline-flex justify-center min-w-[28px] h-6 px-2 rounded-full text-[13px] tabular-nums font-medium items-center",
    n ? "bg-good-soft text-good" : "bg-[var(--glass-strong)] text-muted border border-[var(--glass-line)]")}>{n}</span>;
}

export function CoverageCard({ projectKey, file, onWrite }: { projectKey: string; file: string; onWrite: (focus: string) => void }) {
  const { data, error } = useLoad(() => functionalApi.coverage(projectKey, file), [projectKey, file]);
  const [open, setOpen] = useState(false);
  if (error) return <Notice tone="bad">{error.message}</Notice>;
  if (!data) return <Skeleton height={120} />;
  const c = data;
  const shown = open ? c.gaps : c.gaps.slice(0, 4);
  return (
    <Card id="coverage">
      <CardHead level={2} title="What the test cases cover"
        sub={c.has_plan ? "the agent's jobs and rules, from its instructions, and its tools that change data"
          : "its tools that change data. Draft with AI once to also list its jobs and rules"}>
        {c.gaps.length > 0 && <Button size="sm" icon="sparkles" id="write-gaps" onClick={() => onWrite(c.focus)}>Write tests for these</Button>}
      </CardHead>
      {c.gaps.length ? (
        <div className="grid gap-1.5 mb-4">
          <p className="text-[13px] text-ink-2">{plural(c.gaps.length, "gap")}:</p>
          <ul className="grid gap-1 text-[13px] text-ink-2 list-disc pl-5">
            {shown.map((g) => <li key={`${g.kind}-${g.id}-${g.text}`}>{g.text[0].toUpperCase() + g.text.slice(1)}</li>)}
          </ul>
          {c.gaps.length > 4 && <button className="text-[13px] text-accent text-left w-fit" onClick={() => setOpen(!open)}>{open ? "Show fewer" : `Show all ${c.gaps.length}`}</button>}
        </div>
      ) : <Notice tone="good" className="mb-4">Every job has a normal, an edge and a misuse case, every rule has a case, and every tool is tested both ways.</Notice>}
      <div className="grid gap-4 grid-cols-1 lg:grid-cols-2">
        {c.intents.length > 0 && (
          <div>
            <h3 className="text-[13px] font-semibold mb-2">Jobs</h3>
            <table className="w-full text-[13px]">
              <thead><tr className="text-muted text-left"><th className="font-normal pb-1.5">Job</th>{GROUPS.map(([, t]) => <th key={t} className="font-normal pb-1.5 text-center">{t}</th>)}</tr></thead>
              <tbody>
                {c.intents.map((it) => (
                  <tr key={it.id} className="border-t border-[var(--glass-line)]" title={it.description}>
                    <td className="py-1.5 pr-2">{it.title}</td>
                    {GROUPS.map(([g]) => <td key={g} className="py-1.5 text-center"><Count n={it.counts[g]} /></td>)}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {c.tools.length > 0 && (
          <div>
            <h3 className="text-[13px] font-semibold mb-2">Tools that change data</h3>
            <table className="w-full text-[13px]">
              <thead><tr className="text-muted text-left"><th className="font-normal pb-1.5">Tool</th><th className="font-normal pb-1.5 text-center">Must work</th><th className="font-normal pb-1.5 text-center">Must not / fails honestly</th></tr></thead>
              <tbody>
                {c.tools.map((t) => (
                  <tr key={t.name} className="border-t border-[var(--glass-line)]">
                    <td className="py-1.5 pr-2 font-mono text-[12.5px]">{t.name}</td>
                    <td className="py-1.5 text-center" title={t.works.map((x) => x.title).join("\n")}><Count n={t.works.length} /></td>
                    <td className="py-1.5 text-center" title={t.refuses.map((x) => x.title).join("\n")}><Count n={t.refuses.length} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
      {c.rules.length > 0 && (
        <details className="mt-4">
          <summary className="text-[13px] text-ink-2 cursor-pointer">Rules ({c.rules.filter((r) => r.cases.length).length} of {c.rules.length} have a test case)</summary>
          <ul className="grid gap-1.5 mt-2">
            {c.rules.map((r) => (
              <li key={r.id} className="flex gap-2 items-start text-[13px]">
                <Count n={r.cases.length} />
                <span className="text-ink-2"><b className="font-medium text-ink">{r.id}</b> {r.text}{r.kind === "fact" && <span className="text-muted"> (a fact)</span>}</span>
              </li>
            ))}
          </ul>
        </details>
      )}
      {!c.has_plan && <Help className="mt-3">Jobs and rules come from Draft with AI, which reads the agent's instructions. Cases written by hand still count for the tools.</Help>}
    </Card>
  );
}

export function VariationsCard({ projectKey, file, scenarioId, title, onClose, onAdded }: {
  projectKey: string; file: string; scenarioId: string; title: string; onClose: () => void; onAdded: () => void;
}) {
  const toast = useToast();
  const [drafts, setDrafts] = useState<VariationDraft[] | null>(null);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const write = async () => {
    setBusy(true); setErr(null);
    try {
      const r = await functionalApi.vary(projectKey, scenarioId, file, 3);
      setDrafts(r.drafts);
      setPicked(new Set(r.drafts.filter((d) => !d.problems.length).map((d) => d.id)));
    } catch (e) { setErr((e as Error).message); }
    finally { setBusy(false); }
  };
  const add = async () => {
    if (!drafts) return;
    setBusy(true);
    try {
      const r = await functionalApi.addVariations(projectKey, drafts.filter((d) => picked.has(d.id)), file);
      toast(`Added ${plural(r.added.length, "variation")}${r.skipped.length ? `, skipped ${r.skipped.length}` : ""}`);
      onAdded();
    } catch (e) { toast((e as Error).message, "bad"); }
    finally { setBusy(false); }
  };
  return (
    <Card id="variations">
      <CardHead level={2} title={`Caller variations of "${title}"`}
        sub="the same task, facts and checks, asked by callers who say it differently">
        <Button size="sm" variant="ghost" icon="x" onClick={onClose}>Close</Button>
      </CardHead>
      {err && <Notice tone="bad" className="mb-3">{err}</Notice>}
      {!drafts ? (
        <div className="flex items-center gap-3 flex-wrap">
          <p className="flex-1 min-w-[240px] text-[13px] text-ink-2">Claude rewrites only the caller: other words, another order of details, another kind of caller.
            Vocto keeps the facts, the expected result and the checks from the original, so the variation is checked the same way.</p>
          <Button variant="primary" icon="sparkles" busy={busy} onClick={write} id="write-variations">Write 3 variations</Button>
        </div>
      ) : (
        <div className="grid gap-3">
          {drafts.map((d) => (
            <div key={d.id} className="rounded-[14px] border border-[var(--glass-line)] p-3.5 grid gap-1.5" data-variation={d.id}>
              <div className="flex items-start gap-2 flex-wrap">
                <div className="flex-1 min-w-[200px]">
                  <Check checked={picked.has(d.id)} onChange={(v) => { const n = new Set(picked); if (v) n.add(d.id); else n.delete(d.id); setPicked(n); }}
                    label={<b className="font-medium">{d.label}</b>} />
                </div>
                <Chip>{d.persona}{d.persona_new ? " (added from the library)" : ""}</Chip>
              </div>
              <p className="text-[13px] text-ink-2"><span className="text-muted">Goal:</span> {d.goal}</p>
              {d.rules.length > 0 && <p className="text-[13px] text-ink-2"><span className="text-muted">How they speak:</span> {d.rules.join(" · ")}</p>}
              <p className="text-[13px] text-muted">{d.why}</p>
              {d.problems.length > 0 && <Notice tone="bad">{d.problems.join("; ")}</Notice>}
            </div>
          ))}
          <div className="flex gap-2 justify-end">
            <Button variant="ghost" onClick={write} busy={busy} icon="sparkles">Write again</Button>
            <Button variant="primary" icon="plus" onClick={add} busy={busy} disabled={!picked.size}>Add {plural(picked.size, "variation")}</Button>
          </div>
          <Help>Variations run as normal cases, never critical: the original case still decides the release.</Help>
        </div>
      )}
    </Card>
  );
}
