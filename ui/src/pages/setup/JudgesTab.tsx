// The AI judges of one agent: on or off, what the agent may say (its policy), which quality items the
// judges score on every call (built-in ones for every agent, an industry pack, the agent's own),
// and which models judge. Saved into the profile's `judges` section.
import { useState } from "react";
import { Area, Block, Check, Grid, Help, Problems, SaveBar, Select, Text, type Obj, type Patch } from "../../components/form";
import { Icon } from "../../components/Icon";
import { Button, Card, Chip, Field, Notice, Skeleton, Switch } from "../../components/ui";
import { useLoad } from "../../lib/app";
import { cx } from "../../lib/format";
import { judgesApi, modelName, MODELS, packLabel } from "../../lib/judges";
import { owner, slug } from "../../lib/setup";
import type { CustomItem, RubricItem } from "../../lib/types";
import { asList, asObj, str, useDraft, type TabProps } from "./common";

const SEVERITY: [string, string][] = [["blocker", "Blocker"], ["major", "Major"], ["minor", "Minor"]];
const SECTION: [string, string][] = [["accuracy", "Accuracy"], ["experience", "Caller experience"], ["security", "Security"]];

export function JudgesTab({ detail, reload, projectKey }: TabProps & { projectKey: string }) {
  const { data: page, error } = useLoad(() => judgesApi.page(projectKey), [projectKey]);
  if (error) return <Notice tone="bad">{error.message}</Notice>;
  if (!page) return <Skeleton />;
  return <JudgesForm detail={detail} reload={reload} catalog={page.catalog} suggested={page.suggested_pack} defaults={page.default_items} />;
}

function JudgesForm({ detail, reload, catalog, suggested, defaults }: TabProps & {
  catalog: { universal: RubricItem[]; packs: Record<string, RubricItem[]> }; suggested: string | null; defaults: string[];
}) {
  const file = owner(detail, "judges");
  const d = useDraft({ judges: asObj(detail.merged.judges) }, file, detail.project, reload);
  const j = asObj(d.draft.judges);
  const custom = asList<CustomItem>(j.custom);
  const builtins = new Set([...catalog.universal, ...Object.values(catalog.packs).flat()].map((i) => i.id));
  const chosen = new Set(Array.isArray(j.items) ? (j.items as string[]).filter((i) => builtins.has(i)) : defaults);
  const [showOther, setShowOther] = useState(false);

  // judges.items lists built-in ids plus every custom id; it is left out while it matches the default.
  const write = (patch: Patch, picked: Set<string> = chosen, items: CustomItem[] = custom) => {
    const next = { ...j, ...patch } as Obj;
    const ids = [...picked];
    const isDefault = ids.length === defaults.length && defaults.every((i) => picked.has(i));
    if (isDefault) delete next.items;
    else next.items = [...ids, ...items.map((c) => c.id).filter(Boolean)];
    if (items.length) next.custom = items as unknown as Obj[]; else delete next.custom;
    for (const [k, v] of Object.entries(next)) if (v === undefined || v === "") delete next[k];
    d.setDraft({ judges: next });
  };
  const toggle = (id: string, on: boolean) => {
    const picked = new Set(chosen);
    if (on) picked.add(id); else picked.delete(id);
    write({}, picked);
  };
  const setCustom = (i: number, patch: Partial<CustomItem>) => {
    const items = custom.map((c, k) => {
      if (k !== i) return c;
      const n = { ...c, ...patch } as Record<string, unknown>;
      if (patch.title !== undefined && (!c.id || c.id === slug(c.title || ""))) n.id = slug(patch.title);
      for (const [key, v] of Object.entries(n)) if (v === "" && key !== "id" && key !== "question") delete n[key];
      return n as unknown as CustomItem;
    });
    write({}, chosen, items);
  };
  const addCustom = () => write({}, chosen, [...custom, { id: "", title: "", question: "", severity: "major", section: "experience" }]);
  const removeCustom = (i: number) => write({}, chosen, custom.filter((_, k) => k !== i));

  const enabled = j.enabled !== false;
  const tiebreak = j.tiebreak !== false;
  const group = (title: string, items: RubricItem[], note?: string) => (
    <div className="grid gap-1.5" key={title}>
      <div className="flex items-center gap-2 text-[12.5px] font-semibold text-ink-2">{title}{note && <span className="font-normal text-muted">{note}</span>}</div>
      <div className="grid gap-2 sm:grid-cols-2">
        {items.map((it) => (
          <div key={it.id} data-item={it.id} className={cx("rounded-lg border p-2.5", chosen.has(it.id) ? "border-accent/50 bg-accent-soft/40" : "border-line")}>
            <Check checked={chosen.has(it.id)} onChange={(v) => toggle(it.id, v)}
              label={<span className="inline-flex items-center gap-1.5 flex-wrap">{it.title}
                {it.severity !== "major" && <Chip tone={it.severity === "blocker" ? "bad" : "default"}>{it.severity}</Chip>}
                {it.section === "security" && <Chip tone="warn" icon="lock">security</Chip>}</span>}
              hint={it.question} />
          </div>
        ))}
      </div>
    </div>
  );
  const otherPacks = Object.entries(catalog.packs).filter(([k]) => k !== suggested);
  const otherOn = otherPacks.flatMap(([, v]) => v).filter((i) => chosen.has(i.id)).length;

  return (
    <Card>
      <Block title="AI judges" icon="scale"
        sub="Hard checks see tool calls and the database. Two AI judges read each conversation for what those can't see: made-up facts, tone, staying in scope, protecting personal data. Their findings are only an opinion until you label calls and they catch the failures you find; then they can block a release.">
        <Switch id="judges-enabled" checked={enabled} onChange={(v) => write({ enabled: v ? undefined : false })} label="Judge every call with AI" />
        {!enabled && <Help>Runs still check tools, the database, logs and timing. Turn the judges off for latency or load tests.</Help>}
      </Block>
      {enabled && (
        <>
          <Block title="What the agent may say" sub="The judges compare the agent's answers with this, so they can tell a made-up answer from a correct one.">
            <Field label="Policy and facts" hint="What the agent does and doesn't do, what it knows (prices, hours, refund rules), and where it sends callers it can't help. Plain sentences.">
              <Area id="judges-policy" rows={6} value={str(j.policy)} onChange={(v) => write({ policy: v })}
                placeholder="The agent books, moves and cancels appointments. It doesn't know prices. For anything else it gives the front desk number, 0123 456 789, 9am to 5pm." />
            </Field>
          </Block>
          <Block title="What the judges score" aside={<Button size="sm" variant="ghost" icon="refresh" onClick={() => write({}, new Set(defaults))}>Use the defaults</Button>}
            sub={`${chosen.size + custom.length} item(s) on every call. The defaults are the items for every agent${suggested ? ` plus ${packLabel(suggested)}` : ""}. Scenarios can add their own questions.`}>
            {group("Every agent", catalog.universal)}
            {suggested && catalog.packs[suggested] && group(packLabel(suggested), catalog.packs[suggested], "suggested for this agent's industry")}
            <div>
              <button type="button" id="other-packs" className="text-[12.5px] text-muted hover:text-ink cursor-pointer inline-flex items-center gap-1" onClick={() => setShowOther((s) => !s)}>
                <Icon name="chevron" size={13} className={cx("transition-transform", showOther && "rotate-90")} />
                Other industries{otherOn ? ` (${otherOn} on)` : ""}
              </button>
              {showOther && <div className="grid gap-4 mt-2">{otherPacks.map(([k, v]) => group(packLabel(k), v))}</div>}
            </div>
          </Block>
          <Block title="This agent's own items" sub="Anything specific to this agent that a careful listener would check, such as &quot;Reads back the order total&quot;.">
            {custom.map((c, i) => (
              <div key={i} data-custom={i} className="rounded-lg border border-line p-3 grid gap-3">
                <div className="flex items-start gap-2">
                  <Field label="Title" className="flex-1"><Text value={c.title} placeholder="Reads back the order total" onChange={(v) => setCustom(i, { title: v })} /></Field>
                  <Button size="sm" variant="ghost" iconOnly icon="x" aria-label="Remove item" className="mt-6" onClick={() => removeCustom(i)} />
                </div>
                <Field label="Question for the judges" hint="One yes/no question about the conversation.">
                  <Text value={c.question} placeholder="Before ending, did the agent read back the order total?" onChange={(v) => setCustom(i, { question: v })} />
                </Field>
                <Grid>
                  <Field label="Passes when"><Text value={c.pass_when} placeholder="It says the total and the caller agrees" onChange={(v) => setCustom(i, { pass_when: v })} /></Field>
                  <Field label="Fails when"><Text value={c.fail_when} placeholder="The call ends without the total" onChange={(v) => setCustom(i, { fail_when: v })} /></Field>
                  <Field label="Only applies when" hint="Empty: every call."><Text value={c.applies_when} placeholder="The caller places an order" onChange={(v) => setCustom(i, { applies_when: v })} /></Field>
                  <Field label="Id" hint="Used in scenarios (rubric: id)."><Text mono value={c.id} onChange={(v) => setCustom(i, { id: slug(v) })} /></Field>
                  <Field label="Severity"><Select value={c.severity || "major"} options={SEVERITY} onChange={(v) => setCustom(i, { severity: v })} /></Field>
                  <Field label="Kind"><Select value={c.section || "experience"} options={SECTION} onChange={(v) => setCustom(i, { section: v })} /></Field>
                </Grid>
              </div>
            ))}
            <div><Button id="add-custom-item" size="sm" icon="plus" onClick={addCustom}>Add an item</Button></div>
          </Block>
          <Block title="Judge models" sub="Two different models judge independently. When they disagree, a third deciding judge breaks the tie; if it still isn't clear, the call goes to you for a label.">
            <Switch id="judges-tiebreak" checked={tiebreak} onChange={(v) => write({ tiebreak: v ? undefined : false })} label="Use a deciding judge when the two disagree" />
            {!tiebreak && <Help>Every disagreement then waits for your label.</Help>}
            <Grid>
              <Field label="First judge"><Select value={str(j.primary_model)} placeholder={`${modelName(MODELS[0])} (default)`} options={MODELS.map((m) => [m, modelName(m)])} onChange={(v) => write({ primary_model: v })} /></Field>
              <Field label="Second judge"><Select value={str(j.secondary_model)} placeholder={`${modelName(MODELS[1])} (default)`} options={MODELS.map((m) => [m, modelName(m)])} onChange={(v) => write({ secondary_model: v })} /></Field>
              {tiebreak && <Field label="Deciding judge"><Select value={str(j.tiebreak_model)} placeholder={`${modelName(MODELS[2])} (default)`} options={MODELS.map((m) => [m, modelName(m)])} onChange={(v) => write({ tiebreak_model: v })} /></Field>}
            </Grid>
          </Block>
        </>
      )}
      <Problems items={d.problems} />
      <SaveBar id="save-judges" dirty={d.dirty} busy={d.busy} onSave={() => void d.save()} onRevert={d.revert}
        note={file !== detail.file ? "Shared by the voice and text setups of this project." : undefined} />
    </Card>
  );
}
