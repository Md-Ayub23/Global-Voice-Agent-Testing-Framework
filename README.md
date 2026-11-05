# Vocto: Voice Agent Testing Framework

Vocto is the product name; in the terminal its command (and Python package) is `vatf`.

A local-first framework that tests any voice agent as a black box. It simulates
callers, watches the voice, the tool calls and the backend state, scores each
call with hard checks and calibrated AI judges, and writes a release readiness
report in plain words (Ready / Not ready / Not enough evidence, with the proof). Each agent
can be tested in separate test modes (Full check, Functional, Timing, Speech, Safety,
Resilience, Load, Regression, Real calls): see [docs/test-modes-guide.md](docs/test-modes-guide.md).

**Status: Phase 8 (real-call monitoring).** The framework runs real test calls against the
clinic example agent, in text or by voice: the simulated caller speaks (Piper),
listens (Whisper) and hears only what came down the line. It checks what the agent
said against what it did and what was stored, measures response times on the call
recording, repeats each scenario 3 times and decides GO / NO-GO. Two AI judges also read every
conversation for what hard checks can't see (made-up facts, tone, staying in scope, personal
data), with items for every agent plus packs for ten industries and the agent's own; they only
block a release once a person's labels show they catch real failures (90% of failures caught and of good calls passed, over 20 labels); a third deciding judge breaks ties. Built-in **safety
tests** try to make the agent go wrong (off-topic questions, pushing for passwords, prompt tricks,
other people's data, abuse and more), written for each agent from its industry, tools and policy.
**Harder callers and call quality (Phase 5):** callers with accents, on a phone line with lost
packets, who cut in, say "mm-hm", pause mid-sentence or go quiet; every voice call is scored on 14
call quality metrics (time to first word, p90 / p99, silences, talk-over, stop time, and more) with
good / warn / bad bands, and the report splits results by caller condition. See
[docs/phase5-guide.md](docs/phase5-guide.md).
**Baseline, regression gate and CI (Phase 6):** mark a good run as the agent's baseline and every
later run is compared with it: a test that passed there and fails now blocks the release, and slower
answers, lower scores and worse call quality are listed (and can block, if you choose). The same run
works in CI (JUnit XML, a Markdown job summary, exit codes, a ready file for GitHub Actions, GitLab or
Jenkins), on a schedule as health checks, and sends Slack, Discord or webhook alerts. See
[docs/phase6-guide.md](docs/phase6-guide.md).
**Load testing (Phase 7):** many test calls at once, in a ramp, spike, soak or steady shape. Every
load call is a normal call with all its checks; the result is a GO / NO-GO for that load, the agent's
capacity (calls at once within the limits), where it broke and why, answer times per stage, and a
separate load baseline. Vocto watches its own computer too, so a stage it couldn't drive properly
gives "not enough evidence" instead of blaming the agent. See [docs/phase7-guide.md](docs/phase7-guide.md).
**Real-call monitoring (Phase 8):** import real production calls (Vocto JSON, Vapi, Retell or plain
transcripts) or have the voice platform send them live. Personal data is removed before anything is
saved; each call gets the call quality metrics, real-call checks and the AI judges (on a capped
sample), with trends by day and agent version, alerts through the Phase 6 targets, and one click to
turn a bad call into a test. See [docs/phase8-guide.md](docs/phase8-guide.md).

**Real agents:** besides its own practice agents, Vocto tests a real open-source agent it did not
write: Pipecat's food ordering agent, run on this computer as a phone agent and called the way
Twilio calls it (Twilio Media Streams). Vocto can start that agent itself, and its exit check
restarts it with bugs planted in its own prompts and code to prove each one is caught. See
[docs/real-agents-guide.md](docs/real-agents-guide.md).

**Testing your own agent:** create a project for it in the dashboard (Projects > New project) and
fill in its address, tools, backend and logs access, test plan and test settings with forms. See
[docs/onboarding-guide.md](docs/onboarding-guide.md) for what to ask the agent's team.

- [docs/onboarding-guide.md](docs/onboarding-guide.md): testing a new agent: what to ask its team, and the set-up screens
- [docs/ui-guide.md](docs/ui-guide.md): the dashboard (`uv run vatf ui`): start runs, watch them live, read the reports
- [docs/phase3-guide.md](docs/phase3-guide.md): AI judges, labelling calls and calibration
- [docs/test-plan-guide.md](docs/test-plan-guide.md): writing tests automatically from the agent's instructions, the persona library
- [docs/safety-guide.md](docs/safety-guide.md): safety tests: the attacks, planted secrets, how they affect the decision
- [docs/phase1-guide.md](docs/phase1-guide.md): text mode, how to run it and what each part does
- [docs/phase2-guide.md](docs/phase2-guide.md): voice mode, response times, barge-in, noise, read-back checks
- [docs/phase5-guide.md](docs/phase5-guide.md): harder callers (accents, phone line, cut-ins, "mm-hm", pauses, silence) and call quality metrics with good / warn / bad bands
- [docs/phase6-guide.md](docs/phase6-guide.md): baseline, comparing runs, the regression gate, CI files and reports, alerts, health checks
- [docs/phase7-guide.md](docs/phase7-guide.md): load tests (ramp, spike, soak, steady), capacity, the load baseline
- [docs/phase8-guide.md](docs/phase8-guide.md): monitoring real calls: import or receive them, privacy, scoring, trends, alerts
- [docs/real-agents-guide.md](docs/real-agents-guide.md): testing a real open-source agent (Pipecat food ordering), planted bugs, agents Vocto starts itself
- [docs/phone-guide.md](docs/phone-guide.md): testing a real phone number: Vocto rings the agent through your Twilio or Plivo account
- [docs/twilio-protocol.md](docs/twilio-protocol.md): calling a phone agent the way Twilio Media Streams does
- [docs/voice-protocol.md](docs/voice-protocol.md): how vatf talks to a voice agent over a WebSocket
- [docs/text-http-protocol.md](docs/text-http-protocol.md): how vatf talks to an agent in text over HTTP
- [docs/phase0-contracts.md](docs/phase0-contracts.md): the file formats and interfaces

```
schemas/                 file formats (JSON Schema, written as YAML)
src/vatf/                the framework (contracts/ holds the Phase 0 interfaces, web/ the dashboard)
ui/                      the dashboard's React source (built into src/vatf/web/static/)
zoo/clinic/              test agent #1: clinic booking agent with bug switches (text and voice);
                         service.py runs it as an outside service, like a client's agent
zoo/voice/               voice front-end that puts a zoo text agent on the phone
zoo/latency_ref.py       answers after known delays: the measuring stick for latency
examples/clinic/         clinic profiles (text, voice), personas, scenarios, a hand-written sample run
examples/clinic-service/ the same agent tested only through its addresses, tool gateway, database and logs
projects/                your agents' projects, made in the dashboard (git-ignored)
examples/latency-ref/    timing check for vatf's own response-time numbers
examples/pipecat-food/   project for the real Pipecat food ordering agent (with planted bugs)
targets/pipecat-food/    that agent, as a phone server with its order API (its own uv environment)
tests/                   framework tests (run offline, no API key needed)
docs/                    plain-English guides
```

## Quick start (Windows PowerShell)

Needs Python 3.12+ and [uv](https://docs.astral.sh/uv/). Run everything from the repo folder.

The easy way is the dashboard. It opens in your browser, and everything below can be done from it:

```powershell
uv sync                          # install (once)
uv run vatf ui                   # the dashboard: http://127.0.0.1:8765
```

The same things from the terminal:

```powershell
uv run pytest                    # framework tests, offline (about 5 minutes: voice tests run calls in real time)

# Free dry run: rule-based caller and agent, no API calls.
uv run vatf exit-check examples/clinic --offline

# Real run: Claude plays the caller and the clinic agent.
# Put your key in a .env file first (copy .env.example to .env); git ignores .env.
uv run vatf run examples/clinic
uv run vatf run examples/clinic --agent-opt bugs=lie_on_failure   # should be NO_GO

# Voice: the same agent by phone (speech models download once into .models/, ~750 MB).
uv run vatf run examples/latency-ref                                # timing check, no API key
uv run vatf run examples/clinic --profile profile-voice.yaml --offline
uv run vatf run examples/clinic --profile profile-voice.yaml

# A real open-source agent (Pipecat food ordering): Vocto starts it, calls it like Twilio, stops it.
# The first start installs its own environment and takes a few minutes.
uv run vatf run examples/pipecat-food --start-agent
uv run vatf exit-check examples/pipecat-food                      # clean, then each planted bug
```

Each run prints every call (conversation, tool calls, database result, judgement) and writes the same to `runs/<run_id>/transcript.txt` and `runs/<run_id>/report.html`. Voice calls also save `audio/call.wav` (left caller, right agent). Exit codes: 0 GO, 1 NO_GO,
2 NO_GO (not enough evidence), 3 the framework itself failed.
