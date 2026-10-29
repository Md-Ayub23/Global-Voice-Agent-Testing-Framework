# Text over HTTP: how Vocto talks to an agent in text

`connection.type: text_http`. Fast and cheap test runs against a real agent: no speech models and
no audio, but the same tool, backend, log and outcome checks as a voice run. Most agents have a text
entry point (chat widget, SMS) or can get one with a small adapter in front of their "brain".

Vocto sends JSON POSTs to `connection.target`, one conversation per `session_id`:

```json
{"type": "start",   "session_id": "s-3f9a1c2b7e4d", "metadata": {"now": "2026-11-02T09:00:00+00:00", "test_id": "t-20261102-0001-a7c3"}}
{"type": "message", "session_id": "s-3f9a1c2b7e4d", "text": "Hi, I'd like to book a check-up."}
{"type": "stop",    "session_id": "s-3f9a1c2b7e4d"}
```

The agent answers `start` and `message` with its words, and says when it hangs up:

```json
{"reply": "Riverside Clinic, how can I help?", "end": false}
```

- `metadata.now` is the scenario's fixed "current time" (so "tomorrow" is stable). Use it if you can.
- `metadata.test_id` is sent when the profile's `test_id.propagation` has `session_metadata`. Write it
  on your log lines and pass it to your backend, so Vocto can find exactly what this call did.
- `stop` needs no particular answer.
- An HTTP error during a call is the agent failing (it counts against the agent); not being reachable
  at the start is a Vocto setup problem (an ERROR, never blamed on the agent).

`connection.options`:

| Option | Default | Meaning |
|---|---|---|
| `headers` | none | Sent with every request, e.g. `Authorization: "Bearer ${ACME_AGENT_TOKEN}"`. Secrets are named, never written: put `ACME_AGENT_TOKEN=...` in `.env`. |
| `reply_field` | `reply` | Where the agent's words are in its answer (dots for nesting: `data.text`). |
| `end_field` | `end` | Where the hang-up flag is. |
| `timeout_s` | `60` | How long to wait for one answer. |

The voice equivalent is [voice-protocol.md](voice-protocol.md). A working example of both is
`zoo/clinic/service.py` (the clinic agent run as an outside service).
