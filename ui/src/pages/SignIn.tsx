// The sign-in page, and on the very first visit the page that creates the admin account.
import { useState } from "react";
import { Button, Card, Field, Notice, inputCls } from "../components/ui";
import { authApi, type User } from "../lib/auth";
import { cx } from "../lib/format";

export function SignIn({ setup, done }: { setup: boolean; done: (u: User) => void }) {
  const [username, setUsername] = useState("");
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [again, setAgain] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    setError(null);
    if (setup && password !== again) { setError("The two passwords are not the same."); return; }
    setBusy(true);
    try {
      const r = setup ? await authApi.setup(username.trim(), password, name.trim()) : await authApi.login(username.trim(), password);
      done(r.user);
    } catch (e) { setError((e as Error).message); setPassword(""); setAgain(""); } finally { setBusy(false); }
  };
  return (
    <div className="min-h-screen grid place-items-center px-4 py-10">
      <div className="w-full max-w-100 animate-rise">
        <div className="flex items-center gap-2.5 justify-center mb-6">
          <img src="/static/logo.svg" alt="" className="size-9 rounded-xl" />
          <b className="text-xl tracking-[-.03em]">Vocto</b>
        </div>
        <Card>
          <h1 className="text-lg font-bold mb-1">{setup ? "Create the admin account" : "Sign in"}</h1>
          <p className="text-ink-2 text-sm mb-4">
            {setup ? "This is the first visit to this Vocto. The account you make now can add the rest of your team."
              : "Use the account your Vocto admin gave you."}
          </p>
          <form className="grid gap-3" onSubmit={(e) => { e.preventDefault(); void submit(); }}>
            <Field label="User name">
              <input id="signin-user" className={inputCls} autoComplete="username" autoFocus value={username}
                onChange={(e) => setUsername(e.target.value)} required />
            </Field>
            {setup && (
              <Field label="Your name (optional)">
                <input className={inputCls} autoComplete="name" value={name} onChange={(e) => setName(e.target.value)} />
              </Field>
            )}
            <Field label="Password" hint={setup ? "At least 10 characters; a short sentence is easy to remember." : undefined}>
              <input id="signin-password" className={inputCls} type="password" autoComplete={setup ? "new-password" : "current-password"}
                value={password} onChange={(e) => setPassword(e.target.value)} required />
            </Field>
            {setup && (
              <Field label="Password again">
                <input id="signin-again" className={inputCls} type="password" autoComplete="new-password" value={again}
                  onChange={(e) => setAgain(e.target.value)} required />
              </Field>
            )}
            {error && <Notice tone="bad" icon="alert">{error}</Notice>}
            <Button id="signin-submit" type="submit" variant="primary" icon={setup ? "user" : "lock"} busy={busy}
              disabled={busy || !username.trim() || !password} className={cx("mt-1 justify-center")}>
              {setup ? "Create account and sign in" : "Sign in"}
            </Button>
          </form>
        </Card>
        {!setup && <p className="text-center text-muted text-sm mt-4">Forgot your password? Ask your Vocto admin to reset it. A locked-out admin can run <code className="font-mono">vatf users reset-password</code> on the server.</p>}
      </div>
    </div>
  );
}
