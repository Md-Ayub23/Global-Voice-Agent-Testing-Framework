"""A voice front-end that turns a zoo text agent into a phone agent, served over WebSocket.

This is agent code, not framework code: it plays the part of a real voice
platform (like a small Pipecat or Vapi pipeline) so the framework has a voice
agent to test. It speaks the protocol in docs/voice-protocol.md.

Pipeline per call:
  caller audio -> endpointer (finds when the caller starts and stops talking)
               -> speech to text -> text agent.reply() -> speak_text() -> text to speech
               -> audio back, streamed sentence by sentence as fast as it is made

Barge-in: if the caller talks for barge_in_ms while the agent is still being
heard, the gateway sends {"type": "clear"} and stops its current answer.

The speech engines come from vatf.speech for convenience only; any engines would do.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np

from vatf.audio.pcm import from_pcm16, resample, set_level, to_pcm16
from vatf.contracts.models import AudioClip

FRAME_MS = 20
SEND_CHUNK_MS = 100


class Endpointer:
    """Energy-based speech detector with an adaptive noise floor, fed 20 ms frames."""

    def __init__(self, sample_rate: int, *, end_silence_ms: int = 600, start_ms: int = 60,
                 preroll_ms: int = 200, margin_db: float = 9.0):
        self.sr = sample_rate
        self.floor: float | None = None
        self.margin = margin_db
        self.max_frames = 20_000 // FRAME_MS   # an utterance longer than 20 s is cut there
        self.start_frames = max(1, start_ms // FRAME_MS)
        self.end_frames = max(1, end_silence_ms // FRAME_MS)
        self.preroll = max(1, preroll_ms // FRAME_MS)
        self.history: list[np.ndarray] = []
        self.in_speech = False
        self.run = 0          # consecutive speech frames (before start) / silent frames (in speech)
        self.speech_frames = 0
        self.utterance: list[np.ndarray] = []

    def feed(self, x: np.ndarray) -> tuple[str | None, np.ndarray | None]:
        """Returns ("start", None), ("end", audio) or (None, None)."""
        level = 20 * np.log10(max(float(np.sqrt(np.mean(x.astype(np.float64) ** 2))), 1e-6))
        if self.floor is None:
            self.floor = level  # the line's first frame sets the background level
        speech = level > max(self.floor + self.margin, -50.0)
        # Follow the background: down at once, up slowly (and very slowly while "speech" is
        # heard, so a background that got louder cannot keep the detector stuck in speech).
        if level < self.floor:
            self.floor = level
        else:
            self.floor += 0.02 if speech else 0.05 * (level - self.floor)
        if not self.in_speech:
            self.history = (self.history + [x])[-self.preroll - self.start_frames:]
            self.run = self.run + 1 if speech else 0
            if self.run >= self.start_frames:
                self.in_speech, self.run = True, 0
                self.speech_frames = self.start_frames
                self.utterance = list(self.history)
                return "start", None
            return None, None
        self.utterance.append(x)
        if speech:
            self.run, self.speech_frames = 0, self.speech_frames + 1
        else:
            self.run += 1
        if self.run >= self.end_frames or len(self.utterance) >= self.max_frames:
            audio = np.concatenate(self.utterance)
            self.in_speech, self.run, self.utterance, self.history = False, 0, [], []
            return "end", audio
        return None, None


@dataclass
class Launched:
    url: str
    server: Any

    async def close(self) -> None:
        self.server.close()
        await self.server.wait_closed()


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p for p in parts if p]


class VoiceGateway:
    def __init__(self, build_agent: Callable[[dict], Any], *, tts: Any, stt: Any, voice: str,
                 speak_text: Callable[[str], str] = lambda t: t, end_silence_ms: int = 600,
                 barge_in_ms: int = 300):
        self.build_agent = build_agent
        self.tts, self.stt, self.voice = tts, stt, voice
        self.speak_text = speak_text
        self.end_silence_ms = end_silence_ms
        self.barge_in_ms = barge_in_ms

    async def launch(self, host: str = "127.0.0.1", port: int = 0) -> Launched:
        import websockets

        server = await websockets.serve(self.handle, host, port, max_size=None, ping_interval=None)
        port = server.sockets[0].getsockname()[1]
        return Launched(f"ws://{host}:{port}", server)

    async def handle(self, ws: Any) -> None:
        start = json.loads(await ws.recv())
        if start.get("type") != "start":
            await ws.close(1002, "expected a start message")
            return
        call = _Call(self, ws, int(start.get("sample_rate", 16000)), start.get("metadata") or {})
        await call.run()


class _Call:
    def __init__(self, gw: VoiceGateway, ws: Any, sample_rate: int, metadata: dict):
        self.gw, self.ws, self.sr = gw, ws, sample_rate
        self.metadata = metadata
        self.endpointer = Endpointer(sample_rate, end_silence_ms=gw.end_silence_ms)
        self.play_end = 0.0               # monotonic time when the caller will have heard everything sent
        self.speaking: asyncio.Task | None = None
        self.utterances: asyncio.Queue[np.ndarray] = asyncio.Queue()
        self.agent: Any = None
        self.hung_up = False

    def playing(self) -> bool:
        return time.monotonic() < self.play_end

    async def run(self) -> None:
        self.agent = await asyncio.to_thread(self.gw.build_agent, dict(self.metadata))
        greeting = await asyncio.to_thread(self.agent.start)
        self.speaking = asyncio.create_task(self.say(greeting))
        worker = asyncio.create_task(self.answer_loop())
        try:
            async for msg in self.ws:
                if isinstance(msg, str):
                    if json.loads(msg).get("type") == "stop":
                        break
                    continue
                event, audio = self.endpointer.feed(from_pcm16(msg))
                if (self.endpointer.in_speech and self.playing()
                        and self.endpointer.speech_frames * FRAME_MS >= self.gw.barge_in_ms):
                    await self.barge_in()
                if event == "end":
                    await self.utterances.put(audio)
                if self.hung_up:
                    break
        except Exception:
            pass  # the caller went away
        finally:
            worker.cancel()
            if self.speaking:
                self.speaking.cancel()
            close = getattr(self.agent, "close", None)
            if callable(close):
                await asyncio.to_thread(close)

    async def barge_in(self) -> None:
        if self.speaking and not self.speaking.done():
            self.speaking.cancel()
        self.play_end = 0.0
        await self.ws.send(json.dumps({"type": "clear"}))

    async def answer_loop(self) -> None:
        while True:
            audio = await self.utterances.get()
            heard = (await asyncio.to_thread(self.gw.stt.transcribe, AudioClip(to_pcm16(audio), self.sr))).text
            if not heard.strip():
                continue
            if self.speaking and not self.speaking.done():
                try:
                    await self.speaking
                except asyncio.CancelledError:
                    pass
            reply = await asyncio.to_thread(self.agent.reply, heard)
            self.speaking = asyncio.create_task(self.say(reply))
            if getattr(self.agent, "ended", False):
                try:
                    await self.speaking
                except asyncio.CancelledError:
                    pass
                await asyncio.sleep(max(0.0, self.play_end - time.monotonic()) + 0.3)
                await self.ws.send(json.dumps({"type": "end"}))
                self.hung_up = True
                await self.ws.close()
                return

    async def say(self, text: str) -> None:
        if text.strip():
            await self.ws.send(json.dumps({"type": "transcript", "text": text}))
        for sentence in split_sentences(self.gw.speak_text(text)):
            clip = await asyncio.to_thread(self.gw.tts.synthesize, sentence, self.gw.voice)
            x = set_level(resample(from_pcm16(clip.pcm16), clip.sample_rate_hz, self.sr))
            x = np.concatenate([x, np.zeros(int(0.2 * self.sr), np.float32)])  # pause after a sentence
            step = self.sr * SEND_CHUNK_MS // 1000
            for i in range(0, len(x), step):
                chunk = x[i:i + step]
                await self.ws.send(to_pcm16(chunk))
                self.play_end = max(self.play_end, time.monotonic()) + len(chunk) / self.sr
