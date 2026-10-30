"""twilio_media_stream connector: Vocto plays the part of Twilio and calls the agent's Media Streams server.

Many voice agents take phone calls through Twilio: Twilio opens a WebSocket to the agent's
server and streams the call as JSON messages with 8 kHz mu-law audio (Pipecat, Vocode, Bolna,
the OpenAI Realtime + Twilio samples, and many in-house agents). Vocto opens that same WebSocket
and sends the same messages, so the agent runs exactly as it would on a real phone call, with no
change to its code and no Twilio account. Full protocol: docs/twilio-protocol.md.

  vatf -> agent   {"event": "connected", "protocol": "Call", "version": "1.0.0"}
                  {"event": "start", "start": {"streamSid", "callSid", "accountSid", "tracks": ["inbound"],
                   "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
                   "customParameters": {...}}, "streamSid": ...}
                  {"event": "media", "media": {"track": "inbound", "chunk", "timestamp", "payload"}}
                         every 20 ms for the whole call (payload: base64 mu-law, 160 bytes)
                  {"event": "mark", "mark": {"name"}}   once the agent audio before the agent's mark has played
                  {"event": "stop", "stop": {...}}      the caller hung up
  agent -> vatf   {"event": "media", "media": {"payload"}}  agent audio, any length, may come faster than real time
                  {"event": "mark", "mark": {"name"}}       tell me when the audio sent so far has played
                  {"event": "clear"}                        drop audio not yet played (barge-in)
                  closing the WebSocket                     the agent hung up

customParameters are what a real deployment passes with TwiML <Parameter>: connection.options.
custom_parameters (secrets as ${NAME}), plus test_id and now when the profile's test_id.propagation
has session_metadata, so the agent can put the test id on its tool calls and log lines.

Like websocket_audio, connection.launch may name an in-repo agent ("module:function") that vatf
starts for each call; the zoo clinic answers in this format too (examples/clinic/profile-twilio.yaml).
"""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from ..audio.g711 import pcm16_to_ulaw, ulaw_to_pcm16
from ..audio.pcm import from_pcm16, to_pcm16
from ..audio.recorder import Recorder
from ..contracts.models import CallContext
from ..util import expand_env
from . import ConnectError

CONNECT_TIMEOUT_S = 15
SAMPLE_RATE = 8000          # Twilio Media Streams are always 8 kHz mu-law, mono
JITTER_MS = 60              # default playout buffer, see TwilioStreamSession.play


def _sid(prefix: str) -> str:
    return prefix + secrets.token_hex(16)


def start_messages(ctx: CallContext | None, profile: Mapping[str, Any], *, stream_sid: str, call_sid: str,
                   account_sid: str) -> list[dict[str, Any]]:
    """The two messages Twilio sends when a call's stream starts."""
    conn = profile["connection"]
    params = {str(k): str(v) for k, v in expand_env(conn.get("options", {}).get("custom_parameters") or {}).items()}
    if ctx is not None:
        now = ctx.scenario.get("setup", {}).get("now")
        if now:
            params.setdefault("now", str(now))
        if "session_metadata" in profile.get("test_id", {}).get("propagation", []):
            params["test_id"] = ctx.test_id
    return [
        {"event": "connected", "protocol": "Call", "version": "1.0.0"},
        {"event": "start", "sequenceNumber": "1", "streamSid": stream_sid,
         "start": {"accountSid": account_sid, "streamSid": stream_sid, "callSid": call_sid,
                   "tracks": ["inbound"], "customParameters": params,
                   "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": SAMPLE_RATE, "channels": 1}}},
    ]


class TwilioStreamSession:
    def __init__(self, ws: Any, recorder: Recorder, *, stream_sid: str, call_sid: str, account_sid: str,
                 launched: Any = None, jitter_ms: int = JITTER_MS):
        self._ws = ws
        self._launched = launched
        self._jitter = jitter_ms * recorder.sr // 1000
        self.rec = recorder
        self.stream_sid, self.call_sid, self.account_sid = stream_sid, call_sid, account_sid
        self.closed = asyncio.Event()
        self.closed_by_agent = False
        self.close_reason = ""
        self.transcripts: list[tuple[int, str]] = []    # Twilio carries no transcripts; kept for the call loop
        self.protocol_errors: list[str] = []
        self.marks: list[tuple[str, int]] = []           # (name, sample) the agent's marks still waiting to play
        self._seq = 1                                    # sequenceNumber of the last message sent
        self._chunk = 0
        self._sent_samples = 0
        self._send_lock = asyncio.Lock()
        self._reader = asyncio.create_task(self._read())
        self._marker = asyncio.create_task(self._echo_marks())

    async def _send(self, msg: dict[str, Any]) -> None:
        async with self._send_lock:
            self._seq += 1
            await self._ws.send(json.dumps({**msg, "sequenceNumber": str(self._seq), "streamSid": self.stream_sid}))

    async def _read(self) -> None:
        import websockets

        try:
            async for raw in self._ws:
                if isinstance(raw, (bytes, bytearray)):
                    self._error("binary message (Twilio streams send JSON text only)")
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    self._error(f"not JSON: {raw[:80]!r}")
                    continue
                if not isinstance(msg, dict):
                    self._error(f"not a JSON object: {raw[:80]!r}")
                    continue
                event = msg.get("event")
                if event == "media":
                    payload = (msg.get("media") or {}).get("payload")
                    try:
                        audio = base64.b64decode(payload or "", validate=True)
                    except (ValueError, TypeError):
                        self._error("media payload is not base64")
                        continue
                    if audio:
                        self.play(from_pcm16(ulaw_to_pcm16(audio)))
                elif event == "mark":
                    name = str((msg.get("mark") or {}).get("name", ""))
                    self.marks.append((name, self.rec.cursor))    # played once the queue reaches here
                elif event == "clear":
                    self.rec.clear()
                    await self._release_marks(everything=True)    # Twilio returns pending marks on clear
                # Other messages (e.g. an agent's own JSON for its web client) are not part of the call audio.
        except websockets.ConnectionClosed as e:
            self.closed_by_agent, self.close_reason = True, f"the agent closed the connection ({e.rcvd})"
        except Exception as e:  # noqa: BLE001 - any read failure ends the call
            self.closed_by_agent, self.close_reason = True, f"connection error: {type(e).__name__}: {e}"
        finally:
            if not self.close_reason:
                self.closed_by_agent, self.close_reason = True, "the agent hung up (closed the stream)"
            self.closed.set()

    def play(self, x: np.ndarray) -> None:
        """Queue agent audio the way a phone network's playout (jitter) buffer does.

        Agents that pace their audio in real time (Pipecat does) send each chunk just in time, so
        some arrive a few ms after the previous one has finished playing. A phone line's jitter
        buffer plays those on seamlessly instead of putting a gap in the middle of a word. A chunk
        that comes later than that (a real pause, or the start of the next answer) plays when it
        arrives, so the response time of an answer is never moved."""
        now = self.rec.now_sample()
        cursor = self.rec.cursor
        if self._jitter and self.rec.chunks and cursor < now <= cursor + self._jitter:
            self.rec.agent_audio(x, arrival_s=cursor / self.rec.sr)    # straight after the audio before it
        else:
            self.rec.agent_audio(x)

    def _error(self, text: str) -> None:
        if len(self.protocol_errors) < 20:
            self.protocol_errors.append(text)

    async def _release_marks(self, everything: bool = False) -> None:
        now = self.rec.now_sample()
        while self.marks and (everything or self.marks[0][1] <= now):
            name, _ = self.marks.pop(0)
            try:
                await self._send({"event": "mark", "mark": {"name": name}})
            except Exception:
                return

    async def _echo_marks(self) -> None:
        while not self.closed.is_set():
            if self.marks:
                await self._release_marks()
            await asyncio.sleep(0.02)

    async def send_frame(self, x: np.ndarray) -> None:
        import websockets

        if self.closed.is_set():
            return
        timestamp = self._sent_samples * 1000 // SAMPLE_RATE
        self._chunk += 1
        self._sent_samples += len(x)
        payload = base64.b64encode(pcm16_to_ulaw(to_pcm16(x))).decode("ascii")
        try:
            await self._send({"event": "media", "media": {"track": "inbound", "chunk": str(self._chunk),
                                                          "timestamp": str(timestamp), "payload": payload}})
        except websockets.ConnectionClosed:
            pass  # the agent hung up; the reader records it, the line just goes quiet

    async def send_text(self, text: str) -> None:
        raise NotImplementedError("twilio_media_stream is a voice connector: the caller speaks")

    async def close(self) -> None:
        if not self.closed.is_set():
            try:
                await self._send({"event": "stop", "stop": {"accountSid": self.account_sid, "callSid": self.call_sid}})
            except Exception:
                pass
        try:
            await self._ws.close()
        except Exception:
            pass
        for task in (self._reader, self._marker):
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if self._launched is not None:
            try:
                await self._launched.close()
            finally:
                self._launched = None


class TwilioStreamConnector:
    mode = "voice"
    sample_rate = SAMPLE_RATE

    def __init__(self, profile: Mapping[str, Any], root: Path | None = None, tools: Any = None):
        self._profile = profile
        self._root = root or Path(".")
        self._tools = tools

    async def open(self, ctx: CallContext | None, recorder: Recorder) -> TwilioStreamSession:
        import websockets

        conn = self._profile["connection"]
        if recorder.sr != SAMPLE_RATE:
            raise ConnectError(f"Twilio streams are 8 kHz; the recorder runs at {recorder.sr} Hz")
        launched = None
        if conn.get("launch"):
            from .websocket_audio import launch_agent

            launched = await launch_agent(conn, self._root, self._tools)
            url = launched.url
        else:
            url = conn.get("target", "")
            if not url.startswith(("ws://", "wss://")):
                raise ConnectError(f"connection.target must be the agent's ws:// or wss:// stream URL, got {url!r}")
        options = conn.get("options", {})
        stream_sid, call_sid = _sid("MZ"), _sid("CA")
        try:
            account_sid = str(expand_env(options.get("account_sid") or "")) or _sid("AC")
            headers = {str(k): str(v) for k, v in expand_env(options.get("headers") or {}).items()}
            first = start_messages(ctx, self._profile, stream_sid=stream_sid, call_sid=call_sid,
                                   account_sid=account_sid)
            ws = await asyncio.wait_for(websockets.connect(url, max_size=None, ping_interval=None,
                                                           additional_headers=headers or None),
                                        CONNECT_TIMEOUT_S)
            for msg in first:
                await ws.send(json.dumps(msg))
        except Exception as e:
            if launched is not None:
                await launched.close()
            raise ConnectError(f"could not connect to {url}: {type(e).__name__}: {e}") from e
        return TwilioStreamSession(ws, recorder, stream_sid=stream_sid, call_sid=call_sid, account_sid=account_sid,
                                   launched=launched, jitter_ms=int(options.get("jitter_buffer_ms", JITTER_MS)))
