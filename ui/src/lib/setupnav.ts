// The set-up pages' names and where each sits in the sidebar (kept apart from the pages so the router
// can know them without loading the pages).
import type { IconName } from "../components/Icon";

export type SetupTab = "connection" | "tools" | "backend" | "logs" | "cases" | "callers" | "safety" | "rules" | "ops" | "judges";
interface SetupMeta { value: SetupTab; label: string; icon: IconName; sub: string }
// The set-up pages. Connect holds how Vocto reaches the agent and what else it can check; Agent settings
// holds the rules and helpers every test type shares. Test cases and safety tests belong to their test type.
export const SETUP_TABS: SetupMeta[] = [
  { value: "connection", label: "Connection", icon: "plug", sub: "How callers reach the agent, and the agent brief Vocto writes test cases from." },
  { value: "tools", label: "Tools", icon: "tool", sub: "The APIs the agent calls. Vocto watches every call and can break one on purpose." },
  { value: "backend", label: "Database", icon: "db", sub: "Read-only access, so Vocto checks what really got saved, not what the agent said." },
  { value: "logs", label: "Logs", icon: "logs", sub: "The agent's own log lines, kept next to each call and checked by test cases." },
  { value: "cases", label: "Test cases", icon: "plan", sub: "The situations the agent must handle." },
  { value: "safety", label: "Safety tests", icon: "shield", sub: "Attack calls that try to make the agent leak, lie or go off its job." },
  { value: "rules", label: "Pass rules", icon: "sliders", sub: "What a pass needs for this agent: response times, repeats, coverage, budget, and what may not get worse than the baseline." },
  { value: "callers", label: "Callers", icon: "user", sub: "The people who call in your tests, from calm to impatient. Every test case picks one." },
  { value: "judges", label: "AI judges", icon: "sparkles", sub: "What the AI judges score in every conversation, and what the agent may say." },
  { value: "ops", label: "CI & alerts", icon: "bell", sub: "Run this agent's tests in CI, on a schedule as health checks, and get a message when a release is blocked." },
];
export const CONNECT_TABS: SetupTab[] = ["connection", "tools", "backend", "logs"];
export const SETTINGS_TABS: SetupTab[] = ["rules", "callers", "judges", "ops"];
// Old addresses and where they live now.
export const SETUP_ALIASES: Record<string, SetupTab> = { agent: "connection", plan: "cases", settings: "rules" };
export const setupSection = (tab: string) => (CONNECT_TABS.includes(tab as SetupTab) ? "connect"
  : SETTINGS_TABS.includes(tab as SetupTab) ? "agent-settings" : tab === "safety" ? "mode:safety" : "mode:functional");

