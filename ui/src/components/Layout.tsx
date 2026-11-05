// The app shell. The sidebar has the workspace (home, agents, settings) and, inside an agent, that agent's
// pages in the order the work happens: overview, then 1 Set up, 2 Test (test modes, results), 3 Quality.
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { useApp } from "../lib/app";
import { cx } from "../lib/format";
import { domainLabel, projectByKey, projectHref, projectOfJob } from "../lib/links";
import { ProjectAvatar } from "./dash";
import { Icon, type IconName } from "./Icon";
import { Button, LiveDot } from "./ui";

interface NavItem { href: string; key: string; label: string; icon: IconName }
interface NavGroup { step?: number; title: string; items: NavItem[] }

const WORKSPACE: NavItem[] = [
  { href: "#/", key: "dashboard", label: "Home", icon: "home" },
  { href: "#/projects", key: "projects", label: "Agents", icon: "bot" },
  { href: "#/settings", key: "settings", label: "Settings", icon: "settings" },
];
const agentNav = (k: string): NavGroup[] => [
  { title: "Agent", items: [{ href: projectHref(k), key: "overview", label: "Overview", icon: "overview" }] },
  { step: 1, title: "Set up", items: [
    { href: projectHref(k, "/setup/connection"), key: "setup:connection", label: "Connection", icon: "plug" },
    { href: projectHref(k, "/setup/tools"), key: "setup:data", label: "Tools & data", icon: "db" },
    { href: projectHref(k, "/setup/cases"), key: "setup:cases", label: "Test cases", icon: "plan" },
    { href: projectHref(k, "/setup/callers"), key: "setup:callers", label: "Callers", icon: "user" },
    { href: projectHref(k, "/setup/safety"), key: "setup:safety", label: "Safety tests", icon: "shield" },
    { href: projectHref(k, "/setup/rules"), key: "setup:rules", label: "Pass rules", icon: "sliders" },
    { href: projectHref(k, "/setup/ops"), key: "setup:ops", label: "CI & alerts", icon: "bell" },
  ] },
  { step: 2, title: "Test", items: [
    { href: projectHref(k, "/test"), key: "modes", label: "Test modes", icon: "play-circle" },
    { href: projectHref(k, "/runs"), key: "results", label: "Results", icon: "history" },
  ] },
  { step: 3, title: "Quality", items: [
    { href: projectHref(k, "/judges"), key: "judges", label: "AI judges", icon: "scale" },
    { href: projectHref(k, "/compare"), key: "compare", label: "Compare runs", icon: "compare" },
    { href: projectHref(k, "/calls"), key: "calls", label: "Real calls", icon: "phone" },
  ] },
];

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
        className="w-full flex items-center gap-2.5 p-2 rounded-lg border border-line bg-surface hover:border-line-strong transition-colors text-left cursor-pointer">
        <ProjectAvatar name={current?.name || projectKey} k={projectKey} size={32} />
        <span className="flex-1 min-w-0">
          <b className="block text-[13px] leading-tight truncate">{current?.name || projectKey}</b>
          <span className="block text-[11.5px] text-muted truncate">{current ? `${domainLabel(current.domain)} · ${current.channels.map((c) => (c === "voice" ? "Voice" : "Text")).join(" + ")}` : ""}</span>
        </span>
        <Icon name="updown" size={15} className="text-muted flex-none" />
      </button>
      {open && (
        <div className="absolute left-0 right-0 top-[calc(100%+6px)] z-30 bg-surface border border-line rounded-[10px] shadow-pop p-1.5 animate-rise max-h-[60vh] overflow-auto">
          <div className="px-2 pt-1 pb-1.5 text-[11px] uppercase tracking-[.08em] text-muted">Switch agent</div>
          {(projects || []).map((p) => (
            <a key={p.key} href={projectHref(p.key)} className={cx("flex items-center gap-2.5 px-2 py-1.5 rounded-lg hover:bg-surface-2 hover:no-underline text-ink",
              p.key === projectKey && "bg-accent-soft")}>
              <ProjectAvatar name={p.name} k={p.key} size={26} />
              <span className="flex-1 min-w-0 truncate text-[13px] font-medium">{p.name}</span>
              {p.key === projectKey && <Icon name="check" size={14} className="text-accent-ink" />}
            </a>
          ))}
          <div className="h-px bg-line my-1.5" />
          <a href="#/projects" className="flex items-center gap-2 px-2 py-1.5 rounded-lg hover:bg-surface-2 hover:no-underline text-ink-2 text-[13px]"><Icon name="grid" size={15} />All agents</a>
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

  const toggleTheme = () => {
    const next = theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("vatf-theme", next); } catch { /* private window */ }
    setTheme(next);
    window.dispatchEvent(new Event("vatf-theme"));
  };

  const live = !!activeJob && jobProject?.key === projectKey;
  const link = (n: NavItem) => (
    <a key={n.key} href={n.href} data-nav={n.key} aria-current={section === n.key ? "page" : undefined}
      className={cx("relative z-1 flex items-center gap-2.5 px-2.5 h-[30px] rounded-md text-[13px] transition-colors duration-150 hover:no-underline",
        section === n.key ? "text-ink font-medium" : "text-ink-2 hover:text-ink hover:bg-surface-3/60")}>
      <Icon name={n.icon} size={15} className={section === n.key ? "text-accent" : "text-muted"} />
      {n.label}
      {n.key === "results" && live && <LiveDot className="ml-auto" />}
    </a>
  );
  const heading = (g: NavGroup) => (
    <div className="flex items-center gap-2 px-2.5 pt-5 pb-1.5 font-mono text-[10.5px] uppercase tracking-[.08em] text-muted font-medium">
      {g.step != null && <span className="size-4 rounded grid place-items-center border border-line-strong text-ink-2 text-[9.5px] tracking-normal">{g.step}</span>}
      {g.title}
    </div>
  );

  return (
    <div className="min-h-screen md:grid md:grid-cols-[248px_minmax(0,1fr)] print:block print:min-h-0">
      {navOpen && <div className="fixed inset-0 z-10 bg-black/30 md:hidden animate-fade" onClick={() => setNavOpen(false)} />}
      <aside
        className={cx(
          "print:hidden fixed md:sticky top-0 left-0 bottom-0 z-20 md:z-5 h-screen w-[268px] md:w-auto flex flex-col px-3 py-4 bg-sidebar border-r border-line overflow-y-auto scrollbar-thin",
          "transition-transform duration-200 ease-out-soft md:translate-x-0",
          navOpen ? "translate-x-0 shadow-pop md:shadow-none" : "-translate-x-full",
        )}
      >
        <a href="#/" className="flex items-center gap-2.5 px-2 pt-0.5 pb-4 text-ink hover:no-underline">
          <img src="/static/logo.svg" alt="" className="size-8 rounded-[9px]" />
          <div>
            <b className="block text-[16px] leading-[1.1] tracking-[-.02em] font-semibold">Vocto</b>
            <span className="text-[11.5px] text-muted">Voice agent testing</span>
          </div>
        </a>
        <SlidingNav section={section}>
          <nav className="flex flex-col gap-px" aria-label="Workspace">{WORKSPACE.map(link)}</nav>
          {projectKey && (
            <div className="mt-4 pt-4 border-t border-line">
              <ProjectSwitcher projectKey={projectKey} />
              {agentNav(projectKey).map((g) => (
                <div key={g.title}>
                  {heading(g)}
                  <nav className="flex flex-col gap-px" aria-label={g.title}>{g.items.map(link)}</nav>
                </div>
              ))}
            </div>
          )}
        </SlidingNav>
        <div className="mt-auto pt-4 px-2.5 text-xs text-muted grid gap-2">
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
        <header className={cx("print:hidden sticky top-0 z-4 flex items-center gap-3 px-4 md:px-8 h-14 backdrop-blur-md bg-[color-mix(in_srgb,var(--bg)_85%,transparent)] border-b transition-colors",
          scrolled ? "border-line" : "border-transparent")}>
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
              className="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-accent-soft text-accent-ink text-xs font-semibold whitespace-nowrap hover:no-underline animate-rise">
              <LiveDot /><span className="hidden sm:inline">{activeJob.kind === "exit-check" ? "Exit check running" : "Run in progress"}{jobProject ? ` · ${jobProject.name}` : ""}</span>
              <span className="sm:hidden">Live</span>
            </a>
          )}
          {projectKey && section !== "modes" && section !== "overview" && !live && (
            <Button size="sm" variant="primary" icon="play" href={projectHref(projectKey, "/test")} id="header-run" className="hidden sm:inline-flex">Run tests</Button>
          )}
        </header>
        <main className="px-4 md:px-8 pt-2 pb-16 max-w-[1360px] w-full mx-auto print:p-0 print:max-w-none">{children}</main>
      </div>
    </div>
  );
}

// The highlight behind the current link slides from one link to the next instead of jumping.
function SlidingNav({ section, children }: { section: string; children: ReactNode }) {
  const box = useRef<HTMLDivElement>(null);
  const [mark, setMark] = useState<{ top: number; height: number } | null>(null);
  const [ready, setReady] = useState(false);
  useLayoutEffect(() => {
    const measure = () => {
      const el = box.current?.querySelector<HTMLElement>(`[data-nav="${CSS.escape(section)}"]`);
      if (!el || !box.current) { setMark(null); return; }
      const a = box.current.getBoundingClientRect(), b = el.getBoundingClientRect();
      setMark({ top: b.top - a.top, height: b.height });
    };
    measure();
    const t = setTimeout(() => setReady(true), 50);
    const ro = new ResizeObserver(measure);
    if (box.current) ro.observe(box.current);
    return () => { clearTimeout(t); ro.disconnect(); };
  }, [section]);
  return (
    <div ref={box} className="relative">
      {mark && (
        <span aria-hidden="true" className={cx("absolute left-0 right-0 rounded-md bg-surface border border-line shadow-[0_1px_2px_rgb(0_0_0/.05)]",
          ready && "transition-[top,height] duration-300 ease-out-soft")} style={{ top: mark.top, height: mark.height }}>
                  </span>
      )}
      {children}
    </div>
  );
}
