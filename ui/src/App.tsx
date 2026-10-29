// Routes. The workspace pages (dashboard, projects, settings) sit at the top; everything about one
// agent lives under #/p/<project key>/... The page remounts (and animates in) when the address changes.
import { Component, useEffect, type ReactNode } from "react";
import { Layout } from "./components/Layout";
import { Icon } from "./components/Icon";
import { ToastProvider, TooltipProvider } from "./components/feedback";
import { EmptyState, Notice, Skeleton } from "./components/ui";
import { api } from "./lib/api";
import { AppProvider, useApp } from "./lib/app";
import { projectByKey } from "./lib/links";
import { useHashLocation } from "./lib/router";
import type { Project, RunSummary } from "./lib/types";
import { CallPage } from "./pages/Call";
import { Dashboard } from "./pages/Dashboard";
import { Files } from "./pages/Files";
import { History } from "./pages/History";
import { Live } from "./pages/Live";
import { NewRun } from "./pages/NewRun";
import { ProjectOverviewPage } from "./pages/ProjectOverview";
import { ProjectSetup, SETUP_TABS, type SetupTab } from "./pages/ProjectSetup";
import { NewProject, Projects } from "./pages/Projects";
import { RunReport } from "./pages/RunReport";
import { Settings } from "./pages/Settings";

type Page = (m: RegExpMatchArray, q: URLSearchParams) => ReactNode;
type ProjectPage = (p: Project, m: RegExpMatchArray, q: URLSearchParams) => ReactNode;
const K = "([\\w.\\-]+)";

const ROUTES: [RegExp, string | ((m: RegExpMatchArray) => string), Page][] = [
  [/^\/?$/, "dashboard", () => <Dashboard />],
  [/^\/projects$/, "projects", () => <Projects />],
  [/^\/projects\/new$/, "projects", () => <NewProject />],
  [/^\/settings$/, "settings", () => <Settings />],
  [/^\/runs\/(r-[\w-]+)(\/calls\/t-[\w-]+)?$/, "", (m) => <RunRedirect runId={m[1]} rest={m[2] || ""} />],
];
const PROJECT_ROUTES: [RegExp, string | ((m: RegExpMatchArray) => string), ProjectPage][] = [
  [new RegExp(`^/p/${K}/?$`), "overview", (p) => <ProjectOverviewPage project={p} />],
  [new RegExp(`^/p/${K}/new$`), "new", (p, _m, q) => <NewRun project={p} query={q} />],
  [new RegExp(`^/p/${K}/live(?:/(j-[\\w-]+))?$`), "live", (p, m) => <Live project={p} jobId={m[2] || null} />],
  [new RegExp(`^/p/${K}/runs$`), "runs", (p) => <History project={p} />],
  [new RegExp(`^/p/${K}/runs/(r-[\\w-]+)$`), "runs", (p, m) => <RunReport project={p} runId={m[2]} />],
  [new RegExp(`^/p/${K}/runs/(r-[\\w-]+)/calls/(t-[\\w-]+)$`), "runs", (p, m) => <CallPage project={p} runId={m[2]} testId={m[3]} />],
  [new RegExp(`^/p/${K}/setup/(\\w+)$`), (m) => `setup:${m[2]}`, (p, m, q) =>
    SETUP_TABS.some((t) => t.value === m[2]) ? <ProjectSetup project={p} tab={m[2] as SetupTab} query={q} /> : null],
  [new RegExp(`^/p/${K}/files$`), "setup:plan", (p, _m, q) => <Files project={p} query={q} />],
];

// Old links (#/runs/<id>) go to the run inside its project.
function RunRedirect({ runId, rest }: { runId: string; rest: string }) {
  useEffect(() => {
    api.get<RunSummary[]>("/api/runs").then((runs) => {
      const r = runs.find((x) => x.run_id === runId);
      location.replace(r?.project_key ? `#/p/${r.project_key}/runs/${runId}${rest}` : "#/");
    }, () => location.replace("#/"));
  }, [runId, rest]);
  return <Skeleton />;
}

function ProjectGate({ projectKey, render }: { projectKey: string; render: (p: Project) => ReactNode }) {
  const { projects } = useApp();
  if (!projects) return <Skeleton />;
  const p = projectByKey(projects, projectKey);
  if (!p) {
    return <EmptyState title="Project not found" icon="help" text="It may have been renamed or removed."
      action={<a href="#/projects"><Icon name="back" size={14} /> All projects</a>} />;
  }
  return render(p);
}

// Shows a page's crash instead of a blank screen.
class Boundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };
  static getDerivedStateFromError(error: Error) { return { error }; }
  render() {
    if (this.state.error) {
      return <Notice tone="bad" className="mt-5"><b>Couldn't show this page.</b><br />{this.state.error.message}</Notice>;
    }
    return this.props.children;
  }
}

function Pages() {
  const loc = useHashLocation();
  useEffect(() => { window.scrollTo({ top: 0 }); }, [loc.hash]);
  let page: ReactNode = null;
  let section = "";
  let projectKey: string | null = null;
  for (const [re, key, render] of ROUTES) {
    const m = loc.path.match(re);
    if (m) { page = render(m, loc.query); section = typeof key === "string" ? key : key(m); break; }
  }
  if (!page) {
    for (const [re, key, render] of PROJECT_ROUTES) {
      const m = loc.path.match(re);
      if (m) {
        projectKey = m[1];
        section = typeof key === "string" ? key : key(m);
        page = <ProjectGate projectKey={m[1]} render={(p) => render(p, m, loc.query)} />;
        break;
      }
    }
  }
  return (
    <Layout section={section} projectKey={projectKey}>
      <div key={loc.hash} className="animate-view-in">
        <Boundary>
          {page ?? <EmptyState title="Page not found" icon="help" action={<a href="#/"><Icon name="back" size={14} /> Back to the dashboard</a>} />}
        </Boundary>
      </div>
    </Layout>
  );
}

export function App() {
  return (
    <AppProvider>
      <ToastProvider>
        <TooltipProvider>
          <Pages />
        </TooltipProvider>
      </ToastProvider>
    </AppProvider>
  );
}
