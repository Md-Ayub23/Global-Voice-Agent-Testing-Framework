# vatf: Voice Agent Testing Framework

A local-first framework that tests any voice agent as a black box. It simulates
callers, watches the voice, the tool calls and the backend state, scores each
call with hard checks and calibrated AI judges, and gives a GO / NO-GO release
decision.

**Status: Phase 0 (contracts only).** No test runs yet. This repo currently
holds the file formats, the component interfaces and hand-written examples.
Start with [docs/phase0-contracts.md](docs/phase0-contracts.md).

```
schemas/                 file formats (JSON Schema, written as YAML)
src/vatf/contracts/      interfaces between the layers (Python Protocols)
examples/clinic/         clinic agent profile, personas, scenarios, a sample run
tests/                   checks that the examples match the schemas
docs/                    plain-English guides
```

Run the checks (Python 3.12+, [uv](https://docs.astral.sh/uv/)):

```
uv run --group dev pytest
```
