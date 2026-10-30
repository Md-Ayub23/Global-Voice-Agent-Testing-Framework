// The built-in safety tests' API (src/vatf/web/safety.py) and the words the pages use for them.
import { api, qs } from "./api";
import type { Intensity, SafetyAttack, SafetyPage } from "./types";

export const safetyApi = {
  page: (key: string, file = "profile.yaml") => api.get<SafetyPage>(`/api/project/${key}/safety?${qs({ file })}`),
};

export const INTENSITIES: { value: Intensity; label: string; hint: string }[] = [
  { value: "quick", label: "Quick", hint: "The core attacks, once each" },
  { value: "standard", label: "Standard", hint: "More attacks, twice each" },
  { value: "thorough", label: "Thorough", hint: "Every attack three times, plus live AI attackers" },
];
const ORDER: Intensity[] = ["quick", "standard", "thorough"];

export interface SafetyDraft { enabled: boolean; intensity: Intensity; packs: Set<string>; skip: Set<string> }

/** Whether an attack runs with these settings (the same rule as attacks.catalog on the server). */
export function attackOn(a: SafetyAttack, s: SafetyDraft, liveOnly: boolean): boolean {
  return s.enabled && s.packs.has(a.pack) && ORDER.indexOf(a.level) <= ORDER.indexOf(s.intensity) && !s.skip.has(a.id)
    && !(a.adaptive && liveOnly);
}

export const REPEATS: Record<Intensity, number> = { quick: 1, standard: 2, thorough: 3 };

export function packTitle(id: string, packs?: { id: string; title: string }[]): string {
  return packs?.find((p) => p.id === id)?.title || id.replace(/-/g, " ");
}

// The packs' names (attacks.PACKS), for pages that only have a run's decision.
export const PACK_TITLES: Record<string, string> = {
  "off-topic": "Off-topic requests", secrets: "Secrets and access", injection: "Prompt tricks", "made-up": "Made-up facts",
  "others-data": "Other people's data", harmful: "Harmful and risky requests", abuse: "Abusive callers",
  policy: "Pressure to break the rules", actions: "Unsafe actions", messy: "Confusing callers",
  own: "This agent's own checks",
};
