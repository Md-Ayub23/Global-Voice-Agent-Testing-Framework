# Test modes and the release readiness report

One agent, several kinds of testing. Each **test mode** answers one question, runs its own tests and
writes its own report. Every mode uses the same real calls and checks every call in full (tools, data
saved, what was said, timing, AI judges), so a good-sounding call can never hide a wrong action. What a
mode changes is which tests run and what the report leads with.

In the dashboard: open an agent, then **Test › Test modes**. From the terminal:

```powershell
uv run python -m vatf run examples/clinic --test-mode safety
uv run python -m vatf run examples/clinic --profile profile-voice.yaml --test-mode speech --conditions phone
```

| Mode | The question | What runs |
|---|---|---|
| **Full check** | Is the agent ready to release? | Your test cases and the safety tests in one run. Recommended before every release. |
| **Functional** | Does the agent do the job it was built for? | Your test cases: task done, right tool with the right details, right data saved, honest about what it did. |
| **Timing & turns** | Does a call feel natural? | Voice only. Answer times (typical and slowest), cut-ins, "mm-hm", pauses and silence. |
| **Speech & accents** | Does it understand different callers on real phone lines? | Voice only. Your test cases under harder caller conditions (phone line by default; noisy and hard too). |
| **Safety & security** | Can callers trick it into something harmful? | Only the attack calls written for this agent (Set up › Safety tests). |
| **Resilience** | What happens when your systems fail during a call? | Only the test cases that break a tool on purpose (timeout, error, slow, rubbish answer). |
| **Load & capacity** | How many calls at once can it handle? | The load test (ramp, spike, soak, steady). See [phase7-guide.md](phase7-guide.md). |
| **Regression** | Did the latest change make anything worse? | Your full test set, compared with the run saved as the baseline. Needs a baseline first. |
| **Real calls** | How is it doing with real callers? | Opens Quality › Real calls. See [phase8-guide.md](phase8-guide.md). |

A mode that can't run yet says why on its card (no voice channel, no test case that breaks a tool, safety
tests switched off, no baseline) with a link to fix it.

## The release readiness report

Every run gets a report in plain words instead of a bare code:

| Verdict | Means |
|---|---|
| **Ready to release** | Every check that counts passed, and every key test case passed every repeat. |
| **Ready, with things to watch** | Nothing blocks the release; a few problems to look at before the next one. |
| **Not ready: N blocking issues** | Something that blocks the release failed. The first thing to fix is named. |
| **Not enough evidence yet** | Too few calls gave a clear answer to decide either way. |
| **No result** | The run stopped before the end. |

A focused mode (Functional, Timing, Speech, Safety, Resilience, Load) tests one thing, so a clean run
reads **Passed the … tests**, never "Ready to release", and says to run the Full check before a release.
The codes `GO`, `NO_GO` and `NO_GO_INSUFFICIENT_EVIDENCE` and the exit codes (0, 1, 2) stay the same in
`decision.json`, CI and the API; the terminal also prints a `RESULT:` line in plain words.

The report has:

- **Executive summary**: what was tested, the verdict, the top problems, answer times, safety.
- **Scorecard** by area (task success, actions and data, conversation, speed, speech, safety, failures,
  load, change since baseline, test evidence). Each area stands on its own: there is no overall score, so
  one serious problem can't be averaged away.
- **Findings**: each problem once, with how often it happened, what should happen, who to look at (the
  agent, the test set-up, or the judges' opinion) and **proof**: quotes from the calls with the moment
  they happened. A quote opens the call at that moment.
- **Numbers & coverage**: the key numbers with their limits, which test cases, attacks, tools and
  conditions were covered and which weren't, and how the run was made.

*PDF report* opens the whole report laid out for paper; choose *Save as PDF* in the print window.
*CSV* downloads the verdict, scorecard, findings and numbers for a spreadsheet.
