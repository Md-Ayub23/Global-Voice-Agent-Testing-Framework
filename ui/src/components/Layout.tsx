// The app shell. The sidebar has the workspace (dashboard, projects, settings) and, inside a
// project, that project's own pages: overview, new run, live run, run history and its setup.
import { useEffect, useRef, useState, type ReactNode } from "react";
import { useApp } from "../lib/app";
import { cx } from "../lib/format";
import { domainLabel, projectByKey, projectHref, projectOfJob } from "../lib/links";
import { ProjectAvatar } from "./dash";
import { Icon, type IconName } from "./Icon";
import { Button, LiveDot } from "./ui";

interface NavItem { href: string; key: string; label: string; icon: IconName }

const WORKSPACE: NavItem[] = [
  { href: "#/", key: "dashboard", label: "Dashboard", icon: "home" },
  { href: "#/projects", key: "projects", label: "Projects", icon: "grid" },
  { href: "#/settings", key: "settings", label: "Settings", icon: "settings" },
];
const projectNav = (k: string): NavItem[] => [
  { href: projectHref(k), key: "overview", label: "Overview", icon: "overview" },
  { href: projectHref(k, "/new"), key: "new", label: "New run", icon: "play-circle" },
  { href: projectHref(k, "/live"), key: "live", label: "Live run", icon: "activity" },
  { href: projectHref(k, "/runs"), key: "runs", label: "Run history", icon: "history" },
];
const setupNav = (k: string): NavItem[] => [
  { href: projectHref(k, "/setup/agent"), key: "setup:agent", label: "Agent & connection", icon: "plug" },
  { href: projectHref(k, "/setup/tools"), key: "setup:tools", label: "Tools", icon: "tool" },
  { href: projectHref(k, "/setup/backend"), key: "setup:backend", label: "Backend & database", icon: "db" },
  { href: projectHref(k, "/setup/logs"), key: "setup:logs", label: "Logs", icon: "logs" },
  { href: projectHref(k, "/setup/plan"), key: "setup:plan", label: "Test plan", icon: "plan" },
  { href: projectHref(k, "/setup/settings"), key: "setup:settings", label: "Test settings", icon: "sliders" },
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
        className="w-full flex items-center gap-2.5 p-2 rounded-xl border border-line bg-surface-2/60 hover:bg-surface-2 transition-colors text-left cursor-pointer">
        <ProjectAvatar name={current?.name || projectKey} k={projectKey} size={32} />
        <span className="flex-1 min-w-0">
          <b className="block text-[13px] leading-tight truncate">{current?.name || projectKey}</b>
          <span className="block text-[11.5px] text-muted truncate">{current ? `${domainLabel(current.domain)} · ${current.channels.map((c) => (c === "voice" ? "Voice" : "Text")).join(" + ")}` : ""}</span>
        </span>
        <Icon name="updown" size={15} className="text-muted flex-none" />
      </button>
      {open && (
        <div className="absolute left-0 right-0 top-[calc(100%+6px)] z-30 bg-surface border border-line rounded-xl shadow-pop p-1.5 animate-rise max-h-[60vh] overflow-auto">
          <div className="px-2 pt-1 pb-1.5 text-[11px] uppercase tracking-[.08em] text-muted">Switch project</div>
          {(projects || []).map((p) => (
            <a key={p.key} href={projectHref(p.key)} className={cx("flex items-center gap-2.5 px-2 py-1.5 rounded-lg hover:bg-surface-2 hover:no-underline text-ink",
              p.key === projectKey && "bg-accent-soft")}>
              <ProjectAvatar name={p.name} k={p.key} size={26} />
              <span className="flex-1 min-w-0 truncate text-[13px] font-medium">{p.name}</span>
              {p.key === projectKey && <Icon name="check" size={14} className="text-accent-ink" />}
            </a>
          ))}
          <div className="h-px bg-line my-1.5" />
          <a href="#/projects" className="flex items-center gap-2 px-2 py-1.5 rounded-lg hover:bg-surface-2 hover:no-underline text-ink-2 text-[13px]"><Icon name="grid" size={15} />All projects</a>
          <a href="#/projects/new" className="flex items-center gap-2 px-2 py-1.5 rounded-lg hover:bg-surface-2 hover:no-underline text-ink-2 text-[13px]"><Icon name="plus" size={15} />New project</a>
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

  const link = (n: NavItem) => (
    <a key={n.key} href={n.href} data-nav={n.key}
      className={cx("relative flex items-center gap-[11px] px-2.5 py-[7px] rounded-[9px] font-medium text-[13.5px] transition-colors duration-150 hover:no-underline",
        section === n.key ? "bg-accent-soft text-accent-ink before:absolute before:-left-3.5 before:top-1.5 before:bottom-1.5 before:w-[3px] before:rounded-r before:bg-accent"
          : "text-ink-2 hover:bg-surface-2 hover:text-ink")}>
      <Icon name={n.icon} size={17} />
      {n.label}
      {n.key === "live" && activeJob && jobProject?.key === projectKey && <LiveDot className="ml-auto" />}
    </a>
  );
  const heading = (text: string) => <div className="text-[11px] uppercase tracking-[.08em] text-muted px-2.5 pt-4 pb-1.5 font-semibold">{text}</div>;

  return (
    <div className="min-h-screen md:grid md:grid-cols-[252px_minmax(0,1fr)]">
      {navOpen && <div className="fixed inset-0 z-10 bg-black/30 md:hidden" onClick={() => setNavOpen(false)} />}
      <aside
        className={cx(
          "fixed md:sticky top-0 left-0 bottom-0 z-20 md:z-5 h-screen w-[268px] md:w-auto flex flex-col px-3.5 py-[18px] bg-surface border-r border-line overflow-y-auto scrollbar-thin",
          "transition-transform duration-200 ease-out-soft md:translate-x-0 shadow-pop md:shadow-none",
          navOpen ? "translate-x-0" : "-translate-x-full",
        )}
      >
        <a href="#/" className="flex items-center gap-[11px] px-2 pt-1 pb-4 text-ink hover:no-underline">
          <img src="/static/logo.svg" alt="" className="size-[34px] rounded-[10px] shadow-[0_6px_16px_-6px_rgba(91,91,240,.7)]" />
          <div>
            <b className="block text-[17px] leading-[1.1] tracking-[-.02em]">Vocto</b>
            <span className="text-[11.5px] text-muted">Voice agent testing</span>
          </div>
        </a>
        <nav className="flex flex-col gap-0.5" aria-label="Workspace">{WORKSPACE.map(link)}</nav>
        {projectKey && (
          <>
            {heading("Project")}
            <ProjectSwitcher projectKey={projectKey} />
            <nav className="flex flex-col gap-0.5 mt-2" aria-label="Project">{projectNav(projectKey).map(link)}</nav>
            {heading("Set up")}
            <nav className="flex flex-col gap-0.5" aria-label="Project setup">{setupNav(projectKey).map(link)}</nav>
          </>
        )}
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
        <header className={cx("sticky top-0 z-4 flex items-center gap-3.5 px-4 md:px-8 py-3 backdrop-blur-md bg-[color-mix(in_srgb,var(--bg)_82%,transparent)] border-b transition-colors",
          scrolled ? "border-line" : "border-transparent")}>
          <Button variant="ghost" iconOnly icon="menu" className="md:hidden" aria-label="Menu" onClick={() => setNavOpen((o) => !o)} />
          <div className="flex items-center gap-2 text-muted text-[13px] min-w-0">
            {crumbs.map((c, i) =>
              i === crumbs.length - 1 ? (
                <b key={i} className="text-ink font-semibold truncate">{c.label}</b>
              ) : (
                <span key={i} className="flex items-center gap-2 flex-none">
                  <a href={c.href} className="text-muted hover:text-ink">{c.label}</a><Icon name="chevron" size={13} />
                </span>
              ),
            )}
          </div>
          <span className="flex-1" />
          {activeJob && (
            <a href={jobProject ? projectHref(jobProject.key, `/live/${activeJob.id}`) : "#/"} id="active-pill"
              className="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-accent-soft text-accent-ink text-xs font-bold whitespace-nowrap hover:no-underline animate-rise">
              <LiveDot /><span className="hidden sm:inline">{activeJob.kind === "exit-check" ? "Exit check running" : "Run in progress"}{jobProject ? ` · ${jobProject.name}` : ""}</span>
              <span className="sm:hidden">Live</span>
            </a>
          )}
        </header>
        <main className="px-4 md:px-8 pt-2 pb-16 max-w-[1440px] w-full">{children}</main>
      </div>
    </div>
  );
}
