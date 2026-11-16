// Your account: change your password, and API keys for scripts and CI. Admins also manage the team's
// users and read the audit log here.
import { useState } from "react";
import { useToast } from "../components/feedback";
import { Select } from "../components/form";
import { Icon } from "../components/Icon";
import { Badge, Button, Card, CardHead, Chip, EmptyState, Field, Notice, PageHead, Skeleton, inputCls } from "../components/ui";
import { useCrumbs, useLoad } from "../lib/app";
import { authApi, can, useAuth, type ApiKey, type Role, type User } from "../lib/auth";
import { ago, cx } from "../lib/format";

const ROLE_OPTIONS: [Role, string][] = [["viewer", "Viewer: reads results"], ["editor", "Editor: runs tests, edits agents"], ["admin", "Admin: everything, users and secrets"]];
const when = (t: number | null) => (t ? ago(new Date(t * 1000).toISOString()) : "never");

function Password() {
  const toast = useToast();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [again, setAgain] = useState("");
  const [busy, setBusy] = useState(false);
  const save = async () => {
    if (next !== again) { toast("The two new passwords are not the same", "bad"); return; }
    setBusy(true);
    try {
      await authApi.password(current, next);
      setCurrent(""); setNext(""); setAgain("");
      toast("Password changed. You are signed out everywhere else.");
    } catch (e) { toast((e as Error).message, "bad"); } finally { setBusy(false); }
  };
  return (
    <Card>
      <CardHead level={2} title="Password" sub="changing it signs you out on every other browser" />
      <form className="grid gap-3" onSubmit={(e) => { e.preventDefault(); void save(); }}>
        <Field label="Current password"><input className={inputCls} type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} /></Field>
        <Field label="New password" hint="At least 10 characters."><input className={inputCls} type="password" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} /></Field>
        <Field label="New password again"><input className={inputCls} type="password" autoComplete="new-password" value={again} onChange={(e) => setAgain(e.target.value)} /></Field>
        <div><Button type="submit" variant="primary" icon="save" busy={busy} disabled={busy || !current || !next}>Change password</Button></div>
      </form>
    </Card>
  );
}

function Keys({ user }: { user: User }) {
  const toast = useToast();
  const { data: keys, reload } = useLoad(() => authApi.keys(), []);
  const [name, setName] = useState("");
  const [role, setRole] = useState<Role>(user.role === "admin" ? "editor" : user.role);
  const [made, setMade] = useState<ApiKey | null>(null);
  const create = async () => {
    try { setMade(await authApi.newKey(name.trim(), role)); setName(""); reload(); } catch (e) { toast((e as Error).message, "bad"); }
  };
  const revoke = async (k: ApiKey) => {
    try { await authApi.revokeKey(k.id); toast(`Key ${k.name} removed`); reload(); } catch (e) { toast((e as Error).message, "bad"); }
  };
  return (
    <Card>
      <CardHead level={2} title="API keys" sub="for scripts and CI: send it as Authorization: Bearer <key>" />
      {made?.key && (
        <Notice tone="good" icon="key" className="mb-3">
          <b>Copy this key now: it is shown only once.</b>
          <div className="flex gap-2 items-center mt-2 flex-wrap">
            <code className="font-mono text-xs break-all bg-surface px-2 py-1 rounded-lg border border-line flex-1 min-w-0" id="new-key">{made.key}</code>
            <Button size="sm" icon="copy" onClick={() => { void navigator.clipboard?.writeText(made.key || ""); toast("Copied"); }}>Copy</Button>
          </div>
        </Notice>
      )}
      <form className="flex gap-2 flex-wrap items-end mb-3" onSubmit={(e) => { e.preventDefault(); void create(); }}>
        <Field label="What uses it" className="flex-1 min-w-45"><input id="key-name" className={inputCls} value={name} onChange={(e) => setName(e.target.value)} placeholder="GitHub Actions" /></Field>
        <Field label="It may"><Select className="w-auto" value={role} onChange={(v) => setRole(v as Role)} options={ROLE_OPTIONS.filter(([r]) => can(user, r))} /></Field>
        <Button type="submit" icon="plus" disabled={!name.trim()}>New key</Button>
      </form>
      {!keys ? <Skeleton height={60} /> : keys.length ? (
        <div className="divide-y divide-line border-t border-line">
          {keys.map((k) => (
            <div key={k.id} className="flex items-center gap-2.5 py-2.5 flex-wrap" data-key={k.prefix}>
              <Icon name="key" className="text-muted" />
              <b className="text-sm">{k.name}</b>
              <code className="font-mono text-xs text-muted">{k.prefix}…</code>
              <Chip>{k.role}</Chip>
              <span className="text-muted text-sm flex-1 min-w-30">last used {when(k.last_used)}</span>
              <Button size="sm" variant="danger" onClick={() => void revoke(k)}>Remove</Button>
            </div>
          ))}
        </div>
      ) : <p className="text-muted text-sm">No keys yet.</p>}
    </Card>
  );
}

function Users({ me }: { me: User }) {
  const toast = useToast();
  const { data: users, reload } = useLoad(() => authApi.users(), []);
  const [form, setForm] = useState({ username: "", name: "", password: "", role: "editor" as Role });
  const [busy, setBusy] = useState(false);
  const add = async () => {
    setBusy(true);
    try {
      const u = await authApi.newUser(form);
      toast(`Added ${u.username}. Give them their password in person or by a private channel.`);
      setForm({ username: "", name: "", password: "", role: "editor" });
      reload();
    } catch (e) { toast((e as Error).message, "bad"); } finally { setBusy(false); }
  };
  const edit = async (u: User, change: Parameters<typeof authApi.editUser>[1], done: string) => {
    try { await authApi.editUser(u.id, change); toast(done); reload(); } catch (e) { toast((e as Error).message, "bad"); }
  };
  const reset = async (u: User) => {
    const pw = window.prompt(`New password for ${u.username} (at least 10 characters):`);
    if (pw) await edit(u, { password: pw }, `New password set for ${u.username}; they are signed out everywhere.`);
  };
  const remove = async (u: User) => {
    if (!window.confirm(`Delete ${u.username}? Their API keys stop working.`)) return;
    try { await authApi.deleteUser(u.id); toast(`Deleted ${u.username}`); reload(); } catch (e) { toast((e as Error).message, "bad"); }
  };
  return (
    <Card id="users">
      <CardHead level={2} title="Users" sub="who can sign in, and what each one may do" />
      {!users ? <Skeleton height={80} /> : (
        <div className="divide-y divide-line border-y border-line mb-4">
          {users.map((u) => (
            <div key={u.id} className="flex items-center gap-2.5 py-2.5 flex-wrap" data-user={u.username}>
              <span className="size-7 rounded-full grid place-items-center bg-accent-soft text-accent-ink text-2xs font-bold flex-none">{(u.name || u.username).slice(0, 2).toUpperCase()}</span>
              <div className="min-w-0 flex-1">
                <b className="text-sm">{u.username}</b>{u.name && <span className="text-muted"> · {u.name}</span>}
                <div className="text-muted text-xs">last signed in {when(u.last_login)}{u.disabled ? " · switched off" : ""}</div>
              </div>
              <Select className="w-auto" value={u.role} disabled={u.id === me.id} onChange={(v) => void edit(u, { role: v as Role }, `${u.username} is now ${v}`)}
                options={ROLE_OPTIONS.map(([r]) => [r, r[0].toUpperCase() + r.slice(1)] as [string, string])} />
              {u.id !== me.id && (
                <>
                  <Button size="sm" variant="ghost" onClick={() => void reset(u)}>Reset password</Button>
                  <Button size="sm" variant="ghost" onClick={() => void edit(u, { disabled: !u.disabled }, u.disabled ? `${u.username} can sign in again` : `${u.username} is switched off`)}>
                    {u.disabled ? "Switch on" : "Switch off"}
                  </Button>
                  <Button size="sm" variant="danger" iconOnly icon="x" aria-label={`Delete ${u.username}`} onClick={() => void remove(u)} />
                </>
              )}
            </div>
          ))}
        </div>
      )}
      <form className="grid gap-3 sm:grid-cols-2" onSubmit={(e) => { e.preventDefault(); void add(); }}>
        <Field label="User name"><input id="new-user" className={inputCls} value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} autoComplete="off" /></Field>
        <Field label="Name (optional)"><input className={inputCls} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} autoComplete="off" /></Field>
        <Field label="First password" hint="At least 10 characters. They can change it under Your account."><input className={inputCls} type="password" autoComplete="new-password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} /></Field>
        <Field label="Role"><Select value={form.role} onChange={(v) => setForm({ ...form, role: v as Role })} options={ROLE_OPTIONS} /></Field>
        <div><Button type="submit" variant="primary" icon="plus" busy={busy} disabled={busy || !form.username.trim() || !form.password}>Add user</Button></div>
      </form>
    </Card>
  );
}

const EVENT_WORDS: Record<string, string> = {
  signed_in: "signed in", sign_in_failed: "failed to sign in", signed_out: "signed out", admin_created: "created the admin account",
  user_created: "added a user", user_changed: "changed a user", user_deleted: "deleted a user", password_changed: "changed their password",
  api_key_created: "made an API key", api_key_revoked: "removed an API key", api_key_refused: "API key refused",
  secret_saved: "saved a secret", claude_key_saved: "saved the Claude API key", run_started: "started a run", run_stopped: "stopped a run",
  project_created: "added an agent",
};

function Audit() {
  const { data: rows, error } = useLoad(() => authApi.audit(300), []);
  if (error) return <Notice tone="bad">{error.message}</Notice>;
  return (
    <Card flush id="audit">
      <div className="px-5 pt-4 pb-2"><CardHead level={2} title="Audit log" sub="sign-ins, user and key changes, secrets and runs; newest first" className="mb-0" /></div>
      {!rows ? <Skeleton height={120} /> : rows.length ? (
        <div className="divide-y divide-line border-t border-line max-h-105 overflow-y-auto scrollbar-thin">
          {rows.map((r, i) => {
            const extra = Object.entries(r).filter(([k]) => !["at", "event", "user", "ip"].includes(k)).map(([k, v]) => `${k}: ${String(v)}`).join(" · ");
            return (
              <div key={i} className="px-5 py-2 text-sm flex gap-3 flex-wrap">
                <span className="text-muted w-30 flex-none">{ago(r.at)}</span>
                <b className={cx(r.event.includes("failed") || r.event.includes("refused") ? "text-bad" : "")}>{r.user || r.username as string || "someone"}</b>
                <span>{EVENT_WORDS[r.event] || r.event}</span>
                <span className="text-muted flex-1 min-w-0 truncate">{extra}{r.ip ? ` · from ${r.ip}` : ""}</span>
              </div>
            );
          })}
        </div>
      ) : <EmptyState icon="history" title="Nothing yet" />}
    </Card>
  );
}

export function Account() {
  const { user } = useAuth();
  useCrumbs([{ label: "Your account" }]);
  if (!user) return <EmptyState icon="user" title="Sign-in is off" text="This dashboard runs without accounts." />;
  return (
    <>
      <PageHead title="Your account" sub={<>Signed in as <b>{user.username}</b> <Badge status="PASS" label={user.role} /></>} />
      <div className="grid gap-4 grid-cols-1 lg:grid-cols-2 items-start">
        <Password />
        <Keys user={user} />
        {user.role === "admin" && <div className="lg:col-span-2"><Users me={user} /></div>}
        {user.role === "admin" && <div className="lg:col-span-2"><Audit /></div>}
      </div>
    </>
  );
}
