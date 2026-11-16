// Who is signed in, and the sign-in / account / users API. The server answers 401 to every request
// once a sign-in ends; api.ts then fires "vocto:signed-out" and the sign-in page comes back.
import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { api } from "./api";

export type Role = "viewer" | "editor" | "admin";
export interface User { id: number; username: string; name: string; role: Role; disabled: boolean; created_at: number; last_login: number | null; via?: "session" | "key" }
export interface ApiKey { id: number; name: string; prefix: string; role: Role; created_at: number; last_used: number | null; username: string; key?: string }
export interface AuditRow { at: string; event: string; user: string | null; ip: string; [k: string]: unknown }
interface Status { enabled: boolean; user: User | null; setup: boolean }

const RANK: Record<Role, number> = { viewer: 0, editor: 1, admin: 2 };
export const can = (user: User | null, role: Role) => !user || RANK[user.role] >= RANK[role];   // no sign-in: all allowed

export const authApi = {
  status: () => api.get<Status>("/api/auth/status"),
  login: (username: string, password: string) => api.post<{ user: User }>("/api/auth/login", { username, password }),
  setup: (username: string, password: string, name: string) => api.post<{ user: User }>("/api/auth/setup", { username, password, name }),
  logout: () => api.post("/api/auth/logout", {}),
  password: (current: string, password: string) => api.post("/api/auth/password", { current, password }),
  keys: (all = false) => api.get<ApiKey[]>(`/api/auth/keys${all ? "?all=1" : ""}`),
  newKey: (name: string, role: Role) => api.post<ApiKey>("/api/auth/keys", { name, role }),
  revokeKey: (id: number) => api.post(`/api/auth/keys/${id}/delete`, {}),
  users: () => api.get<User[]>("/api/auth/users"),
  newUser: (u: { username: string; password: string; role: Role; name: string }) => api.post<User>("/api/auth/users", u),
  editUser: (id: number, change: Partial<{ role: Role; disabled: boolean; name: string; password: string }>) => api.post<User>(`/api/auth/users/${id}`, change),
  deleteUser: (id: number) => api.post(`/api/auth/users/${id}/delete`, {}),
  audit: (limit = 500) => api.get<AuditRow[]>(`/api/audit?limit=${limit}`),
};

interface AuthState { user: User | null; enabled: boolean; signOut: () => Promise<void> }
const Ctx = createContext<AuthState>({ user: null, enabled: false, signOut: async () => {} });
export const useAuth = () => useContext(Ctx);

// Shows the sign-in page (or, on the first visit, the "create the admin" page) until someone is signed in.
export function AuthGate({ children, signIn }: { children: ReactNode; signIn: (p: { setup: boolean; done: (u: User) => void }) => ReactNode }) {
  const [status, setStatus] = useState<Status | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const load = useCallback(() => {
    authApi.status().then((s) => { setStatus(s); setFailed(null); }, (e: Error) => setFailed(e.message));
  }, []);
  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    const out = () => setStatus((s) => (s && s.enabled ? { ...s, user: null } : s));
    window.addEventListener("vocto:signed-out", out);
    return () => window.removeEventListener("vocto:signed-out", out);
  }, []);
  const signOut = useCallback(async () => {
    try { await authApi.logout(); } catch { /* signed out anyway */ }
    setStatus((s) => (s ? { ...s, user: null, setup: false } : s));
  }, []);
  if (failed) {
    return <div className="min-h-screen grid place-items-center p-6 text-center text-ink-2">
      <div><b className="text-ink">The Vocto server isn't answering.</b><br />{failed}<br />
        <button type="button" className="mt-3 underline" onClick={load}>Try again</button></div>
    </div>;
  }
  if (!status) return null;
  if (status.enabled && !status.user) return <>{signIn({ setup: status.setup, done: (u) => setStatus({ ...status, user: u, setup: false }) })}</>;
  return <Ctx.Provider value={{ user: status.user, enabled: status.enabled, signOut }}>{children}</Ctx.Provider>;
}
