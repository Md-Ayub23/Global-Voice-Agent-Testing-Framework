"""Joining a WebRTC room as the caller: LiveKit agents, and Retell web calls (Retell's web calls run on
LiveKit rooms; its own web SDK joins them the same way).

The caller publishes one microphone track fed with Vocto's 20 ms frames, listens to the agent's audio
track, and reads what the room sends besides audio:
  - text streams on topic "lk.transcription": LiveKit Agents' transcriptions (the agent's words, and what
    its speech recognition heard from the caller);
  - data packets: Retell's call events (from the participant "server"), and on topic "vocto.tool" a tool
    call an agent reports itself (docs/platforms-guide.md has the three lines a LiveKit agent needs).

RoomLink is the part that talks to LiveKit (livekit.rtc). Tests give a fake one (ROOM_LINK).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Mapping

import numpy as np

from ...audio.pcm import to_pcm16
from ...audio.recorder import Recorder
from .base import SAMPLE_RATE, PlatformError, PlatformSession, Secrets

MAX_REPORTED_TOOLS = 500        # a runaway agent cannot fill memory with reports
TOOL_TOPIC = "vocto.tool"
TRANSCRIPTION_TOPIC = "lk.transcription"
CONNECT_TIMEOUT_S = 20
AGENT_WAIT_S = 20


class RoomLink:
    """One participant in a LiveKit room, through the livekit.rtc SDK."""

    def __init__(self) -> None:
        self.room: Any = None
        self.source: Any = None
        self.identity = ""
        self._streams: list[asyncio.Task] = []
        self.on_audio: Callable[[np.ndarray, str], None] = lambda x, who: None
        self.on_data: Callable[[bytes, str | None, str | None], None] = lambda data, topic, who: None
        self.on_text: Callable[[str, str, Mapping[str, str]], None] = lambda text, who, attrs: None
        self.on_left: Callable[[str, bool], None] = lambda who, agent: None
        self.on_closed: Callable[[str], None] = lambda reason: None
        self.pick_track: Callable[[str, str, bool], bool] = lambda track_name, who, agent: True

    async def connect(self, url: str, token: str) -> None:
        try:
            from livekit import rtc
        except ImportError as e:  # pragma: no cover - a dependency of Vocto
            raise PlatformError("the livekit package is missing: run `uv sync`") from e
        room = rtc.Room()
        self.room = room

        @room.on("track_subscribed")
        def _track(track: Any, publication: Any, participant: Any) -> None:
            if track.kind != rtc.TrackKind.KIND_AUDIO:
                return
            agent = participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT
            if not self.pick_track(publication.name or "", participant.identity, agent):
                return
            self._streams.append(asyncio.ensure_future(self._listen(rtc, track, participant.identity)))

        @room.on("data_received")
        def _data(packet: Any) -> None:
            who = packet.participant.identity if packet.participant is not None else None
            self.on_data(bytes(packet.data), packet.topic, who)

        @room.on("participant_disconnected")
        def _left(participant: Any) -> None:
            self.on_left(participant.identity, participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT)

        @room.on("disconnected")
        def _closed(reason: Any = None) -> None:
            self.on_closed(str(reason) if reason is not None else "")

        def _text(reader: Any, who: str) -> None:
            async def read() -> None:
                try:
                    text = await reader.read_all()
                except Exception:  # noqa: BLE001 - a broken stream loses one transcript, not the call
                    return
                self.on_text(text, who, dict(reader.info.attributes or {}))
            self._streams.append(asyncio.ensure_future(read()))

        room.register_text_stream_handler(TRANSCRIPTION_TOPIC, _text)
        await asyncio.wait_for(room.connect(url, token, rtc.RoomOptions(auto_subscribe=True)), CONNECT_TIMEOUT_S)
        self.identity = room.local_participant.identity
        self.source = rtc.AudioSource(SAMPLE_RATE, 1)
        track = rtc.LocalAudioTrack.create_audio_track("vocto-caller", self.source)
        opts = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        await room.local_participant.publish_track(track, opts)
        # Tracks published before Vocto joined are subscribed during connect(): the handler above saw them.

    async def _listen(self, rtc: Any, track: Any, who: str) -> None:
        stream = rtc.AudioStream(track, sample_rate=SAMPLE_RATE, num_channels=1)
        try:
            async for ev in stream:
                frame = ev.frame
                x = np.frombuffer(bytes(frame.data), dtype="<i2").astype(np.float32) / 32768.0
                self.on_audio(x, who)
        finally:
            await stream.aclose()

    async def push(self, x: np.ndarray) -> None:
        from livekit import rtc

        if self.source is None:
            return
        data = to_pcm16(x)
        await self.source.capture_frame(rtc.AudioFrame(data, SAMPLE_RATE, 1, len(data) // 2))

    async def close(self) -> None:
        for t in self._streams:
            t.cancel()
        if self.room is not None:
            try:
                await self.room.disconnect()
            except Exception:  # noqa: BLE001
                pass
        if self.source is not None:
            try:
                await self.source.aclose()
            except Exception:  # noqa: BLE001
                pass


ROOM_LINK: Callable[[], Any] = RoomLink      # tests put a fake room here


class RoomSession(PlatformSession):
    """A call in a WebRTC room. Subclasses say which participant and track is the agent."""

    agent_track_name: str | None = None      # e.g. Retell's "agent_audio"; None: any agent's audio

    def __init__(self, link: Any, recorder: Recorder, secrets: Secrets):
        super().__init__(recorder, secrets)
        self.link = link
        self.agent_identity: str | None = None
        self.agent_joined = asyncio.Event()
        self._segments: dict[str, int] = {}     # transcription segment id -> index in transcripts / heard
        self.reported_tools: list[dict[str, Any]] = []   # tool calls the agent reported itself (vocto.tool)
        link.pick_track = self._is_agent_track
        link.on_audio = self._audio
        link.on_data = self._data
        link.on_text = self._text
        link.on_left = self._left
        link.on_closed = lambda reason: self.ended(f"the room closed{f' ({reason})' if reason else ''}")

    def _is_agent_track(self, track_name: str, who: str, agent: bool) -> bool:
        if self.agent_track_name is not None:
            ok = track_name == self.agent_track_name
        else:
            ok = agent or self.agent_identity is None
        if ok:
            self.agent_identity = who
            self.agent_joined.set()
        return ok

    def _audio(self, x: np.ndarray, who: str) -> None:
        if who == self.agent_identity and not self.closed.is_set():
            self.play(x)

    def _left(self, who: str, agent: bool) -> None:
        if who == self.agent_identity or (agent and self.agent_identity is None):
            self.ended("the agent left the call")

    def _text(self, text: str, who: str, attrs: Mapping[str, str]) -> None:
        """LiveKit Agents' transcriptions. Segments arrive as streams; a segment id seen again replaces it."""
        text = (text or "").strip()
        if not text:
            return
        mine = who == getattr(self.link, "identity", "")      # the agent sends the caller's transcript as the caller
        target = self.heard if mine else self.transcripts
        seg = attrs.get("lk.segment_id")
        key = f"{'h' if mine else 'a'}:{seg}" if seg else None
        if key and key in self._segments and self._segments[key] < len(target):
            i = self._segments[key]
            target[i] = (target[i][0], text)
            return
        if key:
            self._segments[key] = len(target)
        target.append((self.rec.now_sample(), text))

    def _data(self, data: bytes, topic: str | None, who: str | None) -> None:
        if topic == TOOL_TOPIC:
            self._reported_tool(data)

    def _reported_tool(self, data: bytes) -> None:
        """An agent's own report of a tool call: {"id", "name", "args", "result", "ok"} (docs/platforms-guide.md)."""
        try:
            doc = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self.error("a vocto.tool message that is not JSON")
            return
        items = doc if isinstance(doc, list) else [doc]
        for d in items:
            if len(self.reported_tools) >= MAX_REPORTED_TOOLS:
                if not getattr(self, "_too_many_tools", False):
                    self._too_many_tools = True
                    self.error(f"more than {MAX_REPORTED_TOOLS} vocto.tool reports in one call: the rest are left out")
                return
            if not isinstance(d, Mapping) or not d.get("name"):
                self.error("a vocto.tool message without a tool name")
                continue
            self.reported_tools.append({"kind": "tool", "name": str(d["name"])[:120],
                                        "args": dict(d["args"]) if isinstance(d.get("args"), Mapping) else {},
                                        "result": d.get("result"), "ok": bool(d.get("ok", True)),
                                        "at_ms": self.now_ms() - self.t0_ms, "id": str(d.get("id") or "") or None})

    async def wait_for_agent(self, seconds: float = AGENT_WAIT_S) -> None:
        try:
            await asyncio.wait_for(self.agent_joined.wait(), seconds)
        except asyncio.TimeoutError:
            raise PlatformError(f"the agent did not join the call within {int(seconds)} s "
                                "(is the agent running and dispatched to new rooms?)") from None

    async def send_frame(self, x: np.ndarray) -> None:
        if self.closed.is_set():
            return
        try:
            await self.link.push(x)
        except Exception as e:  # noqa: BLE001 - the line goes quiet; the room's own events end the call
            self.error(f"sending audio failed: {type(e).__name__}: {e}")

    async def close(self) -> None:
        self._closing = True
        try:
            await self.link.close()
        finally:
            self.closed.set()
