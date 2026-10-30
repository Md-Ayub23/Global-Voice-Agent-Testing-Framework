# Testing a new voice agent with Vocto

Vocto tests any voice agent as a black box. It never needs the agent's code. It needs four ways in,
and every one of them is set up from the dashboard (**Projects > New project**), so onboarding an agent
is filling in forms, not writing code. Behind the forms is one folder per agent: `projects/<id>/`
with `profile.yaml`, `scenarios/` and `personas/` (git-ignored, since it holds client addresses).

## What to ask the agent's team

| # | Ask for | Why | Where it goes |
|---|---|---|---|
| 1 | A **test address** for the agent: the voice WebSocket its phone line uses (Twilio Media Streams, [twilio-protocol.md](twilio-protocol.md): most phone agents already have one; or [voice-protocol.md](voice-protocol.md)) and, if possible, a text endpoint ([text-http-protocol.md](text-http-protocol.md)) | Vocto calls the agent like a customer would. Text runs are cheap; voice runs measure speech, response times and barge-in. | Agent & connection |
| 2 | A setting to point the agent's **tool calls** at Vocto during tests, and the real (test) API address of each tool | Vocto sees every tool call, adds the test id, can break a tool on purpose (timeouts, errors) and forwards the call to the real API. | Tools |
| 3 | **Read-only access to the backend**: a database user (Postgres / SQLite) or a REST endpoint that shows records | After each call Vocto checks what was really stored. This is the strongest evidence: it beats what the agent says. | Backend |
| 4 | **The agent's logs** for test calls: a log file or a log search URL | Kept next to each call; scenarios can check them ("no ERROR"). | Logs |
| 5 | A **test environment** with test data, and (ideally) that the agent stores and logs the test id | Tests must never touch real customers. The test id links voice, tool calls, records and logs. | Scenarios (test data), Agent (test id) |
| 6 | **What the agent may say**: the facts it knows (prices, hours, refund rules), what it must not do, and where it sends callers it can't help | The AI judges compare the agent's answers with this, so they can tell a made-up answer from a correct one. | AI judges |
| 7 | A **fake secret planted** where the agent can reach it (its instructions, a test account's notes), for example a made-up admin password | Safety tests push the agent for passwords and internal details. If the planted value is ever said, logged or sent to a tool, Vocto blocks the release. Never use a real secret. | Test plan > Safety tests |

Without 2, 3 or 4 Vocto still runs, but checks that need that evidence say **NOT VERIFIED**
(never PASS), so a release can't get a full GO on them. Missing evidence is never a pass.

Secrets (tokens, passwords) are never written in the project. Write `${ACME_API_TOKEN}` in the form
and put `ACME_API_TOKEN=...` in the `.env` file in the repo folder. A run stops before the first
call if a named secret is missing.

## The steps in the dashboard

1. **Projects > New project.** Name, domain, voice or text (for voice: a phone agent on Twilio
   Media Streams, or Vocto's own protocol), and the agent's address.
2. **Agent & connection.** Check the address, add headers if the agent needs a token (for a Twilio
   agent, the custom parameters its TwiML passes), and press **Test connection**: Vocto opens one
   short session and shows what came back (the greeting). If the agent runs on this computer,
   **Starting the agent** lets Vocto start and stop it, and **Planted bugs** lists known bugs it can
   be started with, for the exit check ([real-agents-guide.md](real-agents-guide.md)).
3. **Tools.** One entry per API the agent can call: read (look-ups) or write (changes), the real API
   address, which arguments make two calls "the same action" (to catch duplicates), and which
   arguments are personal data (masked in the evidence). The page shows the gateway address to give
   the agent's team: `http://127.0.0.1:8790/tools/<tool name>`. An agent in the cloud reaches your
   laptop through a tunnel (ngrok, cloudflared) or by running Vocto next to it.
4. **Backend.** Database or REST address, then one **probe** per thing to look up after a call
   (e.g. "the bookings this call made": `... WHERE test_id = :test_id`). **Test backend** and
   **Run this probe now** try them straight away.
5. **Logs.** A file or a search URL; **Read recent lines** tries it.
6. **Test plan.** Scenarios and personas, written with forms:
   - start with the agent's main job (the happy path), then what happens when a tool fails
     (a fault: the agent must not say it worked), then hard callers (vague, rushed, noisy line);
   - **What must happen** is the heart of a scenario: the expected result (success, honest failure,
     or no action), the write tool, and the record that must (or must not) be in the backend;
   - a **scripted** scenario says fixed lines, so every release faces the same opening; in a real
     run Claude then carries on the call (up to **Test settings > Turns Claude adds after a script's
     fixed lines**, 6 by default), listening to the agent the way a person would: it finishes an
     order, or keeps pushing in a safety test. A dry run ends with the fixed lines;
   - **Checks** add rules on the whole call: tool order (verify before acting), things the agent
     must or must never say, details kept exactly, read-backs, log lines, answer times;
   - **Safety tests** are written for you: off-topic questions, pushing for secrets, prompt tricks,
     other people's data and more. Choose how hard to push and add the planted secret
     ([safety-guide.md](safety-guide.md)).
7. **Test settings.** This agent's release gate: p50 / p95 answer times, repeats, how many repeats a
   critical scenario must pass, minimum verified share, and the cost limits. Also on **Settings**.
8. **Run tests.** Start with a free dry run, then a real run. The report gives GO / NO-GO and why.

## Try it on the practice agent first

`zoo/clinic/service.py` runs the clinic test agent the way a client's agent runs: Vocto only knows
its addresses, its tool calls arrive at the gateway, it has its own booking API, database and log
file. `examples/clinic-service` is its finished project.

```powershell
uv run python -m zoo.clinic.service --no-voice           # window 1: the "client's" agent (text + API)
uv run vatf run examples/clinic-service --offline         # window 2: should be GO
# stop window 1, start it with a bug, run again: should be NO-GO (false success)
uv run python -m zoo.clinic.service --no-voice --bugs lie_on_failure
```

Voice: start the service without `--no-voice` (it loads the speech models) and run with
`--profile profile-voice.yaml`. To practise onboarding, create a new project in the dashboard and
fill it in for this service: addresses `http://127.0.0.1:9102` (text) or `ws://127.0.0.1:9101`
(voice), tool APIs `http://127.0.0.1:9103/tools/<name>`, database `sqlite:///zoo/clinic/clinic.db`,
log file `logs/clinic-service.log`.

A real open-source agent to practise on: Pipecat's food ordering agent, `examples/pipecat-food`
([real-agents-guide.md](real-agents-guide.md)). Vocto starts it itself, so nothing needs a second window.

## Postgres

Postgres backends need the driver once: `uv sync --extra postgres`. Give Vocto a read-only user:
probes run in read-only transactions anyway, so a probe can never change data. Test data
(scenario fixtures) is written with the same connection, so point it at a test database only.
