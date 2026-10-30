# Phase 2: voice mode

Phase 2 puts a real phone line between vatf and the agent. The simulated caller now
speaks (text to speech), listens (speech to text) and hears only what came down the
line. Every call is recorded, and how fast the agent answers is measured on that
recording and gates the release.

Exit criterion (from the research report): vatf's response times match a reference
measured by hand within 50 ms, and a "mispronounce the booking code" bug is caught.

## What you need

```powershell
uv sync        # now also installs numpy, websockets, Piper (speech) and faster-whisper (listening)
```

The first voice run downloads the speech models into `.models/` in the repo folder
(git-ignored), once:

| Model | What for | Size |
|---|---|---|
| Piper `en_GB-alba-medium` | the caller's voice (persona calm-adult) | ~60 MB |
| Piper `en_US-amy-medium` | the clinic agent's voice | ~60 MB |
| whisper `base.en` | the caller's ears, and the clinic agent's ears | ~145 MB |
| whisper `small.en` | careful second listen for read-back checks | ~480 MB |

Everything runs on the laptop CPU; no speech API is called. Set `VATF_MODELS_DIR` to keep the
models somewhere else.

## Running it

```powershell
# 1. Timing check (no API key, about 20 s): a reference agent answers after exactly 500, 900 and 1300 ms.
uv run vatf run examples/latency-ref

# 2. Free voice run: rule-based caller and agent, real speech and listening, no API calls.
uv run vatf run examples/clinic --profile profile-voice.yaml --offline

# 3. Real voice run: Claude plays the caller and the clinic agent.
uv run vatf run examples/clinic --profile profile-voice.yaml
uv run vatf run examples/clinic --profile profile-voice.yaml --agent-opt bugs=misread_reference   # NO_GO

# 4. Phase 2 exit check: clean agent + all four bug switches, by voice.
uv run vatf exit-check examples/clinic --profile profile-voice.yaml --repeats 1   # quick look, ~25 calls
uv run vatf exit-check examples/clinic --profile profile-voice.yaml              # full, 3 repeats, ~75 calls
```

Voice calls run in real time, so a call takes 30 to 60 seconds; a full clinic run (5 scenarios x 3
repeats) takes 10 to 15 minutes. All the Phase 1 options (`--scenario`, `--repeats`, `--show-pii`,
`--quiet`, `--agent-opt`) work the same. Voice adds:

| Option | What it does |
|---|---|
| `--profile profile-voice.yaml` | Use the clinic's voice profile. It `extends` profile.yaml and only changes the connection. |
| `--stt-model base.en` | Whisper model the caller hears with. |
| `--stt-check-model small.en` | Whisper model for the careful second listen. |
| `--speech codec` | Test tones instead of speech (plus `--agent-opt speech=codec`). For vatf's own tests only: it never judges an agent. |

## Checking a response time by hand (exit criterion)

1. Run `uv run vatf run examples/latency-ref` (or any voice run) and open the call folder printed in
   the report: `runs/<run>/calls/<test id>/audio/call.wav`.
2. Open `call.wav` in Audacity (free). Top channel = caller, bottom channel = agent. The time ruler is
   the call clock, the same as the `t_ms` numbers in the transcript.
3. Pick a turn in the transcript, e.g. `CALLER ... [spoke 6.20s-7.96s]` followed by
   `AGENT ... [answered after 903 ms; heard at 8.86s-9.66s]`.
4. In Audacity, click where the caller's waveform ends (about 7.96 s) and drag to where the agent's
   waveform starts (about 8.86 s). The selection length at the bottom is the gap.
5. It should match vatf's "answered after" figure within 50 ms. For the reference agent it should also
   match the delay it was set to (500, 900, 1300 ms). vatf's own test does this automatically and gets
   within a few milliseconds.

## What happens in one voice call

```
load fixtures -> load the speech models -> start the agent (launch) and dial it
the line starts: 20 ms audio frames every 20 ms, with background noise if the scenario has any
loop: wait for the agent to speak and finish (1 s of silence) -> transcribe what was heard
      -> the caller decides what to say -> speak it onto the line
call ends -> save audio/call.wav -> careful second listen (read-back checks only)
-> validator -> backend probe -> lock the evidence -> checks -> verdict.json
```

- **The caller only hears the line.** Its "ears" are Whisper on the agent's audio, never the agent's
  own text. If the agent sends a `transcript` message, it is kept as `platform_transcript` for
  comparison, and the caller never sees it.
- **Turn taking** works like a person on the phone: wait for the agent to start, take 1 s of silence
  as "finished" (`limits.end_of_turn_ms`), and if nothing comes for 15 s (`limits.no_reply_ms`, or the
  persona's `hangs_up_after_silence_ms`) react to the silence. Two silent turns in a row end the call.
- **Faults are real in voice.** A `timeout` or `slow` fault really makes the tool wait, so the caller
  hears the agent go quiet. Those turns are shown but not counted toward the response-time gate.

## Response time (latency)

Measured for every agent answer: from the last sound of the caller's words to the first sound of the
agent's answer, read off the recording (see docs/voice-protocol.md). A turn is listed but not counted
when vatf slowed a tool on purpose, when the agent started before the caller had finished, or when the
agent said nothing.

The run is NO_GO when p50 or p95 over all counted answers is above the profile's
`thresholds.latency_p50_ms` / `latency_p95_ms`. A scenario can also set a per-call
`{type: latency, p95_ms: ...}` check. Production agents should meet 800 / 1500 ms (the defaults).
The clinic zoo agent waits 0.6 s to be sure you have finished, then runs Whisper, Claude and Piper one
after another on the laptop CPU, so its voice profile allows 3500 / 6000 ms. Expect real numbers on
your laptop in the 2 to 5 second range; that is the zoo agent being slow, not vatf.

## New checks

| Check | What it does |
|---|---|
| `readback` | `tool` + `field`: the agent must say the exact value its tool returned (e.g. the booking reference). Digits are matched however they are heard ("4 8 2, 1 9 3", "482-193", "four eight two..."). Every agent turn after the tool result is heard twice: once by the caller's ears and once by the careful model; one correct hearing is enough, so a single mishearing never fails a good agent. With `probe`, the stored value must match too. Works in text mode on the agent's text. |
| `latency` | `p95_ms`: this call's slowest answers must be under the limit. `min_answers` (default 1): at least this many answers must be timed, else NOT_VERIFIED (the latency reference uses 3). |
| barge-in (automatic) | For each `audio.barge_in` in a scenario: the agent must stop talking within `stop_within_ms` (default 1000) after the caller cuts in. Check id `agent-stops-when-interrupted`. |
| `entity_fidelity` | Unchanged, but in voice it now tests the agent's hearing: the caller says its name, the agent hears it through its own speech recognition, and the name it sends to the tool must match. |

## Scenario and persona settings for voice

```yaml
modes: [voice]              # run only in voice (default: both text and voice)
audio:
  noise: office             # none | office | street | cafe | car (synthetic, same every run for a test id)
  noise_file: noise.wav     # or a real recording instead
  snr_db: 15                # speech is 15 dB louder than the noise (lower = noisier)
  barge_in:
    - agent_turn: 2         # the agent's 2nd turn (1 = greeting)
      offset_ms: 700        # start talking 700 ms after the agent starts
      say: Sorry, it's Maria Lopez, born the 14th of March 1985.
      stop_within_ms: 1000
```

Personas set the caller's voice: `speech: {tts_voice: en_GB-alba-medium, speaking_rate: 1.0}`
(any Piper voice name, see https://rhasspy.github.io/piper-samples/).

## The clinic agent by phone

`zoo/clinic/voice.py` puts the same text agent behind a small voice front-end (`zoo/voice/gateway.py`):
an end-of-speech detector that follows the background level, Whisper, the agent, Piper streamed
sentence by sentence, and barge-in (it sends `clear` after 0.3 s of caller speech over it).

New in the clinic backend: every booking gets a six-digit `reference`, returned by `book_appointment`,
stored in the database and read to the caller. New bug switch:

| Bug | Where | Caught by | How |
|---|---|---|---|
| `misread_reference` | the speech layer: the reference is spoken with its 3rd and 4th digits swapped | clinic.book-checkup | `reads-back-reference` FAIL: "the agent never said reference 482193; it was heard saying 481293" |

It only exists in voice: in text mode the agent's words are right, which is exactly why voice tests are
needed. `vatf exit-check` with the voice profile adds it to the three Phase 1 bugs.

New voice-only scenarios (criticality `normal`, so they show up in the scores and the response-time
gate but do not need all repeats to pass):

- `clinic.book-checkup.noisy-line`: the happy path from a noisy office (office noise, 15 dB).
- `clinic.caller-interrupts`: the caller cuts in on the agent's second turn.

## Recordings and personal data

`audio/call.wav` holds the voices as spoken, including names and dates of birth: audio cannot be
masked. The text in events.jsonl and the transcripts is still masked as before. `runs/` is
git-ignored; treat recordings as personal data.

## Listening live

On the dashboard's *Live run* page a voice run has a **Listen live** button. Click it to hear the
call as it happens, the caller and the agent as the caller hears them, in your browser. It is off
until you click it and stops when you leave the page or the run ends; afterwards each call's
recording is on its call page.

Listening never changes a call. The run sends a copy of the line's audio to the dashboard every
100 ms, about 60 ms behind the call, over a local port on this computer. Nothing waits for it: if
the dashboard or the browser falls behind, that audio is skipped, never queued against the call, so
response times are the same whether anyone listens or not. Runs started from a terminal don't send
it at all. Text runs have nothing to hear, so the button isn't shown.

## Known limits of Phase 2

- Agents outside Python can be dialled (`connection.target: ws://...`) and are heard and timed, but
  their tool calls are not seen yet (HTTP tool proxy: Phase 4/6).
- One audio format on the wire: PCM16 mono. Twilio-style mu-law framing comes with phone connectors (Phase 6).
- Whisper `base.en` on a laptop is fast but not perfect; the caller may mishear, as a person can.
  The careful second listen keeps that from failing read-back checks.
- Calls run one at a time and in real time.
