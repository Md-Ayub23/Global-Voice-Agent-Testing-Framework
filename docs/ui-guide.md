# The Vocto dashboard

`uv run python -m vatf ui` (or `uv run vatf ui` where the launcher isn't blocked) starts a small web app on your computer and opens it in your browser
(http://127.0.0.1:8765). Vocto is the product name; the terminal command is still `vatf`. Everything the terminal commands do can be done from it: pick what to
test, start a run, watch every call live, and read the release readiness report. Keep the terminal
window open while you use it; press Ctrl+C there to close the dashboard.

```powershell
uv run vatf ui                  # opens the browser
uv run vatf ui --port 9000      # another port (it also moves on by itself if 8765 is taken)
uv run vatf ui --no-browser     # don't open a browser
```

It needs nothing beyond `uv sync`. Forms save into the same YAML files as before; comments you wrote by hand are kept. It only answers on this computer (127.0.0.1), never on the network.

## How it is organised

The app is built around **agents** (one per voice agent you test; each is a project folder underneath) and,
inside each agent, around **test types**. Home shows every agent side by side. Once you open an agent, the
sidebar shows only that agent: nothing from other agents is on its pages.

| Page | What it is for |
|---|---|
| **Home** | Every agent side by side: key numbers, *Needs attention* (failed test types, agents not connected yet), **Agents by test type** (each agent's latest result in every test type; a result opens its report), runs per day for 14 days and the latest runs. |
| **Agents** | Your agents and Vocto's practice agents. *New agent* asks for the agent's name, industry, voice or text, and (optionally) its address. |
| **Settings** | The Anthropic API key, the speech models and **Secrets** (every `${NAME}` your agents use; kept on this computer only). |

Inside an agent:

| Group | Page | What it is for |
|---|---|---|
| | **Overview** | Only this agent: one line on where it stands, **Results by test type** (latest result, top finding and runs of each type), the findings to look at, **This agent over time** (calls passed and slowest answers per run; a dot opens its report), recent runs and what is left to set up. |
| | **Connect** | How Vocto reaches the agent, for any kind of agent: a phone number (Twilio or Plivo, see [phone-guide.md](phone-guide.md)), a voice WebSocket, Twilio Media Streams, a text API or a Python function. Tabs: **Connection** (with the **Agent brief**: its instructions, which Claude reads to draft test cases), **Tools**, **Database** and **Logs**. |
| | **Release check** | Every test case of every type plus the safety attacks in one run and one report. Run it before a release. |
| Tests | **Functional**, **Conversation & timing**, **Speech & accents**, **Safety**, **Resilience**, **Load & capacity**, **Regression** | One page per test type, with a status dot in the sidebar. Each has its own **Test cases** (Safety: **Attack calls**), a **Run** tab (the form for that kind of testing) and **Reports** (only its runs). See below. |
| Results | **All runs** | Every run, filterable, the run in progress on top and the exit checks. |
| | **AI judges** | Scores, review calls and judge settings. See [phase3-guide.md](phase3-guide.md). |
| | **Real calls** | Production calls, see [phase8-guide.md](phase8-guide.md). |
| | **Agent settings** | What every test type shares: pass rules, callers, AI judge settings, CI & alerts, and the raw files. |

### Test cases per test type

Each test case belongs to one test type (`test_type` in its file). Older files without one are placed by what
they do: a broken tool is Resilience, the caller cutting in is Timing, noise or a phone line is Speech, the rest
Functional. You can change it on the test case's form.

- **New test case** opens a form that starts with what the type needs (Timing: a voice call with a cut-in;
  Speech: a voice call on a noisy line).
- **Draft with AI** has Claude draft cases for that type only, written for this agent from its brief, tools and
  database checks: Functional drafts normal calls, edge cases and misuse; Timing drafts cut-ins, corrections,
  "mm-hm", pauses and silence; Speech drafts noise, phone lines, a bad signal, accents, a quiet voice and spelled
  details; Resilience drafts broken tools. Vocto checks every draft; only the ones you tick become test cases.
  Timing and Speech drafts use callers made for them (says "mm-hm", pauses, goes quiet, accents, quiet voice),
  added from the library when you add the case.
- A run of a type runs its own cases. Timing and Speech also run the Functional cases (answer times are measured
  on every call, and the harder callers are tried on normal calls); Resilience also drills the Functional cases
  that end in a booking or order.

### Reports

A run opens its **release readiness report**: the verdict in plain words, an executive summary and a scorecard,
then **Findings** and an **Analysis** tab with the numbers and charts of the run's own test type:

| Test type | Analysis |
|---|---|
| Functional, Release check, Regression | Calls passed, test cases that passed every repeat, blocking findings, coverage; each test case's repeats (failed first); results by area |
| Conversation & timing | Typical and slowest answers against the limits, answers over the limit, stop time after a cut-in, "mm-hm" handled, check-in after silence; a histogram of every answer time; slowest answers per test case |
| Speech & accents | Calls passed, conditions handled, numbers heard right, words misheard; calls passed and words misheard per caller condition |
| Safety | Attacks handled safely, unsafe answers, calls for you to label; results per kind of attack |
| Resilience | Failures handled, recovered or honest, untrue claims, duplicates; how calls ended per kind of failure |
| Load & capacity | The load report (its own tab) |

The other tabs (Calls, Reliability, Response times, Call quality, AI judges, Safety, Vs baseline, Numbers &
coverage, Run setup) are as before. *PDF report* and *CSV* export the whole report.

Old addresses (for example `#/p/<agent>/setup/cases` or `#/p/<agent>/setup/safety`) still work and open the
new page. The app never shows terminal commands or folder locations.

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

## Phase 6: baseline, Compare runs, CI & alerts

- Results: a Baseline card (per channel, with Remove), a "baseline" mark on that run, "N new failures vs baseline" under a run, a Health checks filter, and CI / health check chips.
- Run report: **Set as baseline** and **Compare** buttons, a **Vs baseline** tab listing every change worst first, and "Started by", "Compared with" and "Alerts" under Run setup.
- Quality › **Compare runs**: any two runs side by side, or *Test two versions (A/B)*: the same tests on two versions of the agent, then version B against version A.
- Set up › Pass rules: the **Regression gate** table (what may not get worse, and whether it blocks).
- Set up › **CI & alerts** (step 7): alert targets with Send test, health check schedule, and a ready CI file to copy.
See [phase6-guide.md](phase6-guide.md).

## Phase 7: load tests

- Test › **Load test**: pick the shape (ramp, spike, soak, steady), the calls at once and how long, who plays the callers (scripted for free, or Claude), the tests and the limits. The plan chart and its length follow the form as you type. Save, then **Start load test** (voice: real speech or test tones).
- The Live page for a load test shows calls at once over time (planned and real), calls started, finished and failed, Vocto's own lag and the latest calls.
- The run report opens on a **Load** tab: capacity, calls, p95 at the top level, tests passed, where it broke, the calls-at-once chart, p95 per stage against the limit, the stage table and notes. **Set as baseline** on a load run sets the load baseline (separate from the normal one); later load tests get a **Vs load baseline** tab.
- Results: load runs carry a "load test" chip with their shape and capacity, and a **Load tests** filter. Load runs are left out of the agent's GO rate.

## Phase 8: real calls

- Quality › **Real calls**, with four tabs. **Overview**: active alerts, KPIs for the last 24 h with this week vs last, calls per day, call quality, why calls were flagged, results by agent version and the alert history. **Calls**: filters, search and the call list. **Import**: drop call files. **Settings**: monitoring on or off, receiving calls live (port, address, token shown once), privacy, AI judges limits, alert rules, delete all.
- A real call's page: the conversation (personal data already removed), why it was flagged, checks, AI judges with labels you can give right there, call quality, details, **Make a test from this call** and Delete.
- Set up › CI & alerts has two new alert events: "Real-call alert" and "Real calls back to normal".
See [phase8-guide.md](phase8-guide.md).

