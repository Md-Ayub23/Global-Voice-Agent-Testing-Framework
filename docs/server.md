# Running Vocto for a team (server mode)

`vatf ui` is Vocto on your own computer. `vatf serve` is the same dashboard as an always-on server
that a whole team signs in to, with roles, API keys for CI, and an audit log.

## Quick start with Docker

```bash
VOCTO_ALLOWED_HOSTS=vocto.example.com docker compose up -d --build
```

Open `http://<server>:8765`. The first visit asks you to create the admin account. Then add your
team under **Settings › Users, keys and audit log**.

Everything that changes lives in the `vocto-data` volume (`/data` in the container):

| Path | What |
|---|---|
| `/data/projects/` | the agents and their test cases |
| `/data/runs/` | every run, recording and report |
| `/data/runs/.vocto/users.db` | users, sign-ins, API keys (hashed) |
| `/data/runs/.vocto/audit.jsonl` | the audit log |
| `/data/.env` | secrets the agents use (Settings › Secrets) |
| `/data/.models/` | speech models, downloaded on the first voice run (about 750 MB) |

Back up the volume; nothing else needs saving.

## Without Docker

```bash
uv sync
uv run python -m vatf serve --host 0.0.0.0 --port 8765
```

## HTTPS (needed)

Vocto speaks plain HTTP. Put a TLS proxy in front and set `VOCTO_SECURE_COOKIES=1`, so the sign-in
cookie is only sent over HTTPS. Set `VOCTO_TRUSTED_PROXIES` to the proxy's address too, or every user
shares the proxy's address and its sign-in limit (10 a minute). With Caddy:

```
vocto.example.com {
    reverse_proxy 127.0.0.1:8765
}
```

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `VOCTO_ALLOWED_HOSTS` | this machine's names | host names the server answers to, comma-separated (`*` = any, when the proxy checks) |
| `VOCTO_SECURE_COOKIES` | off | send the sign-in cookie only over HTTPS |
| `VOCTO_TRUSTED_PROXIES` | none | the HTTPS proxy's address or network (e.g. `127.0.0.1` or `172.16.0.0/12` for Docker), so sign-in limits and the audit log see each user's own address (from `X-Forwarded-For`) instead of the proxy's |
| `VOCTO_SESSION_DAYS` / `VOCTO_SESSION_IDLE_HOURS` | 14 / 72 | longest a sign-in lasts / signed out after this long unused |
| `VOCTO_LOGIN_PER_MINUTE` | 10 | sign-in attempts per address a minute (an account also locks for 15 minutes after 5 wrong passwords) |
| `VOCTO_URL_POLICY` | `server` | typed-in agent URLs may not reach the server itself (docs/security.md) |
| `VOCTO_MAX_RUNS` / `VOCTO_MAX_QUEUED` | 2 / 20 | runs at once / waiting |
| `ANTHROPIC_API_KEY` | none | Claude key for live runs (or set it in Settings) |

All of docs/security.md's settings apply too.

## Roles

| Role | May |
|---|---|
| viewer | read everything: agents, test cases, runs, reports, real calls |
| editor | also run tests, edit agents and test cases, label calls, make API keys up to editor |
| admin | also manage users, secrets and the Claude key, read the audit log, and change the settings that make Vocto run code or send a secret somewhere: `agent_process`, Python agents (`text_python`, `connection.launch`), `python:` tool handlers, and every `${NAME}` secret reference |

## API keys (CI and scripts)

Make one under **Your account › API keys** and send it with every request:

```bash
curl -H "Authorization: Bearer vk_..." https://vocto.example.com/api/runs
```

A key never does more than its owner, is shown once, and can be removed at any time.

## Users from the terminal

```bash
uv run python -m vatf users list
uv run python -m vatf users add maria --role editor
uv run python -m vatf users reset-password ram      # e.g. the only admin forgot their password
```

(In Docker: `docker compose exec vocto vatf users list --root /data`.)

## Outbound agents on a real phone line

To test an agent that makes calls, give its phone connection an `outbound` section: the Twilio
number the agent should call (`answer_number`) and, if the agent has one, its "make a call" API
(`trigger_url`). Vocto points that number at the test for as long as it takes the agent to ring, then
puts it back as it was (docs/phone-guide.md).
