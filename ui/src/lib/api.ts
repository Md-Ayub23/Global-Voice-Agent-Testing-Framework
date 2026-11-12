// The dashboard server's JSON API. Writes carry the X-VATF header the server requires.

// Vocto's checks write counts as "2 time(s)" (the run files and other tools parse that form), so the
// words are tidied here, on the way to the screen: "1 time", "2 times", "0 rows".
const WORDY = new Set(["message", "why", "note"]);
const COUNTED = /\b(\d[\d,]*) ((?:[A-Za-z]+ )?)([a-z]+)\(s\)/g;
export const tidyCounts = (s: string) =>
  s.includes("(s)") ? s.replace(COUNTED, (_m, n: string, pre: string, word: string) => `${n} ${pre}${word}${n.replace(/,/g, "") === "1" ? "" : "s"}`) : s;
export const reviver = (key: string, value: unknown) => (typeof value === "string" && WORDY.has(key) ? tidyCounts(value) : value);

async function request<T>(method: string, url: string, body?: unknown): Promise<T> {
  const init: RequestInit = { method, headers: {} };
  if (method !== "GET") {
    init.headers = { "X-VATF": "1", "Content-Type": "application/json" };
    init.body = JSON.stringify(body ?? {});
  }
  let res: Response;
  try {
    res = await fetch(url, init);
  } catch {
    throw new Error("The dashboard server isn't answering. Is the `vatf ui` window still open?");
  }
  const text = await res.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text, reviver) : null;
  } catch {
    data = { error: text };
  }
  if (!res.ok) throw new Error((data as { error?: string } | null)?.error || `${res.status} ${res.statusText}`);
  return data as T;
}

export const api = {
  get: <T>(url: string) => request<T>("GET", url),
  post: <T>(url: string, body?: unknown) => request<T>("POST", url, body),
  put: <T>(url: string, body?: unknown) => request<T>("PUT", url, body),
};

export const qs = (params: Record<string, string>) => new URLSearchParams(params).toString();
