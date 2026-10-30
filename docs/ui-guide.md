# The Vocto dashboard

`uv run vatf ui` starts a small web app on your computer and opens it in your browser
(http://127.0.0.1:8765). Vocto is the product name; the terminal command is still `vatf`. Everything the terminal commands do can be done from it: pick what to
test, start a run, watch every call live, and read the GO / NO-GO report. Keep the terminal
window open while you use it; press Ctrl+C there to close the dashboard.

```powershell
uv run vatf ui                  # opens the browser
uv run vatf ui --port 9000      # another port (it also moves on by itself if 8765 is taken)
uv run vatf ui --no-browser     # don't open a browser
```

It needs nothing beyond `uv sync`. Forms save into the same YAML files as before; comments you wrote by hand are kept. It only answers on this computer (127.0.0.1), never on the network.

## How it is organised

The app is built around **projects**: one project per voice agent you test. The sidebar has the
workspace pages at the top, and once you open a project, that project's own pages below it (with a
switcher to jump to another project).

| Page | What it is for |
|---|---|
| **Dashboard** (home) | Every agent at a glance: how many projects and which kinds (voice / text, industry), test runs, the release GO rate, runs per day for the last 14 days, each project's last result and recent decisions, and the latest runs across all projects. Runs with bug switches on are bug tests: they are listed but left out of the GO rate. |
| **Projects** | Your agents and Vocto's practice agents as cards. *New project* asks for the agent's name, industry, voice or text, and (optionally) its address. |
| **Settings** | Each agent's test settings (p50 / p95 limits, repeats, coverage, budget), **Secrets** (every `${NAME}` your projects use, with a place to set its value; kept on this computer only), the Anthropic API key and the speech models. |

Inside a project:

| Page | What it is for |
|---|---|
| **Overview** | The agent, its latest release decision, a setup checklist (connect the agent, tools, backend, logs, test plan, AI judges, safety tests, first run) with a link to each step, its numbers and recent runs. |
| **New run** | Choose the channel (voice or text), scenarios, caller (Claude or a free dry run) and repeats, then *Start run*. *Include safety tests* adds the attack calls written for this agent. Practice agents also offer bug switches and the exit check. |
| **Live run** | A run as it happens: the conversation of the current call, tool calls, broken tools, response times and checks as each call is judged. For voice runs, *Listen live* plays the call in your browser as it happens (see [phase2-guide.md](phase2-guide.md#listening-live)). |
| **Run history** | Every run of this agent; filter by decision or channel. Practice agents also have an *Exit checks* tab. |
| **AI judges** | What the AI judges score (items for every agent, the industry pack, the agent's own, scenario questions), whether each item is trusted to block yet, how many of your failures it caught and good calls it passed, *Teach the judges* examples, and recent results. *Label calls* opens the queue. See [phase3-guide.md](phase3-guide.md). |
| **Label calls** | Calls waiting for your label, the most useful first (judges disagree, a judge failed it, bug tests). Pick Pass / Fail / Doesn't apply per item without seeing the judges' answers, with an optional why; after saving you see the judges' answers, and the run's decision is updated. |
| **Run report** and **Call** | The decision and why, scorecard, coverage, safety tests (each kind of attack and every finding), reliability, response times and every call; for one call the recording, conversation, said vs did vs stored, each check with its evidence, and the database after the call. |
| **Set up** (7 pages) | **Agent & connection**, **Tools**, **Backend & database**, **Logs**, **Test plan** (scenarios and personas as forms, and the safety tests: how hard to push, which attacks, planted secrets), **AI judges** (on/off, the agent's policy, items, custom items, models and the deciding judge) and **Test settings**. Each page has a *Next* button, so a new project can be set up in order. See [onboarding-guide.md](onboarding-guide.md). |

The app never shows terminal commands or folder locations. Everything a run needs is set up in it.

## Where things are saved

The dashboard reads and writes the same files as the terminal: `runs/<run_id>/` holds each run
(`run.json` says how it was started and how it ended, `decision.json`, `transcript.txt`,
`report.html`, and one folder per call). Exit checks started from the dashboard also keep a
summary in `runs/.ui/exit-checks/`. Personal data is masked in the live view the same way it is
in the saved files, unless *Keep names and dates readable* was switched on.

## For developers

The dashboard is a React app (React 19, TypeScript, Tailwind CSS 4, built with Vite). Its source is
in `ui/`; the built files are committed in `src/vatf/web/static/`, so `uv run vatf ui` needs no Node.

- `src/vatf/web/server.py`: the HTTP server (Python standard library only) and its JSON API.
- `src/vatf/web/jobs.py`: starts each run as the real `vatf run` / `vatf exit-check` command and follows it.
- `src/vatf/web/data.py`: reads projects, runs and calls for the pages, and builds the dashboard numbers (`dashboard`, `project_overview`) and the secrets list.
- `ui/src/pages/`: one file per page. `ui/src/App.tsx` maps addresses to pages: `#/` (dashboard), `#/projects`, `#/settings`, and `#/p/<project>/...` for one project's pages.
- `ui/src/components/`: shared pieces: `Timeline.tsx` (the conversation), `charts.tsx`, `ui.tsx`
  (buttons, cards, badges), `Layout.tsx` (sidebar and top bar).
- `ui/src/lib/`: the API client, the response types (`types.ts`, matching `data.py`), formatting.
- `ui/src/index.css`: the colours (light and dark), fonts and animations.
- Live updates use server-sent events: `GET /api/jobs/<id>/stream`.
- Tests: `tests/test_web.py` (it also checks that the committed build is complete) and `tests/test_dashboard.py` (dashboard numbers, project keys, secrets).

To change the dashboard (needs Node 20 or newer, once: `cd ui && npm install`):

```bash
uv run vatf ui --no-browser      # terminal 1: the Python server on port 8765
cd ui && npm run dev             # terminal 2: http://localhost:5173, reloads as you edit
cd ui && npm run build           # when done: type-checks, then writes src/vatf/web/static/
```

Commit both `ui/` and `src/vatf/web/static/`. To add a page for a later phase, add a file in
`ui/src/pages/`, a route in `App.tsx` (under `PROJECT_ROUTES` if it belongs to one agent), a link in `components/Layout.tsx`, and any data it needs in
`data.py` plus a route in `server.py`.
