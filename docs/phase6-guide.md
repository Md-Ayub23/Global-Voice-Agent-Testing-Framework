# Phase 6: baseline, regression gate and CI

Phase 6 answers "is this version worse than the last good one?" for any agent, and runs the same
check in CI, on a schedule, and from the dashboard.

On Windows computers that block the `vatf.exe` launcher (Application Control), type every command
below as `uv run python -m vatf ...`. CI files use that form too.

## Module flow

```
run (orchestrator) -> decision.json + run.json
      -> snapshot.py   the numbers worth comparing (tests, scores, p50/p95, call quality bands,
                       judge pass rates, safety findings, how the run was made)
      -> compare.py    snapshot vs the baseline's snapshot (baselines/<profile>.json)
                       -> comparison.json, and a copy of the baseline used (baseline.json)
      -> decision.py   apply_regression: rule `regression` (NO-GO) / `regression_warnings`
      -> cireport.py   JUnit XML + Markdown (also $GITHUB_STEP_SUMMARY)
      -> alerts.py     Slack / Discord / webhook (alerts.json)
health.py + web/schedule.py start short runs on a schedule; ciconfig.py writes CI files.
```

## Baseline

A baseline is a snapshot of a known-good run, saved in the agent's folder as
`baselines/<profile file name>.json` (e.g. `examples/clinic/baselines/profile-voice.json`). It is
small and self-contained, so commit it with the agent: CI then compares against it without the old
run. One baseline per profile, because text and voice runs are measured differently.

- Dashboard: Results › open a good run › **Set as baseline** (or the Results page's Baseline card).
- Terminal: `uv run python -m vatf baseline set <run_id>`, `... baseline show <project>`, `... baseline clear <project>`.

Runs with bug switches or planted bugs can't be the baseline. A dry run or a NO-GO run can, with a warning.

## The regression gate

Every run is compared with the baseline when there is one (`--baseline auto`, the default;
`--baseline none` turns it off; `--baseline FILE` uses another snapshot). Changes:

| Change | Default | Limit (Pass rules › Regression gate) |
|---|---|---|
| New failure: a test that passed every repeat in the baseline fails now | **blocks** | |
| Slower answers: p50 or p95 rose by more than both limits | warns | 20% and 150 ms |
| Lower scores: Accuracy, Experience, Security or Safety | warns | 10 points |
| Call quality: a metric moved to a worse band | warns | |
| AI judge item pass rate fell | warns | 20 points |
| New safety finding (both runs had safety tests) | warns | |

Only new failures block by default: with 3 repeats, small timing and score changes are mostly
noise. Runs made differently (mode, caller conditions, speech engine, dry run vs live) are "not
like-for-like": they are compared and shown but never blocked. The exit check never compares (it
proves each bug is caught on its own). Relabelling a run's judge items compares again with the same
baseline the run used.

Profile section:

```yaml
regression:
  enabled: true
  new_failures: {blocks: true}
  latency: {blocks: false, increase_pct: 20, increase_ms: 150}
  scores: {blocks: false, drop_points: 10}
  metrics: {blocks: false}
  judges: {blocks: false, drop_points: 20}
  safety: {blocks: false}
```

Compare any two runs: dashboard **Quality › Compare runs**, or `uv run python -m vatf compare <run_a> <run_b>`
(exit code 1 when something would block).

## CI

```
uv run python -m vatf run <project> --profile <file> --ci --quiet --junit reports/vocto.xml --summary reports/vocto.md
```

Exit codes: 0 GO, 1 NO-GO, 2 not enough evidence, 3 Vocto itself failed. `--junit` writes one test
case per call plus "decision" and "vs baseline" cases; `--summary` writes Markdown, and on GitHub
Actions the summary is also added to the job's page. **Set up › CI & alerts** writes a ready file for
GitHub Actions, GitLab CI or Jenkins for this agent, with the secrets it needs (only the `${NAME}`
values the profile uses, plus the Claude key unless it is a dry run). Nothing is added to the
repository's `.github` folder by Vocto. Agents under `projects/` are kept on this computer only
(git-ignored), so commit the agent's folder before CI can test it.

## Alerts

```yaml
alerts:
  - name: Team channel
    type: slack            # slack (also Google Chat, Mattermost), discord or webhook (JSON with everything)
    url: ${SLACK_WEBHOOK_URL}
    events: [no_go, regression, recovered]   # also: insufficient
```

Sent after every run (dashboard, terminal, CI, health check), except runs with bug switches. A failed
alert is recorded in the run's alerts.json and never changes the result. **Send test** on the CI &
alerts page checks an address before you save it.

## Health checks

```yaml
health_checks:
  enabled: true
  every_minutes: 60
  scenarios: []        # empty = the critical tests (all, when none is critical)
  repeats: 1
  conditions: as_written
  judges: false
  start_agent: false   # true: start the agent for each check (agent_process)
```

The dashboard starts one when due, while it is open and no other run is going. For round-the-clock
checks use the CI file's daily schedule, cron, or Windows Task Scheduler with
`uv run python -m vatf health <project>`. Health runs show a "health check" chip and a filter in Results.

## Checked

`tests/test_phase6.py` (20 tests): comparison rules and limits, not like-for-like, safety findings,
the decision and its schema, an offline clinic run end to end (baseline, bug run NO-GO with one new
failure, clean run GO, `--baseline none`, compare exit code), refusal of bug runs, relabelling, the
exit check, health runs, JUnit/Markdown, alerts (events, bug runs, recovered, errors), CI files
(parsed as YAML) and the dashboard API.
