# vatf: Voice Agent Testing Framework

A local-first framework that tests any voice agent as a black box. It simulates
callers, watches the voice, the tool calls and the backend state, scores each
call with hard checks and calibrated AI judges, and gives a GO / NO-GO release
decision.

**Status: Phase 1 (text mode end to end).** The framework runs real test calls
against the clinic example agent in text mode, checks what the agent said
against what it did and what was stored, repeats each scenario 3 times and
decides GO / NO-GO. Voice is Phase 2, calibrated AI judges are Phase 3.

- [docs/phase1-guide.md](docs/phase1-guide.md): how to run it and what each part does
- [docs/phase0-contracts.md](docs/phase0-contracts.md): the file formats and interfaces

```
schemas/                 file formats (JSON Schema, written as YAML)
src/vatf/                the framework (contracts/ holds the Phase 0 interfaces)
zoo/clinic/              test agent #1: clinic booking agent with bug switches
examples/clinic/         clinic profile, personas, scenarios, a hand-written sample run
tests/                   framework tests (run offline, no API key needed)
docs/                    plain-English guides
```

## Quick start (Windows PowerShell)

Needs Python 3.12+ and [uv](https://docs.astral.sh/uv/). Run everything from the repo folder.

```powershell
uv sync                          # install
uv run pytest                    # framework tests, offline

# Free dry run: rule-based caller and agent, no API calls.
uv run vatf exit-check examples/clinic --offline

# Real run: Claude plays the caller and the clinic agent.
# Put your key in a .env file first (copy .env.example to .env); git ignores .env.
uv run vatf run examples/clinic
uv run vatf run examples/clinic --agent-opt bugs=lie_on_failure   # should be NO_GO
```

Each run prints every call (conversation, tool calls, database result, judgement) and writes the same to `runs/<run_id>/transcript.txt` and `runs/<run_id>/report.html`. Exit codes: 0 GO, 1 NO_GO,
2 NO_GO (not enough evidence), 3 the framework itself failed.
