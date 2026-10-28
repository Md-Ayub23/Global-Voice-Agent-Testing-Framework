# vatf voice protocol (websocket_audio)

How vatf talks to a voice agent over a WebSocket. One connection is one phone call.
It is deliberately close to what phone platforms already do (Twilio Media Streams,
Pipecat and Vapi WebSocket transports), so a thin adapter is enough to put a real
agent behind it.

## Messages

| Direction | Kind | Message | Meaning |
|---|---|---|---|
| vatf -> agent | text | `{"type": "start", "sample_rate": 16000, "encoding": "pcm16", "metadata": {"now": "...", "test_id": "..."}}` | First message. `test_id` is sent when the profile lists `session_metadata` in `test_id.propagation`. |
| vatf -> agent | binary | 20 ms of PCM16 mono little-endian audio | Sent in real time for the whole call: speech, or silence / background noise between turns, like a phone line. |
| vatf -> agent | text | `{"type": "stop"}` | The caller hung up. vatf then closes the socket. |
| agent -> vatf | binary | PCM16 audio, any chunk size | May be sent faster than real time; vatf plays it out in order, like a phone's playout buffer. |
| agent -> vatf | text | `{"type": "clear"}` | Drop audio sent but not yet played. This is how an agent stops talking when the caller interrupts. |
| agent -> vatf | text | `{"type": "transcript", "text": "..."}` | Optional. What the agent says it is saying; stored as `platform_transcript` for comparison only. The simulated caller never sees it. |
| agent -> vatf | text | `{"type": "end"}` | The agent hung up (closing the socket means the same). |

## Timing

vatf records both sides on one clock that starts with the call (`t_ms` in `events.jsonl`).

- The caller's frame holding samples `[s, s + 320)` leaves when its last sample has been "spoken",
  so sample `n` of the caller's track was spoken at `n / sample_rate` seconds.
- Each agent chunk is placed on the agent's track at `max(arrival time, end of audio already queued)`.
  A `clear` removes everything after the moment it arrived.
- Both tracks are saved as one stereo file per call, `audio/call.wav` (left caller, right agent),
  so 3.250 s in an audio editor is `t_ms` 3250 in the timeline.

**Response time** (latency) of an answer = first sound of the agent's answer on the agent track
minus the last sound of the caller's words on the caller track. Edges are where the waveform
rises above about -40 dBFS, which is what you see by eye in an audio editor.

## Connecting an agent

- **Python agent started by vatf** (the zoo agents): `connection.launch: "module:function"`.
  vatf calls `launch(tools=<tool proxy>, options=<connection.options>)` for each call. It returns an
  object with `.url` and `async close()`. Tool calls go through vatf's tool proxy, so they are recorded
  and faults can be injected.
- **Agent already running elsewhere**: leave out `launch` and set `connection.target: ws://host:port/path`.
  vatf hears and times it, but it cannot see the agent's tool calls: an HTTP form of the tool proxy
  for outside agents is planned (Phase 4/6). Until then, checks on tool calls and the outcome check
  are not reliable for such an agent; speech, timing and backend probes are.
