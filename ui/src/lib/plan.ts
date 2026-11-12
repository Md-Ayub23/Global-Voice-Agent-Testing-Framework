// The test plan writer's API (src/vatf/web/plans.py): the agent's instructions, the latest draft plan,
// writing a new one, adding ticked drafts as tests, and the persona library.
import { api, qs } from "./api";

export type PlanCategory = "happy" | "edge" | "error" | "adversarial" | "audio" | "timing" | "speech";
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
  traits: Record<string, string>; speech: { tts_voice?: string; speaking_rate?: number; accent?: string; volume_db?: number };
  behaviour: { interrupts?: boolean; backchannels?: boolean; pauses_mid_sentence?: boolean; goes_quiet?: boolean }; use?: string;
}
export interface PlanPage {
  agent_prompt: string; has_rules: boolean; plan: Plan | null; api_key: boolean; model: string;
  mix: Record<PlanCategory, number>; categories: Record<PlanCategory, string>; library: LibraryPersona[];
}

export const planApi = {
  page: (key: string, file = "profile.yaml", test_type?: string) =>
    api.get<PlanPage>(`/api/project/${key}/plan?${qs({ file, ...(test_type ? { test_type } : {}) })}`),
  write: (key: string, body: { count: number; focus: string; file: string; test_type?: string }) => api.post<PlanPage>(`/api/project/${key}/plan/write`, body),
  add: (key: string, ids: string[], criticality: Record<string, string>, file: string, test_type?: string) =>
    api.post<{ added: string[]; personas: string[]; skipped: { id: string; reason: string }[]; page: PlanPage }>(
      `/api/project/${key}/plan/add`, { ids, criticality, file, test_type }),
  addPersonas: (key: string, ids: string[]) => api.post<{ added: string[] }>(`/api/project/${key}/personas/library`, { ids }),
};

// Functional: what the test cases cover (src/vatf/coverage.py) and caller variations (src/vatf/variations.py).
export interface CaseRef { id: string; title: string }
export interface Coverage {
  has_plan: boolean; plan_date: string | null; cases: number;
  intents: { id: string; title: string; description: string; counts: { happy: number; edge: number; adversarial: number }; cases: CaseRef[] }[];
  rules: { id: string; text: string; kind: PlanRule["kind"]; cases: CaseRef[] }[];
  tools: { name: string; description: string; works: CaseRef[]; refuses: CaseRef[] }[];
  gaps: { kind: "intent" | "rule" | "tool"; id: string; text: string }[];
  focus: string;
}
export interface VariationDraft {
  n: number; id: string; file: string; label: string; why: string; goal: string; persona: string; persona_new: boolean;
  rules: string[]; problems: string[]; scenario: Record<string, unknown>; of: string;
}
export const functionalApi = {
  coverage: (key: string, file = "profile.yaml") => api.get<Coverage>(`/api/project/${key}/coverage?${qs({ file })}`),
  vary: (key: string, scenario_id: string, file = "profile.yaml", count = 3) =>
    api.post<{ drafts: VariationDraft[]; cost_usd: number }>(`/api/project/${key}/variations`, { scenario_id, file, count }),
  addVariations: (key: string, drafts: VariationDraft[], file = "profile.yaml") =>
    api.post<{ added: string[]; skipped: { id: string; reason: string }[] }>(`/api/project/${key}/variations/add`, { drafts, file }),
};

export const KIND_LABEL = (k: string) => k.replace(/_/g, " ");
