// Plain-English formatting for times, durations, money and ids.
import { twMerge } from "tailwind-merge";

export function fmtMs(ms: number | null | undefined): string {
  if (ms == null) return "—";
  return ms >= 10000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms).toLocaleString()} ms`;
}

export function fmtDuration(ms: number | null | undefined): string {
  if (ms == null || Number.isNaN(ms)) return "—";
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, "0")}s`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
}

export const fmtSecs = (ms: number | null | undefined) => (ms == null ? "" : `${(ms / 1000).toFixed(2)}s`);

export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

export function ago(iso: string | null | undefined): string {
  if (!iso) return "";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  const days = Math.round(s / 86400);
  return days === 1 ? "yesterday" : `${days} days ago`;
}

export const pct = (v: number | null | undefined) => (v == null ? "—" : `${Math.round(v * 100)}%`);
export const money = (v: number | null | undefined) => (v == null ? "—" : !v ? "$0" : `$${Number(v).toFixed(v < 0.1 ? 4 : 2)}`);
export const shortScenario = (id: string | null | undefined) => String(id || "").replace(/^[^.]+\./, "");

// Joins class names; when two set the same thing (p-[18px] then p-3), the later one wins, so a
// component's defaults can be overridden with className.
export function cx(...parts: (string | false | null | undefined)[]): string {
  return twMerge(parts.filter(Boolean).join(" "));
}
