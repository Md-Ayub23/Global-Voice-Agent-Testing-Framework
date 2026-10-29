// One caller persona: who the simulated caller is and how they behave.
import { useState } from "react";
import { useToast } from "../../components/feedback";
import { Area, Block, Check, Grid, Num, Problems, SaveBar, Select, Text, getIn, setIn, type Json, type Obj } from "../../components/form";
import { Card, Field } from "../../components/ui";
import { useApp } from "../../lib/app";
import { setupApi, slug, type ProfileDetail } from "../../lib/setup";
import { str } from "./common";

const BLANK: Obj = { id: "", description: "", traits: { patience: "medium", verbosity: "normal", mood: "calm", clarity: "high" } };

export function PersonaForm({ detail, file, initial, onDone }: { detail: ProfileDetail; file: string | null; initial: Obj | null; onDone: (file?: string) => void }) {
  const toast = useToast();
  const { system } = useApp();
  const [doc, setDoc] = useState<Obj>(initial || BLANK);
  const [saved, setSaved] = useState(JSON.stringify(initial || BLANK));
  const [busy, setBusy] = useState(false);
  const [problems, setProblems] = useState<string[]>([]);
  const dirty = JSON.stringify(doc) !== saved;
  const at = (...p: string[]) => getIn(doc, p);
  const put = (path: string[], v: Json | undefined) => setDoc(setIn(doc, path, v));
  const voices = system?.models.piper_voices || [];

  const save = async () => {
    const id = str(doc.id) || slug(str(doc.description).split(/[.,]/)[0] || "caller");
    const target = file || `personas/${id}.yaml`;
    setBusy(true);
    try {
      const r = await setupApi.saveDoc(detail.project, target, { ...doc, id }, !file);
      if (!r.saved) { setProblems(r.problems); toast("Not saved: see the problems below", "bad"); return; }
      setSaved(JSON.stringify({ ...doc, id }));
      toast(`Saved ${target}`);
      onDone(target);
    } catch (e) { toast((e as Error).message, "bad"); } finally { setBusy(false); }
  };

  const trait = (key: string, label: string, opts: string[]) => (
    <Field label={label}><Select value={str(at("traits", key))} options={opts} onChange={(v) => put(["traits", key], v)} /></Field>
  );
  return (
    <Card>
      <Block title={file ? `Persona ${str(doc.id)}` : "New persona"} sub="Who calls. One scenario can be run with different personas.">
        <Grid>
          <Field label="Id" hint="Short name scenarios use, e.g. anxious-elderly."><Text id="persona-id" mono value={str(doc.id)} disabled={!!file} onChange={(v) => put(["id"], slug(v))} /></Field>
          <Field label="Language"><Select value={str(doc.language) || "en"} options={[["en", "English"]]} onChange={(v) => put(["language"], v === "en" ? undefined : v)} /></Field>
        </Grid>
        <Field label="Who they are" hint="One or two plain sentences; the simulated caller plays this person.">
          <Area id="persona-description" value={str(doc.description)} onChange={(v) => put(["description"], v)} rows={2}
            placeholder="A retired teacher who is a bit hard of hearing and asks the agent to repeat numbers." />
        </Field>
        <Grid cols={4}>
          {trait("patience", "Patience", ["low", "medium", "high"])}
          {trait("verbosity", "Talks", ["terse", "normal", "rambling"])}
          {trait("mood", "Mood", ["calm", "anxious", "frustrated", "angry"])}
          {trait("clarity", "Gives information", ["low", "medium", "high"])}
        </Grid>
      </Block>
      <Block title="Voice and behaviour" sub="Voice runs only.">
        <Grid cols={3}>
          <Field label="Voice (Piper)" hint="Downloaded on first use.">
            <Select value={str(at("speech", "tts_voice"))} placeholder="Default voice" onChange={(v) => put(["speech", "tts_voice"], v)}
              options={[...new Set([...voices, "en_US-amy-medium", "en_US-ryan-medium", "en_GB-alan-medium", "en_GB-jenny_dioco-medium"])]} />
          </Field>
          <Field label="Speaking speed" hint="1 = normal, 1.3 = fast."><Num value={at("speech", "speaking_rate") as number} step={0.1} min={0.5} max={2} placeholder={1}
            onChange={(v) => put(["speech", "speaking_rate"], v)} /></Field>
          <Field label="Hangs up after silence (ms)"><Num value={at("behaviour", "hangs_up_after_silence_ms") as number} placeholder={8000} min={0}
            onChange={(v) => put(["behaviour", "hangs_up_after_silence_ms"], v)} /></Field>
        </Grid>
        <Check checked={!!at("behaviour", "interrupts")} onChange={(v) => put(["behaviour", "interrupts"], v || undefined)}
          label="May talk over the agent" hint="Exact cut-ins are set per scenario (Audio)." />
      </Block>
      <Problems items={problems} />
      <SaveBar id="save-persona" dirty={dirty || !file} busy={busy} onSave={() => void save()} onRevert={() => onDone()} revertLabel="Back to the list" />
    </Card>
  );
}
