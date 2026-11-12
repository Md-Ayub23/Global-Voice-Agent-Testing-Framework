// The AI analysis of a run (src/vatf/analysis.py): findings grouped into causes, with a suggested fix.
// It runs only when asked (it costs money), is always marked as AI, and never changes the decision.
import { useState } from "react";
import { Icon } from "./Icon";
import { Button, Card, CardHead, Notice } from "./ui";
import { api } from "../lib/api";
import { cx } from "../lib/format";
import type { RunAnalysis, Readiness } from "../lib/types";

const WHO: Record<string, string> = { agent: "Change the agent", setup: "Vocto's set-up", test: "The test case", unclear: "Unclear" };
const SURE: Record<string, string> = { high: "Fairly sure", medium: "Likely", low: "A guess" };

export function AnalysisCard({ runId, r, saved, callHref, apiKey }: {
  runId: string; r: Readiness; saved: RunAnalysis | null | undefined; callHref: (testId: string) => string; apiKey: boolean;
}) {
  const [doc, setDoc] = useState<RunAnalysis | null>(saved || null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  if (!r.findings.length) return null;
  const ask = async () => {
    setBusy(true); setErr(null);
    try { setDoc(await api.post<RunAnalysis>(`/api/runs/${runId}/analysis`, {})); }
    catch (e) { setErr((e as Error).message); }
    finally { setBusy(false); }
  };
  const jump = (id: string) => document.querySelector(`[data-finding="${id}"]`)?.scrollIntoView({ behavior: "smooth", block: "center" });
  return (
    <Card id="ai-analysis">
      <CardHead level={2} title={<span className="inline-flex items-center gap-2">What's going wrong and how to fix it <span className="text-[12px] font-semibold px-1.5 py-0.5 rounded-md bg-surface-3 text-ink-2">AI</span></span>}
        sub="Claude reads the findings and their proof, and groups them into causes. It explains; it never changes the result.">
        <Button size="sm" variant={doc ? "ghost" : "primary"} icon="sparkles" busy={busy} disabled={busy || !apiKey} onClick={ask} id="analyse">
          {doc ? "Ask again" : "Explain with AI"}
        </Button>
      </CardHead>
      {!apiKey && <Notice tone="warn" className="text-[13px]">Add your Claude API key in Settings to use this.</Notice>}
      {err && <Notice tone="bad" className="text-[13px]">{err}</Notice>}
      {!doc && !err && apiKey && <div className="text-[13px] text-muted">One request to {busy ? "Claude…" : "Claude (usually a few cents). Nothing is sent until you press the button."}</div>}
      {doc && (
        <div className="flex flex-col gap-3">
          {doc.stale && <Notice tone="warn" className="text-[13px]">The findings changed since this was written (a label was saved). Ask again for an up-to-date analysis.</Notice>}
          {doc.summary && <p className="text-[13.5px] leading-[1.55]">{doc.summary}</p>}
          {doc.check_first && <div className="text-[13.5px]"><b>Check first:</b> {doc.check_first}</div>}
          <div className="grid gap-2.5">
            {doc.causes.map((c, i) => (
              <div key={i} data-cause={i} className={cx("glass border rounded-[16px] p-3.5 flex flex-col gap-1.5", c.blocks ? "border-[color-mix(in_srgb,var(--bad)_35%,var(--line))]" : "border-line")}>
                <div className="flex flex-wrap items-center gap-2">
                  <b className="text-[14px] flex-1 min-w-0">{c.title}</b>
                  <span className="text-[12px] text-muted">{WHO[c.who] || c.who} · {SURE[c.confidence] || c.confidence}</span>
                </div>
                <div className="text-[13.5px] text-ink-2 leading-[1.5]">{c.explanation}</div>
                {c.fix && <div className="text-[13.5px]"><Icon name="tool" size={13} className="inline mr-1 -mt-0.5" /><b>Try:</b> {c.fix}</div>}
                <div className="flex flex-wrap gap-1.5 text-[13px] text-muted items-center">
                  <span>Explains</span>
                  {c.findings.map((id) => <button key={id} type="button" onClick={() => jump(id)} className="font-mono underline decoration-dotted cursor-pointer">{id}</button>)}
                  {c.calls.length > 0 && <span className="ml-2">Calls</span>}
                  {c.calls.map((t) => <a key={t} href={callHref(t)} className="font-mono">{t.slice(-9)}</a>)}
                </div>
              </div>
            ))}
          </div>
          {doc.not_covered.length > 0 && <div className="text-[13px] text-muted">Not grouped by the AI: {doc.not_covered.join(", ")}. Read those findings on their own.</div>}
          <div className="text-[12px] text-muted">Written by {doc.model} · ${doc.cost_usd.toFixed(3)} · {new Date(doc.created_at).toLocaleString()}</div>
        </div>
      )}
    </Card>
  );
}
