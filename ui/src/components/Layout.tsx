// The app shell. Outside an agent the sidebar is the workspace (home, agents, settings, your agents).
// Inside an agent it shows only that agent, in the order the work happens: overview, connect, its tests
// (one page per test type, each with a status dot), its results, and its settings at the bottom.
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { api } from "../lib/api";
import { useApp, useLoad } from "../lib/app";
import { cx } from "../lib/format";
import { connectionWords, mainProfile, projectByKey, projectHref, projectOfJob } from "../lib/links";
import { MODE_TITLE, TEST_TYPES, TONE_DOT, TYPE_STATUS, unavailableWords } from "../lib/modes";
import type { ModeBrief, Project } from "../lib/types";
import { ProjectAvatar } from "./dash";
import { Icon, type IconName } from "./Icon";
import { Button, LiveDot } from "./ui";

interface NavItem { href: string; key: string; label: string; icon?: IconName; dot?: string; title?: string }

const WORKSPACE: NavItem[] = [
  { href: "#/", key: "dashboard", label: "Home", icon: "home" },
  { href: "#/projects", key: "projects", label: "Agents", icon: "bot" },
  { href: "#/settings", key: "settings", label: "Settings", icon: "settings" },
];

function kindOf(p: Project | null): string | null {
  const m = p ? mainProfile(p) : null;
  if (!m) return null;
  const t = m.connection?.type;
  return t === "phone_call" ? "phone" : t === "twilio_media_stream" ? "twilio_stream" : t === "text_http" ? "text_http"
    : t === "text_python" ? "in_repo" : "websocket_audio";
}

// The agent's own pages. Each test type's dot is its latest result (grey: not run yet, or it can't run).
function agentNav(k: string, modes: ModeBrief[] | null): { title?: string; items: NavItem[] }[] {
  const byId = new Map((modes || []).map((m) => [m.id, m]));
  return [
    { items: [
      { href: projectHref(k), key: "overview", label: "Overview", icon: "overview" },
      { href: projectHref(k, "/setup/connection"), key: "connect", label: "Connect", icon: "plug" },
      { href: projectHref(k, "/test/full"), key: "mode:full", label: "Release check", icon: "play-circle" },
    ] },
    { title: "Tests", items: TEST_TYPES.map((id) => {
      const m = byId.get(id);
      const st = m ? TYPE_STATUS[m.status] : null;
      return { href: projectHref(k, `/test/${id}`), key: `mode:${id}`, label: MODE_TITLE[id],
        dot: st ? TONE_DOT[st.tone] : undefined, title: m ? (m.status === "unavailable" ? unavailableWords(m) : st!.label) : undefined };
    }) },
    { title: "Results", items: [
      { href: projectHref(k, "/runs"), key: "results", label: "All runs", icon: "history" },
      { href: projectHref(k, "/judges"), key: "judges", label: "AI judges", icon: "scale" },
      { href: projectHref(k, "/calls"), key: "calls", label: "Real calls", icon: "phone" },
    ] },
  ];
}

function currentTheme(): "light" | "dark" {
  const t = document.documentElement.dataset.theme;
  if (t === "light" || t === "dark") return t;
  return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function ProjectSwitcher({ projectKey }: { projectKey: string }) {
  const { projects } = useApp();
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const current = projectByKey(projects, projectKey);
  const main = current ? mainProfile(current) : null;
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => { if (!box.current?.contains(e.target as Node)) setOpen(false); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", esc);
    return () => { document.removeEventListener("mousedown", close); document.removeEventListener("keydown", esc); };
  }, [open]);
  useEffect(() => { setOpen(false); }, [projectKey]);
  return (
    <div ref={box} className="relative">
      <button type="button" id="project-switcher" onClick={() => setOpen((o) => !o)} aria-expanded={open}
        className="w-full flex items-center gap-2.5 p-2 rounded-[14px] border border-[var(--glass-line)] bg-surface shadow-[0_6px_16px_-12px_rgb(30_45_100/.45)] hover:border-line-strong transition-colors text-left cursor-pointer">
        <ProjectAvatar kind={kindOf(current)} size={28} />
        <span className="flex-1 min-w-0">
          <b className="block text-[13px] font-semibold leading-tight truncate">{current?.name || projectKey}</b>
          <span className="block text-[13px] text-muted truncate">{main ? connectionWords(main, current?.example) : "Not connected"}</span>
        </span>
        <Icon name="updown" size={15} className="text-muted flex-none" />
      </button>
      {open && (
        <div className="absolute left-0 right-0 top-[calc(100%+6px)] z-30 bg-surface border border-line rounded-[16px] shadow-pop p-1.5 animate-rise max-h-[60vh] overflow-auto">
          <div className="px-2 pt-1 pb-1.5 text-[13px] font-medium text-muted">Switch agent</div>
          {(projects || []).map((p) => (
            <a key={p.key} href={projectHref(p.key)} className={cx("flex items-center gap-2.5 px-2 py-1.5 rounded-lg hover:bg-surface-2 hover:no-underline text-ink",
              p.key === projectKey && "bg-surface-2")}>
              <ProjectAvatar kind={kindOf(p)} size={22} />
              <span className="flex-1 min-w-0 truncate text-[13px] font-medium">{p.name}</span>
              {p.key === projectKey && <Icon name="check" size={14} className="text-accent" />}
            </a>
          ))}
          <div className="h-px bg-line my-1.5" />
          <a href="#/projects/new" className="flex items-center gap-2 px-2 py-1.5 rounded-lg hover:bg-surface-2 hover:no-underline text-ink-2 text-[13px]"><Icon name="plus" size={15} />New agent</a>
        </div>
      )}
    </div>
  );
}

export function Layout({ section, projectKey, children }: { section: string; projectKey: string | null; children: ReactNode }) {
  const { system, offline, activeJob, crumbs, projects } = useApp();
  const [navOpen, setNavOpen] = useState(false);
  const [theme, setTheme] = useState(currentTheme);
  const [scrolled, setScrolled] = useState(false);
  const jobProject = projectOfJob(activeJob, projects);

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 4);
    window.addEventListener("scroll", onScroll);
    return () => window.removeEventListener("scroll", onScroll);
  }, []);
  useEffect(() => { setNavOpen(false); }, [section, crumbs]);
  // Moving to another page fades the content area up (the page itself is not rebuilt; only the motion replays).
  const mainRef = useRef<HTMLElement>(null);
  useEffect(() => {
    let last = location.hash.split("?")[0];
    const onHash = () => {
      const now = location.hash.split("?")[0];
      const el = mainRef.current;
      if (now === last || !el) return;
      last = now;
      el.classList.remove("route-in");
      void el.offsetWidth;
      el.classList.add("route-in");
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const toggleTheme = () => {
    const next = theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("vatf-theme", next); } catch { /* private window */ }
    setTheme(next);
    window.dispatchEvent(new Event("vatf-theme"));
  };

  const live = !!activeJob && jobProject?.key === projectKey;
  // The test types' dots: reloaded when a run starts or ends.
  const { data: modes } = useLoad(() => (projectKey ? api.get<ModeBrief[]>(`/api/project/${projectKey}/modes`) : Promise.resolve(null)),
    [projectKey, activeJob?.id, activeJob?.status]);
  const link = (n: NavItem) => (
    <a key={n.key} href={n.href} data-nav={n.key} aria-current={section === n.key ? "page" : undefined} title={n.title}
      className={cx("relative z-1 flex items-center gap-2.5 px-3 h-[34px] rounded-[12px] text-[13.5px] transition-colors duration-200 hover:no-underline",
        section === n.key ? "text-ink font-semibold" : "text-ink-2 hover:text-ink hover:bg-[var(--glass-strong)]")}>
      {n.icon && <Icon name={n.icon} size={16} className={section === n.key ? "text-accent" : "text-muted"} />}
      <span className="truncate">{n.label}</span>
      {n.key === "results" && live && <LiveDot className="ml-auto" />}
      {n.dot && <i className={cx("ml-auto size-[7px] rounded-full flex-none", n.dot)} />}
    </a>
  );
  const heading = (title: string) => (
    <div className="px-3 pt-4 pb-1 text-[13px] text-muted font-semibold">{title}</div>
  );

  return (
    <div className="min-h-screen md:grid md:grid-cols-[256px_minmax(0,1fr)] print:block print:min-h-0">
      {navOpen && <div className="fixed inset-0 z-10 bg-black/30 md:hidden animate-fade" onClick={() => setNavOpen(false)} />}
      <aside
        className={cx(
          "print:hidden fixed md:sticky top-0 md:top-3 left-0 bottom-0 z-20 md:z-5 h-screen md:h-[calc(100vh-24px)] w-[268px] md:w-auto md:ml-3 flex flex-col px-3 py-4 glass md:rounded-[22px] max-md:!bg-surface overflow-y-auto scrollbar-thin",
          "transition-transform duration-200 ease-out-soft md:translate-x-0",
          navOpen ? "translate-x-0 shadow-pop md:shadow-none" : "-translate-x-full",
        )}
      >
        <a href="#/" className="flex items-center gap-2.5 px-2 pt-0.5 pb-4 text-ink hover:no-underline">
          <img src="/static/logo.svg" alt="" className="size-[30px] rounded-[10px] shadow-[0_6px_14px_-8px_var(--accent)]" />
          <b className="text-[17px] leading-none tracking-[-.03em] font-bold">Vocto</b>
        </a>
        <SlidingNav section={section}>
          {projectKey ? (
            <>
              <a href="#/" className="flex items-center gap-1.5 px-2 pb-2.5 text-[13px] text-muted hover:text-ink hover:no-underline" id="all-agents">
                <Icon name="back" size={13} />All agents
              </a>
              <ProjectSwitcher projectKey={projectKey} />
              {agentNav(projectKey, modes).map((g, i) => (
                <div key={g.title || i} className={g.title ? "" : "mt-3"}>
                  {g.title && heading(g.title)}
                  <nav className="flex flex-col gap-px" aria-label={g.title || "Agent"}>{g.items.map(link)}</nav>
                </div>
              ))}
            </>
          ) : (
            <>
              <nav className="flex flex-col gap-px" aria-label="Workspace">{WORKSPACE.map(link)}</nav>
              {projects && projects.length > 0 && (
                <>
                  {heading("Your agents")}
                  <nav className="flex flex-col gap-px" aria-label="Agents">
                    {projects.map((p) => link({ href: projectHref(p.key), key: `agent:${p.key}`, label: p.name }))}
                    <a href="#/projects/new" className="flex items-center gap-2.5 px-3 h-[34px] rounded-[12px] text-[13.5px] text-accent-ink hover:bg-[var(--glass-strong)] hover:no-underline">
                      <Icon name="plus" size={15} />New agent
                    </a>
                  </nav>
                </>
              )}
            </>
          )}
        </SlidingNav>
        {projectKey && (
          <nav className="mt-3 pt-3 border-t border-line flex flex-col gap-px" aria-label="Agent settings">
            {link({ href: projectHref(projectKey, "/setup/rules"), key: "agent-settings", label: "Agent settings", icon: "sliders" })}
          </nav>
        )}
        <div className="mt-auto pt-4 px-2.5 text-[13px] text-muted grid gap-2">
          <div className="flex items-center">
            {offline ? (
              <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-bad-soft text-bad font-bold"><Icon name="alert" size={13} />Server stopped</span>
            ) : system?.api_key.set ? (
              <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-good-soft text-good font-bold"><Icon name="key" size={13} />API key set</span>
            ) : system ? (
              <a href="#/settings" className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-warn-soft text-warn font-bold hover:no-underline"><Icon name="key" size={13} />No API key</a>
            ) : null}
          </div>
          <div className="flex items-center">
            <span>Vocto {system?.version || ""}</span>
            <span className="flex-1" />
            <Button variant="ghost" size="sm" iconOnly icon={theme === "dark" ? "sun" : "moon"} onClick={toggleTheme}
              title="Light or dark" aria-label="Switch light or dark theme" id="theme-btn" />
          </div>
        </div>
      </aside>

      <div className="min-w-0 flex flex-col">
        <header className={cx("print:hidden sticky top-0 z-4 flex items-center gap-3 px-4 md:px-8 h-14 border-b transition-[background-color,border-color] duration-200",
          scrolled ? "glass !border-x-0 !border-t-0 !rounded-none !shadow-none border-b-[var(--line)]" : "bg-transparent border-transparent")}>
          <Button variant="ghost" iconOnly icon="menu" className="md:hidden" aria-label="Menu" onClick={() => setNavOpen((o) => !o)} />
          <div className="flex items-center gap-2 text-muted text-[13px] min-w-0">
            {crumbs.map((c, i) =>
              i === crumbs.length - 1 ? (
                <b key={i} className="text-ink font-semibold truncate">{c.label}</b>
              ) : (
                <span key={i} className={cx("items-center gap-2 min-w-0", i < crumbs.length - 2 ? "hidden md:flex flex-none" : "flex flex-shrink")}>
                  <a href={c.href} className="text-muted hover:text-ink truncate">{c.label}</a><Icon name="chevron" size={13} className="flex-none" />
                </span>
              ),
            )}
          </div>
          <span className="flex-1" />
          {activeJob && (
            <a href={jobProject ? projectHref(jobProject.key, `/live/${activeJob.id}`) : "#/"} id="active-pill"
              className="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-accent-soft text-accent-ink text-[13px] font-semibold whitespace-nowrap hover:no-underline animate-rise">
              <LiveDot /><span className="hidden sm:inline">{activeJob.kind === "exit-check" ? "Exit check running" : "Run in progress"}{jobProject ? ` · ${jobProject.name}` : ""}</span>
              <span className="sm:hidden">Live</span>
            </a>
          )}

        </header>
        <main ref={mainRef} className="route-in px-4 md:px-8 pt-2 pb-16 max-w-[1360px] w-full mx-auto print:p-0 print:max-w-none">{children}</main>
      </div>
    </div>
  );
}

// The highlight behind the current link. It moves straight to the new link: sidebar navigation is used
// tens of times a day, and motion there only reads as delay.
function SlidingNav({ section, children }: { section: string; children: ReactNode }) {
  const box = useRef<HTMLDivElement>(null);
  const [mark, setMark] = useState<{ top: number; height: number } | null>(null);
  useLayoutEffect(() => {
    const measure = () => {
      const el = box.current?.querySelector<HTMLElement>(`[data-nav="${CSS.escape(section)}"]`);
      if (!el || !box.current) { setMark(null); return; }
      const a = box.current.getBoundingClientRect(), b = el.getBoundingClientRect();
      setMark({ top: b.top - a.top, height: b.height });
    };
    measure();
    const ro = new ResizeObserver(measure);
    if (box.current) ro.observe(box.current);
    return () => ro.disconnect();
  }, [section]);
  return (
    <div ref={box} className="relative">
      {mark && (
        <span aria-hidden="true" className={cx("absolute left-0 right-0 rounded-[12px] bg-surface shadow-[0_6px_16px_-10px_rgb(30_45_100/.45)] transition-[top] duration-300 ease-[var(--spring)]",)} style={{ top: mark.top, height: mark.height }}>
                  </span>
      )}
      {children}
    </div>
  );
}
