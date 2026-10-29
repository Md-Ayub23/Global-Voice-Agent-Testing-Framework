// What every page can reach: the system status (polled), the running job, the breadcrumbs.
import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { api } from "./api";
import type { Job, Project, System } from "./types";

export interface Crumb { label: string; href?: string }

interface AppState {
  system: System | null;
  offline: boolean;            // the dashboard server stopped answering
  activeJob: Job | null;
  poll: () => Promise<void>;
  crumbs: Crumb[];
  setCrumbs: (c: Crumb[]) => void;
  setActiveJob: (j: Job | null) => void;
  projects: Project[] | null;  // every project, for the sidebar and project pages
  reloadProjects: () => Promise<void>;
}
const Ctx = createContext<AppState | null>(null);

export function AppProvider({ children }: { children: ReactNode }) {
  const [system, setSystem] = useState<System | null>(null);
  const [offline, setOffline] = useState(false);
  const [activeJob, setActiveJob] = useState<Job | null>(null);
  const [crumbs, setCrumbsState] = useState<Crumb[]>([]);
  const [projects, setProjects] = useState<Project[] | null>(null);
  const alive = useRef(true);

  const reloadProjects = useCallback(async () => {
    try { setProjects(await api.get<Project[]>("/api/projects")); } catch { setProjects((p) => p ?? []); }
  }, []);
  useEffect(() => { void reloadProjects(); }, [reloadProjects]);

  const poll = useCallback(async () => {
    try {
      const sys = await api.get<System>("/api/system");
      if (!alive.current) return;
      setSystem(sys);
      setActiveJob(sys.active_job);
      setOffline(false);
    } catch {
      if (alive.current) setOffline(true);
    }
  }, []);

  useEffect(() => {
    alive.current = true;
    poll();
    const t = setInterval(poll, 4000);
    return () => { alive.current = false; clearInterval(t); };
  }, [poll]);

  const setCrumbs = useCallback((c: Crumb[]) => {
    setCrumbsState(c);
    document.title = `${c[c.length - 1]?.label || "Dashboard"} · Vocto`;
  }, []);

  return (
    <Ctx.Provider value={{ system, offline, activeJob, poll, crumbs, setCrumbs, setActiveJob, projects, reloadProjects }}>
      {children}
    </Ctx.Provider>
  );
}

export function useApp(): AppState {
  const v = useContext(Ctx);
  if (!v) throw new Error("useApp outside AppProvider");
  return v;
}

// Sets the page's breadcrumbs (and the browser tab title) whenever they change.
export function useCrumbs(crumbs: Crumb[]) {
  const { setCrumbs } = useApp();
  const key = JSON.stringify(crumbs);
  useEffect(() => {
    setCrumbs(JSON.parse(key));
  }, [key, setCrumbs]);
}

// Loads data for a page: { data, error, reload }. `deps` re-run the load when they change.
export function useLoad<T>(load: () => Promise<T>, deps: unknown[]): { data: T | null; error: Error | null; reload: () => void } {
  const [state, setState] = useState<{ data: T | null; error: Error | null }>({ data: null, error: null });
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let live = true;
    setState((s) => ({ data: s.data, error: null }));
    load().then(
      (data) => live && setState({ data, error: null }),
      (error: Error) => live && setState({ data: null, error }),
    );
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);
  return { ...state, reload: () => setTick((t) => t + 1) };
}
