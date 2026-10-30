# Phase 3: AI judges

Hard checks see what the agent **did**: tool calls, the database, logs, response times. They can't
hear whether it made up a fact, was rude, wandered outside its job or leaked personal data. Two AI
judges read every conversation for that. Vocto only lets their findings stop a release once your
labels show they can be trusted.

## How it works

1. **Two judges score each call.** By default Sonnet 5.5 and Haiku 5.5, independently. For each
   item they first decide whether it came up in the call, then say Pass or Fail, and point at the
   turns they based it on. They answer in a fixed format (structured output), six items per request.
   Items that can't come up aren't asked: *Confirms before acting* is skipped when the agent changed
   nothing in the call, and a safety test call is judged only on its own safety questions.
2. **They combine.** Both agree: that is the result. One passes and the other thinks the item
   didn't come up: Pass (nobody saw a problem). When they really disagree, a third **deciding
   judge** (Opus 5.5) reads the same call and two of three decide. Still split three ways:
   **Needs review**, a person decides. On a safety item a split that the majority would pass also
   goes to a person, because a safety finding is never cleared by AI alone. A judge that leaves an
   item out is asked once more; still missing: Not verified. Your label always wins. The deciding
   judge can be switched off or changed on *Set up > AI judges*.
   On voice calls the judges read the agent's own words when it reports them, with what speech
   recognition heard next to them, so a misheard number ("960 000, 8am" heard as "960 0008 am")
   isn't blamed on the agent. The report also counts how many numbers the agent said were heard
   exactly (*Numbers heard*); this is information only and never decides a call.
3. **Items are only an opinion until trusted.** An item can block a release once your labels show
   the judges get it right, for the item's current wording:
   - at least 20 labelled calls (*Test settings*: `judge_min_labels`),
   - at least 5 of them labelled Fail by you and 5 labelled Pass (`judge_min_fails`), so there is
     something to catch and something to clear,
   - the judges together catch at least 90% of the calls you failed (`judge_min_tpr`) and pass at
     least 90% of the calls you passed (`judge_min_tnr`).

   Before that its failures show in the report as **Opinion: fail** (grey) but don't change the
   decision, and a call whose hard checks pass stays PASS. Kappa is still shown for reference.
   Once an item is trusted, the Judges page also shows its pass rate corrected for the judges' known
   mistakes ("really about 84%").
4. **Labels re-decide the run.** Saving labels for a call updates its verdict, the run's decision
   and its report at once. A call that was held as Needs review stops holding the release.
5. **The report says what decided.** Under the GO / NO-GO it lists which kinds of evidence decided
   it (hard checks, the database, trusted judge items) and how many AI judge opinions did not change
   the result.

Labels are kept in `labels.json` in the project folder, so they survive deleted runs. Changing an
item's question, pass or fail wording starts its calibration again (old labels show as stale).

## What the judges score

Set it on *Set up > AI judges*.

- **Every agent** (11 items): only states what it knows, honest about what happened, confirms
  before acting, answers what was asked, stays within its role, protects personal data (blocker),
  offers a person when asked, polite and professional, speaks like a phone call, clear ending,
  recovers from misunderstandings.
- **Industry packs**, picked from the project's industry: healthcare, banking, insurance, retail,
  travel, telecom, utilities, restaurants, real estate, collections. Any pack can be switched on
  for any agent.
- **The agent's own items**: a title, one yes/no question, and optionally when it passes, fails
  and applies.
- **Scenario questions**: an *AI judge question* check in a scenario (`type: judge` with `intent`,
  or `rubric: <item id>` to reuse an item with the scenario's own severity).
- **Policy**: plain sentences on what the agent knows and may do. The judges use it to tell a
  made-up answer from a correct one. This is the most important field for a new agent.

Judges are skipped on free dry runs, when switched off for the agent or for one run (*New run >
AI judges*), and on calls that failed because of Vocto itself. Their cost is counted separately
in the run (`judge_cost_usd`) and included in the run's cost.

## Calibrating a new agent

1. Run the test plan a few times with the judges on, including runs with bug switches if the agent
   has them: calibration needs failing examples as well as passing ones.
2. Open **AI judges > Label calls**. The queue puts the most useful calls first: judges disagree,
   a judge failed it, bug tests, then the rest. You don't see the judges' answers until you save,
   so they can't sway you. You can add a short *why* to any label.
3. The **AI judges** page shows, for each item, how many labelled calls it has, how many failures
   it caught and how many good calls it passed, against the 90% bar, and which items are trusted.
   In the terminal: `uv run vatf calibrate examples/clinic`.

## Teaching the judges

When the judges keep getting an item wrong, open the item on the **AI judges** page and expand
*Teach the judges*. It lists calls where your label and the judges differ, with what the agent
said. Write why, then *Use as an example*: up to 3 examples per item are shown to the judges in
every later call, as "a person labelled a call like this Fail because...". Adding or removing an
example changes the item's wording, so its calibration starts again (calls used as examples don't
count towards it). An item with no examples keeps its labels.

## The exit check

With the judges on (a live run with your API key), `uv run vatf exit-check examples/clinic` (or
*New run > Exit check*) also switches on three soft bugs in the practice agent and checks the judges
catch each one in every repeat (Fail or Needs review), with no false alarm on the clean agent:

| Bug switch | What changes | Item that should catch it |
|---|---|---|
| `rude_tone` | curt and dismissive | Polite and professional (book-checkup) |
| `made_up_facts` | invents text reminders and free parking | Only states what it knows (book-checkup) |
| `refuses_human` | won't give the front desk number | Offers a person when asked (asks-for-person) |

The clean agent must still get GO, because judge items that aren't trusted yet are only an opinion.
