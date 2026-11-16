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
// null leaves them alone (a page shown inside another page, which sets its own).
export function useCrumbs(crumbs: Crumb[] | null) {
  const { setCrumbs } = useApp();
  const key = JSON.stringify(crumbs);
  useEffect(() => {
    if (key !== "null") setCrumbs(JSON.parse(key));
  }, [key, setCrumbs]);
}

// Loads data for a page: { data, error, reload }. `deps` re-run the load when they change.
// Coming back to a page shows what it had last time at once and refreshes it underneath (stale while
// revalidate), so moving around the app never waits on a blank skeleton twice. Only read-only views opt in
// ({ keep: true }): a form must start from the file as it is now, never a remembered copy. The memory is keyed by the
// loader's own code and its deps, holds the 80 most recent loads, and is cleared on sign-out.
const memo = new Map<string, unknown>();
const MEMO_MAX = 80;
export const forgetLoads = () => memo.clear();
if (typeof window !== "undefined") window.addEventListener("vocto:signed-out", forgetLoads);

export function useLoad<T>(load: () => Promise<T>, deps: unknown[], opts?: { keep?: boolean }): { data: T | null; error: Error | null; reload: () => void } {
  let key: string | null = null;
  if (opts?.keep) { try { key = load.toString() + "|" + JSON.stringify(deps); } catch { key = null; } }
  const [state, setState] = useState<{ data: T | null; error: Error | null }>(() => ({ data: key && memo.has(key) ? (memo.get(key) as T) : null, error: null }));
  const [tick, setTick] = useState(0);
  const lastKey = useRef(key);
  useEffect(() => {
    let live = true;
    const cached = key && memo.has(key) ? (memo.get(key) as T) : undefined;
    // A reload keeps what is on screen; new deps (another agent, another run) never show the old one's data.
    const same = lastKey.current === key;
    lastKey.current = key;
    setState((s) => ({ data: cached !== undefined ? cached : same ? s.data : null, error: null }));
    load().then(
      (data) => {
        if (key) { memo.delete(key); memo.set(key, data); if (memo.size > MEMO_MAX) memo.delete(memo.keys().next().value!); }
        if (live) setState({ data, error: null });
      },
      (error: Error) => live && setState({ data: null, error }),
    );
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);
  return { ...state, reload: () => setTick((t) => t + 1) };
}
