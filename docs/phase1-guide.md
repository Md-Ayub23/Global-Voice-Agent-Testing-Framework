# Phase 1: text mode end to end

Phase 1 fills in the boxes that Phase 0 drew, for text mode. A run now really
calls the agent, records everything, checks it and gives a release decision.

Exit criterion (from the research report): each clinic bug switch is caught
in every repeat, and the clean agent gives zero false alarms across 3 repeats.
`vatf exit-check` tests exactly that.

## Running it

From the repo folder, in PowerShell:

```powershell
uv sync
uv run vatf validate examples/clinic      # check the project files
uv run vatf run examples/clinic           # full run (needs ANTHROPIC_API_KEY)
uv run vatf exit-check examples/clinic    # clean agent + each bug switch
```

For real runs, copy `.env.example` to `.env` in the repo folder and paste your
Anthropic API key after `ANTHROPIC_API_KEY=`. vatf reads it at start-up; a key
already set in the shell wins. `.env` is in `.gitignore`, so it is never committed.

Useful options for `run` and `exit-check`:

| Option | What it does |
|---|---|
| `--show-pii` | Keep personal data readable in the saved run (for checking only; the run is marked "NOT masked"). |
| `--offline` | No API calls at all. A rule-based caller answers from the fact sheet, the validator is skipped, and the profile's `connection.offline_options` are applied (for the clinic: `brain: rules`). A smoke test, not a real test. |
| `--agent-opt brain=rules` | Use the clinic agent's deterministic brain instead of Claude (Claude still plays the caller unless `--offline`). |
| `--agent-opt bugs=lie_on_failure` | Turn on a bug switch (`lie_on_failure`, `skip_confirmation`, `double_booking`; comma-separate several). |
| `--scenario ID` | Run one scenario only (repeatable). |
| `--repeats N` | Override the profile's 3 repeats. |
| `--model ID` | Caller and validator model (default `claude-haiku-5-5`). |

A full real run is 9 calls (3 scenarios x 3 repeats). With Claude Haiku 5.5 as
caller, validator and clinic agent it should cost a few cents. The profile's
`budget_usd` caps it; when the cap is reached the remaining calls stop as
harness errors.

## Reading the output

`vatf run` prints every call in plain English, then the decision:

- **Conversation and actions**: each line says who acted. `CALLER` is the simulated customer, `AGENT` is the agent
  under test, `AGENT uses tool` / `tool answer` are its tool calls as the proxy saw them, `vatf breaks tool` is an
  injected fault, and `database check` is the backend probe after the call.
- **Database after the call**: the rows the probe found for this call's test id.
- **How it was judged**: expected outcome, what the agent said, what it did, what was stored, and the outcome class.
- **Checks**: every check with PASS / FAIL / UNSURE / skip and the event numbers it is based on.

The same text is saved as `runs/<run id>/transcript.txt` (and `story.txt` in each call folder), and
`report.html` shows it as tables with failed calls opened. Add `--quiet` to print only the decision.
Add `--show-pii` to keep names, dates of birth and ids readable in the saved run, so you can check that
the right values went in and came back. Such a run is marked "NOT masked"; `runs/` is git-ignored, but don't
share it.

## What happens in one call

```
load fixtures -> open the agent -> greeting
loop: caller speaks -> agent answers (tool calls go through the proxy)
call ends -> validator checks the caller -> backend probe -> lock the evidence
-> hard checks + outcome check -> verdict.json
```

1. **Fixtures** (`setup.fixtures`) reset the clinic database, so every call starts clean.
2. **Connector** (`text_python`) imports the agent named in `connection.target` and talks to it in plain text.
3. **Tool proxy** sits between the agent and its tools. It records a `tool_request` and `tool_response` for every call, adds the `X-Test-Id` header, and injects the scenario's faults (timeout, HTTP error, slow, broken response). `forward: true` sends the request to the backend before returning the fault.
4. **Caller**: Claude plays the persona with the goal and fact sheet. It only sees what the agent said. Scripted scenarios use their fixed lines instead.
5. **Validator**: after the call, Claude checks that the caller kept to its facts and goal. If not, the call is a harness ERROR and is rerun (up to `max_harness_retries`).
6. **Backend probe** runs the profile's SQL to see what was really stored for this test id.
7. **Lock**: PII is masked (see below) and `events_sha256` seals the timeline.
8. **Evaluation**: hard checks, then the outcome check, then the validator result, into `verdict.json`.

## The outcome check

The heart of the framework: compares what the agent **said**, what it **did**
(write tool calls and their results) and what was **stored** (backend probe).

| Class | Said | Did / stored |
|---|---|---|
| TRUE_SUCCESS | success | write ok, record correct |
| HONEST_FAILURE | failure | no record |
| FALSE_SUCCESS | success | write failed or no record |
| FALSE_FAILURE | failure | record exists |
| SILENT_DATA_BUG | success or nothing | write ok, record missing or wrong |
| DUPLICATE_ACTION | any | the same write succeeded twice (idempotency key), or extra records |
| UNEXPECTED_ACTION | any | something written when nothing should be |
| NOT_VERIFIED | unclear | or no backend evidence: never counted as a pass |

"Said" comes from the scenario's regex patterns, read from the agent's turns
after the last write call, so a retry is judged on how it ended. A turn that
matches both lists, or none after a write, is "unclear" and gives NOT_VERIFIED.
In a failure scenario, a call where the agent never attempted the write is
NOT_VERIFIED (the fault was never reached), never a pass.

## Hard checks

`tool_called`, `tool_not_called`, `tool_order`, `probe`, `said`, `not_said`,
`max_turns`, `entity_fidelity` and `handoff` run in Phase 1. Scripted turn checks
look only at their own turn and get ids `turn<n>.<i>`. `latency` is
NOT_APPLICABLE in text mode (it gates only in voice mode), and `judge` items are
skipped as NOT_APPLICABLE until Phase 3. If the agent raises an error mid-call,
the blocker check `agent-did-not-crash` fails.

`entity_fidelity` compares each caller fact with the tool arguments and the
backend record of the same name. Case, dots, spacing and a leading title do not
matter ("Patel" equals "Dr Patel"). The speech leg (what our STT heard) comes in
voice mode.

## PII masking

When a call is locked, every value of an argument listed in a tool's `pii_args`
(in tool requests, responses and probe rows) is replaced by a salted hash such
as `[masked:8bb543ace231]`. The same value always gives the same hash within a
call, so the checks can still compare names and ids without storing them.
Free text (what the caller and agent said) is masked too: the caller's personal facts
and the personal values the agent sent to its tools become `[full_name]`,
`[date_of_birth]` and so on, including first or last name alone and spoken dates
("14th of March, 1985"). The profile's `pii.mask_patterns` are applied as well. The llm/ folder
keeps the raw prompts, so treat it as sensitive.

## The clinic agent (zoo/clinic)

An ordinary agent, not framework code. Two brains:

- `llm` (default): Claude Haiku 5.5 with tool use.
- `rules`: a deterministic state machine. Free and repeatable, used by the tests.

Bug switches: `lie_on_failure` (a failed booking is reported to the brain as a
success), `double_booking` (every booking request is sent twice) and
`skip_confirmation` (books as soon as a time is picked). The first two live in
the agent's tool layer, so they behave the same with either brain.

Which scenario catches which bug:

| Bug | Caught by | How |
|---|---|---|
| lie_on_failure | clinic.book-checkup.tool-timeout | outcome FALSE_SUCCESS |
| skip_confirmation | clinic.confirm-before-booking | turn2.1: booking called before the caller said yes |
| double_booking | clinic.book-checkup | outcome DUPLICATE_ACTION |

## Writing your own text agent

Point `connection.target` at `module:function`. The function is called as
`build_agent(tools=..., session=..., options=...)` and returns an object with
`start() -> str`, `reply(text) -> str` and optionally `ended: bool`. Call tools
as `tools.call(name, **args)`; you get `{"ok", "status", "body"}` back. Tool
upstreams can be `python:module:function` handlers or `http(s)://` URLs.

## Known limits of Phase 1

- Text mode only: no audio, so no latency gate, no barge-in, no STT errors.
- Only SQLite backends for probes and fixtures.
- Calls run one at a time (`parallel_calls` is not used yet).
- Judge items are skipped (Phase 3).
- `--offline` checks the plumbing, not agent quality: the rule-based caller only handles the clinic's simple questions.
