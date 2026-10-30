// New run for one project: what to test and how. The server turns the form into the same run the
// command line would start, so both always agree.
import { useEffect, useMemo, useState } from "react";
import { Tip, useToast } from "../components/feedback";
import { Icon } from "../components/Icon";
import { Button, Card, CardHead, Choice, Chip, Divider, EmptyState, Field, Notice, PageHead, Seg, Skeleton, Switch, inputCls } from "../components/ui";
import { api } from "../lib/api";
import { useApp, useCrumbs, useLoad } from "../lib/app";
import { cx } from "../lib/format";
import { navigate } from "../lib/router";
import { channelLabel, projectHref } from "../lib/links";
import type { Job, Options, Profile, Project, RunDetail, RunInfo } from "../lib/types";

const CHOICES: Record<string, [string, string][]> = {
  brain: [["llm", "Claude (LLM)"], ["rules", "Rules (offline)"]],
  speech: [["local", "Real speech"], ["codec", "Test tones"]],
};
const OPTION_LABEL: Record<string, string> = {
  brain: "Agent's brain", model: "Agent's model", voice: "Agent's voice", stt_model: "Agent's hearing model",
  speech: "Agent's speech", delays_ms: "Reply delays (ms)",
};
const optLabel = (k: string) => OPTION_LABEL[k] || k.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
const OPTION_HELP: Record<string, string> = {
  brain: "How the agent thinks. Claude is the realistic one; rules is free and deterministic.",
  model: "The Claude model the agent itself uses.",
  bugs: "Switch on bugs on purpose: Vocto should catch each one.",
  voice: "The Piper voice the agent speaks with.",
  stt_model: "The Whisper model the agent listens with.",
  speech: "The agent's own speech engines. Test tones only for Vocto's own tests.",
  delays_ms: "Delays (ms) the reference agent waits before each answer.",
};
const STT = ["tiny.en", "base.en", "small.en", "medium.en"];
const EXIT_BUGS = { text: ["lie_on_failure", "skip_confirmation", "double_booking"], voice: ["lie_on_failure", "skip_confirmation", "double_booking", "misread_reference"] };
// The bug switches only the AI judges can catch (rudeness, made-up facts...): part of a live exit check.
const SOFT_BUGS = ["rude_tone", "made_up_facts", "refuses_human"];
// The bug switches the built-in safety tests catch (answering off-topic, leaking a secret, obeying an injection).
const SAFETY_BUGS = ["answers_off_topic", "leaks_secrets", "obeys_injection"];
const exitBugsOf = (p: Profile, judges: boolean, safety: boolean) => [...EXIT_BUGS[p.mode],
  ...[...(judges ? SOFT_BUGS : []), ...(safety ? SAFETY_BUGS : [])].filter((b) => p.bugs?.some((x) => x.name === b))];
// A real agent's planted bugs that this exit check can catch (the same rules as `vatf exit-check`).
const plantedOf = (p: Profile, judges: boolean, safety: boolean) => (p.planted_bugs || []).filter((b) =>
  (!b.judge || judges) && (!b.scenario.startsWith("safety.") || safety) && (b.scenario.startsWith("safety.") || p.scenarios.some((s) => s.id === b.scenario && s.in_mode)));
const splitBugs = (s: string | undefined | null) => String(s || "").split(",").map((x) => x.trim()).filter(Boolean);

interface Form {
  command: "run" | "exit-check";
  project: string;
  profile: string;
  scenarios: Set<string>;
  options: Options;        // only what the user changed
  bugs: Set<string>;
  offline: boolean;
  model: string;
  speech: string;
  stt_model: string;
  stt_check_model: string;
  repeats: string;
  show_pii: boolean;
  judges: boolean;
  safety: boolean;
  start_agent: boolean;    // Vocto starts the agent itself (agent_process)
  planted_bug: string;
}

export function NewRun({ project, query }: { project: Project; query: URLSearchParams }) {
  useCrumbs([{ label: "Projects", href: "#/projects" }, { label: project.name, href: projectHref(project.key) }, { label: "New run" }]);
  const { system } = useApp();
  const from = query.get("from");
  const { data, error } = useLoad(async () => {
    const projects = [project];
    let prefill: RunInfo | null = null;
    let prefillError: string | null = null;
    if (from) {
      try { prefill = (await api.get<RunDetail>(`/api/runs/${from}`)).info; } catch (e) { prefillError = (e as Error).message; }
    }
    return { projects, prefill, prefillError };
  }, [from, project]);
  if (error) return <Notice tone="bad" className="mt-5">{error.message}</Notice>;
  if (!data || !system) return <Skeleton />;   // the form's defaults depend on the API key being set
  if (!data.projects.length) {
    return <EmptyState title="No test projects found" icon="folder" />;
  }
  return <NewRunForm projects={data.projects} prefill={data.prefill} prefillError={data.prefillError} query={query} />;
}

function defaultsOf(p: Profile, offline: boolean): Options {
  return offline ? { ...p.options, ...p.offline_options } : { ...p.options };
}

function NewRunForm({ projects, prefill, prefillError, query }: { projects: Project[]; prefill: RunInfo | null; prefillError: string | null; query: URLSearchParams }) {
  const { system, activeJob, setActiveJob, projects: allProjects } = useApp();
  const jobProject = allProjects?.find((p) => p.path === activeJob?.request?.project);
  const activeJobHref = activeJob && jobProject ? projectHref(jobProject.key, `/live/${activeJob.id}`) : "#/";
  const toast = useToast();
  useEffect(() => { if (prefillError) toast(prefillError, "bad"); }, [prefillError, toast]);

  const [f, setF] = useState<Form>(() => initialForm(projects, prefill, query, !system?.api_key?.set, system?.default_model || "claude-haiku-5-5"));
  const set = (patch: Partial<Form>) => setF((x) => ({ ...x, ...patch }));
  const project = projects.find((p) => p.path === f.project) || projects[0];
  const profile = project.profiles.find((p) => p.file === f.profile) || project.profiles[0];
  const d = defaultsOf(profile, f.offline);
  const voice = profile.mode === "voice";
  const exit = f.command === "exit-check";
  const noKey = !system?.api_key?.set;
  const optKeys = Object.keys(profile.options || {}).filter((k) => k !== "bugs");
  const inMode = profile.scenarios.filter((x) => x.in_mode);

  const pickProfile = (projectPath: string, file: string) => {
    const proj = projects.find((p) => p.path === projectPath) || projects[0];
    const p = proj.profiles.find((x) => x.file === file) || proj.profiles[0];
    setF((x) => ({
      ...x, project: proj.path, profile: p.file, options: {},
      scenarios: new Set(p.scenarios.filter((s) => s.in_mode).map((s) => s.id)),
      bugs: new Set(splitBugs(defaultsOf(p, x.offline).bugs)),
    }));
  };

  const judgesOn = f.judges && !f.offline && profile.judges !== false;
  const safetyOn = f.safety && !!profile.safety?.enabled && !!profile.safety.attacks;
  const planted = !!profile.planted_bugs?.length;
  const plantedOn = plantedOf(profile, judgesOn, safetyOn);
  const exitBugs = planted ? plantedOn.map((b) => b.id) : exitBugsOf(profile, judgesOn, safetyOn);
  const startAgent = !!profile.agent_process && f.start_agent;
  const safetyCalls = !safetyOn ? 0 : f.repeats ? (Number(f.repeats) || 1) * profile.safety!.attacks : profile.safety!.calls;
  const request = useMemo(() => {
    const agent_options: Options = {};
    for (const [k, v] of Object.entries(f.options)) if (String(v) !== String(d[k] ?? "")) agent_options[k] = v;
    const bugs = [...f.bugs].sort().join(",");
    if (f.command === "run" && bugs !== [...new Set(splitBugs(d.bugs))].sort().join(",")) agent_options.bugs = bugs;
    const ids = inMode.map((x) => x.id);
    const chosen = ids.filter((id) => f.scenarios.has(id));
    return {
      command: f.command, project: f.project, profile: profile.file,
      scenarios: chosen.length === ids.length ? [] : chosen,
      repeats: f.repeats || null, agent_options, offline: f.offline, model: f.model, judges: f.judges, safety: safetyOn,
      speech: f.speech, stt_model: f.stt_model, stt_check_model: f.stt_check_model, show_pii: f.show_pii,
      start_agent: f.command === "run" && startAgent, planted_bug: f.command === "run" && startAgent ? f.planted_bug : "",
    };
  }, [f, profile, d, inMode, safetyOn, startAgent]);

  const planned = useMemo(() => {
    const sc = inMode.filter((x) => exit || f.scenarios.has(x.id));
    const reps = (x: { repeats?: number }) => Number(f.repeats) || x.repeats || 3;
    const per = sc.reduce((a, x) => a + reps(x), 0) + safetyCalls;
    if (exit && planted) {  // the clean agent runs everything; each planted bug only its own scenario
      return per + plantedOn.reduce((a, b) => {
        const x = inMode.find((s) => s.id === b.scenario);
        return a + (x ? reps(x) : Number(f.repeats) || 1);
      }, 0);
    }
    return exit ? per * (1 + exitBugs.length) : per;
  }, [inMode, exit, f.scenarios, f.repeats, exitBugs.length, safetyCalls, planted, plantedOn]);

  const [starting, setStarting] = useState(false);
  const start = async () => {
    setStarting(true);
    try {
      const job = await api.post<Job>("/api/jobs", request);
      setActiveJob(job);
      navigate(projectHref(project.key, `/live/${job.id}`));
    } catch (e) {
      toast((e as Error).message, "bad");
      setStarting(false);
    }
  };

  const toggle = (s: Set<string>, v: string) => { const n = new Set(s); n.has(v) ? n.delete(v) : n.add(v); return n; };
  const n = (i: number) => i - (exit ? 1 : 0);   // section numbers shift when the scenario card is hidden

  return (
    <>
      <PageHead title="New run" sub={<>Test <b className="text-ink">{project.name}</b>: Vocto calls the agent, checks every call and gives a release decision.</>}>
        {(profile.bugs || planted || exit) && (
          <Seg id="cmd" value={f.command} onChange={(v) => set({ command: v })}
            options={[{ value: "run", label: "Test run", icon: "play" }, { value: "exit-check", label: "Exit check", icon: "shield" }]} />
        )}
      </PageHead>
      {exit && planted && (
        <Notice className="mb-4">
          <b>Exit check</b> starts the real agent clean and runs every scenario, then restarts it once with each planted bug and runs the scenario that should catch it. It passes when the clean agent gets GO and every planted bug is caught in every repeat.
          {exitBugs.length ? ` Planted bugs: ${exitBugs.join(", ")}.` : " No planted bug can run with these settings."}
          {(profile.planted_bugs || []).length > plantedOn.length && (
            <> Left out: {(profile.planted_bugs || []).filter((b) => !plantedOn.includes(b)).map((b) => b.id).join(", ")}
              {" "}({!judgesOn ? "some need the AI judges, which a free dry run doesn't have" : ""}{!judgesOn && !safetyOn ? "; " : ""}{!safetyOn ? "some are caught by the safety tests: switch them on" : ""}).</>
          )}
          {" "}Agent output goes to its own log in the runs folder.
        </Notice>
      )}
      {exit && !planted && (
        <Notice className="mb-4">
          <b>Exit check</b> runs the clean agent, then once with each bug switch ({exitBugs.join(", ")}). It passes when the clean agent gets GO and every bug is caught in every repeat. It uses all scenarios.
          {judgesOn ? " With the AI judges on it also checks that they catch the soft bugs (rudeness, made-up facts, refusing a person)."
            : !f.offline && profile.judges !== false ? " Turn the AI judges on to check the soft bugs too." : ""}
          {safetyOn && exitBugs.some((b) => SAFETY_BUGS.includes(b)) ? " With safety tests on, the attack calls must catch the safety bugs too (answering off-topic, leaking a secret, obeying a prompt trick)." : ""}
        </Notice>
      )}
      <div className="grid gap-4 items-start grid-cols-1 xl:grid-cols-[minmax(0,1fr)_360px]">
        <div className="flex flex-col gap-4 stagger">
          <Card>
            <CardHead level={2} title="1 · Channel" sub="how Vocto calls the agent" />
            <div className="flex flex-wrap gap-2">
              {project.profiles.map((x) => (
                <Choice key={x.file} radio on={x.file === profile.file} off={!x.ok} onClick={() => pickProfile(f.project, x.file)} data-profile={x.file} className="flex-1 min-w-[220px]">
                  <div className="flex items-center gap-2.5 flex-wrap">
                    <Icon name={x.mode === "voice" ? "mic" : "text"} size={15} />
                    <b>{channelLabel(x, project.profiles)}</b>
                  </div>
                  <div className="text-ink-2 text-[12.5px]">
                    {x.ok ? (x.mode === "voice" ? "Like a phone call: real speech both ways, response times" : "Text messages: fast and cheap, the same checks") : "Not ready: finish its setup first"}
                  </div>
                </Choice>
              ))}
            </div>
            {!profile.ok && <Notice tone="bad" className="mt-3">{profile.problems.map((p) => <div key={p}>{p}</div>)}<a href={projectHref(project.key, "/setup/agent")}>Open the setup</a></Notice>}
          </Card>

          {!exit && (
            <Card>
              <CardHead level={2} title="2 · Scenarios" sub={`${inMode.length} run in ${profile.mode} mode`}>
                <Button size="sm" variant="ghost" id="all" onClick={() => set({ scenarios: new Set(inMode.map((x) => x.id)) })}>All</Button>
                <Button size="sm" variant="ghost" id="none" onClick={() => set({ scenarios: new Set() })}>None</Button>
              </CardHead>
              <div className="flex flex-col gap-2">
                {profile.scenarios.map((x) => (
                  <Choice key={x.id} on={f.scenarios.has(x.id) && x.in_mode} off={!x.in_mode} data-scenario={x.id}
                    title={x.in_mode ? undefined : `Runs in ${x.modes.join(" and ")} mode only`}
                    onClick={() => set({ scenarios: toggle(f.scenarios, x.id) })}>
                    <div className="flex flex-wrap items-center gap-2">
                      <b>{x.title}</b>
                      {x.criticality === "critical" ? <Chip tone="bad" icon="flag">critical</Chip> : <Chip>normal</Chip>}
                      <Chip>{x.style === "scripted" ? "scripted lines" : `simulated caller${x.persona ? ` · ${x.persona}` : ""}`}</Chip>
                      {x.faults.length > 0 && <Chip tone="warn" icon="bolt">{x.faults.join(", ")}</Chip>}
                      {x.audio?.noise && <Chip icon="wave">{x.audio.noise} noise</Chip>}
                      {!!x.audio?.barge_in && <Chip icon="bolt">barge-in</Chip>}
                      {!x.in_mode && <Chip>{x.modes.join("/")} only</Chip>}
                    </div>
                    <div className="text-ink-2 font-mono text-[11.5px] mt-[3px]">
                      {x.id} · {x.repeats}× · {x.checks} check(s){x.expected ? ` · expects ${x.expected.replace("_", " ")}` : ""}
                    </div>
                  </Choice>
                ))}
              </div>
            </Card>
          )}

          <Card>
            <CardHead level={2} title={`${n(3)} · Agent under test`} sub={profile.connection.target.includes("://") ? <span className="font-mono">{profile.connection.target}</span> : "started by Vocto"} />
            <div className="flex flex-col gap-3">
              {!exit && profile.agent_process && (
                <div className="grid gap-3 grid-cols-1 md:grid-cols-2">
                  <div className="grid gap-1.5 font-semibold text-[13px]">
                    Start the agent
                    <Switch id="start-agent" checked={f.start_agent} onChange={(v) => set({ start_agent: v, planted_bug: v ? f.planted_bug : "" })} label="Start the agent for me" />
                    <span className="font-normal text-xs text-muted">
                      {f.start_agent ? "Vocto starts the agent on this computer before the first call and stops it after the last. Stop it first if it is already running."
                        : "Off: the agent must already be running at its address."}
                    </span>
                  </div>
                  {planted && (
                    <Field label="Planted bug" hint={f.start_agent ? "Start the agent with one known bug in its own code or prompts: Vocto should catch it." : "Needs “Start the agent for me”."}>
                      <select id="planted-bug" className={inputCls} disabled={!f.start_agent} value={f.planted_bug} onChange={(e) => set({ planted_bug: e.target.value })}>
                        <option value="">None: the agent as released</option>
                        {profile.planted_bugs!.map((b) => <option key={b.id} value={b.id}>{b.id}: {b.title}</option>)}
                      </select>
                    </Field>
                  )}
                </div>
              )}
              {!exit && profile.bugs && (
                <div>
                  <div className="flex flex-wrap items-center gap-2.5 mb-2"><b className="text-[12.5px]">Bug switches</b><span className="text-muted text-[12.5px]">{OPTION_HELP.bugs}</span></div>
                  <div className="flex flex-wrap gap-2" id="bugs">
                    {profile.bugs.map((b) => {
                      const voiceOnly = /^voice only/i.test(b.help) && !voice;
                      const on = f.bugs.has(b.name);
                      return (
                        <Tip key={b.name} content={b.help}>
                          <button type="button" data-bug={b.name} disabled={voiceOnly} onClick={() => set({ bugs: toggle(f.bugs, b.name) })}
                            className={cx("inline-flex items-center gap-1.5 px-[9px] py-0.5 rounded-full text-xs font-semibold border leading-[18px] transition-colors",
                              voiceOnly ? "opacity-45 cursor-not-allowed bg-surface-2 text-ink-2 border-line"
                                : on ? "cursor-pointer bg-bad-soft text-bad border-[color-mix(in_srgb,var(--bad)_35%,transparent)]"
                                  : "cursor-pointer bg-surface-2 text-ink-2 border-line hover:border-line-strong")}>
                            <Icon name={on ? "alert" : "plus"} size={12} />{b.name}
                          </button>
                        </Tip>
                      );
                    })}
                  </div>
                </div>
              )}
              {!exit && !profile.bugs && "bugs" in (profile.options || {}) && (
                <Field label="Bug switches" hint={OPTION_HELP.bugs}>
                  <input className={cx(inputCls, "font-mono text-[13px]")} data-opt="bugs" defaultValue={[...f.bugs].join(",")}
                    onChange={(e) => set({ bugs: new Set(splitBugs(e.target.value)) })} />
                </Field>
              )}
              <div className="grid gap-3 grid-cols-1 md:grid-cols-2">
                {optKeys.length ? optKeys.map((k) => {
                  const locked = f.offline && k in (profile.offline_options || {});
                  const val = locked ? d[k] : (f.options[k] ?? d[k] ?? "");
                  const choices = CHOICES[k];
                  const change = (v: string) => set({ options: { ...f.options, [k]: v } });
                  return (
                    <Field key={k} label={optLabel(k)} hint={locked ? "Set by the free dry run." : OPTION_HELP[k] || "An option of this agent."}>
                      {choices ? (
                        <select className={inputCls} data-opt={k} disabled={locked} value={String(val)} onChange={(e) => change(e.target.value)}>
                          {choices.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
                          {!choices.some(([v]) => v === String(val)) && <option value={String(val)}>{String(val)}</option>}
                        </select>
                      ) : (
                        <input className={cx(inputCls, "font-mono text-[13px]")} data-opt={k} disabled={locked} value={String(val)} onChange={(e) => change(e.target.value)} />
                      )}
                    </Field>
                  );
                }) : <div className="text-muted text-[12.5px]">Nothing to choose: Vocto calls the agent at its address.</div>}
              </div>
            </div>
          </Card>

          <Card>
            <CardHead level={2} title={`${n(4)} · Simulated caller`} />
            <div className="grid gap-3 grid-cols-1 md:grid-cols-2" id="caller">
              <Choice radio on={!f.offline} data-offline="0" onClick={() => set({ offline: false })}>
                <b className="block font-semibold">Real run</b>
                <div className="text-ink-2 text-[12.5px]">Claude plays the caller and checks it stayed on script. Uses your API key.</div>
              </Choice>
              <Choice radio on={f.offline} data-offline="1"
                onClick={() => set({ offline: true, options: Object.fromEntries(Object.entries(f.options).filter(([k]) => !(k in (profile.offline_options || {})))) })}>
                <b className="block font-semibold">Free dry run</b>
                <div className="text-ink-2 text-[12.5px]">
                  Rule-based caller, no validator, no API calls
                  {Object.keys(profile.offline_options || {}).length > 0 && " (the practice agent answers by rules too)"}. A smoke test.
                  {profile.agent_process && !Object.keys(profile.offline_options || {}).length && " The agent still thinks with its own brain."}
                </div>
              </Choice>
            </div>
            {!f.offline && noKey && (
              <Notice tone="warn" icon="key" className="mt-3">No API key is set, so a real run's calls will fail. <a href="#/settings">Add it in Settings</a> or choose the free dry run.</Notice>
            )}
            {!f.offline && (
              <Field label="Caller and validator model" hint={`Default ${system?.default_model || ""}`} className="mt-3.5 max-w-[340px]">
                <input id="model" className={cx(inputCls, "font-mono text-[13px]")} value={f.model} onChange={(e) => set({ model: e.target.value.trim() })} />
              </Field>
            )}
          </Card>

          {voice && (
            <Card>
              <CardHead level={2} title={`${n(5)} · Caller's voice and ears`} />
              <div className="grid gap-3 grid-cols-1 md:grid-cols-2" id="speech">
                <Choice radio on={f.speech === "local"} data-speech="local" onClick={() => set({ speech: "local" })}>
                  <b className="block font-semibold">Real speech</b>
                  <div className="text-ink-2 text-[12.5px]">Piper speaks, Whisper listens, on this computer. Models download once (~750 MB).</div>
                </Choice>
                <Choice radio on={f.speech === "codec"} data-speech="codec" onClick={() => set({ speech: "codec" })}>
                  <b className="block font-semibold">Test tones</b>
                  <div className="text-ink-2 text-[12.5px]">Only for testing Vocto itself: never use it to judge an agent.</div>
                </Choice>
              </div>
              {f.speech === "local" && (
                <div className="grid gap-3 grid-cols-1 md:grid-cols-2 mt-3.5">
                  <Field label="Caller hears with" hint="Fast model for the live call.">
                    <select id="stt" className={inputCls} value={f.stt_model} onChange={(e) => set({ stt_model: e.target.value })}>{STT.map((m) => <option key={m}>{m}</option>)}</select>
                  </Field>
                  <Field label="Careful second listen" hint="Re-checks what the agent said, for read-back checks.">
                    <select id="stt2" className={inputCls} value={f.stt_check_model} onChange={(e) => set({ stt_check_model: e.target.value })}>{STT.map((m) => <option key={m}>{m}</option>)}</select>
                  </Field>
                </div>
              )}
            </Card>
          )}

          <Card>
            <CardHead level={2} title={`${n(5) + (voice ? 1 : 0)} · Run settings`} />
            <div className="grid gap-3 grid-cols-1 md:grid-cols-2">
              <Field label="Repeats per scenario" hint="GO needs every repeat of a critical scenario to pass.">
                <input id="repeats" className={inputCls} type="number" min={1} max={50} placeholder={`from test settings (${profile.thresholds?.repeats ?? 3})`}
                  value={f.repeats} onChange={(e) => set({ repeats: e.target.value })} />
              </Field>
              {!f.offline && profile.judges !== false && (
                <div className="grid gap-1.5 font-semibold text-[13px]">
                  AI judges
                  <Switch id="judges" checked={f.judges} onChange={(v) => set({ judges: v })} label="Judge tone, facts and scope" />
                  <span className="font-normal text-xs text-muted">
                    {f.judges ? <>Two judges read every call (a little extra cost). They advise until you <a href={projectHref(project.key, "/judges")}>calibrate them</a>.</>
                      : "Off: only hard checks (tools, database, logs, timing)."}
                  </span>
                </div>
              )}
              {profile.safety?.enabled && (
                <div className="grid gap-1.5 font-semibold text-[13px]">
                  Safety tests
                  <Switch id="safety" checked={f.safety} onChange={(v) => set({ safety: v })} label="Include safety tests" />
                  <span className="font-normal text-xs text-muted">
                    {f.safety ? <>{profile.safety.attacks} attack calls written for this agent: off-topic questions, pushing for secrets, prompt tricks and more. <a href={projectHref(project.key, "/setup/plan")}>Choose them in the test plan</a>.</>
                      : "Off: only the scenarios above."}
                  </span>
                </div>
              )}
              <div className="grid gap-1.5 font-semibold text-[13px]">
                Personal data
                <Switch id="pii" checked={f.show_pii} onChange={(v) => set({ show_pii: v })} label="Keep names and dates readable" />
                <span className={cx("font-normal text-xs", f.show_pii ? "text-bad" : "text-muted")}>
                  {f.show_pii ? "This run will NOT be masked. Don't share or commit it." : "Off: names, dates of birth and ids are masked in the saved run."}
                </span>
              </div>
            </div>
          </Card>
        </div>

        <div className="xl:sticky xl:top-[76px] flex flex-col gap-4">
          <Card>
            <CardHead title="Summary" />
            <div className="flex flex-col gap-[9px]">
              <Row label="Agent"><b className="text-right">{project.name}</b></Row>
              <Row label="Channel"><Chip tone={voice ? "agent" : "caller"} icon={voice ? "mic" : "text"}>{voice ? "Voice" : "Text"}</Chip></Row>
              <Row label={exit ? "Runs" : "Scenarios"}><b>{exit ? 1 + exitBugs.length : f.scenarios.size}</b></Row>
              <Row label="Planned calls"><b className="tabular-nums">{planned}</b></Row>
              <Row label="Caller"><b>{f.offline ? "rules (free)" : "Claude"}</b></Row>
              <Row label="AI judges"><b>{judgesOn ? "on" : "off"}</b></Row>
              {profile.safety?.enabled && <Row label="Safety tests"><b>{safetyOn ? `${safetyCalls} calls` : "off"}</b></Row>}
              {!exit && startAgent && <Row label="Agent"><b>{f.planted_bug ? <Chip tone="bad">{f.planted_bug}</Chip> : "started by Vocto"}</b></Row>}
              {!exit && startAgent && f.planted_bug && (
                <Notice tone="warn" icon="info" className="text-[12.5px]">With a planted bug the right answer is <b>NO-GO</b> for the scenario that should catch it.</Notice>
              )}
              {!exit && f.bugs.size > 0 && (
                <>
                  <Row label="Bugs on"><div className="flex flex-wrap justify-end gap-1.5">{[...f.bugs].map((b) => <Chip key={b} tone="bad">{b}</Chip>)}</div></Row>
                  <Notice tone="warn" icon="info" className="text-[12.5px]">With bugs switched on, the right answer is <b>NO-GO</b>: that means Vocto caught them.</Notice>
                </>
              )}
            </div>
            <Divider />
            <Button id="go" variant="primary" size="lg" className="w-full" busy={starting} icon={starting ? undefined : "play"}
              disabled={starting || !profile.ok || (!exit && f.scenarios.size === 0)} onClick={start}>
              {starting ? "Starting…" : exit ? "Start exit check" : "Start run"}
            </Button>
            {activeJob && (
              <Notice tone="warn" className="mt-2.5 text-[12.5px]">A run is in progress. <a href={activeJobHref}>Watch it</a>; start this one when it's done.</Notice>
            )}
          </Card>
        </div>
      </div>
    </>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return <div className="flex items-start gap-2.5"><span className="text-muted flex-1">{label}</span>{children}</div>;
}

function initialForm(projects: Project[], prefill: RunInfo | null, query: URLSearchParams, offline: boolean, model: string): Form {
  let projectPath = projects[0].path;
  if (prefill && projects.some((p) => p.path === prefill.project)) projectPath = prefill.project;
  else if (query.get("project") && projects.some((p) => p.path === query.get("project"))) projectPath = query.get("project")!;
  const project = projects.find((p) => p.path === projectPath)!;
  const wanted = prefill?.profile_file || query.get("profile") || "profile.yaml";
  const profile = project.profiles.find((p) => p.file === wanted) || project.profiles[0];
  const f: Form = {
    command: "run", project: project.path, profile: profile.file,
    scenarios: new Set(profile.scenarios.filter((s) => s.in_mode).map((s) => s.id)),
    options: {}, bugs: new Set(splitBugs(defaultsOf(profile, offline).bugs)),
    offline, model, speech: "local", stt_model: "base.en", stt_check_model: "small.en", repeats: "", show_pii: false, judges: true, safety: true,
    start_agent: true, planted_bug: "",
  };
  if (prefill) {
    f.offline = !!prefill.offline;
    if (prefill.scenario_ids?.length) f.scenarios = new Set(prefill.scenario_ids);
    if (prefill.repeats) f.repeats = String(prefill.repeats);
    if (prefill.caller_model) f.model = prefill.caller_model;
    if (prefill.speech) Object.assign(f, { speech: prefill.speech.engine, stt_model: prefill.speech.ears_model, stt_check_model: prefill.speech.check_model });
    f.show_pii = prefill.pii_masked === false;
    f.judges = prefill.judges?.why !== "Judges were switched off for this run.";
    f.safety = !!prefill.safety;
    const d = defaultsOf(profile, f.offline);
    f.bugs = new Set(splitBugs(d.bugs));
    for (const [k, v] of Object.entries(prefill.agent_options || {})) {
      if (k === "bugs" && profile.planted_bugs?.some((b) => b.id === v)) f.planted_bug = String(v);
      else if (k === "bugs") f.bugs = new Set(splitBugs(v));
      else if (String(v) !== String(d[k] ?? "")) f.options[k] = String(v);
    }
  }
  return f;
}
