# Phase 0: the contracts, in plain English

Phase 0 writes down the shapes of every file and every interface before any
real code exists. If these are right, each later phase is "fill in one box"
instead of "redesign everything". Nothing here runs a test yet.

Exit criterion (from the research report): the clinic profile and one
clinic scenario are written by hand and reviewed together. They are in
`examples/clinic/`.

## The chain everything hangs off

```
Run  ->  Scenario x Persona x Repeat  ->  Call (test_id)
     ->  Timeline events  ->  Check results (cite events)  ->  Call verdict
     ->  Run decision (GO / NO_GO / NO_GO_INSUFFICIENT_EVIDENCE)
```

Every number in the final report can be followed back down this chain to
the exact event that caused it.

## The files, one by one

| File | Who writes it | Layer | Schema |
|---|---|---|---|
| `profile.yaml` | a person, once per agent | L1 | `schemas/client_profile.schema.yaml` |
| `personas/*.yaml` | a person (later: generated as a grid) | L2/L3 | `schemas/persona.schema.yaml` |
| `scenarios/*.yaml` | a person (later: generated from the profile) | L2 | `schemas/scenario.schema.yaml` |
| `calls/<test_id>/call.json` | the framework, per call | L6 | `schemas/call_record.schema.yaml` |
| `calls/<test_id>/events.jsonl` | the observers, during the call | L5/L6 | `schemas/trace_event.schema.yaml` |
| `calls/<test_id>/verdict.json` | the evaluators, after the call | L7 | `schemas/call_verdict.schema.yaml` |
| `decision.json` | the decision engine, per run | L8 | `schemas/run_decision.schema.yaml` |

People write YAML (easy to read and comment). The framework writes JSON
(easy for code). The schemas are JSON Schema, stored as YAML so they are
readable too.

### 1. Client profile (L1)

"How do I reach this agent, what tools does it have, and how do I check its
backend?" Key parts:

- `connection` - how to talk to it. The clinic agent starts as
  `text_python` (we import it directly) and becomes `websocket_audio` in Phase 2.
- `tools` - each tool is `read` or `write`. Write tools are what the outcome
  check follows. `idempotency_key` says which arguments make two calls "the
  same action", so we can spot double bookings.
- `backend.probes` - the SQL or REST queries we run after the call to see
  what really changed. If a client gives no backend access, set
  `access: none` and every backend-dependent check becomes NOT_VERIFIED,
  never PASS.
- `thresholds` - the release gate settings agreed from the research:
  3 repeats, all 3 must pass, latency p50 800 ms / p95 1500 ms, kappa 0.6.

### 2. Persona (L2/L3)

Who is calling. Kept separate from the scenario so the same scenario can be
run by a calm caller and a rushed, vague one. Traits are small fixed scales
(patience, verbosity, mood, clarity) so we can later generate personas as a
grid instead of writing them all by hand.

### 3. Scenario (L2)

One situation to test. Two styles:

- **simulated** - an LLM caller gets a persona, a goal and a fact sheet and
  has a free conversation. Realistic, costs LLM calls.
- **scripted** - fixed caller lines, with exact checks after each agent
  reply. Fast, cheap, no caller LLM. (The LiveKit / Pipecat style.)

A scenario can contain:

- `setup.faults` - errors the tool proxy injects (timeout, HTTP 500, slow,
  broken response). `forward: true` means the backend really saved the
  booking but the agent was told it failed, which tests a nasty edge case.
- `outcome` - the heart of the framework. It says which write tool is the
  "action", which probe finds the record, and what the agent's claim should
  be (`success`, `honest_failure` or `no_action`). Values like `$doctor`
  point at the caller's fact sheet.
- `checks` - hard checks such as `tool_order` (look the patient up before
  booking) or `entity_fidelity` (did the name and doctor survive from
  speech to tool call to database), and `judge` items for soft qualities.

### 4. Run trace (L5/L6)

Each call gets a folder named by its test id:

```
runs/<run_id>/calls/<test_id>/
  call.json      who/what/when, plus the lock hash
  events.jsonl   the timeline, one event per line
  verdict.json   the evaluators' results
  audio/         recordings (voice mode)
  llm/           every LLM request and response, for cost and caching
```

Events are **raw evidence only**: what was said, which tool was called with
what, what the probe found. No opinions. Each event has a `seq` number so
verdicts can cite it, and a `t_ms` time on one shared call clock.

Two details that matter later:

- `agent_turn.heard_text` is what *our* STT heard. The caller simulator only
  ever sees this. The platform's own transcript is kept separately for
  comparison and is never shown to the caller.
- Audio events carry `start_ms` / `end_ms` from the recorder, so latency is
  measured from the audio itself, not from when our slow laptop STT finished.

When the call ends, PII is masked and `events_sha256` is written into
`call.json`. After that the evidence is locked: any change is detectable.

### 5. Verdict (L7)

One per call. It holds:

- `outcome` - the said / did / stored comparison and its class
  (TRUE_SUCCESS, FALSE_SUCCESS, SILENT_DATA_BUG, ...).
- `checks` - one result per check. Every PASS or FAIL **must** cite at least
  one event (the schema rejects it otherwise), and says which level of
  evidence it rests on (backend > tool traffic > platform > speech > AI opinion).
- `status` - rolled up as ERROR, then FAIL, then NEEDS_REVIEW, then
  NOT_VERIFIED, then PASS.

Rules built into the shape:

- A judge result is always `ai_opinion` evidence.
- A judge item whose kappa is below 0.6 has `can_block: false` (advisory).
- "AI can raise but never clear" is enforced by the `VerdictBuilder`
  interface: judges can add FAIL or NEEDS_REVIEW, never remove a FAIL.

### 6. Run decision (L8)

One per run. Checked in this order:

1. **NO_GO** if any blocking FAIL exists (false success, silent data bug,
   duplicate action, leak, security, or any `blocker` check).
2. **NO_GO** if a critical scenario failed any repeat (pass^3 not met), or
   latency p95 is over the limit.
3. **NO_GO_INSUFFICIENT_EVIDENCE** if coverage is below 90%, a critical
   scenario has too few verified repeats, or NEEDS_REVIEW items are open.
   This means "fix the harness or add tests", not "fix the agent".
4. **GO** otherwise.

Exit codes for CI: 0 GO, 1 NO_GO, 2 insufficient evidence, 3 the framework
itself crashed.

Accuracy and Experience are scored separately, always next to their coverage.

## The interfaces (`src/vatf/contracts/`)

Each box from the architecture talks to the others only through these.

| Interface | File | Layer | First implementation |
|---|---|---|---|
| `LLMClient` | `llm.py` | shared | Claude API adapter + scripted fake for tests |
| `TextToSpeech`, `SpeechToText` | `speech.py` | L3 | Piper, faster-whisper tiny (Phase 2) |
| `Connector`, `AgentSession` | `connector.py` | L4 | text_python (Phase 1) |
| `EventSink`, `Observer`, `ToolProxy`, `BackendProbe` | `observers.py` | L5 | HTTP tool proxy, SQLite probe |
| `CallerSimulator`, `SimulatorValidator` | `caller.py` | L3 | Haiku caller, Haiku validator |
| `EvidenceStore`, `CallWriter` | `evidence.py` | L6 | local folders |
| `Evaluator`, `VerdictBuilder` | `evaluation.py` | L7 | hard checks, outcome check |
| `DecisionEngine`, `ReportWriter` | `evaluation.py` | L8 | rules above, static HTML |
| `RunOrchestrator` | `orchestrator.py` | runner | plan, run, retry harness errors only |

The shared value types (results, outcome classes, events) are in `models.py`.

## Worked example

`examples/clinic/runs/r-20261102-0001/` is a hand-written run of the
timeout scenario against the clinic agent with its "lie on failure" bug
switched on. Read `events.jsonl` top to bottom: the booking call times out
at event 16, the agent says "You're booked" at event 17, and the probe finds
no appointment at event 21. The verdict classes this as FALSE_SUCCESS citing
exactly those three events, and the run decision is NO_GO.

## Decisions from the review (26 Oct 2026)

Ram asked for the most practical, workable answer to each open question.

1. **A real-looking bug inside an ERROR call.** The call is rerun as normal.
   Its findings stay in the report as warnings but never count toward the
   decision. Why: a broken harness can produce fake bugs, and a real bug will
   show up again in the rerun anyway, because every critical scenario is
   repeated 3 times.
2. **How we tell what the agent claimed.** Regex patterns in Phase 1. If the
   final reply matches neither the success nor the failure list (or matches
   both), the claim is "unclear" and the outcome becomes NOT_VERIFIED, never
   PASS. Why: our own zoo agents use known phrasing, so patterns work, and
   an unclear reply can never sneak through as a pass. In Phase 3 the judge
   is asked only about the unclear ones, which keeps LLM cost low.
3. **Latency in text mode.** Timings are recorded and shown for
   information, but they never gate. Latency gates only in voice mode,
   measured from the recorded audio. Why: text-mode timing includes no
   speech at all, so it would give false confidence.
4. **Finding the record without a test id column.** Probes can use
   `:test_id` or caller facts inside the call's time window
   (`:call_started_at`, `:call_ended_at`). Our zoo agents store the test id.
   Real clients that cannot store it are probed by, for example, name +
   doctor + time window. Why: it works for both cases, and the profile picks
   whichever one is available.
5. **Names for scripted checks.** Optional. Checks you don't name are given
   `turn<n>.<i>` ids (for example `turn2.1`), and you can still set your own
   id when you want a readable name in the report.
