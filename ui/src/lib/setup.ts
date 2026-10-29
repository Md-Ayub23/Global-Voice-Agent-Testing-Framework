// The project set-up API (src/vatf/web/setup.py): one profile file's sections, personas and scenarios.
import type { Obj, TestOutcome } from "../components/form";
import { api, qs } from "./api";
import type { ProjectFile } from "./types";

export interface ProfileDetail {
  project: string;
  file: string;
  folder: string;
  raw: Obj;          // this file as written (sections it doesn't have come from `extends`)
  merged: Obj;       // the profile as a run sees it
  base: Obj;         // what `extends` gives (empty when the file stands alone)
  extends: string | null;
  problems: string[];
  defaults: { thresholds: Record<string, number | null>; limits: Record<string, number | null> };
  gateway: { used: boolean; host: string; port: number; tools: Record<string, string> };
  personas: string[];
  scenarios: ProjectFile[];
  persona_files: ProjectFile[];
  profiles: string[];
}

export interface SaveResult { saved: boolean; problems: string[]; detail?: ProfileDetail; file?: string }

export const setupApi = {
  profile: (project: string, file: string) => api.get<ProfileDetail>(`/api/setup/profile?${qs({ project, file })}`),
  saveProfile: (project: string, file: string, sections: Obj) => api.put<SaveResult>("/api/setup/profile", { project, file, sections }),
  doc: (project: string, file: string) => api.get<{ file: string; doc: Obj }>(`/api/setup/doc?${qs({ project, file })}`),
  saveDoc: (project: string, file: string, doc: Obj, create: boolean) => api.put<SaveResult>("/api/setup/doc", { project, file, doc, create }),
  deleteDoc: (project: string, file: string) => api.post<{ deleted: string }>("/api/setup/delete", { project, file }),
  testConnection: (project: string, file: string) => api.post<TestOutcome>("/api/setup/test-connection", { project, file }),
  testBackend: (project: string, file: string, probe?: string, binds?: Obj) => api.post<TestOutcome>("/api/setup/test-backend", { project, file, probe, binds }),
  testLogs: (project: string, file: string) => api.post<TestOutcome>("/api/setup/test-logs", { project, file }),
  newProject: (body: Obj) => api.post<{ path: string; id: string; key: string }>("/api/setup/new-project", body),
};

// Which file a shared section (tools, backend, logs, the agent's name) is saved in: the selected
// file if it has the section itself, otherwise the file it extends.
export function owner(detail: ProfileDetail, section: string): string {
  if (detail.raw[section] !== undefined || !detail.extends) return detail.file;
  return detail.extends;
}

export const slug = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40);

// :name bind values in a probe query, in order.
export const binds = (query: string) => [...new Set([...query.matchAll(/(?<![\w:]):([A-Za-z_]\w*)/g)].map((m) => m[1]))];
