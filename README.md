# Vocto: Voice Agent Testing Framework

Vocto is the product name; in the terminal its command (and Python package) is `vatf`.

A local-first framework that tests any voice agent as a black box. It simulates
callers, watches the voice, the tool calls and the backend state, scores each
call with hard checks and calibrated AI judges, and gives a GO / NO-GO release
decision.

**Status: Phase 3 (AI judges).** The framework runs real test calls against the
clinic example agent, in text or by voice: the simulated caller speaks (Piper),
listens (Whisper) and hears only what came down the line. It checks what the agent
said against what it did and what was stored, measures response times on the call
recording, repeats each scenario 3 times and decides GO / NO-GO. Two AI judges also read every
conversation for what hard checks can't see (made-up facts, tone, staying in scope, personal
data), with items for every agent plus packs for ten industries and the agent's own; they only
block a release once a person's labels show they catch real failures (90% of failures caught and of good calls passed, over 20 labels); a third deciding judge breaks ties. Built-in **safety
tests** try to make the agent go wrong (off-topic questions, pushing for passwords, prompt tricks,
other people's data, abuse and more), written for each agent from its industry, tools and policy.

**Testing your own agent:** create a project for it in the dashboard (Projects > New project) and
fill in its address, tools, backend and logs access, test plan and test settings with forms. See
[docs/onboarding-guide.md](docs/onboarding-guide.md) for what to ask the agent's team.

- [docs/onboarding-guide.md](docs/onboarding-guide.md): testing a new agent: what to ask its team, and the set-up screens
- [docs/ui-guide.md](docs/ui-guide.md): the dashboard (`uv run vatf ui`): start runs, watch them live, read the reports
- [docs/phase3-guide.md](docs/phase3-guide.md): AI judges, labelling calls and calibration
- [docs/safety-guide.md](docs/safety-guide.md): safety tests: the attacks, planted secrets, how they affect the decision
- [docs/phase1-guide.md](docs/phase1-guide.md): text mode, how to run it and what each part does
- [docs/phase2-guide.md](docs/phase2-guide.md): voice mode, response times, barge-in, noise, read-back checks
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
```

Each run prints every call (conversation, tool calls, database result, judgement) and writes the same to `runs/<run_id>/transcript.txt` and `runs/<run_id>/report.html`. Voice calls also save `audio/call.wav` (left caller, right agent). Exit codes: 0 GO, 1 NO_GO,
2 NO_GO (not enough evidence), 3 the framework itself failed.
