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

The app is built around **agents**: one per voice agent you test (each is a project folder underneath).
The sidebar has the workspace pages at the top. Once you open an agent, its own pages appear below in the
order the work happens: **Overview**, then **1 Set up**, **2 Test** and **3 Quality**, with a switcher to jump
to another agent. Pages that belong together are tabs of one page, so nothing is hidden inside another page.

| Page | What it is for |
|---|---|
| **Home** | Every agent at a glance: *Needs attention* (agents whose latest run was NO-GO or not enough evidence, and your agents not ready to run), how many agents and which kinds, test runs, the release GO rate, runs per day for 14 days, each agent's last result and the latest runs. Runs with bug switches on are bug tests: listed but left out of the GO rate. |
| **Agents** | Your agents and Vocto's practice agents as cards. *New agent* asks for the agent's name, industry, voice or text, and (optionally) its address. |
| **Settings** | The Anthropic API key, the speech models, **Secrets** (every `${NAME}` your agents use; kept on this computer only) and a link to each agent's pass rules. |

Inside an agent:

| Group | Page | What it is for |
|---|---|---|
| | **Overview** | The agent, its latest release decision, a set-up checklist with a link to each step, its numbers and recent runs. |
| 1 Set up | **Connection** | Who the agent is and how Vocto reaches it (voice format, Twilio Media Streams or Vocto's protocol, *Starting the agent* and its planted bugs). |
| | **Tools & data** | Three tabs: **Tools** (the APIs the agent calls), **Database** (read-only access to check what really got saved) and **Logs**. |
| | **Test cases** | The scenarios as a list and forms, and *Write tests for me* (Claude drafts a plan from the agent's instructions). *Advanced* opens the raw file editor. |
| | **Callers** | Personas (who calls): accent, voice, speed, loudness and behaviour (cuts in, says "mm-hm", pauses mid-sentence, says um, goes quiet once), and *Add from library*. A test's own **Audio** block (Test cases) sets its noise and phone line (narrowband, lost packets). See [phase5-guide.md](phase5-guide.md). |
| | **Safety tests** | How hard to push, which attacks, your own questions, planted secrets. |
| | **Pass rules** | What a GO needs for this agent: p50 / p95 limits, repeats, coverage, budget, and **Call quality**: each metric's good and warn values and a *Blocks* switch. |
| 2 Test | **Run tests** | Choose the channel, scenarios, caller (Claude or a free dry run) and repeats, then *Start run*; or an exit check. *Include safety tests* adds the attack calls. Voice runs can lay **Caller conditions** over every test: As written, Phone line, Noisy or Hard. |
| | **Results** | A run in progress is pinned on top (*Watch live* shows the calls as they happen; for voice, *Listen live* plays them). Below, every run, filterable, and an *Exit checks* tab. A run opens its page: the decision and why, scorecard and coverage, then tabs for **Calls**, **Reliability**, **Response times**, **Call quality** (metric tiles with bands, results by caller condition), **AI judges**, **Safety** and **Run setup**. A call shows its recording and conversation, with tabs beside it for **Checks**, **AI judges**, **Timing**, **Quality**, **Database** and **Evidence**. |
| 3 Quality | **AI judges** | Three tabs: **Scores** (what the judges score, whether each item is trusted to block yet, *Teach the judges*), **Review calls** (label calls, the most useful first, without seeing the judges' answers) and **Judge settings** (on/off, the agent's policy, items, models). See [phase3-guide.md](phase3-guide.md). |

Each set-up page has a *Next* button, so a new agent can be set up in order. Addresses from before this layout
(for example `#/p/<agent>/setup/plan`) still work and open the new page. See [onboarding-guide.md](onboarding-guide.md).

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
- `ui/src/pages/`: one file per page. `ui/src/App.tsx` maps addresses to pages: `#/` (home), `#/projects` (agents), `#/settings`, and `#/p/<project>/...` for one agent's pages; `components/Layout.tsx` holds the sidebar groups.
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
