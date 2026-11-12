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

/** How Vocto reaches the agent through this profile, in a few words ("Phone · Twilio", "Voice WebSocket", "Text (HTTP)").
 * Same words as connection_label in src/vatf/web/data.py. `example`: one of Vocto's practice agents. */
export function connectionWords(profile: Profile, example = false): string {
  const c = profile.connection;
  if (c.type === "phone_call") {
    const prov = (profile.options?.provider || "twilio").toString();
    return `Phone · ${prov.charAt(0).toUpperCase()}${prov.slice(1)}`;
  }
  if (c.type === "twilio_media_stream") return "Twilio media stream";
  if (c.type === "text_http") return "Text (HTTP)";
  if (c.type === "text_python") return example ? "Practice agent (text)" : "Python function";
  if (c.launch && example) return "Practice agent (voice)";
  return "Voice WebSocket";
}

/** A profile's name in pickers: how Vocto reaches the agent, plus the file when two read the same. */
export function channelLabel(profile: Profile, all: Profile[], example = false): string {
  const words = connectionWords(profile, example);
  const twins = all.filter((p) => connectionWords(p, example) === words);
  return twins.length < 2 ? words : `${words} (${profile.file.replace(/\.ya?ml$/, "")})`;
}

const VOICE_ORDER = ["phone_call", "websocket_audio", "twilio_media_stream"];
/** The agent's main connection; same rule as main_profile in src/vatf/web/data.py. */
export function mainProfile(project: Pick<Project, "profiles">): Profile | null {
  const ps = project.profiles;
  if (!ps.length) return null;
  const first = ps.find((p) => p.file === "profile.yaml") || ps[0];
  if (first.mode === "voice") return first;
  const voice = ps.filter((p) => p.mode === "voice")
    .sort((a, b) => (VOICE_ORDER.indexOf(a.connection?.type) + 10) % 10 - (VOICE_ORDER.indexOf(b.connection?.type) + 10) % 10);
  return voice[0] || first;
}

/** How Vocto reaches the agent, in plain words. */
export function reachLabel(profile: Profile): string {
  const c = profile.connection;
  if (c.type === "phone_call") return "real phone number";
  if (c.launch) return "started by Vocto";
  if (c.type === "twilio_media_stream") return "phone audio stream";
  if (c.type === "websocket_audio") return "voice web link";
  return c.type === "text_http" ? "web address" : "in this computer";
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
