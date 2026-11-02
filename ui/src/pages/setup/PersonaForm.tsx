// One caller persona: who the simulated caller is and how they behave.
import { useState } from "react";
import { useToast } from "../../components/feedback";
import { Area, Block, Check, Grid, Num, Problems, SaveBar, Select, Text, getIn, setIn, type Json, type Obj } from "../../components/form";
import { Card, Field } from "../../components/ui";
import { useApp } from "../../lib/app";
import { setupApi, slug, type ProfileDetail } from "../../lib/setup";
import { str } from "./common";

type Knob = [key: string, label: string, placeholder: number, step?: number];
const BEHAVIOURS: [key: string, label: string, hint: string, fields: Knob[]][] = [
  ["interrupts", "Cuts in on long answers", "Talks over the agent once it has gone on a while; the agent should stop and listen.",
    [["interrupt_after_ms", "After the agent talked (ms)", 2500], ["interrupt_chance", "Chance per long answer (0 to 1)", 0.5, 0.1], ["interrupt_max", "Most per call", 2]]],
  ["backchannels", "Says \"mm-hm\" while the agent talks", "A short \"mm-hm\" or \"okay\"; the agent should keep talking, not stop.",
    [["backchannel_after_ms", "After the agent talked (ms)", 1800], ["backchannel_chance", "Chance per answer (0 to 1)", 0.6, 0.1], ["backchannel_max", "Most per call", 3]]],
  ["pauses_mid_sentence", "Pauses in the middle of sentences", "On longer lines; the agent should wait for the rest, not jump in.",
    [["pause_ms", "Pause (ms)", 1200, 100], ["pause_chance", "Chance per line (0 to 1)", 0.5, 0.1]]],
  ["fillers", "Says um and uh", "Starts some answers with um, uh or hmm.", [["filler_chance", "Chance per line (0 to 1)", 0.4, 0.1]]],
  ["goes_quiet", "Goes quiet once", "Once per call, says nothing for a while; a good agent checks in (\"are you still there?\").",
    [["quiet_ms", "Says nothing for (ms)", 12000, 1000]]],
];

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
  const accents = system?.accents || [];
  const accent = accents.find((a) => a.id === str(at("speech", "accent")));

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
      <Block title="Voice" sub="Voice runs only. An accent picks a matching voice; a voice chosen here wins over it.">
        <Grid cols={3}>
          <Field label="Accent" hint="Tests the agent's speech recognition. Downloaded on first use.">
            <Select id="persona-accent" value={str(at("speech", "accent"))} placeholder="No accent set"
              options={accents.map((a) => [a.id, a.label] as [string, string])} onChange={(v) => put(["speech", "accent"], v || undefined)} />
          </Field>
          <Field label="Voice (Piper)" hint={accent ? `From the accent: ${accent.voice}` : "Downloaded on first use."}>
            <Select value={str(at("speech", "tts_voice"))} placeholder={accent ? "From the accent" : "Default voice"} onChange={(v) => put(["speech", "tts_voice"], v || undefined)}
              options={[...new Set([...voices, "en_US-amy-medium", "en_US-ryan-medium", "en_GB-alan-medium", "en_GB-jenny_dioco-medium"])]} />
          </Field>
          <Field label="Speaking speed" hint="1 = normal, 1.3 = fast."><Num value={at("speech", "speaking_rate") as number} step={0.1} min={0.5} max={2} placeholder={1}
            onChange={(v) => put(["speech", "speaking_rate"], v)} /></Field>
          <Field label="Loudness (dB)" hint="Negative is softer, e.g. -10 for a quiet caller."><Num id="persona-volume" value={at("speech", "volume_db") as number} step={1} min={-20} max={6} placeholder={0}
            onChange={(v) => put(["speech", "volume_db"], v || undefined)} /></Field>
          <Field label="Hangs up after silence (ms)"><Num value={at("behaviour", "hangs_up_after_silence_ms") as number} placeholder={8000} min={0}
            onChange={(v) => put(["behaviour", "hangs_up_after_silence_ms"], v)} /></Field>
        </Grid>
      </Block>
      <Block title="Behaviour" sub="How hard this caller is to serve. Each call makes the same choices again (they follow the call's id), so a call can be replayed.">
        <div className="grid gap-3.5" id="persona-behaviour">
          {BEHAVIOURS.map(([key, label, hint, fields]) => (
            <div key={key} className="grid gap-2">
              <Check checked={!!at("behaviour", key)} onChange={(v) => put(["behaviour", key], v || undefined)} label={label} hint={hint} />
              {!!at("behaviour", key) && fields.length > 0 && (
                <Grid cols={3} className="pl-6">
                  {fields.map(([f, flabel, ph, step]) => (
                    <Field key={f} label={flabel}><Num id={`behaviour-${f}`} value={at("behaviour", f) as number} placeholder={ph} step={step ?? 1} min={0}
                      onChange={(v) => put(["behaviour", f], v)} /></Field>
                  ))}
                </Grid>
              )}
            </div>
          ))}
        </div>
      </Block>
      <Problems items={problems} />
      <SaveBar id="save-persona" dirty={dirty || !file} busy={busy} onSave={() => void save()} onRevert={() => onDone()} revertLabel="Back to the list" />
    </Card>
  );
}
