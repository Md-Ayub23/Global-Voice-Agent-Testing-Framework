# Phase 7: load testing

Phase 7 answers "does the agent still answer fast and correctly when many people call at once?"
for any agent, with a GO / NO-GO for that load and the number of calls at once it can take.

On Windows computers that block the `vatf.exe` launcher (Application Control), type every command
below as `uv run python -m vatf ...`.

## Module flow

```
profile `load` section (Test › Load test, or by hand)
  -> load.py        settings (defaults, checks), stages (ramp / spike / soak / steady), which tests
  -> loadrun.py     LoadOrchestrator: keeps N calls in progress per stage; every call is a normal
                    call (orchestrator.run_call) with its own verdict, checks and call quality metrics.
                    HarnessMonitor samples Vocto's own event-loop lag; load_live.json every second.
                    The tool gateway routes each tool call to its call by the test id.
  -> loadstats.py   per call: ok / failed (couldn't connect, agent error, stopped answering, ran too
                    long) / Vocto error; per stage: failed %, p50-p99, pass rate, must-never findings,
                    call quality; capacity, the breaking point, slowdown / pass drop / soak drift
                    -> load.json
  -> decision.py    apply_load: load_* rules (NO-GO, not enough evidence or warnings)
  -> snapshot.py / compare.py   a separate load baseline (baselines/<profile>-load.json), compared
                    level by level; then CI reports and alerts as in Phase 6
```

## Shapes

| Shape | What it does | Use it to |
|---|---|---|
| ramp | `start_at` up to `calls_at_once` in `steps` levels, `step_minutes` each | find the capacity |
| spike | `start_at` for `before_minutes`, `calls_at_once` for `minutes`, `start_at` for `after_minutes` | check it copes with a sudden rush and recovers |
| soak | `calls_at_once` for `minutes`, read in windows of `window_minutes` | find slow leaks (answers slower over time) |
| steady | `calls_at_once` for `minutes` | a plain check at the busiest time you expect |

Each simulated caller makes one test call after another (closed loop): a stage keeps its level of
calls in progress, new calls start 0.25 s apart (all at once in a spike), `pause_seconds` adds a
wait between a caller's calls, and `max_calls` caps the whole test. The tests are taken in turn.
With none picked, a load test uses the channel's tests without planted tool faults (they slow the
agent on purpose) and without safety attacks.

```yaml
load:
  shape: ramp
  calls_at_once: 10
  start_at: 1
  steps: 4
  step_minutes: 2
  caller: free          # free: scripted lines + rule-based caller (no Claude spend); claude
  judges: false         # AI judges on every load call cost money
  start_agent: false    # start the agent with agent_process first
  limits:
    max_failed_pct: 2   # failed calls per stage
    p95_ms: null        # empty: voice uses the release p95 (thresholds.latency_p95_ms); text has none
    slowdown:  {blocks: false, pct: 50, ms: 300}
    pass_drop: {blocks: false, points: 20}
    drift:     {blocks: false, pct: 50}
    recovery:  {blocks: false, pct: 25, ms: 200}   # spike: back to normal speed after the rush
    max_lag_ms: 100     # Vocto's own lag: above it a stage is "not enough evidence"
    min_calls: 3
```

## The decision

Every stage runs at or below the target, so every stage must meet the limits:

- **NO-GO** when a stage has more failed calls than `max_failed_pct`, an answer time p95 over the
  limit, a call quality metric set to block in the bad band, or a must-never finding: saying
  success when the action failed (or the reverse), a wrong value saved, a duplicate or unexpected
  action, a forbidden phrase, a secret, a wrong tool order or a wrong read-back.
- A test that fails for a reason load can cause by itself, like a slot already taken because many
  calls share one copy of the test data, only lowers the pass rate.
- **Not enough evidence** when a stage had fewer than `min_calls` finished calls, when Vocto's own
  computer fell behind (lag p95 over `max_lag_ms`) or when more than 10% of calls failed because of
  Vocto itself. It never blames the agent for Vocto's limits.
- **Recovery after a spike**: Vocto compares the agent's answer times after the rush with the ones
  before it. "Normal" is the p95 before the spike plus `pct` percent of it or `ms`, whichever is more.
  The agent has recovered once 3 answers in a row are back at normal speed and they stay there (p95 of
  the answers from then on), with no more failed calls than before. The report shows how many seconds
  that took (*Recovery after the spike*); if it never got back, it says so and why.
- Slowdown (top level against the lowest), pass drop, soak drift and recovery are warnings; switch
  Blocks on to make them NO-GO too.
- **Capacity** is the highest level (calls at once) where every stage at that level and below met
  the limits; the report also names where it broke and why.

Tool calls: with many calls at once, the gateway routes each tool call by the test id header
(`test_id.header_name`, default `X-Test-Id`) or a `test_id` field. A tool call with no id goes to a
shared fallback and is counted; tool checks then give "not enough evidence", never a false NO-GO.
The Load test page warns when the agent isn't told the test id.

Test data (backend fixtures) is loaded once, before the first call: many calls share one copy, as
callers share a real system.

## Running it

```
uv run python -m vatf load examples/load-ref --start-agent
uv run python -m vatf load <agent folder> --shape spike --calls 20 --minutes 2
uv run python -m vatf load <agent folder> --ci --junit load.xml --summary load.md
uv run python -m vatf baseline <agent folder> --load            # show the load baseline
```

Options override the profile for one run: `--shape`, `--calls`, `--minutes`, `--step-minutes`,
`--max-calls`, `--caller free|claude`, `--scenario` (repeatable), `--speech local|codec`,
`--offline`, `--no-judges`, `--conditions`, `--start-agent`, `--baseline auto|none|<file>`.
Exit codes as for a run: 0 GO, 1 NO-GO, 2 not enough evidence, 3 Vocto couldn't run it.

In the dashboard: **Test › Load test** sets the shape, size, callers, tests and limits (a chart shows
the plan as you type), saves them to the profile and starts the test. The Live page shows the calls
at once over time and the latest calls. The run report opens on a **Load** tab: capacity, failed
calls, p95 per stage against the limit, the stage table, warnings and notes. Results has a "load
test" chip and filter. **Set as baseline** on a load run sets the load baseline; later load tests
show a **Vs load baseline** tab (p95, failed calls and pass rate per level, and capacity).

## The reference agent

`zoo/load_ref.py` (profile `examples/load-ref`) is one shared voice server whose answer delay is
known exactly: 300 ms + 250 ms for each extra call in progress, and it refuses calls above 6. With
the p95 limit of 1000 ms its capacity is exactly 3 calls at once. A ramp to 5 gives capacity 3 and
NO-GO at 4 and 5 (1,054 and 1,305 ms); steady at 3 gives GO (804 ms); steady at 8 gives NO-GO with
"couldn't connect" failures.

Each call to a `launch` or `text_python` agent starts its own copy of the agent, so a load test
there measures many copies, not one server; the page and the report say so. Point the connection
at a deployed agent to load-test the real thing.

## Checked

`tests/test_phase7.py` (18 tests): settings and their checks, stages for every shape, which tests
are used, gateway routing by test id (and calls with no id), call outcomes, must-never findings,
stage statistics and capacity, answers counted in the stage they happened in, soak drift, the
load baseline comparison, the reference agent end to end (capacity exactly 3), the clinic agent
(GO, and double booking NO-GO with the baseline, CI reports and alerts), the CLI refusing bad
settings, snapshots, CI reports and the dashboard API.
