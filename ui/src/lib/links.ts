// Links inside the dashboard. Everything about one agent lives under #/p/<project key>/..., so a
// page can link to "its" run history or new run without knowing which project it is showing.
import type { Job, Mode, Profile, Project, RunSummary } from "./types";

/** The project key of the page being shown, from the address (#/p/<key>/...). */
export function currentProjectKey(): string | null {
  const m = (location.hash || "").match(/^#\/p\/([\w.-]+)/);
  return m ? m[1] : null;
}

/** A link inside the current project: pl("/runs") -> #/p/<key>/runs. */
export function pl(path = "", key: string | null = currentProjectKey()): string {
  return key ? `#/p/${key}${path}` : `#${path || "/"}`;
}

export const projectHref = (key: string, path = "") => `#/p/${key}${path}`;

export function runHref(run: Pick<RunSummary, "run_id"> & { project_key?: string | null }, path = ""): string {
  const key = run.project_key ?? currentProjectKey();
  return key ? `#/p/${key}/runs/${run.run_id}${path}` : `#/runs/${run.run_id}${path}`;
}

export const projectOfJob = (job: Job | null, projects: Project[] | null) =>
  job && projects ? projects.find((p) => p.path === job.request?.project) ?? null : null;

export const projectByKey = (projects: Project[] | null, key: string) => projects?.find((p) => p.key === key) ?? null;

/** "Voice" / "Text" for a profile, with its name when a project has two of the same kind. */
export function channelLabel(profile: Profile, all: Profile[]): string {
  const base = profile.mode === "voice" ? "Voice" : "Text";
  return all.filter((p) => p.mode === profile.mode).length > 1 ? `${base} · ${profile.name || profile.file}` : base;
}

export const modeLabel = (m: Mode | null | undefined) => (m === "voice" ? "Voice" : m === "text" ? "Text" : "—");

const DOMAIN_NAMES: Record<string, string> = { general: "General", test: "Test agent" };
export const domainLabel = (d: string | null | undefined) => {
  const k = (d || "general").trim().toLowerCase();
  return DOMAIN_NAMES[k] || k.replace(/(^|[\s-])\w/g, (c) => c.toUpperCase());
};

/** Two letters for a project's avatar. */
export function initials(name: string): string {
  const words = name.replace(/\(.*?\)/g, "").split(/\s+/).filter(Boolean);
  return ((words[0]?.[0] || "") + (words[1]?.[0] || words[0]?.[1] || "")).toUpperCase();
}

/** A steady avatar color per project, from its key. */
export function avatarHue(key: string): number {
  let h = 0;
  for (const c of key) h = (h * 31 + c.charCodeAt(0)) % 360;
  return h;
}
