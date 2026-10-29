// The dashboard server's JSON API. Writes carry the X-VATF header the server requires.

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
    data = text ? JSON.parse(text) : null;
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
