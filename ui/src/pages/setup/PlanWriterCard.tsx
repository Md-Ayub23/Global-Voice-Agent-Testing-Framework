// "Write tests for me" on the Test plan page: paste the agent's instructions, Claude works out its jobs and
// rules and writes a draft plan in a fixed mix (normal calls, edge cases, tool failures, misuse, noise),
// Vocto checks every draft, and only the ones you tick become tests. Also the persona library.
import { useEffect, useMemo, useState } from "react";
import { useToast } from "../../components/feedback";
import { Area, Block, Help, Num, Text } from "../../components/form";
import { Icon } from "../../components/Icon";
import { Button, Card, Chip, Choice, Notice, Skeleton } from "../../components/ui";
import { useLoad } from "../../lib/app";
import { cx, plural } from "../../lib/format";
import { KIND_LABEL, planApi, type LibraryPersona, type Plan, type PlanCategory, type PlanPage, type PlanTest } from "../../lib/plan";
import { owner, setupApi } from "../../lib/setup";
import type { TabProps } from "./common";

const ORDER: PlanCategory[] = ["happy", "edge", "error", "adversarial", "audio", "timing", "speech"];
// What Claude writes for each test type, in the card's own words.
const TYPE_SUB: Record<string, string> = {
  functional: "normal calls for each of its jobs, edge cases (missing details, changes of mind, out-of-scope asks) and agent-specific misuse",
  timing: "voice calls about turn-taking: the caller cutting in or correcting a detail, saying \"mm-hm\", pausing mid-sentence and going quiet",
  speech: "voice calls that are hard to hear: background noise, a phone line, a bad signal, accents, a quiet voice and spelled names and numbers",
  resilience: "calls where one of the agent's tools fails (an error, a timeout, a slow or unreadable answer) and it must recover or say so honestly",
};
const TYPE_COUNT: Record<string, number> = { functional: 15, timing: 6, speech: 6, resilience: 6 };
const CRIT = ["critical", "normal", "exploratory"] as const;
const n = (k: number, word: string) => `${k} ${word}${k === 1 ? "" : "s"}`;

export function PlanWriterCard({ detail, reload, projectKey, testType, focus }: TabProps & { projectKey: string; testType?: string; focus?: string }) {
  const { data, error, reload: reloadPage } = useLoad(() => planApi.page(projectKey, detail.file, testType), [projectKey, detail, testType]);
  if (error) return <Notice tone="bad">{error.message}</Notice>;
  if (!data) return <Skeleton height={200} />;
  return <Writer detail={detail} reload={() => { reload(); reloadPage(); }} page={data} projectKey={projectKey} testType={testType} initialFocus={focus} />;
}

function Writer({ detail, reload, page, projectKey, testType, initialFocus }: TabProps & { page: PlanPage; projectKey: string; testType?: string; initialFocus?: string }) {
  const toast = useToast();
  const [prompt, setPrompt] = useState(page.agent_prompt);
  const [savingPrompt, setSavingPrompt] = useState(false);
  const [count, setCount] = useState(testType ? TYPE_COUNT[testType] || 10 : 20);
  const [focus, setFocus] = useState(initialFocus || "");
  useEffect(() => { if (initialFocus) setFocus(initialFocus); }, [initialFocus]);
  const [writing, setWriting] = useState(false);
  const [plan, setPlan] = useState<Plan | null>(page.plan);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [crit, setCrit] = useState<Record<string, string>>({});
  const [open, setOpen] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [showAnalysis, setShowAnalysis] = useState(false);

  useEffect(() => {
    setPlan(page.plan);
    setPicked(new Set((page.plan?.tests || []).filter((t) => t.selected && !t.accepted).map((t) => t.id)));
    setCrit({});
  }, [page.plan]);

  const promptDirty = prompt !== page.agent_prompt;
  const savePrompt = async () => {
    setSavingPrompt(true);
    try {
      const r = await setupApi.saveProfile(detail.project, owner(detail, "agent_prompt"), { agent_prompt: prompt.trim() ? prompt : null });
      if (!r.saved) toast(r.problems[0] || "Not saved", "bad"); else { toast("Instructions saved"); reload(); }
    } catch (e) { toast((e as Error).message, "bad"); }
    setSavingPrompt(false);
  };
  const write = async () => {
    if (promptDirty) await savePrompt();
    setWriting(true);
    try {
      const p = await planApi.write(projectKey, { count: Math.max(testType ? 1 : 5, Math.min(60, Math.round(count) || 20)), focus, file: detail.file, test_type: testType });
      setPlan(p.plan);
      setPicked(new Set((p.plan?.tests || []).filter((t) => t.selected).map((t) => t.id)));
      toast(`Wrote ${p.plan?.tests.length ?? 0} draft tests`);
    } catch (e) { toast((e as Error).message, "bad"); }
    setWriting(false);
  };
  const add = async () => {
    setAdding(true);
    try {
      const r = await planApi.add(projectKey, [...picked], crit, detail.file, testType);
      toast(`Added ${r.added.length} test${r.added.length === 1 ? "" : "s"}${r.personas.length ? ` and ${r.personas.length} persona${r.personas.length === 1 ? "" : "s"}` : ""}`);
      if (r.skipped.length) toast(`${r.skipped.length} skipped: ${r.skipped[0].reason}`, "bad");
      reload();
    } catch (e) { toast((e as Error).message, "bad"); }
    setAdding(false);
  };

  const groups = useMemo(() => ORDER.map((c) => ({ c, tests: (plan?.tests || []).filter((t) => t.category === c) })).filter((g) => g.tests.length), [plan]);
  const canWrite = page.api_key && (prompt.trim() || page.has_rules || detail.merged.description);
  const toggle = (id: string, on: boolean) => setPicked((s) => { const n = new Set(s); if (on) n.add(id); else n.delete(id); return n; });

  return (
    <Card id="plan-writer">
      <Block title={testType ? "Draft test cases with AI" : "Write tests for me"} icon="sparkles"
        sub={testType
          ? <>Claude reads the agent brief, its tools and its database checks, and drafts {TYPE_SUB[testType]}, written for this agent. Vocto checks every draft the same way it checks a hand-written test. Nothing becomes a test case until you tick it.</>
          : <>Claude reads what the agent is told to do, lists its jobs and rules, and writes a draft test plan: about 40% normal calls, 30% edge cases, 15% tool failures, 10% misuse and 5% noise and interruptions. Vocto checks every draft the same way it checks a hand-written test. Nothing becomes a test until you tick it.</>}>
        {!page.api_key && <Notice tone="warn">Add your Claude API key in <a href="#/settings">Settings</a> first: the plan is written by Claude.</Notice>}
      </Block>
      <Block title={testType ? "Agent brief" : "The agent's instructions"} sub="Paste its system prompt as written. Its tools, database checks and the judges' business rules are read too. The same brief is on Connect.">
        <Area id="agent-prompt" rows={7} value={prompt} onChange={setPrompt}
          placeholder="You are the booking assistant for… Always confirm the date and time before booking. Never…" />
        {promptDirty && <div className="mt-2"><Button size="sm" busy={savingPrompt} icon="save" onClick={() => void savePrompt()}>Save instructions</Button></div>}
      </Block>
      <Block title="Plan size">
        <div className="flex items-end gap-3 flex-wrap">
          <label className="grid gap-1 text-[13px] text-muted w-[120px]">How many<Num id="plan-count" value={count} min={testType ? 1 : 5} max={60} step={testType ? 1 : 5} onChange={(v) => setCount(v ?? 20)} /></label>
          <label className="grid gap-1 text-[13px] text-muted flex-1 min-w-[240px]">Anything to focus on (optional)<Text id="plan-focus" value={focus} onChange={setFocus} placeholder="e.g. refunds, callers who give a wrong order number" /></label>
          <Button id="write-plan" variant="primary" icon="sparkles" busy={writing} disabled={!canWrite || writing} onClick={() => void write()}>
            {testType ? (plan ? "Draft again" : "Draft test cases") : plan ? "Write a new plan" : "Write the plan"}
          </Button>
        </div>
        <Help>{writing ? "Claude is reading the instructions and writing the tests. This takes one to three minutes…" : `Written by ${page.model}. ${testType ? "Six drafts cost roughly $0.15 to $0.40." : "A 20-test plan costs roughly $0.30 to $0.80."}`}</Help>
      </Block>

      {plan && (
        <>
          <Block title="What Claude understood" sub={plan.analysis.summary}
            aside={<Button size="sm" variant="ghost" icon={showAnalysis ? "minus" : "plus"} onClick={() => setShowAnalysis(!showAnalysis)}>{showAnalysis ? "Hide" : "Show"}</Button>}>
            <div className="flex gap-2 flex-wrap text-[13px]">
              <Chip>{n(plan.analysis.intents.length, "job")}</Chip><Chip>{n(plan.analysis.rules.length, "rule")}</Chip>
              <Chip>{n(plan.analysis.slots.length, "detail")} to collect</Chip>
              {plan.analysis.unclear.length > 0 && <Chip tone="warn">{n(plan.analysis.unclear.length, "open question")}</Chip>}
            </div>
            {showAnalysis && (
              <div id="plan-analysis" className="grid gap-3 mt-3 text-[13px]">
                <div><b>Jobs:</b> {plan.analysis.intents.map((i) => `${i.description} (${i.id})`).join("; ")}</div>
                <div><b>Rules:</b><ul className="mt-1 grid gap-0.5">{plan.analysis.rules.map((r) => (
                  <li key={r.id} className="flex gap-2"><span className="font-mono text-muted w-8">{r.id}</span><span className="flex-1">{r.text}</span><Chip className="text-[13px]">{r.kind.replace("_", " ")}</Chip></li>))}</ul></div>
                {plan.analysis.unclear.length > 0 && <div><b>Not said in the instructions</b> (worth asking the agent's owner):<ul className="mt-1 list-disc pl-5">{plan.analysis.unclear.map((u, i) => <li key={i}>{u}</li>)}</ul></div>}
              </div>
            )}
          </Block>
          <Block title={`Draft tests (${plan.tests.length})`}
            sub={`Written ${new Date(plan.created_at).toLocaleString()}${plan.asked.focus ? `, focus: ${plan.asked.focus}` : ""}. Ticked ones passed every check. Click a title to see the call it will make.`}>
            <div className="grid gap-4">
              {groups.map(({ c, tests }) => (
                <div key={c} data-category={c}>
                  <div className="text-[13px] font-semibold text-muted mb-1.5">{page.categories[c]} · {tests.length}</div>
                  <div className="grid gap-1.5">
                    {tests.map((t) => (
                      <DraftRow key={t.id} t={t} on={picked.has(t.id)} open={open === t.id} crit={crit[t.id] || t.criticality}
                        onToggle={(v) => toggle(t.id, v)} onOpen={() => setOpen(open === t.id ? null : t.id)}
                        onCrit={(v) => setCrit({ ...crit, [t.id]: v })} />
                    ))}
                  </div>
                </div>
              ))}
            </div>
          </Block>
          <div className="flex items-center gap-3 flex-wrap px-5 pb-5">
            <Button id="add-plan-tests" variant="primary" icon="plus" busy={adding} disabled={!picked.size || adding} onClick={() => void add()}>
              Add {picked.size} test case{picked.size === 1 ? "" : "s"}
            </Button>
            <span className="text-[13px] text-muted">Personas they need are added from the library. You can edit any test afterwards.</span>
          </div>
        </>
      )}
    </Card>
  );
}

function DraftRow({ t, on, open, crit, onToggle, onOpen, onCrit }: {
  t: PlanTest; on: boolean; open: boolean; crit: string; onToggle: (v: boolean) => void; onOpen: () => void; onCrit: (v: string) => void;
}) {
  const sc = t.scenario as Record<string, any>;
  const blocked = t.problems.length > 0 || t.accepted;
  return (
    <div data-draft={t.id} className={cx("rounded-[12px] border", on ? "border-accent/60 bg-accent-soft/30" : "border-line")}>
      <div className="flex items-center gap-3 p-2.5 flex-wrap">
        <input type="checkbox" aria-label={`Add ${t.title}`} className="accent-[var(--accent)]" checked={on && !blocked} disabled={blocked} onChange={(e) => onToggle(e.target.checked)} />
        <button type="button" className="flex-1 min-w-[200px] text-left cursor-pointer" onClick={onOpen}>
          <span className="font-semibold">{t.title}</span>
          <span className="block text-[13px] text-muted">{KIND_LABEL(t.kind)} · caller: {t.persona}{t.persona_new ? " (from library)" : ""}{t.rules.length ? ` · tests ${t.rules.map((r) => r.id).join(", ")}` : ""}</span>
        </button>
        {t.accepted && <Chip tone="good" icon="check">added</Chip>}
        {t.duplicate_of && !t.accepted && <Chip tone="warn" title={`Close to ${t.duplicate_of}`}>looks like an existing test</Chip>}
        {t.problems.length > 0 && <Chip tone="bad">problems</Chip>}
        {sc.setup?.faults && <Chip tone="tool">breaks {sc.setup.faults[0].tool}</Chip>}
        {sc.outcome && <Chip tone="tool">{String(sc.outcome.expected).replace("_", " ")}</Chip>}
        {sc.modes?.includes("voice") && !sc.modes?.includes("text") && <Chip icon="mic">voice only</Chip>}
        <select aria-label="Criticality" className="text-[13px] bg-transparent border border-line rounded-md px-1.5 py-0.5" value={crit} disabled={blocked} onChange={(e) => onCrit(e.target.value)}>
          {CRIT.map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
      </div>
      {open && (
        <div className="px-4 pb-3 grid gap-2 text-[13px] border-t border-line pt-2.5">
          <div>{t.why}</div>
          {t.rules.map((r) => <div key={r.id} className="text-ink-2"><span className="font-mono text-muted">{r.id}</span> {r.text}</div>)}
          <div><b>The caller is told:</b> {sc.caller?.goal}</div>
          {sc.caller?.facts && Object.keys(sc.caller.facts).length > 0 && (
            <div className="text-ink-2"><b>Details:</b> {Object.entries(sc.caller.facts).map(([k, v]) => `${k}: ${v}`).join(", ")}</div>)}
          {sc.outcome && <div className="text-ink-2"><b>Database check:</b> {sc.outcome.record ? `${plural(sc.outcome.record.expect_count, "record")} in ${sc.outcome.record.probe}${sc.outcome.record.expect_fields ? ` with ${Object.entries(sc.outcome.record.expect_fields).map(([k, v]) => `${k} = ${v}`).join(", ")}` : ""}` : `only what the agent says about ${sc.outcome.action.tool}`}</div>}
          {(sc.checks || []).map((c: any) => <div key={c.id} className="text-ink-2"><Icon name={c.type === "judge" ? "scale" : "tool"} size={13} className="inline mr-1" />{c.type === "judge" ? c.intent : c.description}</div>)}
          {t.notes.map((n, i) => <div key={i} className="text-muted text-[13px]"><Icon name="info" size={13} className="inline mr-1" />{n}</div>)}
          {t.problems.map((p, i) => <div key={i} className="text-bad text-[13px]">{p}</div>)}
        </div>
      )}
    </div>
  );
}

// The persona library: ready-made callers to add to this project.
export function PersonaLibrary({ projectKey, onAdded, detail }: { projectKey: string; onAdded: () => void; detail: TabProps["detail"] }) {
  const toast = useToast();
  const { data } = useLoad(() => planApi.page(projectKey, detail.file), [projectKey, detail]);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  if (!data) return <Skeleton height={160} />;
  const add = async () => {
    setBusy(true);
    try { const r = await planApi.addPersonas(projectKey, [...picked]); toast(`Added ${r.added.length} persona${r.added.length === 1 ? "" : "s"}`); setPicked(new Set()); onAdded(); }
    catch (e) { toast((e as Error).message, "bad"); }
    setBusy(false);
  };
  return (
    <div id="persona-library" className="grid gap-3">
      <div className="grid gap-2 sm:grid-cols-2">
        {data.library.map((p: LibraryPersona) => (
          <Choice key={p.id} data-library={p.id} on={picked.has(p.id) || p.added} off={p.added}
            onClick={() => setPicked((s) => { const n = new Set(s); if (n.has(p.id)) n.delete(p.id); else n.add(p.id); return n; })}>
            <div className="font-semibold flex items-center gap-2">{p.title}{p.added && <Chip className="text-[13px]">in this project</Chip>}</div>
            <div className="text-ink-2 text-[13px]">{p.description}</div>
            <div className="text-muted text-[13px] mt-1">{p.speech.accent ? `${p.speech.accent} accent` : p.speech.tts_voice?.replace("-medium", "")} · speed {p.speech.speaking_rate}
              {p.behaviour.interrupts ? " · interrupts" : ""}{p.behaviour.backchannels ? " · says mm-hm" : ""}{p.behaviour.noises ? " · coughs" : ""}{p.behaviour.pauses_mid_sentence ? " · pauses" : ""}
              {p.behaviour.goes_quiet ? " · goes quiet" : ""}{p.speech.volume_db ? ` · ${p.speech.volume_db} dB` : ""}</div>
          </Choice>
        ))}
      </div>
      <div><Button size="sm" variant="primary" icon="plus" disabled={!picked.size} busy={busy} onClick={() => void add()}>Add {picked.size || ""} persona{picked.size === 1 ? "" : "s"}</Button></div>
    </div>
  );
}
