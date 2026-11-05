# Phase 8: monitoring real calls

Phase 8 answers "how is the agent doing with real customers, right now?" for any agent. Real calls
come in from the voice platform (a file you import, or sent live by the platform), personal data is
removed before anything is saved, and each call is scored with the same call quality metrics and AI
judges as test calls. Trends show how the agent is doing over time and by version, and alerts go to
the same Slack, Discord or webhook targets as Phase 6.

On Windows computers that block the `vatf.exe` launcher (Application Control), type every command
below as `uv run python -m vatf ...`.

## Module flow

```
call file (upload / CLI)  or  platform webhook (POST /ingest/<agent>, with a token)
  -> monitor/intake.py   reads Vocto JSON, Vapi end-of-call reports, Retell call_ended/call_analyzed,
                         plain transcripts ("Agent: ..." / "Caller: ..."), JSON lists and JSON lines
                         -> one RealCall each (turns, tool calls, times when the file has them)
  -> monitor/redact.py   removes personal data BEFORE anything is written: emails, card numbers
                         (Luhn), bank numbers / IBANs, ID numbers (SSN, Aadhaar, PAN, UK NI), phone
                         numbers, birth dates, street addresses and postcodes, names, the values the
                         agent sent to its tools, and the agent's own patterns. The caller's number
                         is kept only as a salted hash (to count repeat callers).
  -> monitor/store.py    runs/monitor/<agent>/calls/<id>/{call.json, events.jsonl, verdict.json}
                         + index.json; the same event timeline as test calls
  -> monitor/score.py    Phase 5 call quality metrics + real-call checks + the agent's AI judges
                         (on a sample, within a daily limit and budget) -> verdict.json, flags
  -> monitor/trends.py   last 24 h, this week vs last, calls per day, by agent version, top flags
  -> monitor/watch.py    alert rules over a rolling window; raised and cleared alerts go out once
                         through alerts.py (events "monitor" and "monitor_recovered")
```

## Real-call checks

A real call has no script, so only checks that fit any call run on it. The AI judges cover the rest.

| Check | Result |
|---|---|
| A tool failed | Fail, shown and flagged, but it doesn't fail the call on its own (the agent may have handled it well) |
| Possible false success | Needs review: a tool failed and the agent then said it was done, with no later successful retry |
| Dropped or left hanging | Fail: the call ended on a platform error, or the caller's last words got no answer |

Call quality metrics (time to first word, response p90 / p99, silences, talk-over and more) need
turn times in the file. Calls without times are still checked and judged; they show "no timings".

## Privacy

- Removal happens in memory, before the first write. The judges only ever see the cleaned text.
- Settings › Privacy chooses which kinds of data to remove (all by default) and adds your own patterns.
- Recording links are dropped unless you choose to keep them (the recordings stay on the platform).
- Calls older than "Keep calls for" (30 days by default) are deleted automatically, and older calls
  are refused at import. Delete one call, or all of them, from the dashboard.

## On the dashboard

Quality › **Real calls**:
- **Overview**: active alerts, calls in the last 24 h, pass rate, flagged share, response p90, handed
  to a person, calls per day, call quality, why calls were flagged, results by agent version, and the
  alert history. **Check alerts now** runs the rules straight away.
- **Calls**: every call with filters (flagged, to review, failed, handed to a person, waiting for
  judges), search and version. Open a call to see the conversation, why it was flagged, the checks,
  the judges (label them right there: your label replaces theirs and teaches them), call quality,
  and **Make a test from this call**, which writes a scripted test case from the caller's lines
  (with stand-ins where personal data was removed) and opens it in Set up › Test cases.
- **Import**: drop call files.
- **Settings**: switch monitoring on, receive calls live (port, address, token), privacy, judges
  (share of calls, daily limit, daily budget), alert rules, and delete all real calls.

## Receiving calls live

Switch on "Receive calls from the platform" and press **Make a token**. The token is saved as a
secret in the `.env` file and shown once. The address is `http://127.0.0.1:8791/ingest/<agent>`
(one port for all agents; 8790 is left for the tool gateway).

- Vapi: set the assistant's Server URL to the address and its server secret to the token.
- Retell: set the webhook URL to the address followed by `?token=` and the token.
- Any other platform: POST each call as Vocto JSON with `Authorization: Bearer <token>`.

The dashboard must be open to receive calls. A platform in the cloud can't reach your computer
directly: put a tunnel (for example cloudflared or ngrok) in front of the address. Without a token
calls are refused (403); a wrong token gets 401.

## Commands

```
uv run python -m vatf monitor import examples/clinic calls.jsonl vapi-report.json
uv run python -m vatf monitor status examples/clinic
uv run python -m vatf monitor judge examples/clinic --limit 20
uv run python -m vatf monitor check examples/clinic       # alerts + retention, for a scheduler
uv run python -m vatf monitor listen examples/clinic       # receive calls without the dashboard
```

`status` and `check` exit with 1 while an alert is active, so a scheduler or CI job can act on it.

## Vocto JSON

```json
{
  "id": "call-1042",
  "started_at": "2026-11-02T09:30:00Z",
  "agent_version": "v12",
  "caller": "+14155550123",
  "ended_reason": "customer-ended-call",
  "turns": [
    {"role": "agent", "text": "Riverside Clinic, how can I help?", "start_ms": 300, "end_ms": 1800},
    {"role": "caller", "text": "I'd like a checkup tomorrow.", "start_ms": 2400, "end_ms": 4200},
    {"role": "agent", "text": "You're booked for 9 am.", "start_ms": 5000, "end_ms": 6500,
     "tools": [{"name": "book_appointment", "args": {"slot": "2026-11-03T09:00"}, "result": {"ok": true}, "at_ms": 4500}]}
  ]
}
```

Times are optional. A file can hold one call, a list, `{"calls": [...]}` or one call per line.

## Not verified yet

- The Vapi and Retell readers follow those platforms' public formats; they have not been tried with
  live payloads from a real account.
- Live AI judging of real calls needs an Anthropic key (the tests use a scripted model).
