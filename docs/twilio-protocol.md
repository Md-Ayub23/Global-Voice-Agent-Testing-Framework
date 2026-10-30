# Twilio Media Streams (twilio_media_stream)

Many voice agents already take phone calls through Twilio. When a call comes in, Twilio opens a
WebSocket to the agent's server (the `<Stream url="...">` in the agent's TwiML) and streams the
call over it as JSON messages. Pipecat, Vocode, Bolna, LiveKit's telephony samples, the OpenAI
Realtime + Twilio demos and many in-house agents work this way.

With `connection.type: twilio_media_stream`, Vocto plays the part of Twilio: it opens that same
WebSocket and sends the same messages. The agent runs exactly as it would on a real phone call,
with no change to its code and no Twilio account or phone number.

```yaml
connection:
  type: twilio_media_stream
  target: wss://agent.example.com/media      # the <Stream url> of the agent's TwiML
  options:
    custom_parameters: {shop: "downtown", api_key: "${SHOP_KEY}"}   # the TwiML <Parameter>s
    account_sid: ${TWILIO_ACCOUNT_SID}         # optional: only if the agent checks it
    jitter_buffer_ms: 60                       # optional, default 60
test_id:
  propagation: [proxy_header, session_metadata]   # session_metadata: test_id goes in customParameters
```

In the dashboard: **Set up > Agent & connection > Voice > Voice format: Twilio Media Streams**.

## Messages

| Direction | Message | Meaning |
|---|---|---|
| Vocto -> agent | `{"event": "connected", "protocol": "Call", "version": "1.0.0"}` | The stream is open. |
| Vocto -> agent | `{"event": "start", "streamSid", "start": {"streamSid", "callSid", "accountSid", "tracks": ["inbound"], "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1}, "customParameters": {...}}}` | The call starts. `customParameters` holds `options.custom_parameters`, plus `test_id` (with `session_metadata`) and `now` (the scenario's time). |
| Vocto -> agent | `{"event": "media", "media": {"track": "inbound", "chunk", "timestamp", "payload"}}` | Every 20 ms for the whole call: 160 bytes of 8 kHz mu-law, base64. Speech, or silence and line noise between turns. |
| Vocto -> agent | `{"event": "mark", "mark": {"name"}}` | Sent back once the agent audio before the agent's own mark has played (or at once after a `clear`, as Twilio does). |
| Vocto -> agent | `{"event": "stop", "stop": {"accountSid", "callSid"}}` | The caller hung up. Vocto then closes the socket. |
| agent -> Vocto | `{"event": "media", "streamSid", "media": {"payload"}}` | Agent audio: mu-law 8 kHz, any length, may be sent faster than real time. |
| agent -> Vocto | `{"event": "mark", "streamSid", "mark": {"name"}}` | "Tell me when everything I sent so far has played." |
| agent -> Vocto | `{"event": "clear", "streamSid"}` | Drop audio not yet played: the agent stops talking because the caller cut in. |
| agent -> Vocto | closing the WebSocket | The agent hung up. |

Every message Vocto sends carries `sequenceNumber` (counting up from 1) and `streamSid`, as Twilio's do.
Binary frames or broken JSON from the agent are noted as protocol problems in the call (and shown by
**Test connection**); they don't end the call.

## Timing and the jitter buffer

The audio is 8 kHz mu-law (G.711), the same as a phone line, so Vocto's caller speech goes through
the same narrowband channel a real caller's does. Vocto's encoder is bit-exact with the standard
G.711 tables.

Agents that pace their audio in real time (Pipecat does) send each chunk just in time, so some
arrive a few milliseconds after the previous one finished playing. A phone network's playout
buffer plays those on without a gap, and so does Vocto: audio that arrives within
`jitter_buffer_ms` (60 ms) of the end of the queued audio is joined on. Audio that comes later than
that (a real pause, or the start of the next answer) plays when it arrives, so the measured
response time of an answer is never moved by the buffer.

Response times, barge-in (how fast the agent stops after a `clear`), noise and read-back checks
work exactly as for the Vocto voice protocol ([voice-protocol.md](voice-protocol.md)).

## Checking an agent

**Test connection** on the Agent page makes an 8 second silent call and reports whether the
agent greeted the caller, after how long, and any protocol problems. A phone agent should speak
first.

The zoo clinic agent answers in this format too: `examples/clinic/profile-twilio.yaml` (started by
Vocto for each call) and `examples/clinic-service/profile-twilio.yaml` (the running service).
A real open-source agent tested this way: [real-agents-guide.md](real-agents-guide.md).
