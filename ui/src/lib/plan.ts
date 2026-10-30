// The test plan writer's API (src/vatf/web/plans.py): the agent's instructions, the latest draft plan,
// writing a new one, adding ticked drafts as tests, and the persona library.
import { api, qs } from "./api";

export type PlanCategory = "happy" | "edge" | "error" | "adversarial" | "audio";
export interface PlanRule { id: string; text: string; kind: "must" | "must_not" | "escalate" | "fact" }
export interface PlanTest {
  n: number; id: string; file: string; category: PlanCategory; kind: string; title: string; why: string;
  intent: string | null; rules: PlanRule[]; persona: string; persona_new: boolean;
  criticality: "critical" | "normal" | "exploratory"; duplicate_of: string | null;
  notes: string[]; problems: string[]; selected: boolean; accepted?: boolean;
  scenario: Record<string, unknown>;
}
export interface Plan {
  created_at: string; model: string; cost_usd: number;
  asked: { count: number; focus: string; mix: Record<PlanCategory, number> };
  got: Partial<Record<PlanCategory, number>>;
  analysis: {
    summary: string;
    intents: { id: string; description: string; tools: string[] }[];
    slots: { name: string; description: string; example: string }[];
    rules: PlanRule[];
    workflows: { intent: string; steps: string[] }[];
    unclear: string[];
  };
  tests: PlanTest[];
}
export interface LibraryPersona {
  id: string; title: string; description: string; added: boolean;
  traits: Record<string, string>; speech: { tts_voice?: string; speaking_rate?: number }; behaviour: { interrupts?: boolean };
}
export interface PlanPage {
  agent_prompt: string; has_rules: boolean; plan: Plan | null; api_key: boolean; model: string;
  mix: Record<PlanCategory, number>; categories: Record<PlanCategory, string>; library: LibraryPersona[];
}

export const planApi = {
  page: (key: string, file = "profile.yaml") => api.get<PlanPage>(`/api/project/${key}/plan?${qs({ file })}`),
  write: (key: string, body: { count: number; focus: string; file: string }) => api.post<PlanPage>(`/api/project/${key}/plan/write`, body),
  add: (key: string, ids: string[], criticality: Record<string, string>, file: string) =>
    api.post<{ added: string[]; personas: string[]; skipped: { id: string; reason: string }[]; page: PlanPage }>(
      `/api/project/${key}/plan/add`, { ids, criticality, file }),
  addPersonas: (key: string, ids: string[]) => api.post<{ added: string[] }>(`/api/project/${key}/personas/library`, { ids }),
};

export const KIND_LABEL = (k: string) => k.replace(/_/g, " ");
