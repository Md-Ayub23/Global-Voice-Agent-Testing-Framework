# Phase 5: harder callers and call quality

Phase 5 makes Vocto's simulated caller harder to serve, the way real callers are, and scores every
voice call on call quality metrics with good / warn / bad bands. Nothing changes on the agent's side:
all of it is set per caller, per test or per run in the dashboard.

Plan with the module diagram: [Vocto Phase 5 plan](https://claude.ai/artifact/2sbKwkpzo8xxFHTP2aTkvc).

## Module flow

Data moves one way. Each module reads only the output of the one before it.

```
Set up (UI)  ->  conditions.py  ->  voice_call.py + audio/line.py  ->  events.jsonl
                                                                          |
Report (UI)  <-  decision.py + judges.py  <-  verdict.json  <-  metrics.py
```

| Module | What it owns |
|---|---|
| `src/vatf/conditions.py` | Turns the persona, the test's audio settings and the run's preset into one `CallConditions` per call. Written down on the call's `call_started` event. |
| `src/vatf/accents.py` | Accent name to a Piper voice (and speaker, for Piper's multi-speaker voices). |
| `src/vatf/audio/line.py` | Phone-line effects on what the caller sends: narrowband phone sound and lost packets. |
| `src/vatf/voice_call.py` | The caller's behaviours during the call (cut-ins, "mm-hm", pauses, fillers); the orchestrator's call loop handles "goes quiet". |
| `src/vatf/metrics.py` | Call quality numbers from `events.jsonl`, with bands. Per call (`verdict.metrics.quality`) and per run (`decision.quality`, `decision.by_condition`). |
| `src/vatf/decision.py` | A metric switched to *Blocks* whose run value is bad makes the run NO-GO; other warn / bad metrics are listed as warnings on a GO. |
| `src/vatf/judges.py` | Tells the AI judges the call's conditions, so noise or a phone line is not blamed on the agent. |

## Harder callers

Voice runs only. Text runs ignore all of this.

**Who the caller is** (Set up > Callers, the persona file):

| Setting | File field | What the caller does |
|---|---|---|
| Accent | `speech.accent` | Speaks with that accent (see below). A voice chosen in `speech.tts_voice` wins over it. |
| Loudness | `speech.volume_db` | Speaks this many dB softer (negative) or louder. |
| Cuts in on long answers | `behaviour.interrupts` (+ `interrupt_after_ms` 2500, `interrupt_chance` 0.5, `interrupt_max` 2, `interrupt_lines`) | Talks over an agent turn that has gone on that long. Measured like a scripted cut-in. It keeps the turn number, so a scripted test's turn checks still line up. |
| Says "mm-hm" | `behaviour.backchannels` (+ `backchannel_after_ms` 1800, `backchannel_chance` 0.6, `backchannel_max` 3) | A short "mm-hm" or "okay" while the agent talks. The agent should keep talking. |
| Pauses mid-sentence | `behaviour.pauses_mid_sentence` (+ `pause_ms` 1200, `pause_chance` 0.5) | Stops for a moment in the middle of lines of 6 words or more (at a comma when there is one). The agent should wait. |
| Says um and uh | `behaviour.fillers` (+ `filler_chance` 0.4) | Starts some lines with um, uh or hmm. |
| Goes quiet once | `behaviour.goes_quiet` (+ `quiet_ms` 12000) | Once per call (its 2nd, 3rd or 4th line), says nothing for that long. A good agent checks in. |

`interrupts: true` used to be only a label; it now makes the caller cut in. Every random choice
(which line pauses, which turn the caller goes quiet on) follows the call's id, so a call can be replayed.

**Accents** (`src/vatf/accents.py`): american, american-2, british, northern-english, scottish, irish,
indian, indian-2, and non-native English from speakers whose first language is Hindi, Mandarin,
Spanish, Arabic, Korean or Vietnamese (hindi-l1, mandarin-l1, spanish-l1, arabic-l1, korean-l1,
vietnamese-l1). Irish and Indian come from Piper's `en_GB-vctk-medium` voice (the VCTK speakers' own
accent labels); the non-native ones from `en_US-l2arctic-medium` (L2-ARCTIC). These download once on
first use, like the other voices. A voice written `name#speaker` picks one speaker of a multi-speaker
voice; a wrong speaker name stops the call's speech setup with the list of names.

**Where the caller calls from** (Set up > Test cases > a test > Audio, the scenario's `audio`):

| Setting | Field | Effect |
|---|---|---|
| Background noise | `noise`, `snr_db` | office, street, cafe, car, and now **tv** (a TV or radio in the room), or a WAV file (`noise_file`). |
| Phone line | `line.codec: phone` | Band-limited to 300-3400 Hz and quantized with G.711 mu-law, frame by frame on the line (the filter keeps its state, so no clicks). |
| Packets lost | `line.packet_loss_pct` | That share of 20 ms frames never arrives, in short bursts (mean two frames, seeded by the call). The gap is filled the way a phone does (G.711 Appendix I): the last frame replayed, fading to silence by 60 ms, never plain digital silence. |

**A whole run under stress** (Run tests > Caller's voice and ears > Caller conditions, or
`vatf run --conditions <preset>`), laid over every test without editing them:

| Preset | What it adds |
|---|---|
| As written | Nothing: each test's own audio and caller. |
| Phone line | Phone line with 1% of packets lost. |
| Noisy | Cafe noise at 10 dB. |
| Hard | Street noise at 8 dB, phone line, 3% packets lost, and every caller cuts in, says "mm-hm" and pauses mid-sentence. |

A preset's noise and line win over the test's; it turns behaviours on and never off.

## Call quality metrics

Read off the call's timeline (`events.jsonl`), so they work for any agent. Defaults below; change
them per agent in Set up > Pass rules > Call quality (profile `metrics.<id>.good / warn / blocks`).

| Metric | id | Run value | Good | Warn |
|---|---|---|---|---|
| Time to first word | `ttfw_ms` | p90 over calls | ≤ 1,500 ms | ≤ 3,000 ms |
| Response time p90 | `response_p90_ms` | over all timed answers | ≤ 1,500 ms | ≤ 2,500 ms |
| Response time p99 | `response_p99_ms` | over all timed answers | ≤ 2,500 ms | ≤ 4,000 ms |
| Longest silence from the agent | `longest_silence_ms` | p90 over calls | ≤ 3,000 ms | ≤ 6,000 ms |
| Agent talked over the caller | `talked_over` | share of calls | ≤ 5% | ≤ 15% |
| Stop time after a cut-in | `barge_in_stop_ms` | p90 over cut-ins | ≤ 800 ms | ≤ 1,500 ms |
| Kept talking through "mm-hm" | `backchannel_kept` | share of mm-hms | ≥ 90% | ≥ 70% |
| Checked in after caller silence | `checkin_ms` | p90 (never = bad) | ≤ 7,000 ms | ≤ 12,000 ms |
| Agent share of talk time | `talk_share` | mean | ≤ 65% | ≤ 80% |
| Agent speaking speed | `agent_wpm` | mean | ≤ 190 wpm | ≤ 220 wpm |
| Agent words heard wrong (WER) | `heard_wer` | over all words | ≤ 10% | ≤ 20% |
| Turns to reach the goal | `turns_to_goal` | mean | no band | |
| Handed to a person | `escalated` | share of calls | no band | |
| Call length | `duration_s` | mean | no band | |

How each is measured:

- **Time to first word**: from the moment the connection opened to the first sound of the greeting.
- **Response times**: the same timed answers as the p50 / p95 gate (talk-over and injected tool delays don't count).
- **Longest silence**: the longest wait for the agent in a call, before an answer or in a pause inside one (a "went on" part after a gap, such as during a tool call).
- **Talked over**: the agent's first sound came before the caller had finished (including in a mid-sentence pause). An answer to the caller's own cut-in is left out.
- **Stop time**: how long the agent kept talking after the caller cut in (scripted and persona cut-ins).
- **Kept talking through "mm-hm"**: the agent did not clear its audio within a second and its turn went on at least 300 ms past the mm-hm.
- **WER**: only for agents that report their own words (`platform_transcript`): their words against what the caller heard. When the agent stopped part way (the caller cut in, or it stopped on an mm-hm), only the words before the stop are compared.
- **Handed to a person**: a `handoff` event, or a tool whose name has transfer, escalate, handoff, human, operator or live agent in it.
- **Turns to reach the goal**: caller turns until the test's outcome tool answered ok.

Text runs get turns to goal, handed to a person and call length.

## The decision

- A metric with *Blocks* on whose run value is in the bad band: **NO-GO**, rule `metric`, decided by `call_quality`.
- Other warn or bad metrics on a GO: a `metric_warnings` reason that names them and links the calls.
- The p50 / p95 response time gate is unchanged.

## Where to see it

- Run report > **Call quality** tab: a tile per metric with its value, band and limits, and **Results by caller condition** (pass rate, p50 / p90 and talk-over per line-and-voice label).
- Call page > **Quality** tab: that call's conditions and each metric with its band.
- The call's story and transcript show cut-ins, mm-hms, pauses and silences.

## Checking it

`tests/test_phase5.py`: conditions and presets, accent speaker lookup, the phone filter (passes 1 kHz,
cuts 100 Hz and 6 kHz, frame-by-frame equals one pass), packet loss rate and bursts, every metric on a
hand-made timeline, WER on a cut-off turn, packet loss concealment, bands, run values, the decision, schemas, and voice runs with the tone codec:
the latency reference agent on a phone line with lost packets (timings still within 50 ms) and the clinic
agent with a caller who cuts in and says mm-hm.

Not covered yet: keypad tones (DTMF), other languages, comparing against a baseline (Phase 6), load (Phase 7).
