# Writing tests automatically (Phase 4)

Vocto can write a draft test plan for any agent from what the agent is told to do. You review the drafts
and tick the ones to keep; only those become scenario files. This follows the public design of tools such
as Hamming (tests written from the agent's prompt, a fixed mix of test kinds, a persona library), built
Vocto's own way: every draft is checked by the same rules as a hand-written test, and nothing is trusted
until a person ticks it.

## How to use it

Dashboard: **Set up > Test plan > Write tests for me**.

1. Paste the agent's instructions (its system prompt) and save. The profile's tools, database checks and
   the judges' business rules (Set up > AI judges) are read too.
2. Pick how many tests (default 20) and, if you like, something to focus on.
3. **Write the plan**. Claude (Opus 5.5 by default) takes one to three minutes; a 20-test plan costs about
   $0.30 to $0.80.
4. Read **What Claude understood**: the agent's jobs, its rules (R1, R2, ...) and what the instructions
   don't say. If a rule is wrong, fix the instructions and write again.
5. Open any draft to see what the caller is told, its details, the database check and the notes. Change a
   test's criticality in its dropdown. Untick anything you don't want.
6. **Add N tests to the plan**. The files go into `scenarios/gen-*.yaml` (with a comment naming the rules
   they test), and any library persona they use is added to `personas/`.

Command line, same thing:

    uv run vatf plan examples/pipecat-food                # writes the plan, prints it, saves plans/latest.json
    uv run vatf plan examples/pipecat-food --count 30 --focus "callers who change their order"
    uv run vatf plan examples/pipecat-food --add          # also saves the drafts that passed the checks

The profile keeps the instructions in `agent_prompt:`.

## What it writes

| Share | Kind | Examples |
| --- | --- | --- |
| 40% | Normal calls | the main job, the same job with other valid choices |
| 30% | Edge cases | wrong or missing details, vague requests, changing topic, contradicting itself, out-of-scope asks, asking for a person, cancelling halfway |
| 15% | Tool failures | Vocto breaks a tool the call uses (error, timeout, slow, bad reply); nothing may be saved and the agent must say so |
| 10% | Misuse | another person's data, skipping verification, forbidden actions, pressure (general attacks are the safety tests' job) |
| 5% | Noise and interruptions | background noise, cutting in on the agent, fast speech (voice runs only) |

Every draft is a simulated call: Vocto's AI caller gets a persona, a goal and made-up details. Where the
call should change the database, the draft carries the outcome check (said vs did vs stored), using the
database check and the success / failure words your own tests already use for that tool.

## The checks Vocto makes on every draft (no AI)

- **Schema and references**: the same checks as `vatf validate` (tools, probes, personas, fields).
- **Traced to the instructions**: a test that names no rule (and isn't a plain normal call of a known job)
  runs as *exploratory*, so it can never block a release. *Critical* is kept only for tests of a must or
  must-not rule (or the main job).
- **Sound expectations**: if the draft breaks the very tool that saves the record, its expected result
  becomes an honest failure with nothing saved. Database fields the check doesn't return are dropped.
- **Duplicates**: a draft that reads like an existing test (or an earlier draft) is shown but unticked.
- **Shared setup**: the database fixtures and fixed "now" time most of your tests use are copied in.

## The persona library

Twelve ready-made callers, added from **Personas > Add from library** or automatically when a ticked draft
uses one: clear and polite, impatient, angry, confused, rambling, slow and careful, fast talker,
interrupts, changes topic, won't give details, repeats questions, speaking a second language. They use the
three local Piper voices. They describe behaviours to test against, not real groups of people. Once added,
they are normal persona files you can edit.

## Limits

- Claude writes the plan, so it needs an API key (Settings). Running the tests afterwards works as before.
- The drafts are only as good as the instructions. Read the rules list before adding tests; a test of a
  rule the agent was never given will fail for the wrong reason.
- Expected database values (for example `size: large`) are Claude's guess at how the agent stores them.
  Check them in the draft before adding a critical test.
- Multi-call tests (the same caller rings back and the agent must remember) and silent callers come in a
  later phase.
