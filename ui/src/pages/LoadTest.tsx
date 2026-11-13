// Test › Load test (Phase 7): many test calls at once, in a shape the agent's owner picks, with a release readiness
// for that load. The settings are the profile's `load` section (saved here); the plan chart follows the
// form as it is edited; Start runs `vatf load` and opens the Live page.
import { useMemo, useState, type ReactNode } from "react";
import { useToast } from "../components/feedback";
import { Block, Grid, Num, Problems, SaveBar, type Obj } from "../components/form";
import { LoadChart, useWidth } from "../components/load";
import { Badge, Button, Card, CardHead, Chip, Choice, EmptyState, Field, Notice, PageHead, Seg, Skeleton, Switch, Table, rowLink } from "../components/ui";
import { api, qs } from "../lib/api";
import { useApp, useCrumbs, useLoad } from "../lib/app";
import { fmtDate, fmtMs } from "../lib/format";
import { SHAPES, SHAPE_LABEL, fmtSeconds, peakFromHour, roughCalls, stages as planStages } from "../lib/load";
import { channelLabel, projectHref } from "../lib/links";
import { navigate } from "../lib/router";
import { decisionTitle } from "../lib/status";
import type { Job, LoadLimits, LoadPage, LoadSettings, Project } from "../lib/types";
import { asObj } from "./setup/common";

type NumKey = "calls_at_once" | "start_at" | "steps" | "step_minutes" | "minutes" | "before_minutes" | "after_minutes" | "window_minutes" | "pause_seconds" | "max_calls"
  | "peak_calls_per_hour" | "call_minutes" | "expected_peak";
const SIZE: Record<string, [NumKey, string, ReactNode, number?][]> = {
  ramp: [
    ["start_at", "Start at (calls at once)", "The first level."],
    ["calls_at_once", "Up to (calls at once)", "The target: the busiest time you expect."],
    ["steps", "Steps", "Levels from the start to the target, spread evenly."],
    ["step_minutes", "Minutes per step", "Long enough for several calls per caller.", 0.5],
  ],
  spike: [
    ["start_at", "Normal traffic (calls at once)", "Before and after the spike."],
    ["calls_at_once", "Spike (calls at once)", "The sudden jump."],
    ["before_minutes", "Minutes before the spike", "", 0.5],
    ["minutes", "Spike minutes", "", 0.5],
    ["after_minutes", "Minutes after (recovery)", "Does it come back to normal answer times?", 0.5],
  ],
  soak: [
    ["calls_at_once", "Calls at once", "Held the whole time."],
    ["minutes", "Minutes", "Long: an hour or more finds slow leaks.", 5],
    ["window_minutes", "Read in windows of (minutes)", "The last window is compared with the first.", 1],
  ],
  steady: [
    ["calls_at_once", "Calls at once", "Held the whole time."],
    ["minutes", "Minutes", "", 0.5],
  ],
  find_limit: [
    ["start_at", "Start at (calls at once)", "The first level."],
    ["calls_at_once", "Try up to (calls at once)", "The most it tries. It stops at the first level that breaks a limit."],
    ["steps", "Steps", "Levels from the start up to the most, spread evenly."],
    ["step_minutes", "Minutes per step", "Long enough for at least 10 calls per level.", 0.5],
  ],
};

// The form's values over the defaults, the way src/vatf/load.py settings() reads them.
function effective(own: Obj, defaults: LoadPage["defaults"]): LoadSettings {
  const { limits: ownLimits, ...rest } = own as Obj & { limits?: Obj };
  const lim = { ...defaults.limits } as unknown as Record<string, unknown>;
  for (const [k, v] of Object.entries(asObj(ownLimits))) {
    const d = lim[k];
    lim[k] = d && typeof d === "object" ? { ...(d as Obj), ...asObj(v) } : v;
  }
  const out = { ...defaults, ...(rest as Partial<LoadSettings>), limits: lim as unknown as LoadLimits } as LoadSettings;
  // Calls per hour, as the server reads them: the expected peak, and the target unless one is typed.
  if (out.peak_calls_per_hour && out.call_minutes) {
    out.expected_peak = peakFromHour(out.peak_calls_per_hour, out.call_minutes);
    if (out.shape !== "find_limit" && own.calls_at_once == null) out.calls_at_once = Math.min(200, out.expected_peak);
  }
  if (out.shape === "find_limit" && out.expected_peak && own.calls_at_once == null) out.calls_at_once = Math.min(200, Math.max(out.calls_at_once, 2 * out.expected_peak));
  return out;
}

// `embedded`: shown as the Run tab of Test modes › Load & capacity, under that page's own header.
export function LoadTestPage({ project, query, embedded }: { project: Project; query: URLSearchParams; embedded?: boolean }) {
  useCrumbs(embedded ? null : [{ label: project.name, href: projectHref(project.key) }, { label: "Load test" }]);
  const here = embedded ? "/test/load" : "/load";
  const profiles = project.profiles;
  const wanted = query.get("file");
  const file = profiles.some((p) => p.file === wanted) ? wanted!
    : profiles.find((p) => p.mode === "voice" && p.ok)?.file || profiles.find((p) => p.file === "profile.yaml")?.file || profiles[0]?.file || "profile.yaml";
  const { data, error, reload } = useLoad(() => api.get<LoadPage>(`/api/project/${project.key}/load?${qs({ file })}`), [project.key, file]);
  const picker = profiles.length > 1 && (
    <div className="flex items-center gap-2.5 min-w-0 max-w-full">
      <span className="text-muted text-[13px] flex-none">Channel</span>
      <Seg id="profile-file" value={file} onChange={(f) => navigate(`${projectHref(project.key, here)}?${qs({ file: f })}`)}
        options={profiles.map((p) => ({ value: p.file, label: channelLabel(p, profiles, project.example), icon: p.mode === "voice" ? "mic" : "text" }))} />
    </div>
  );
  return (
    <>
      {embedded ? picker && <div className="mb-4">{picker}</div> : (
        <PageHead title="Load test" sub={<>Many test calls to <b className="text-ink">{project.name}</b> at the same time: does it still answer fast and correctly when many people call at once?</>}>
          {picker}
        </PageHead>
      )}
      {error ? <Notice tone="bad">{error.message}</Notice> : !data ? <Skeleton /> : <LoadForm key={file} page={data} project={project} reload={reload} />}
    </>
  );
}

function LoadForm({ page, project, reload }: { page: LoadPage; project: Project; reload: () => void }) {
  const { system, activeJob, setActiveJob } = useApp();
  const toast = useToast();
  const profile = project.profiles.find((p) => p.file === page.file);
  const voice = page.mode === "voice";
  const [draft, setDraft] = useState<Obj>(() => ({ ...(page.saved as Obj) }));
  const [busy, setBusy] = useState(false);
  const [problems, setProblems] = useState<string[]>([]);
  const [speech, setSpeech] = useState<"local" | "codec">("local");
  const [offline, setOffline] = useState(false);
  const [starting, setStarting] = useState(false);
  const [chartBox, chartW] = useWidth<HTMLDivElement>(340);
  const dirty = JSON.stringify(draft) !== JSON.stringify(page.saved || {});
  const s = useMemo(() => effective(draft, page.defaults), [draft, page.defaults]);
  const plan = useMemo(() => planStages(s), [s]);
  const total = plan.stages.length ? plan.stages[plan.stages.length - 1].end_s : 0;
  const peak = Math.max(0, ...plan.stages.map((x) => x.level));
  const callSecs = s.call_minutes ? s.call_minutes * 60 : Math.min(page.agent.max_call_seconds, voice ? 40 : 15);
  const [phoneOk, setPhoneOk] = useState(false);
  const phone = page.plan?.phone;
  const shortStages = s.call_minutes ? plan.stages.filter((x) => (x.level * x.seconds) / (s.call_minutes! * 60 + s.pause_seconds) < s.limits.min_calls) : [];
  const factNames = [...new Set(page.tests.flatMap((t) => t.facts || []))].sort();

  const set = (k: string, v: unknown) => {
    const next = { ...draft } as Obj;
    if (v === undefined || v === null || v === "" || (Array.isArray(v) && !v.length)) delete next[k]; else next[k] = v as Obj[string];
    setDraft(next);
  };
  const ownLimits = asObj(draft.limits);
  const setLimit = (k: string, v: unknown, sub?: string) => {
    const lim = { ...ownLimits } as Obj;
    if (sub) {
      const row = { ...asObj(lim[k]) } as Obj;
      const def = (page.defaults.limits as unknown as Record<string, Obj>)[k][sub];
      if (v === undefined || v === def) delete row[sub]; else row[sub] = v as Obj[string];
      if (Object.keys(row).length) lim[k] = row; else delete lim[k];
    } else if (v === undefined) delete lim[k]; else lim[k] = v as Obj[string];
    set("limits", Object.keys(lim).length ? lim : undefined);
  };
  const num = (k: NumKey) => draft[k] as number | undefined;
  const ph = (k: NumKey) => `${page.defaults[k]} (default)`;

  const save = async () => {
    setBusy(true);
    try {
      const r = await api.put<{ saved: boolean; problems: string[] }>(`/api/project/${project.key}/load`, { file: page.file, load: draft });
      if (r.saved) { setProblems([]); toast("Load settings saved"); reload(); }
      else setProblems(r.problems);
    } catch (e) { toast((e as Error).message, "bad"); }
    finally { setBusy(false); }
  };

  const tests = page.tests;
  const chosen = (s.scenarios || []) as string[];
  const toggleTest = (id: string) => {
    const base = chosen.length ? chosen : page.tests_used;
    const next = base.includes(id) ? base.filter((x) => x !== id) : [...base, id];
    const isDefault = next.length === page.tests_used.length && next.every((x) => page.tests_used.includes(x)) && !chosen.length;
    set("scenarios", isDefault ? undefined : next);
  };
  const using = chosen.length ? chosen : page.tests_used;
  const needsClaude = !offline && (s.caller === "claude" || s.judges);
  const noKey = needsClaude && system && !system.api_key.set;
  const running = !!activeJob && activeJob.status === "running";
  const p95Ph = page.voice_p95_ms != null ? `${page.voice_p95_ms} (release p95)` : "none (text has no answer times)";

  const start = async () => {
    setStarting(true);
    try {
      const job = await api.post<Job>("/api/jobs", { command: "load", project: project.path, profile: page.file,
        offline, start_agent: !!s.start_agent && !!page.agent.agent_process, ...(voice ? { speech } : {}), ...(phone ? { confirm_phone: phoneOk } : {}) });
      setActiveJob(job);
      navigate(projectHref(project.key, `/live/${job.id}`));
    } catch (e) { toast((e as Error).message, "bad"); setStarting(false); }
  };

  return (
    <div className="flex flex-col gap-4">
    {/* The settings and the pinned Start panel share their own grid, so the pinned panel stops where the
        settings end and never slides over the past load tests below. */}
    <div className="grid gap-4 items-start grid-cols-1 xl:grid-cols-[minmax(0,1fr)_380px]">
      <div className="flex flex-col gap-4 min-w-0">
        {page.agent.launched_per_call && (
          <Notice tone="warn" icon="info">
            Vocto starts its own copy of this agent for every call ({profile?.connection.type || "launch"}), so a load test measures many separate copies, not one shared server.
            The numbers still show whether this computer and the agent's own services (its model, its APIs) keep up. To load-test a deployed agent, point the connection at its address.
          </Notice>
        )}
        {page.agent.uses_gateway && !page.agent.test_id_sent && (
          <Notice tone="warn" icon="tool">
            The agent's tool calls go through Vocto's tool gateway, but the agent isn't told the call's test id, so with many calls at once Vocto can't tell which call made which tool call.
            Have the agent pass the <code>{page.agent.test_id_header}</code> header (or a <code>test_id</code> field) on its tool calls, and set the test id to go in the session start
            (Set up › Connection). Until then tool checks in a load test give "not enough evidence", never a false “Not ready”.
          </Notice>
        )}

        <Card>
          <CardHead level={2} title="1 · Shape" sub="How the number of calls at once changes over the test." />
          <div className="grid gap-3 grid-cols-1 md:grid-cols-2" id="load-shape">
            {SHAPES.map((x) => (
              <Choice key={x.value} radio on={s.shape === x.value} data-shape={x.value} onClick={() => set("shape", x.value === page.defaults.shape ? undefined : x.value)}>
                <b className="block font-semibold">{x.label}</b>
                <div className="text-ink-2 text-[13px]">{x.about}</div>
              </Choice>
            ))}
          </div>
        </Card>

        <Card id="load-busiest">
          <CardHead level={2} title="Your busiest time" sub="Businesses know their busiest hour, not “calls at once”. Vocto works it out (calls per minute × minutes per call) and uses it as the target, and the report says how much headroom you have." />
          <Grid>
            <Field label="Calls in your busiest hour" hint="Empty: type the calls at once below instead.">
              <Num id="load-peak_calls_per_hour" value={num("peak_calls_per_hour")} placeholder="e.g. 120" min={1} onChange={(v) => set("peak_calls_per_hour", v)} />
            </Field>
            <Field label="Average call length (minutes)" hint="Also warns when a stage is too short to judge.">
              <Num id="load-call_minutes" value={num("call_minutes")} placeholder="e.g. 4" min={0.1} step={0.5} onChange={(v) => set("call_minutes", v)} />
            </Field>
            {!s.peak_calls_per_hour && (
              <Field label="Or: calls at once at your busiest time" hint="For the headroom sentence.">
                <Num id="load-expected_peak" value={num("expected_peak")} placeholder={s.shape === "find_limit" ? "none" : `${s.calls_at_once} (the target)`} min={1} onChange={(v) => set("expected_peak", v)} />
              </Field>
            )}
          </Grid>
          {s.peak_calls_per_hour && s.call_minutes ? (
            <div className="text-[13px] text-ink-2 mt-2" id="load-peak-text">
              {s.peak_calls_per_hour} calls an hour of {s.call_minutes} min each = <b>{s.expected_peak} call{s.expected_peak === 1 ? "" : "s"} at once</b> at your busiest time
              {s.shape === "find_limit" ? `; find the limit tries up to ${s.calls_at_once}.` : num("calls_at_once") == null ? ", used as the target." : `; your own target of ${s.calls_at_once} is kept.`}
            </div>
          ) : null}
        </Card>

        <Card>
          <CardHead level={2} title="2 · Size and length" sub="Each simulated caller makes one test call after another. An empty field uses the default shown in grey." />
          <Grid>
            {SIZE[s.shape].map(([k, label, hint, step]) => (
              <Field key={k} label={label} hint={hint}>
                <Num id={`load-${k}`} value={num(k)} placeholder={k === "calls_at_once" && s.calls_at_once !== page.defaults.calls_at_once ? `${s.calls_at_once} (from your busiest time)` : ph(k)}
                  step={step ?? 1} min={k.endsWith("minutes") ? 0 : 1} onChange={(v) => set(k, v)} />
              </Field>
            ))}
            <Field label="Pause between a caller's calls (seconds)" hint="0: the next call starts as soon as one ends.">
              <Num id="load-pause_seconds" value={num("pause_seconds")} placeholder={ph("pause_seconds")} min={0} onChange={(v) => set("pause_seconds", v)} />
            </Field>
            <Field label="Most calls in the whole test" hint="A safety cap: the test stops starting calls after this many.">
              <Num id="load-max_calls" value={num("max_calls")} placeholder={ph("max_calls")} min={1} onChange={(v) => set("max_calls", v)} />
            </Field>
          </Grid>
        </Card>

        <Card>
          <CardHead level={2} title="3 · Callers and tests" />
          <Block title="Who plays the callers">
            <div className="grid gap-3 grid-cols-1 md:grid-cols-2" id="load-caller">
              <Choice radio on={s.caller === "free"} data-caller="free" onClick={() => set("caller", undefined)}>
                <b className="block font-semibold">Scripted (free)</b>
                <div className="text-ink-2 text-[13px]">Each test's own lines and the rule-based caller. No Claude spend, however many calls.</div>
              </Choice>
              <Choice radio on={s.caller === "claude"} data-caller="claude" onClick={() => set("caller", "claude")}>
                <b className="block font-semibold">Claude</b>
                <div className="text-ink-2 text-[13px]">Real conversations, as in a normal run. Costs money per call: keep the test small.</div>
              </Choice>
            </div>
            <div className="flex flex-wrap gap-x-8 gap-y-3 mt-1">
              <label><Switch id="load-judges" checked={!!s.judges} onChange={(v) => set("judges", v || undefined)} label="AI judges on every call" /></label>
              {voice && <label><Switch id="load-light" checked={s.light !== false} onChange={(v) => set("light", v ? undefined : false)} label="Light callers" /></label>}
              <label className={page.agent.agent_process ? "" : "opacity-55"}>
                <Switch id="load-start-agent" checked={!!s.start_agent && page.agent.agent_process} label="Start the agent for me"
                  onChange={(v) => page.agent.agent_process && set("start_agent", v || undefined)} />
              </label>
            </div>
            <div className="text-muted text-[13px]">
              {s.judges ? "The judges read every load call (money). Only trusted items count. " : "Judges are off: load calls are checked by the hard checks (tools, database, words said). "}
              {page.agent.agent_process ? "Vocto can start the agent before the first call and stop it after the last." : "Starting the agent needs Set up › Connection › “How to start the agent”."}
              {voice && (s.light !== false ? " Light callers: each caller line is made once and reused, and the careful second listen is skipped, so this computer can push more calls; answer times still come from the audio."
                : " Full callers: every line is spoken fresh and every call gets the careful second listen (heavier on this computer).")}
            </div>
          </Block>
          {voice && (
            <Block title="Caller conditions" sub="Laid over every load call.">
              <div className="grid gap-3 grid-cols-1 md:grid-cols-2" id="load-conditions">
                {(system?.conditions || [{ id: "as_written", title: "As written", about: "Each test's own audio." }]).map((c) => (
                  <Choice key={c.id} radio on={s.conditions === c.id} data-conditions={c.id} onClick={() => set("conditions", c.id === "as_written" ? undefined : c.id)}>
                    <b className="block font-semibold">{c.title}</b>
                    <div className="text-ink-2 text-[13px]">{c.about}</div>
                  </Choice>
                ))}
              </div>
            </Block>
          )}
          <Block title="Tests" sub={chosen.length ? "The callers take these tests in turn." : "None picked: every test in this channel, without the ones with planted tool faults (they slow the agent on purpose) and without safety attacks."}
            aside={chosen.length ? <Button size="sm" variant="ghost" onClick={() => set("scenarios", undefined)}>Use the default</Button> : undefined}>
            {tests.length ? (
              <div className="grid gap-2 grid-cols-1 md:grid-cols-2" id="load-tests">
                {tests.map((t) => (
                  <Choice key={t.id} on={using.includes(t.id)} data-test={t.id} onClick={() => toggleTest(t.id)}>
                    <b className="block font-semibold text-[13px]">{t.title}</b>
                    <div className="flex flex-wrap gap-1 mt-0.5">
                      <span className="text-muted text-[13px] font-mono">{t.id}</span>
                      {t.criticality === "critical" && <Chip tone="bad" className="text-[13px]">critical</Chip>}
                      {t.faults && <Chip tone="warn" className="text-[13px]">planted tool fault</Chip>}
                    </div>
                  </Choice>
                ))}
              </div>
            ) : <Notice tone="warn">This channel has no tests yet. Add some in Set up › Test cases.</Notice>}
            {factNames.length > 0 && (
              <div className="grid gap-1.5" id="load-unique">
                <div className="font-semibold text-[13px]">A value of its own on every call</div>
                <div className="flex flex-wrap gap-2">
                  {factNames.map((f) => {
                    const on = (s.unique_facts || []).includes(f);
                    return <Choice key={f} on={on} data-fact={f} onClick={() => { const cur = s.unique_facts || []; set("unique_facts", on ? cur.filter((x) => x !== f) : [...cur, f]); }}>
                      <span className="text-[13px] font-mono">{f}</span></Choice>;
                  })}
                </div>
                <div className="text-muted text-[13px]">
                  Every load call is checked for leaks: one caller's details (name, phone, date of birth, reference) turning up in another call blocks the result.
                  Picked details get a made-up value per call, so calls of the same test can be told apart too. Only pick details your system doesn't need to find the caller.
                </div>
              </div>
            )}
            <div className="text-muted text-[13px]">
              The test data (database rows) is loaded once, before the first call: many calls share one copy of it, as callers share a real system.
              A booking test may then find its slot already taken; that lowers the pass rate but never counts as a broken rule.
            </div>
          </Block>
        </Card>

        <Card>
          <CardHead level={2} title="4 · Limits" sub="Every stage runs at or below the target, so every stage must meet these. A stage that breaks one makes the run “Not ready”." />
          <Grid>
            <Field label="Failed calls per stage, at most (%)" hint="Couldn't connect, agent error, stopped answering, ran too long.">
              <Num id="load-max_failed_pct" value={ownLimits.max_failed_pct as number} placeholder={`${page.defaults.limits.max_failed_pct} (default)`} min={0} step={0.5}
                onChange={(v) => setLimit("max_failed_pct", v)} />
            </Field>
            <Field label="Answer time p95 per stage, at most (ms)" hint={voice ? "Empty: the release gate's p95 (Pass rules)." : "Text runs have no answer times."}>
              <Num id="load-p95_ms" value={ownLimits.p95_ms as number} placeholder={p95Ph} min={1} onChange={(v) => setLimit("p95_ms", v)} />
            </Field>
            <Field label="Vocto's own lag, at most (ms)" hint="If this computer falls behind, a stage gives “not enough evidence” instead of blaming the agent.">
              <Num id="load-max_lag_ms" value={ownLimits.max_lag_ms as number} placeholder={`${page.defaults.limits.max_lag_ms} (default)`} min={1} onChange={(v) => setLimit("max_lag_ms", v)} />
            </Field>
            <Field label="Finished calls a stage needs" hint="Fewer: the stage shows “too few to judge”.">
              <Num id="load-min_calls" value={ownLimits.min_calls as number} placeholder={`${page.defaults.limits.min_calls} (default)`} min={1} onChange={(v) => setLimit("min_calls", v)} />
            </Field>
            <Field label="Timed answers a stage's p95 needs" hint="Fewer: its p95 is shown but can't break the limit.">
              <Num id="load-min_answers" value={ownLimits.min_answers as number} placeholder={`${page.defaults.limits.min_answers} (default)`} min={1} onChange={(v) => setLimit("min_answers", v)} />
            </Field>
          </Grid>
          <div className="mt-4" id="load-trends">
            <Table compact head={["Getting worse under load", "Limit", "Blocks"]}>
              {([
                ["slowdown", "Slower at the top", "p95 at the top level against the lowest level (both limits).", [["pct", "%"], ["ms", "ms"]]],
                ["pass_drop", "Fewer tests passed", "Pass rate at the top level against the lowest level.", [["points", "points"]]],
                ["drift", "Drift over a soak", "The last window against the first (p95 and failed calls).", [["pct", "%"]]],
                ["recovery", "Recovery after a spike", "After the spike, answers must be back within this of before it (p95 and failed calls).", [["pct", "%"], ["ms", "ms"]]],
              ] as [keyof LoadLimits, string, string, [string, string][]][]).map(([k, title, about, fields]) => {
                const cur = s.limits[k] as unknown as Record<string, number | boolean>;
                const def = page.defaults.limits[k] as unknown as Record<string, number | boolean>;
                return (
                  <tr key={k} data-trend={k}>
                    <td className="min-w-[160px]"><b>{title}</b><div className="text-muted text-[13px]">{about}</div></td>
                    <td>
                      <div className="flex flex-wrap gap-2">
                        {fields.map(([f, unit]) => (
                          <label key={f} className="flex items-center gap-1.5 text-[13px] text-muted">
                            <span className="w-[76px]"><Num id={`load-${k}-${f}`} min={0} value={asObj(ownLimits[k])[f] as number} placeholder={String(def[f])}
                              onChange={(v) => setLimit(k, v, f)} /></span>{unit}
                          </label>
                        ))}
                      </div>
                    </td>
                    <td className="w-[90px]"><label><Switch id={`load-${k}-blocks`} checked={!!cur.blocks} label="" onChange={(v) => setLimit(k, v, "blocks")} /></label></td>
                  </tr>
                );
              })}
            </Table>
            <div className="text-muted text-[13px] mt-2">Off: a warning on the report. On: also “Not ready”. They compare two parts of one test, so a small agent with few calls gives noisy numbers.</div>
          </div>
        </Card>
        <Problems items={problems} />
        <SaveBar id="save-load" dirty={dirty} busy={busy} onSave={() => void save()} onRevert={() => { setDraft({ ...(page.saved as Obj) }); setProblems([]); }}
          note={plan.error ? <span className="text-bad">{plan.error}</span> : "Saved in this channel's profile, under load."} />
      </div>

      <div className="flex flex-col gap-4 xl:sticky xl:top-[72px]">
        <Card id="load-plan">
          <CardHead level={2} title="The plan" sub={plan.error ? undefined : `${SHAPE_LABEL[s.shape]} · ${plan.stages.length} stage${plan.stages.length === 1 ? "" : "s"}`} />
          {plan.error ? <Notice tone="bad">{plan.error}</Notice> : (
            <>
              <div ref={chartBox}><LoadChart stages={plan.stages} width={chartW} height={170} /></div>
              <dl className="grid grid-cols-2 gap-x-4 gap-y-2 mt-3 text-[13px]">
                <div><dt className="text-muted text-[13px]">Takes</dt><dd className="font-semibold" id="plan-length">{fmtSeconds(total)}</dd></div>
                <div><dt className="text-muted text-[13px]">Most at once</dt><dd className="font-semibold" id="plan-peak">{peak}</dd></div>
                <div><dt className="text-muted text-[13px]">Calls, roughly</dt><dd className="font-semibold">~{roughCalls(plan.stages, callSecs, s.pause_seconds, s.max_calls)}{" "}
                  <span className="text-muted font-normal text-[13px]">(cap {s.max_calls})</span></dd></div>
                <div><dt className="text-muted text-[13px]">Tests used</dt><dd className="font-semibold">{using.length}</dd></div>
                {s.expected_peak ? <div><dt className="text-muted text-[13px]">Your peak</dt><dd className="font-semibold" id="plan-expected">{s.expected_peak} at once</dd></div> : null}
                {voice && <div className="col-span-2"><dt className="text-muted text-[13px]">p95 limit per stage</dt>
                  <dd className="font-semibold">{fmtMs((ownLimits.p95_ms as number) ?? page.voice_p95_ms)}</dd></div>}
              </dl>
              {shortStages.length > 0 && (
                <Notice tone="warn" icon="info" className="mt-3">
                  {shortStages.length} stage{shortStages.length === 1 ? "" : "s"} will finish fewer than {s.limits.min_calls} calls and show as “too few to judge”. Make the steps longer
                  (at {s.call_minutes} min a call, {s.limits.min_calls} calls at 1 at once take {fmtSeconds(s.limits.min_calls * (s.call_minutes! * 60 + s.pause_seconds))}).
                </Notice>
              )}
              <div className="mt-3 grid gap-1">
                {plan.stages.map((x) => (
                  <div key={x.index} className="flex items-center gap-2 text-[13px]">
                    <span className="flex-1 text-ink-2">{x.name}</span>
                    <span className="tabular-nums font-semibold">{x.level}×</span>
                    <span className="tabular-nums text-muted w-[64px] text-right">{fmtSeconds(x.seconds)}</span>
                  </div>
                ))}
              </div>
            </>
          )}
        </Card>

        <Card id="load-start">
          <CardHead level={2} title="Start" />
          {voice && (
            <Field label="Caller's speech" className="mb-3">
              <Seg id="load-speech" value={speech} onChange={setSpeech}
                options={[{ value: "local", label: "Real speech" }, { value: "codec", label: "Test tones" }]} />
            </Field>
          )}
          {voice && speech === "local" && <div className="text-muted text-[13px] -mt-1.5 mb-3">Every call speaks and listens on this computer. Many at once need a strong machine: Vocto watches its own lag and says if it fell behind.</div>}
          {voice && speech === "codec" && <div className="text-muted text-[13px] -mt-1.5 mb-3">Tones only: for testing Vocto itself or an agent that doesn't need words.</div>}
          {Object.keys(profile?.offline_options || {}).length > 0 && (
            <label className="block mb-3"><Switch id="load-offline" checked={offline} onChange={setOffline} label="Free dry run (the agent's offline options)" /></label>
          )}
          {noKey && <Notice tone="warn" icon="key" className="mb-3">No API key is set, and Claude callers or the judges need one. <a href="#/settings">Add it in Settings</a>.</Notice>}
          {!page.agent.launched_per_call && !(s.start_agent && page.agent.agent_process) && profile?.connection.target && (
            <Notice className="mb-3" icon="plug" tone="warn">
              The agent must already be running at <code className="break-all">{profile.connection.target}</code>.
              {page.agent.agent_process ? <> Or switch on <b>Start the agent for me</b> (3 · Callers and tests) and Vocto starts and stops it.</> : " Start it before the load test."}
              {" "}If nothing answers there, the test stops at once.
            </Notice>
          )}
          {phone && !dirty && (
            <Notice tone="warn" icon="phone" className="mb-3">
              <div id="load-phone">{phone.text}{phone.cost != null ? <> Estimated cost at your prices: <b>{phone.cost} {phone.currency}</b>.</> : " Set your prices in Set up › Connection › Running costs to see an estimate."}</div>
              <label className="block mt-2"><Switch id="load-phone-ok" checked={phoneOk} onChange={setPhoneOk} label="I understand the cost and my account allows this many calls at once" /></label>
            </Notice>
          )}
          {running && <Notice className="mb-3" icon="activity">A run is in progress. <a href={projectHref(project.key, "/live")}>Watch it</a>; start this when it ends.</Notice>}
          <Button id="start-load" variant="primary" icon="play" className="w-full justify-center" busy={starting}
            disabled={starting || running || dirty || !!plan.error || !!page.error || !using.length || !!noKey || (!!phone && !phoneOk)}
            onClick={() => void start()}>Start load test</Button>
          <div className="text-muted text-[13px] mt-2">
            {dirty ? "Save your changes first." : page.error ? page.error : `About ${fmtSeconds(total)}. Calls already finished stay saved if you stop it.`}
          </div>
          <div className="text-muted text-[13px] mt-3">From a terminal:<code className="block mt-1 px-2.5 py-1.5 rounded-[10px] bg-surface-2 border border-line font-mono text-[12px] text-ink-2 break-all leading-relaxed">uv run python -m vatf load {project.path}{page.file !== "profile.yaml" ? ` --profile ${page.file}` : ""}{s.start_agent ? " --start-agent" : ""}</code></div>
        </Card>

        <BaselineCard page={page} projectKey={project.key} />
      </div>
    </div>

      <PastRuns page={page} projectKey={project.key} />
    </div>
  );
}

function BaselineCard({ page, projectKey }: { page: LoadPage; projectKey: string }) {
  const b = page.baseline;
  return (
    <Card id="load-baseline">
      <CardHead level={3} title="Load baseline" sub="Each load test is compared with it, level by level. Separate from the normal baseline." />
      {page.baseline_problem && <Notice tone="warn" className="mb-2">{page.baseline_problem}</Notice>}
      {b ? (
        <div className="grid gap-1 text-[13px]">
          <div><a href={projectHref(projectKey, `/runs/${b.run_id}`)} className="font-mono text-[13px]">{b.run_id}</a> · <Badge status={b.decision} label={decisionTitle(b.decision)} /></div>
          <div className="text-ink-2">Capacity {b.load?.capacity ?? "—"} at once · set {fmtDate(b.set_at)}</div>
          {b.note && <div className="text-muted text-[13px]">{b.note}</div>}
        </div>
      ) : <div className="text-muted text-[13px]">None yet. Open a load test you trust in Results and choose Set as baseline.</div>}
    </Card>
  );
}

function PastRuns({ page, projectKey }: { page: LoadPage; projectKey: string }) {
  return (
    <Card flush id="load-runs">
      <CardHead level={2} title="Past load tests" className="px-[18px] pt-4 mb-2" />
      {page.runs.length ? (
        <Table head={["Result", "Shape", "Capacity", "Calls", "Started"]}>
          {page.runs.map((r) => (
            <tr key={r.run_id} className={rowLink} onClick={() => navigate(projectHref(projectKey, `/runs/${r.run_id}`))}>
              <td>{r.decision ? <Badge status={r.decision} label={decisionTitle(r.decision)} /> : <Badge status={r.status} />}</td>
              <td>{r.load ? `${SHAPE_LABEL[r.load.shape]} to ${r.load.calls_at_once}` : "—"}</td>
              <td className="tabular-nums">{r.load?.capacity ?? "—"}</td>
              <td className="tabular-nums">{r.load?.calls ?? r.calls_started}{r.load?.failed ? <span className="text-bad"> · {r.load.failed} failed</span> : null}</td>
              <td className="text-ink-2 whitespace-nowrap">{fmtDate(r.started_at)}</td>
            </tr>
          ))}
        </Table>
      ) : <EmptyState title="No load tests yet" text="Save the settings and start one: its report shows how many calls at once the agent can take." icon="activity" />}
    </Card>
  );
}
