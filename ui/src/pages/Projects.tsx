// Projects: one per voice agent under test. Each holds how to reach the agent, its tools, backend and
// logs, the test settings and the test plan; and its own runs.
import { useState } from "react";
import { MiniHistory, ProjectAvatar, When } from "../components/dash";
import { useToast } from "../components/feedback";
import { Area, Help, Text } from "../components/form";
import { Icon } from "../components/Icon";
import { Badge, Button, Card, CardHead, Chip, Choice, EmptyState, Field, Notice, PageHead, Skeleton } from "../components/ui";
import { api } from "../lib/api";
import { useApp, useCrumbs, useLoad } from "../lib/app";
import { cx } from "../lib/format";
import { domainLabel, modeLabel, projectHref } from "../lib/links";
import { navigate } from "../lib/router";
import { setupApi, slug } from "../lib/setup";
import type { Dashboard } from "../lib/types";

type Row = Dashboard["projects"][number];

function ProjectCard({ p }: { p: Row }) {
  const s = p.stats;
  return (
    <Card data-project={p.key} className="group flex flex-col gap-3.5 transition-[box-shadow,border-color] hover:shadow-card hover:border-line-strong">
      <div className="flex items-start gap-3">
        <ProjectAvatar name={p.name} k={p.key} size={42} />
        <div className="flex-1 min-w-0">
          <a href={projectHref(p.key)} className="font-semibold text-[15px] text-ink leading-snug block">{p.name}</a>
          <div className="text-muted text-xs mt-0.5">{domainLabel(p.domain)}{p.example ? " · practice agent" : ""}</div>
        </div>
        {s.last_decided ? <Badge status={s.last_decided.decision} /> : p.ready ? <Chip tone="good" icon="check">ready</Chip> : <Chip tone="warn" icon="alert">to set up</Chip>}
      </div>
      {p.description && <p className="text-ink-2 text-[13px] line-clamp-2">{p.description}</p>}
      <div className="flex flex-wrap gap-1.5">
        {p.channels.map((c) => <Chip key={c} tone={c === "voice" ? "agent" : "caller"} icon={c === "voice" ? "mic" : "text"}>{modeLabel(c)}</Chip>)}
        <Chip icon="target">{s.scenarios} scenario{s.scenarios === 1 ? "" : "s"}</Chip>
      </div>
      <div className="flex items-center gap-3 pt-3 mt-auto border-t border-line text-[12.5px]">
        <MiniHistory items={s.history.slice(-8)} empty="No runs yet" />
        <span className="flex-1" />
        {s.last_run && <span className="text-muted"><When iso={s.last_run.started_at} /></span>}
      </div>
      <div className="flex gap-2">
        <Button size="sm" icon="overview" href={projectHref(p.key)}>Open</Button>
        <Button size="sm" icon="play" href={projectHref(p.key, "/new")}>Run tests</Button>
        <Button size="sm" variant="ghost" icon="settings" href={projectHref(p.key, "/setup/agent")}>Set up</Button>
      </div>
    </Card>
  );
}

export function Projects() {
  useCrumbs([{ label: "Projects" }]);
  const { data, error } = useLoad(() => api.get<Dashboard>("/api/dashboard"), []);
  if (error) return <Notice tone="bad" className="mt-5">{error.message}</Notice>;
  if (!data) return <Skeleton />;
  const mine = data.projects.filter((p) => !p.example);
  const examples = data.projects.filter((p) => p.example);
  return (
    <>
      <PageHead title="Projects" sub="One project per voice agent you test. Each has its own setup, test plan, runs and results.">
        <Button id="new-project" variant="primary" icon="plus" href="#/projects/new">New project</Button>
      </PageHead>
      <h2 className="font-semibold mb-2.5">Your agents</h2>
      {mine.length ? <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3 stagger mb-8">{mine.map((p) => <ProjectCard key={p.key} p={p} />)}</div> : (
        <Card className="mb-8"><EmptyState icon="bot" title="No agents yet" text="Create a project for the voice agent you want to test. You'll need its address, and ideally test access to its tools, database and logs."
          action={<Button variant="primary" icon="plus" href="#/projects/new">New project</Button>} /></Card>
      )}
      {examples.length > 0 && (
        <>
          <h2 className="font-semibold mb-1">Practice agents</h2>
          <p className="text-muted text-[12.5px] mb-2.5">Vocto's own agents with bugs you can switch on, used to prove that every bug gets caught.</p>
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3 stagger">{examples.map((p) => <ProjectCard key={p.key} p={p} />)}</div>
        </>
      )}
    </>
  );
}

const DOMAINS = ["Healthcare", "Banking", "Insurance", "Retail", "Travel", "Telecom", "Restaurants", "Real estate", "Utilities"];

export function NewProject() {
  useCrumbs([{ label: "Projects", href: "#/projects" }, { label: "New project" }]);
  const toast = useToast();
  const { reloadProjects } = useApp();
  const [name, setName] = useState("");
  const [domain, setDomain] = useState("");
  const [description, setDescription] = useState("");
  const [channel, setChannel] = useState<"voice" | "text">("voice");
  const [target, setTarget] = useState("");
  const [busy, setBusy] = useState(false);
  const create = async () => {
    if (!name.trim()) { toast("Give the agent a name", "bad"); return; }
    setBusy(true);
    try {
      const r = await setupApi.newProject({ name, id: slug(name), domain: domain.trim().toLowerCase(), description, channel, target });
      await reloadProjects();
      toast(`Created ${name}`);
      navigate(projectHref(r.key, "/setup/agent"));
    } catch (e) { toast((e as Error).message, "bad"); } finally { setBusy(false); }
  };
  return (
    <>
      <PageHead title="New project" sub="Tell Vocto about the agent. Tools, database, logs and the test plan come next, step by step." />
      <div className="grid gap-4 items-start grid-cols-1 xl:grid-cols-[minmax(0,860px)_minmax(0,1fr)]">
        <Card>
          <div className="grid gap-5">
            <div>
              <CardHead level={2} title="1 · The agent" className="mb-3" />
              <div className="grid gap-4">
                <Field label="Name"><Text id="np-name" value={name} onChange={setName} placeholder="Acme Bank card support" /></Field>
                <Field label="What it does"><Area value={description} onChange={setDescription} rows={2} placeholder="Answers card questions, blocks lost cards and orders replacements." /></Field>
                <div>
                  <div className="font-semibold text-[13px] mb-1.5">Industry</div>
                  <div className="flex flex-wrap gap-1.5" id="np-domain">
                    {DOMAINS.map((d) => (
                      <button key={d} type="button" onClick={() => setDomain(domain === d ? "" : d)}
                        className={cx("px-3 py-1 rounded-full border text-[12.5px] font-semibold cursor-pointer transition-colors",
                          domain.toLowerCase() === d.toLowerCase() ? "bg-accent-soft text-accent-ink border-transparent" : "bg-surface text-ink-2 border-line hover:border-line-strong")}>{d}</button>
                    ))}
                    <input className="px-3 py-1 rounded-full border border-line bg-surface text-[12.5px] w-[140px] outline-none focus:border-accent"
                      placeholder="Other…" value={DOMAINS.includes(domain) ? "" : domain} onChange={(e) => setDomain(e.target.value)} aria-label="Other industry" />
                  </div>
                </div>
              </div>
            </div>
            <div className="h-px bg-line" />
            <div>
              <CardHead level={2} title="2 · How Vocto talks to it" className="mb-3" />
              <div className="grid gap-2 sm:grid-cols-2">
                <Choice radio on={channel === "voice"} onClick={() => setChannel("voice")} data-channel="voice">
                  <b className="flex items-center gap-2"><Icon name="mic" size={15} />Voice</b>
                  <div className="text-muted text-[12.5px] mt-0.5">Real speech both ways over a WebSocket. Response times, interruptions, noisy lines.</div>
                </Choice>
                <Choice radio on={channel === "text"} onClick={() => setChannel("text")} data-channel="text">
                  <b className="flex items-center gap-2"><Icon name="text" size={15} />Text</b>
                  <div className="text-muted text-[12.5px] mt-0.5">Messages over HTTP. Fast and cheap; the same checks without speech. Voice can be added later.</div>
                </Choice>
              </div>
              <Field label="Agent's address" hint="Optional now: you can add it on the next page." className="mt-4">
                <Text id="np-target" mono value={target} onChange={setTarget} placeholder={channel === "voice" ? "wss://agent.example.com/call" : "https://agent.example.com/vocto"} />
              </Field>
            </div>
            <div className="flex items-center gap-3 flex-wrap">
              <Button id="np-create" variant="primary" size="lg" icon="plus" busy={busy} disabled={busy || !name.trim()} onClick={() => void create()}>Create project</Button>
              <Button variant="ghost" href="#/projects">Cancel</Button>
            </div>
          </div>
        </Card>
        <Card className="bg-surface-2/60">
          <CardHead title="What happens next" />
          <ol className="grid gap-3.5">
            {([
              ["plug", "Connect the agent", "Its voice or text address, and a quick connection test."],
              ["tool", "Tools", "The APIs it calls. Vocto watches each call and can break one on purpose."],
              ["db", "Backend & database", "Read-only access, so Vocto checks what really got saved."],
              ["logs", "Logs", "Optional: the agent's own log lines, kept with each call."],
              ["plan", "Test plan", "Scenarios (what callers try) and personas (who calls)."],
              ["play", "Run tests", "Get a GO or NO-GO release decision with the evidence."],
            ] as const).map(([icon, title, text]) => (
              <li key={title} className="flex gap-3 items-start">
                <span className="size-8 rounded-[10px] grid place-items-center bg-surface text-accent-ink border border-line flex-none"><Icon name={icon} size={15} /></span>
                <span><b className="text-[13.5px]">{title}</b><span className="block text-[12.5px] text-ink-2">{text}</span></span>
              </li>
            ))}
          </ol>
          <Help className="mt-4">Passwords and API tokens are never stored in a project: you'll keep them under Settings › Secrets.</Help>
        </Card>
      </div>
    </>
  );
}
