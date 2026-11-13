# Test modes and the release readiness report

One agent, several kinds of testing. Each **test mode** answers one question, runs its own tests and
writes its own report. Every mode uses the same real calls and checks every call in full (tools, data
saved, what was said, timing, AI judges), so a good-sounding call can never hide a wrong action. What a
mode changes is which tests run and what the report leads with.

In the dashboard: open an agent, then **Test › Test modes**. From the terminal:

```powershell
uv run python -m vatf run examples/clinic --test-mode safety
uv run python -m vatf run examples/clinic --profile profile-voice.yaml --test-mode speech --sweep as_written,phone,noisy,quiet
uv run python -m vatf run examples/clinic --test-mode resilience --drills timeout,error,rate_limited,saved_lost
```

| Mode | The question | What runs |
|---|---|---|
| **Full check** | Is the agent ready to release? | Your test cases and the safety tests in one run. Recommended before every release. |
| **Functional** | Does the agent do the job it was built for? | Your test cases: task done, right tool with the right details, right data saved, honest about what it did. |
| **Timing & turns** | Does a call feel natural? | Voice only. Answer times (typical and slowest), cut-ins, "mm-hm", pauses and silence. |
| **Speech & accents** | Does it understand different callers on real phone lines? | Voice only. Every test case once under each caller condition you pick (clean line, phone line, noise, quiet or fast caller, bad signal, accents), side by side. See below. |
| **Safety & security** | Can callers trick it into something harmful? | Only the attack calls written for this agent (Set up › Safety tests). |
| **Resilience** | What happens when your systems fail during a call? | Your test cases again with a tool failing on purpose (the booking tool or the lookups, once or on every call, crashes, refusals or a dropped call), one kind of failure at a time, plus any test case that breaks a tool itself. See below. |
| **Load & capacity** | How many calls at once can it handle? | The load test (ramp, spike, soak, steady). See [phase7-guide.md](phase7-guide.md). |
| **Regression** | Did the latest change make anything worse? | Your full test set, compared with the run saved as the baseline. Needs a baseline first. |
| **Real calls** | How is it doing with real callers? | Opens Quality › Real calls. See [phase8-guide.md](phase8-guide.md). |

A mode that can't run yet says why on its card (no voice channel, no test case that breaks a tool, safety
tests switched off, no baseline) with a link to fix it.

## Functional

Every Functional call is checked on what the agent **said**, what its tools **did** and what was
**stored**, and now also on what else it touched:

- **Nothing else changed.** Vocto copies the watched tables right before the call (after the test data
  loads) and again after it, and compares them row by row. A new row the outcome check read, the number
  of new rows the test expects, or a row an expected write call named by id are fine; anything else (another
  customer's booking cancelled, a second order edited) is a blocking finding. Set it up on Connect ›
  Database: the tables to watch (default: the tables your probes read) and what may always change
  (`audit_log`, `bookings.updated_at`). A test can allow more with `allow_changes`. Read-only, and
  skipped for load tests. Try it: `uv run python -m vatf run examples/clinic --offline --agent-opt brain=rules --agent-opt bugs=cancel_other`.
- **Only expected writes.** Using a write tool the test doesn't expect is a finding (a blocker when it went
  through). A test can allow more with `allow_tools`.
- **Unclear claims read by AI.** When the agent's last words match neither the success nor the failure
  patterns ("Lovely, that's all sorted then"), two different AI models read them. Only when both give the
  same answer, each quoting words that really are in the call, does it count, marked "read by AI";
  otherwise the outcome stays Not verified. The AI never sees the tools or the database: whether the claim
  was true is still Vocto's own comparison.
- **Tasks with several steps.** `more_outcomes` lists further actions (book a table and add a note); each
  is checked like the first, as `outcome-2`, `outcome-3`.
- **The right answer.** `expected_answer` on a test case (opening hours, a price) asks the AI judges whether
  the agent's reply agrees with it, in any words.
- **What the test cases cover.** The Functional page lists the agent's jobs (normal, edge, misuse cases),
  its rules and its write tools (must work, must not be used), with the gaps. *Write tests for these* asks
  the plan writer for just the gaps.
- **Caller variations.** The sparkle button on a test case asks Claude to rewrite only the caller (other
  words, order, kind of caller). Facts, outcome and checks are copied by Vocto, so the variation is
  checked the same way; variations run as normal cases, never critical.
- **Report.** The Analysis tab leads with tasks done right (with its likely range), untrue claims, details
  saved right and other changes, then how each task ended (said, did, saved) and results per job.

## Timing & turns

Timing runs your test cases as voice calls and times everything from the recording, the way a caller
hears it. All timing numbers are measured by code; AI never times anything.

- **One answer-time rule.** The typical answer (p50) and the slowest 5% (p95) must be within the limits in
  Pass rules (800 ms and 1500 ms by default). p90 and p99 are shown next to them, never as a second
  limit. Below 20 timed answers the p95 says "too few to judge" and shows what is known ("at least 1.4 s");
  with enough answers it shows its likely range.
- **Plain answers and tool answers apart.** An answer that needed a tool (the agent called one in that
  turn) is timed apart from a plain answer. For tool answers there are two numbers: the first sound
  (often "let me check") and the full wait until the real answer, after the tool answered.
- **Coughs and door bangs.** The caller behaviour *Coughs, or a door bangs, while the agent talks* (persona
  `behaviour.noises`, on in the *Hard* preset and the *Caller who coughs* persona) plays a cough, a door bang
  or a clatter over the agent. It is not speech, so the agent should keep talking: **Kept talking through a
  cough or bang**.
- **Cut off in a pause.** When the caller stops to think mid-sentence (behaviour *pauses mid-sentence*), an
  agent that starts before the caller has finished is counted as **Cut the caller off in a pause**, apart from
  *talked over the caller*.
- **Said the same thing again.** Agent turns nearly the same as an earlier one (5 words or more), unless the
  caller asked it to repeat, went quiet, or repeated themselves. Text and voice. A stuck agent asking for the
  same details again and again shows up here.
- **Where the time went (optional).** If the agent's logs report its own speech recognition, AI and voice
  times, add one pattern per stage on the profile (`logs.timing.stt`, `llm`, `tts`, each with a group named
  `ms`, e.g. `llm_latency=(?P<ms>\d+)`). The report shows each stage's typical and slowest time.
- **Report.** The Analysis tab leads with typical wait, slowest waits (with range and number of answers),
  stops when cut in, keeps going through "mm-hm", and longest silence; then the wait spread split into plain and
  tool answers, slowest answers per test case, **moments to listen to** (the 5 slowest waits and slow stops,
  each opening the call at that moment), and where the time went.

```powershell
uv run python -m vatf run examples/clinic --profile profile-voice.yaml --test-mode timing --conditions hard
```

## Speech & accents

Pick the **conditions to compare** on the run page. Every test case runs once under each one, so 6 test
cases × 4 conditions is 24 calls (raise *Repeats per test and condition* to see how steady each one is).
Keep **As written** on: it is the clean result the others are compared with.

| Condition | The caller |
|---|---|
| As written | Each test's own audio and caller, unchanged. |
| Phone line | A narrowband phone line with 1% of packets lost. |
| Noisy | A busy cafe behind the caller (10 dB speech-to-noise). |
| Street noise | Traffic loud enough to compete with the caller (5 dB). |
| Quiet caller | Speaks softly (12 dB quieter) on a phone line. |
| Fast talker | Speaks about 30% faster than normal. |
| Bad mobile signal | A phone line that loses 5% of its audio packets. |
| Unsteady network | 80 ms of network jitter, with 1% of audio packets arriving out of order. |
| People talking nearby | Other voices in the background, 10 dB below the caller: the hardest noise for speech recognition. |
| Speakerphone in a room | The caller's voice echoes (about 0.6 s of reverberation). |
| Numbers on the keypad | Numbers of 6 digits or more (phone numbers, codes) are typed as touch tones instead of said. |
| Accents | The caller's voice is swapped for an accent (American, British, Scottish, Irish, Indian, or English spoken with a Hindi, Mandarin, Spanish, Arabic, Korean or Vietnamese first language). |

The report shows **Results by caller condition**: each condition with the calls passed, the details the
agent got right, answer times, and a status. *Handled* means every call passed; *Struggled* means some
failed; *Broke* means none passed. "Worse than clean" marks a condition that did worse than As written by
more than chance (Fisher's exact test); a smaller gap reads "no clear difference with this many calls".

**Details by type.** Every caller fact a tool received is checked as the caller said it and given a type
(name, phone number, date or time, email, address, code, amount). The report shows details right per type
and per condition, and lists the details saved wrong: what the caller said next to what was saved, each
opening the call at that moment.

**Fair test.** After a voice call, Vocto's own careful speech recogniser listens to the caller's side of the
recording: the audio exactly as the agent received it, after noise and the phone line. If it can't hear a
fact either, a wrong detail is marked **too hard to hear (test issue)**, not the agent's failure.

**Read back before saving.** Whether the agent read the caller's details back, and the caller said yes, before
the booking or order was saved. The habit that prevents most speech mistakes. Shown, never blocking.

**Find the breaking point.** On the run page, *Find the breaking point* runs up to 5 test cases (key ones
first) once at cafe noise 20, 15, 10, 5 and 0 dB and at 1, 3, 5 and 10% packet loss. The report says how far
the agent works ("works down to 5 dB") with a chart per step. These calls never decide the release.
From the terminal: `--sweep as_written,snr:20,snr:15,snr:10,snr:5,snr:0,loss:1,loss:3,loss:5,loss:10`.

**Synthetic or real voices.** The report always says whether the callers were synthetic voices (a best
case: machine voices are cleaner than people) or recordings of real callers.

**Hard-to-hear test cases.** On Speech › Test cases, *Write with AI* can now write details that are easy to
mishear on purpose: fifteen and fifty, the 13th and the 30th, letters like B, D, P and T in a code, and
uncommon names.
The executive summary and the Speech scorecard name the conditions that broke the agent.

**How many words the agent misheard.** If the agent reports what its speech recognition heard (the
`heard` message of the [voice protocol](voice-protocol.md), or a log line set in **Set up › Tools & data ›
Logs › What the agent heard**), Vocto compares it with what its caller really said and reports **Caller
words the agent misheard (WER)**, per call and per condition. A goodbye the agent never answered is left
out. Without either, the number is not shown: Vocto never guesses it. What the agent heard is masked like
the caller's words, so names and dates are not saved in plain text.

### Tamil callers

Vocto's caller speaks English or Tamil. A caller speaks the agent's language (**Set up › Connection ›
Language it speaks**) unless its persona sets its own (**Set up › Callers › Language**), so one agent can be
tested with English and Tamil callers side by side.

- **Voice**: two community Piper voices, Tamil female (`ta_IN-rasa_female-medium`, the default) and
  Tamil male (`ta_IN-rasa_male-medium`), trained on AI4Bharat's Rasa speech data (CC-BY-4.0). They
  download once, about 60 MB each, and also read English words and numbers mixed into Tamil the way
  Tamil speakers say them. Accents are English voices, so a Tamil caller doesn't use one.
- **The caller's words**: Claude plays the caller in everyday spoken Tamil, in Tamil script, keeping
  names, email addresses and reference codes exactly as in the test's facts. Cut-ins, "mm-hm"s and
  fillers are Tamil too ("ஒரு நிமிஷம்", "சரி", "அது வந்து"). The free dry-run caller only speaks
  English, so test a Tamil agent with a real run.
- **How Vocto hears the agent**: Whisper's English-only models (`base.en`, `small.en`) can't hear Tamil,
  so a Tamil call uses the multilingual model of the same size, at least `small` for the live call and
  `medium` for the careful second listen. Both download once. The run's setup says which were used.
- **Checks**: Tamil words count as words in every score (misheard words, speaking speed). A reference
  number read back in Tamil number words ("ஏழு ஏழு மூணு ஒண்ணு") is checked digit by digit. A reference
  with letters read in Tamil can't be matched in a transcript with certainty, so it is marked *not
  verified* for a person to listen to, never failed.

## Resilience

Real systems fail: a booking API times out, a service is busy, a login expires, a slot is taken a second
before the agent books it, the line drops. Resilience mode checks what the agent does then, using **your
own test cases**: each one that ends in a booking or order is run again with one of its tools failing on
purpose, once per kind of failure you pick on **Run**. Three choices make a drill:

- **What breaks**: the booking or order tool (default), or **the lookups before it** (free times, the
  menu, the account). When a lookup fails, the risk is the agent inventing what it couldn't read.
- **How often**: **first call only** (outages often clear, so a retry may work) or **every call** (an
  outage that lasts the whole call: the agent must say so, stop retrying and offer a way forward).
- **Which failure**:

| Failure | What breaks | It passes when |
|---|---|---|
| Doesn't answer | The tool times out (8 s). | Done on a retry and confirmed, or the caller is told honestly it couldn't be done. |
| Server error | The tool answers with an error (500). | The same. |
| Too many requests | The service is busy (429) and says to try again in 2 s. | The same. |
| Login expired | The agent's access token has expired (401). | The same. |
| Rubbish answer | The tool answers with broken data; nothing is saved. | The same. |
| Slow answer | The tool takes 4 s longer, then answers fine. | The task still gets done. |
| Saved, but the answer was lost | The booking is saved, but the agent gets a time-out back. | Done exactly once, and the caller isn't told it failed. |
| Already taken | The booking is refused (409); the same request is refused again. | Another option the caller agrees to, or an honest no. Never "it's booked". |
| Payment declined | The payment or order is refused (402). | The same. |
| Out of stock | The order is refused (409). | The same. |
| Call dropped | The line drops just after the booking is sent; the caller rings back on a new call. | Exactly one booking at the end (text runs only: the second call would need its own recording). |

**Every call** applies to the five crash failures. A drill on every call passes only with an honest "it
can't be done right now". On top of the outcome check, each drill gets hard checks:

- **Stops retrying**: after a failure the agent tries the tool again at most 3 times (counted until a try
  works). An agent that loops on a dead service fails.
- **Acts on real data** (lookup drills, blocks): nothing is booked or ordered before some lookup really
  answered. Booking a slot the agent never saw fails.
- **Offers a person** (every-call drills, when the agent has a transfer or callback tool): it hands the
  caller over, or offers to.
- And one AI judge question on every drill: *did the agent offer a way forward* (try later, another
  option, a callback, a person)? Like every judge item it is an opinion until the judges are trusted.

A drill fails when the agent claims a booking that wasn't made, says it failed when it was done, or makes
it twice. *Saved, but the answer was lost* and *Call dropped* are the duplicate-request tests: an agent
that blindly retries books twice. The report says how to fix it (look the booking up before retrying or
giving up, and give the tool a request id so a retry is safe).

**How your tools say no.** The refusal drills use a plain answer such as `{"error": "conflict"}` unless
you give your own. On **Resilience › Test cases**, *How your tools say no* shows each tool that changes
data with its three refusals; **Write with AI** drafts answers shaped like a real API for that tool (from
its description), you check or edit them and press **Save** (profile `resilience.refusals`).

A drill keeps the test's caller (persona, goal, facts) and the checks that stay true when a tool fails
(no secrets, no forbidden tool, identity first); checks written for the happy path are left out. Scripted
test cases aren't drilled, because their fixed lines can't react to a failure. Each drill is one call, so
*Repeats per test and failure* starts at 1.

From the terminal:

```powershell
uv run python -m vatf run examples/clinic --test-mode resilience --drills error,slot_taken,dropped_call --drill-on both --drill-repeat both
```

The **Analysis** tab shows: failures handled, untrue claims and duplicates (target 0), and retries (text)
or the longest silence during a failure (voice); how calls ended per failure; a **test cases × failures**
grid (a red cell opens its failed call); and **How it recovered**: retries, the most in one call, calls
that kept trying too long, retries sent before a 429 said to, and in voice whether the caller heard the
agent within 3 s of the failure and the longest silence. All of these are measured from the tool calls and
the recording, never by AI. Failures inside the agent's own speech, AI or voice providers are out of
scope: Vocto can't break those from outside.

**Unsteady network.** The *Unsteady network* caller condition (voice) sends the caller's audio with
80 ms of network jitter and 1% of packets out of order, as on a poor mobile data or Wi-Fi call. Pick it
in Speech & accents to compare it with a clean line, or as a run's caller conditions.

**Recovery after a load spike** is part of the load test: see [phase7-guide.md](phase7-guide.md).

## Regression

Regression answers "did the latest change make anything worse?". It runs your full test set and compares
it with the run you saved as the **baseline** (open a good run, **Set as baseline**). AI agents don't
answer the same way twice, so a plain "it failed once" comparison raises false alarms. Vocto guards
against that in five ways:

- **Confirm before blocking.** A test that passed every repeat in the baseline and fails just once now
  runs 5 more times at once. It blocks only if at least 2 of its calls failed; otherwise it shows as
  **Suspected (didn't repeat)** and never blocks. Two or more failures block straight away.
- **Flaky tests.** Each test's pass rate over its last 10 runs (runs with bug switches left out). A test
  that both passed and failed is **flaky**: its single failures don't block, and it is listed on its own
  for the team to fix.
- **Edited tests.** Every run saves a fingerprint of each test case. A test you edited since the baseline
  shows as **Test edited**, never as a regression.
- **What changed.** Each run records the agent version label (the **Agent version** field on Run, or
  `--agent-version`), fingerprints of the profile and the agent's prompt (`agent_prompt`), the AI judge
  and caller models and Vocto's version. The report lists what differs from the baseline. When the judge
  models changed, judge scores never block (the judges moved, not the agent).
- **Rolling baseline (option).** `regression.baseline.rolling: 3` compares with the last 3 good runs made
  the same way, pooled, instead of one lucky or unlucky run.

```yaml
regression:
  confirm: {reruns: 5, min_failures: 2}   # reruns: 0 = one failure blocks, as before
  baseline: {rolling: 0}                  # 3 = the last 3 good runs pooled
```

The **Analysis** tab leads with Confirmed regressions, Suspected, Fixed since baseline and Flaky tests,
then the pass rate over the last 10 runs (version labels on the line), what changed between the runs, a
before / now table and the flaky list. A test that is fixed since the baseline has **Keep as a regression
test**: it tags the test case `regression`, and kept tests run in every scheduled health check along
with the critical ones.

```powershell
uv run python -m vatf run examples/clinic --test-mode regression --agent-version v1.4
```

## Recorded caller lines

A scripted test case's line can be played from a **recording of a real caller** instead of Vocto's
synthetic voice (voice runs). Real voices, accents, mumbling and the noise of a real phone test the
agent's hearing in a way synthetic speech can't. In **Set up › Test cases**, open a scripted test case
and use *Add a recording* on a line: a WAV file (16-bit, up to 60 s) of someone saying that line. It
is kept in the agent's `recordings/` folder. Keep the line's words as what is said in the recording:
the checks and the misheard-words score use them.

- The recording is levelled to a normal speaking volume, then goes down the call's phone line like any
  caller line, so the line conditions (phone line, noise, lost packets, unsteady network, quiet caller)
  still apply. An accent or a fast talker can't change a recording.
- Text runs send the line's words. A missing or unreadable recording stops a voice run before the
  first call, naming the test case and line.
- The call view marks the line *real caller recording*.
- Use recordings only with the speaker's consent. They contain their voice and anything they said.

## Cost per completed task

Enter what a call costs you in **Set up › Connection › Running costs**: per call, per minute of a call
(phone line, speech and AI, all in), per reply and per tool call, in your currency. Vocto can't see
your bills, so with no prices it shows no cost.

Every report then shows the **agent cost per completed task**: what all the calls that should have
booked or ordered something cost, divided by how many really did. A failed call still costs money, so
an agent that needs two calls per booking costs twice as much per booking. Calls Vocto couldn't make
(setup errors) are left out. Vocto's own AI cost (Claude playing the caller and judging) is shown
separately.

## A/B test: two versions side by side

**Quality › Compare runs › Test two versions (A/B)** runs the same tests on two versions of the agent,
one after the other, with the same caller and settings. Version B is either another channel set up for
the new version (its own address or phone number), or the same channel with other agent options (for
example a new model). The page then shows **version B against version A**: tests that pass with one
and fail with the other, answer times, call quality, AI judge results and the cost per completed task.
Nothing in an A/B test blocks a release: it shows which version does better, and you decide. Raise the
repeats to trust a small difference. Each version also has its own full report.

```powershell
uv run python -m vatf run examples/clinic --ab-with profile-v2.yaml
uv run python -m vatf run examples/clinic --ab-with profile.yaml --ab-agent-opt model=claude-sonnet-5-5
```

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
