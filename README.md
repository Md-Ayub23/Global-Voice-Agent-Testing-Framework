# vatf: Voice Agent Testing Framework

A local-first framework that tests any voice agent as a black box. It simulates
callers, watches the voice, the tool calls and the backend state, scores each
call with hard checks and calibrated AI judges, and gives a GO / NO-GO release
decision.

**Status: Phase 2 (voice mode).** The framework runs real test calls against the
clinic example agent, in text or by voice: the simulated caller speaks (Piper),
listens (Whisper) and hears only what came down the line. It checks what the agent
said against what it did and what was stored, measures response times on the call
recording, repeats each scenario 3 times and decides GO / NO-GO. Calibrated AI
judges are Phase 3.

- [docs/phase1-guide.md](docs/phase1-guide.md): text mode, how to run it and what each part does
- [docs/phase2-guide.md](docs/phase2-guide.md): voice mode, response times, barge-in, noise, read-back checks
- [docs/voice-protocol.md](docs/voice-protocol.md): how vatf talks to a voice agent over a WebSocket
- [docs/phase0-contracts.md](docs/phase0-contracts.md): the file formats and interfaces

```
schemas/                 file formats (JSON Schema, written as YAML)
src/vatf/                the framework (contracts/ holds the Phase 0 interfaces)
zoo/clinic/              test agent #1: clinic booking agent with bug switches (text and voice)
zoo/voice/               voice front-end that puts a zoo text agent on the phone
zoo/latency_ref.py       answers after known delays: the measuring stick for latency
examples/clinic/         clinic profiles (text, voice), personas, scenarios, a hand-written sample run
examples/latency-ref/    timing check for vatf's own response-time numbers
tests/                   framework tests (run offline, no API key needed)
docs/                    plain-English guides
```

## Quick start (Windows PowerShell)

Needs Python 3.12+ and [uv](https://docs.astral.sh/uv/). Run everything from the repo folder.

```powershell
uv sync                          # install
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
