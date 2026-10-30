// AI judges of one project: what the judges score, how far each item is from being trusted, what they
// found lately, and where they disagree with you. An item blocks a release only once your labels show
// the judges catch real failures and pass good calls often enough (both 90% by default, over 20 labels
// with 5+ failures); until then its findings are opinions. Disagreements can become worked examples.
import { useState } from "react";
import { CountUp } from "../components/CountUp";
import { Kpi } from "../components/dash";
import { Tip, useToast } from "../components/feedback";
import { Icon } from "../components/Icon";
import { Button, Card, CardHead, Chip, EmptyState, Notice, PageHead, Skeleton } from "../components/ui";
import { useCrumbs, useLoad } from "../lib/app";
import { cx } from "../lib/format";
import { JUDGE_NAME, judgesApi, modelName, packLabel, pctOf, VOTE_LABEL } from "../lib/judges";
import { projectHref } from "../lib/links";
import type { Disagreement, JudgeItemRow, Project, Thresholds } from "../lib/types";

export function JudgesPageView({ project }: { project: Project }) {
  useCrumbs([{ label: "Projects", href: "#/projects" }, { label: project.name, href: projectHref(project.key) }, { label: "AI judges" }]);
  const [tick, setTick] = useState(0);
  const { data, error } = useLoad(() => judgesApi.page(project.key), [project.key, tick]);
  if (error) return <Notice tone="bad" className="mt-5">{error.message}</Notice>;
  if (!data) return <Skeleton height={520} />;
  const s = data.summary;
  const th = data.thresholds;
  const on = data.settings.enabled;
  const st = data.settings;

  return (
    <>
      <PageHead title="AI judges" sub="AI judges read every conversation for what hard checks can't see. Your labels decide when to trust them.">
        <Button icon="sparkles" href={projectHref(project.key, "/setup/judges")}>Set up judges</Button>
        <Button id="label-calls" variant="primary" icon="tag" href={projectHref(project.key, "/labels")}>
          Label calls{s.to_label ? ` (${s.to_label})` : ""}
        </Button>
      </PageHead>
      <div className="flex flex-col gap-4">
        {!on && <Notice tone="warn"><b>The judges are off for this agent.</b> Runs check tools, the database, logs and timing only. <a href={projectHref(project.key, "/setup/judges")}>Turn them on</a>.</Notice>}
        {data.problems.length > 0 && <Notice tone="warn">{data.problems.slice(0, 4).map((p, i) => <div key={i} className="text-[12.5px]">{p}</div>)}</Notice>}

        <div className="grid gap-4 grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 stagger">
          <Kpi icon="scale" label="Items judged" value={<CountUp to={s.items} />}
            foot={`${modelName(st.primary_model)} and ${modelName(st.secondary_model)}${st.tiebreak ? `, ${modelName(st.tiebreak_model)} decides splits` : ""}`} />
          <Kpi icon="shield" label="Trusted to block" tone={s.calibrated ? "good" : "accent"} value={<>{s.calibrated}<span className="text-muted text-lg font-bold"> / {s.items}</span></>}
            foot={s.calibrated ? "the rest give opinions only" : "all give opinions until you label calls"} />
          <Kpi icon="tag" label="Calls you labelled" value={<CountUp to={s.labelled_calls} />} foot={`an item needs ${th.min_labels}, with ${th.min_fails}+ failures`} />
          <Kpi icon="eye" label="Waiting for you" tone={s.to_review ? "warn" : "accent"} value={<CountUp to={s.to_label} />}
            foot={s.to_review ? `${s.to_review} where the judges split` : s.judged_runs ? "calls with no label yet" : "no judged runs yet"} />
        </div>

        <HowItWorks th={th} tiebreak={st.tiebreak} />

        <Card flush>
          <div className="px-[18px] pt-4 pb-2">
            <CardHead level={2} title="What the judges score" sub={`Each item on the calls where it can come up, plus the questions scenarios add. Recent results come from the last ${s.judged_runs || 0} judged run(s).`} className="mb-0" />
          </div>
          {data.items.length ? (
            <div id="judge-items" className="divide-y divide-line border-t border-line">
              {data.items.map((it) => <ItemRow key={it.id} item={it} th={th} projectKey={project.key} onChange={() => setTick((t) => t + 1)} />)}
            </div>
          ) : (
            <EmptyState icon="scale" title="No items" text="Choose what the judges score in the judges setup."
              action={<Button href={projectHref(project.key, "/setup/judges")}>Set up judges</Button>} />
          )}
        </Card>
      </div>
    </>
  );
}

function HowItWorks({ th, tiebreak }: { th: Thresholds; tiebreak: boolean }) {
  const steps = [
    ["scale", "Two judges score each call", `Each says Pass, Fail or Doesn't apply for the items that can come up, and points at the lines it based that on.${tiebreak ? " When they split, a third, stronger judge decides; safety items always come to you." : ""}`],
    ["eye", "Splits come to you", "What the judges can't settle is marked Needs you and waits for your label. You label without seeing their answers."],
    ["shield", "Proven items are trusted", `After ${th.min_labels} labels (${th.min_fails}+ failures, ${th.min_fails}+ passes), an item is trusted when the judges caught ${pctOf(th.min_tpr)} of the failures and passed ${pctOf(th.min_tnr)} of the good calls. Only then can its failures stop a release; before, they're opinions.`],
  ] as const;
  return (
    <Card>
      <div className="grid gap-4 sm:grid-cols-3">
        {steps.map(([icon, title, text], i) => (
          <div key={title} className="flex items-start gap-3">
            <span className="size-8 rounded-[10px] grid place-items-center bg-accent-soft text-accent-ink flex-none"><Icon name={icon} size={16} /></span>
            <div><b className="text-[13.5px]">{i + 1}. {title}</b><div className="text-[12.5px] text-ink-2 mt-0.5">{text}</div></div>
          </div>
        ))}
      </div>
    </Card>
  );
}

// A share on a 0..1 track, with a tick at the threshold.
function RateMeter({ label, value, min, detail }: { label: string; value: number | null; min: number; detail: string }) {
  const ok = value != null && value >= min;
  return (
    <Tip className="block" content={detail}>
      <div className="grid grid-cols-[112px_minmax(0,1fr)_38px] items-center gap-2 text-[11.5px]">
        <span className="text-muted truncate">{label}</span>
        <div className="relative h-2 rounded-full bg-surface-3">
          {value != null && <div className={cx("absolute inset-y-0 left-0 rounded-full", ok ? "bg-good" : "bg-warn")} style={{ width: `${Math.max(value * 100, 4)}%` }} />}
          <i className="absolute -top-1 -bottom-1 w-0.5 rounded bg-ink-2" style={{ left: `calc(${min * 100}% - 1px)` }} aria-hidden="true" />
        </div>
        <span className="tabular-nums font-semibold text-right">{pctOf(value)}</span>
      </div>
    </Tip>
  );
}

function ItemRow({ item, th, projectKey, onChange }: { item: JudgeItemRow; th: Thresholds; projectKey: string; onChange: () => void }) {
  const c = item.calibration;
  const r = item.recent;
  const judged = r.PASS + r.FAIL + r.NEEDS_REVIEW;
  const progress = Math.min(1, c.labelled_calls / th.min_labels);
  const [open, setOpen] = useState(false);
  const teachable = item.disagreements.length + item.examples.length;
  return (
    <div data-item={item.id} className="px-[18px] py-3.5 grid gap-3">
      <div className="grid gap-3 lg:grid-cols-[minmax(0,1.5fr)_minmax(0,1fr)_minmax(0,1.1fr)_150px] items-start">
        <div className="min-w-0">
          <div className="flex items-center gap-1.5 flex-wrap">
            <b className="text-[13.5px]">{item.title}</b>
            <Chip className="text-[11px]">{packLabel(item.pack)}</Chip>
            {item.severity === "blocker" && <Chip tone="bad" className="text-[11px]">blocker</Chip>}
            {item.severity === "minor" && <Chip className="text-[11px]">minor</Chip>}
            {item.needs === "write" && <Chip className="text-[11px]">only when something changed</Chip>}
          </div>
          <div className="text-[12.5px] text-ink-2 mt-0.5">{item.question}</div>
          {item.scenarios.length > 0 && item.pack === "scenario" && <div className="text-[11.5px] text-muted mt-0.5">in {item.scenarios.join(", ")}</div>}
        </div>
        <div className="grid gap-1.5">
          {c.can_block
            ? <Chip tone="good" icon="shield" className="justify-self-start">Trusted</Chip>
            : <Chip tone="default" icon="info" className="justify-self-start">Opinion only</Chip>}
          <span className="text-[11.5px] text-muted">{c.status}</span>
        </div>
        <div className="grid gap-1.5">
          <RateMeter label="Catches failures" value={c.tpr} min={c.min_tpr}
            detail={c.fails ? `Of the ${c.fails} call(s) you labelled Fail, the judges failed ${c.caught}. The tick is ${pctOf(c.min_tpr)}.` : "No call labelled Fail yet."} />
          <RateMeter label="Passes good calls" value={c.tnr} min={c.min_tnr}
            detail={c.passes ? `Of the ${c.passes} call(s) you labelled Pass, the judges passed ${c.cleared}. The tick is ${pctOf(c.min_tnr)}.` : "No call labelled Pass yet."} />
          <div className="flex items-center gap-2 text-[11.5px] text-muted">
            <div className="flex-1 h-1 rounded-full bg-surface-3 overflow-hidden"><div className="h-full rounded-full bg-accent" style={{ width: `${progress * 100}%` }} /></div>
            <span className="tabular-nums">{c.labelled_calls} / {th.min_labels} labels · {c.fails} fail</span>
          </div>
        </div>
        <div className="text-[12px] grid gap-0.5">
          {judged ? (
            <>
              <span><b className="text-good tabular-nums">{r.PASS}</b> <span className="text-ink-2">passed</span></span>
              <span><b className={cx("tabular-nums", r.FAIL ? (c.can_block ? "text-bad" : "text-ink") : "text-ink-2")}>{r.FAIL}</b> <span className="text-ink-2">failed{!c.can_block && r.FAIL ? " (opinion)" : ""}</span></span>
              <span><b className={cx("tabular-nums", r.NEEDS_REVIEW ? "text-warn" : "text-ink-2")}>{r.NEEDS_REVIEW}</b> <span className="text-ink-2">need you</span></span>
              {item.true_pass_rate != null && <Tip content="The agent's likely real pass rate on this item, corrected for the judges' known mistakes."><span className="text-muted">really about {pctOf(item.true_pass_rate)}</span></Tip>}
            </>
          ) : <span className="text-muted">{r.NOT_APPLICABLE ? `didn't come up in ${r.NOT_APPLICABLE} call(s)` : "not judged yet"}</span>}
        </div>
      </div>
      {teachable > 0 && (
        <div>
          <button type="button" data-teach={item.id} className="text-[12.5px] text-muted hover:text-ink cursor-pointer inline-flex items-center gap-1" onClick={() => setOpen((o) => !o)}>
            <Icon name="chevron" size={13} className={cx("transition-transform", open && "rotate-90")} />
            Teach the judges{item.disagreements.length ? ` · ${item.disagreements.length} call(s) where they disagreed with you` : ""}{item.examples.length ? ` · ${item.examples.length} example(s) in use` : ""}
          </button>
          {open && <Teach item={item} projectKey={projectKey} onChange={onChange} />}
        </div>
      )}
    </div>
  );
}

// Worked examples: a labelled call where the judges got it wrong, shown to them under this item.
function Teach({ item, projectKey, onChange }: { item: JudgeItemRow; projectKey: string; onChange: () => void }) {
  const toast = useToast();
  const [busy, setBusy] = useState<string | null>(null);
  const act = async (id: string, body: Parameters<typeof judgesApi.teach>[1], done: string) => {
    setBusy(id);
    try {
      await judgesApi.teach(projectKey, body);
      toast(done);
      onChange();
    } catch (e) {
      toast((e as Error).message, "bad");
    } finally {
      setBusy(null);
    }
  };
  return (
    <div className="mt-2 grid gap-2">
      <Notice className="text-[12.5px]">
        An example shows the judges how you want this item judged, with no training needed. Adding or removing one changes the
        item's wording, so its trust is measured again on new labels. If the judges keep getting it wrong, a clearer line in
        the <a href={projectHref(projectKey, "/setup/judges")}>judges' policy</a> often helps more.
      </Notice>
      {item.examples.map((e, i) => (
        <div key={`ex-${i}`} data-example={i} className="rounded-lg border border-accent/40 bg-accent-soft/30 p-2.5 text-[12.5px] grid gap-1">
          <div className="flex items-center gap-2 flex-wrap"><Chip tone="accent" icon="sparkles" className="text-[11px]">example in use</Chip>
            <b>Correct result: {VOTE_LABEL[e.answer]}</b></div>
          <div className="text-ink-2 break-words">{e.said}</div>
          {e.why && <div className="text-muted">Why: {e.why}</div>}
          <div><Button size="sm" variant="ghost" icon="x" busy={busy === `ex-${i}`}
            onClick={() => void act(`ex-${i}`, { remove: true, item: item.id, said: e.said }, "Example removed")}>Remove</Button></div>
        </div>
      ))}
      {item.disagreements.map((d) => <DisagreementRow key={d.key} d={d} busy={busy === d.key}
        onUse={(why) => void act(d.key, { label_key: d.key, why }, "Added as an example for the judges")} />)}
    </div>
  );
}

function DisagreementRow({ d, busy, onUse }: { d: Disagreement; busy: boolean; onUse: (why: string) => void }) {
  const [why, setWhy] = useState(d.note);
  return (
    <div data-disagreement={d.key} className="rounded-lg border border-line p-2.5 text-[12.5px] grid gap-1.5">
      <div className="flex items-center gap-2 flex-wrap">
        <b>You said {VOTE_LABEL[d.label]}</b>
        <span className="text-muted">the judges said {d.judges === "NEEDS_REVIEW" ? "different things" : d.judges ? VOTE_LABEL[d.judges as keyof typeof VOTE_LABEL] || d.judges : "nothing usable"}</span>
      </div>
      <div className="text-ink-2 break-words">{d.excerpt}</div>
      {Object.entries(d.reasons).filter(([, r]) => r).map(([j, r]) => (
        <div key={j} className="text-muted"><b className="font-semibold">{JUDGE_NAME[j as keyof typeof JUDGE_NAME] || j}:</b> {r}</div>
      ))}
      <div className="flex items-center gap-2 flex-wrap">
        <input className="flex-1 min-w-[200px] h-8 rounded-lg border border-line-strong bg-surface px-2.5 text-[12.5px]" value={why}
          placeholder="Why is that the right answer? (optional)" onChange={(e) => setWhy(e.target.value)} />
        <Button size="sm" variant="primary" icon="sparkles" busy={busy} onClick={() => onUse(why)}>Use as an example</Button>
      </div>
    </div>
  );
}
