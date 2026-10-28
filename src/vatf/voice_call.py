"""One call in voice mode: the caller speaks through TTS, listens through STT, and every
turn's timing is read off the recorded audio.

Turn taking, the way a person on the phone does it:
  * after the caller finishes speaking, wait for the agent to start talking
    (up to the persona's silence limit, then say so to the caller as "(silence)");
  * the agent's turn is over when nothing more is heard for end_of_turn_ms and
    none of its audio is still queued to play;
  * then transcribe what was heard and let the caller answer.

Barge-in (scenario audio.barge_in): when the agent's n-th turn has been audible
for offset_ms, the caller starts saying the given line over it. The agent should
stop talking; how long that took is recorded on the interrupted turn.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

import numpy as np

from .audio.pcm import from_pcm16, resample, set_level, to_pcm16
from .audio.recorder import Line, Recorder
from .audio.vad import trim
from .contracts.models import AudioClip

POLL_S = 0.03
RECORDING = "audio/call.wav"


@dataclass
class Heard:
    start: int | None = None          # first agent sample of the turn (None: the agent said nothing)
    end: int | None = None            # last agent sample
    text: str = ""
    interrupted: bool = False
    stopped_after_ms: int | None = None
    barge: tuple[int, int, str] | None = None   # (start, end, words) of the caller's barge-in


@dataclass
class VoiceSettings:
    end_of_turn_ms: int = 1000        # silence that tells the caller the agent has finished
    no_reply_ms: int = 15000          # how long the caller waits for an answer before reacting
    max_turn_s: float = 90.0          # one agent turn may not go on longer than this
    barge_in: list[Mapping[str, Any]] = field(default_factory=list)


class VoiceConversation:
    def __init__(self, *, session: Any, recorder: Recorder, line: Line, tts: Any, stt: Any,
                 voice: str, speaking_rate: float, settings: VoiceSettings, writer: Any, proxy: Any):
        self.session, self.rec, self.line = session, recorder, line
        self.tts, self.stt = tts, stt
        self.voice, self.rate = voice, speaking_rate
        self.cfg = settings
        self.writer, self.proxy = writer, proxy
        self.agent_turns = 0
        self._transcripts_used = 0
        self.ms = recorder.ms

    # ---- the caller's mouth -------------------------------------------------
    async def synthesize(self, text: str) -> np.ndarray:
        clip = await asyncio.to_thread(self.tts.synthesize, text, self.voice, self.rate)
        x = resample(from_pcm16(clip.pcm16), clip.sample_rate_hz, self.rec.sr)
        return set_level(trim(x, self.rec.sr))

    async def say(self, text: str, turn: int, clip: np.ndarray | None = None,
                  emit: bool = True) -> tuple[int, int]:
        clip = await self.synthesize(text) if clip is None else clip
        start, end = self.line.speak(clip)
        self.proxy.turn = turn  # tool calls from here on belong to this caller turn
        if emit:
            self.emit_caller_turn(text, start, end, turn)
        return start, end

    def emit_caller_turn(self, text: str, start: int, end: int, turn: int, barge_in: bool = False) -> None:
        data: dict[str, Any] = {"text": text,
                                "audio": {"file": RECORDING, "start_ms": self.ms(start), "end_ms": self.ms(end)}}
        if barge_in:
            data["barge_in"] = True
        self.writer.emit("caller", "caller_turn", data, turn=turn, t_ms=self.ms(start))

    # ---- the caller's ears --------------------------------------------------
    async def transcribe(self, start: int, end: int) -> str:
        pad = self.rec.sr // 20
        x = self.rec.agent.slice(start - pad, end + pad)
        return (await asyncio.to_thread(self.stt.transcribe, AudioClip(to_pcm16(x), self.rec.sr))).text.strip()

    def _done(self) -> bool:
        return self.session.closed.is_set() and not self.rec.agent_playing()

    async def listen(self, since: int, caller_end: int | None, turn: int) -> Heard:
        """Wait for the agent's next turn, heard after sample `since`."""
        self.agent_turns += 1
        barges = [b for b in self.cfg.barge_in if b["agent_turn"] == self.agent_turns]
        barge_clip = await self.synthesize(barges[0]["say"]) if barges else None
        wait_from = max(since, caller_end or since)
        heard = Heard()
        deadline_wait = None
        started_at = time.monotonic()
        while True:
            if self.line.error:
                raise self.line.error
            now = self.rec.now_sample()
            spans = self.rec.agent_spans(since, now)
            if spans and heard.start is None:
                heard.start = spans[0][0]
            if heard.start is None:
                if now < wait_from:
                    await asyncio.sleep(POLL_S)
                    continue
                deadline_wait = deadline_wait or wait_from + self.cfg.no_reply_ms * self.rec.sr // 1000
                if now >= deadline_wait or self._done():
                    return heard                                  # nothing came
                await asyncio.sleep(POLL_S)
                continue
            if barge_clip is not None and now >= heard.start + barges[0]["offset_ms"] * self.rec.sr // 1000:
                if self.rec.agent_playing(now) or (spans and now - spans[-1][1] < self.rec.sr // 5):
                    return await self._barge_in(heard, barge_clip, barges[0], since, turn)
                barge_clip = None                                 # the agent had already finished
            last_end = spans[-1][1]
            quiet = now - last_end >= self.cfg.end_of_turn_ms * self.rec.sr // 1000
            if (quiet and not self.rec.agent_playing(now)) or self._done():
                heard.end = last_end
                heard.text = await self.transcribe(heard.start, heard.end)
                return heard
            if time.monotonic() - started_at > self.cfg.max_turn_s:
                heard.end = last_end
                heard.text = await self.transcribe(heard.start, heard.end)
                return heard
            await asyncio.sleep(POLL_S)

    async def _barge_in(self, heard: Heard, clip: np.ndarray, barge: Mapping[str, Any], since: int,
                        turn: int) -> Heard:
        # The caller_turn event is written after the interrupted agent turn, so the timeline reads in order.
        b_start, b_end = await self.say(barge["say"], turn + 1, clip=clip, emit=False)
        heard.barge = (b_start, b_end, barge["say"])
        heard.interrupted = True
        await self.line.wait_until(b_end + self.rec.sr // 20)
        # The agent's interrupted speech is the sound run that was going on when the caller cut in.
        limit = time.monotonic() + self.cfg.max_turn_s
        while True:
            now = self.rec.now_sample()
            spans = [s for s in self.rec.agent_spans(since, now) if s[1] > b_start]
            ongoing = spans and spans[0][0] <= b_start + self.rec.sr // 50
            if not ongoing:
                stop = b_start                                     # silent by the time the caller spoke
                break
            run_end = spans[0][1]
            if now - run_end >= self.rec.sr * 3 // 10 or time.monotonic() > limit or self._done():
                stop = run_end
                break
            await asyncio.sleep(POLL_S)
        heard.end = max(stop, heard.start or stop)
        heard.stopped_after_ms = self.ms(max(0, stop - b_start))
        heard.text = await self.transcribe(heard.start, heard.end)
        return heard

    def emit_agent_turn(self, heard: Heard, turn: int, caller_end: int | None) -> None:
        data: dict[str, Any] = {"heard_text": heard.text}
        if heard.start is None:
            data["silent"] = True
        else:
            data["audio"] = {"file": RECORDING, "start_ms": self.ms(heard.start), "end_ms": self.ms(heard.end)}
            if caller_end is not None:
                data["latency_ms"] = self.ms(heard.start) - self.ms(caller_end)
        if heard.interrupted:
            data["interrupted"] = True
            data["stopped_after_ms"] = heard.stopped_after_ms
            data["barge_in_at_ms"] = self.ms(heard.barge[0])
        # What the agent itself reported saying since the last turn (kept for comparison only;
        # the caller never sees it).
        hi = heard.end if heard.end is not None else self.rec.now_sample()
        own = [t for s, t in self.session.transcripts[self._transcripts_used:] if s <= hi]
        self._transcripts_used += len(own)
        if own:
            data["platform_transcript"] = " ".join(own)
        self.writer.emit("connector", "agent_turn", data, turn=turn,
                         t_ms=self.ms(heard.start) if heard.start is not None else None)
