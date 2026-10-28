"""The call recorder and the caller's phone line.

One clock for everything: the call clock of the evidence store (seconds since
call_started). Sample n of a track is heard at n / sample_rate seconds on that
clock, so the saved call.wav lines up with events.jsonl exactly: 3.250 s in an
audio editor is t_ms 3250 in the timeline.

  caller track  what our caller put on the line (speech + background noise),
                written frame by frame as it is sent
  agent track   what the caller heard: each audio chunk from the agent is
                played at max(arrival time, end of what is already queued),
                like a phone's playout buffer. A "clear" message from the
                agent drops the audio not yet played (how agents stop talking
                when interrupted).

Latency is measured on these tracks: end of the caller's speech (known exactly,
we synthesized it) to the first sound of the agent's answer as heard.
"""

from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

import numpy as np

from .vad import speech_spans

FRAME_MS = 20


class Track:
    """A growable float32 track."""

    def __init__(self, sample_rate: int, seconds: float = 60):
        self.sr = sample_rate
        self._buf = np.zeros(int(seconds * sample_rate), dtype=np.float32)
        self.length = 0

    def _ensure(self, n: int) -> None:
        if n > len(self._buf):
            bigger = np.zeros(max(n, 2 * len(self._buf)), dtype=np.float32)
            bigger[: self.length] = self._buf[: self.length]
            self._buf = bigger

    def write(self, start: int, x: np.ndarray) -> None:
        self._ensure(start + len(x))
        self._buf[start:start + len(x)] = x
        self.length = max(self.length, start + len(x))

    def zero(self, start: int, end: int) -> None:
        if end > start:
            self._buf[start:min(end, len(self._buf))] = 0

    def slice(self, start: int, end: int) -> np.ndarray:
        start, end = max(0, start), min(end, self.length)
        return self._buf[start:end].copy() if end > start else np.zeros(0, dtype=np.float32)


@dataclass
class Clear:
    at_sample: int
    dropped_samples: int


class Recorder:
    def __init__(self, sample_rate: int, clock: Callable[[], float]):
        self.sr = sample_rate
        self.clock = clock                      # seconds on the call clock
        self.caller = Track(sample_rate)
        self.agent = Track(sample_rate)
        self.cursor = 0                         # end of the agent audio queued for playout (sample)
        self.clears: list[Clear] = []
        self.chunks = 0

    def now_sample(self) -> int:
        return int(self.clock() * self.sr)

    def ms(self, sample: int) -> int:
        return int(round(sample * 1000 / self.sr))

    # ---- agent side ---------------------------------------------------------
    def agent_audio(self, x: np.ndarray, arrival_s: float | None = None) -> int:
        """Queue a chunk from the agent; returns the sample where it starts playing."""
        arrival = int((self.clock() if arrival_s is None else arrival_s) * self.sr)
        start = max(arrival, self.cursor)
        self.agent.write(start, x)
        self.cursor = start + len(x)
        self.chunks += 1
        return start

    def clear(self, at_s: float | None = None) -> None:
        at = int((self.clock() if at_s is None else at_s) * self.sr)
        if self.cursor > at:
            self.agent.zero(at, self.cursor)
            self.clears.append(Clear(at, self.cursor - at))
            self.cursor = at
            self.agent.length = at  # nothing after this point was heard

    def agent_playing(self, now: int | None = None) -> bool:
        """True while queued agent audio has not finished playing."""
        return self.cursor > (self.now_sample() if now is None else now)

    def agent_spans(self, start: int, end: int, merge_gap_ms: int = 300) -> list[tuple[int, int]]:
        """Speech spans of the agent as heard, between two samples (absolute sample numbers)."""
        spans = speech_spans(self.agent.slice(start, end), self.sr, merge_gap_ms=merge_gap_ms)
        return [(start + a, start + b) for a, b in spans]


class Line:
    """The caller's side of the call: sends 20 ms frames in real time, speech or background.

    Frames go out on a fixed schedule (sample-accurate, no drift): the frame
    holding samples [s, s + 320) leaves when its last sample has been "spoken".
    Between utterances the line carries silence or the scenario's background noise,
    like a real phone line, so the agent's own end-of-speech detection sees a
    continuous stream.
    """

    def __init__(self, recorder: Recorder, send: Callable[[np.ndarray], Awaitable[None]],
                 noise: np.ndarray | None = None):
        self.rec = recorder
        self._send = send
        self._noise = noise
        self.frame = recorder.sr * FRAME_MS // 1000
        self._clips: list[tuple[int, np.ndarray]] = []   # (start sample, audio), in order
        self.next_sample = 0                             # first sample of the next frame to send
        self.speech_end = 0                              # end of the last queued utterance
        self._task: asyncio.Task | None = None
        self.error: BaseException | None = None

    def start(self) -> None:
        _fine_timer()
        self.next_sample = self.rec.now_sample() // self.frame * self.frame + self.frame
        self.speech_end = self.next_sample
        self._task = asyncio.create_task(self._run())

    def speak(self, clip: np.ndarray) -> tuple[int, int]:
        """Queue an utterance right after anything already queued. Returns its (start, end) samples."""
        start = max(self.next_sample, self.speech_end)
        self._clips.append((start, clip))
        self.speech_end = start + len(clip)
        return start, self.speech_end

    def busy(self) -> bool:
        return self.speech_end > self.next_sample

    async def wait_until(self, sample: int) -> None:
        while self.rec.now_sample() < sample:
            if self.error:
                raise self.error
            await asyncio.sleep(0.01)

    async def _run(self) -> None:
        try:
            while True:
                start = self.next_sample
                due = (start + self.frame) / self.rec.sr
                delay = due - self.rec.clock()
                if delay > 0:
                    await asyncio.sleep(delay)
                x = self._frame(start)
                self.rec.caller.write(start, x)
                self.next_sample = start + self.frame
                await self._send(x)
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # the connection dropped: the call loop sees it via .error
            self.error = e

    def _frame(self, start: int) -> np.ndarray:
        end = start + self.frame
        x = np.zeros(self.frame, dtype=np.float32)
        if self._noise is not None and len(self._noise):
            idx = np.arange(start, end) % len(self._noise)
            x += self._noise[idx]
        while self._clips and self._clips[0][0] + len(self._clips[0][1]) <= start:
            self._clips.pop(0)
        for c_start, clip in self._clips:
            if c_start >= end:
                break
            a, b = max(start, c_start), min(end, c_start + len(clip))
            if b > a:
                x[a - start:b - start] += clip[a - c_start:b - c_start]
        return x

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None


_timer_set = False


def _fine_timer() -> None:
    """Windows wakes sleeping tasks only every 15.6 ms by default; ask for 1 ms so frames leave on time."""
    global _timer_set
    if sys.platform == "win32" and not _timer_set:
        try:
            import ctypes

            ctypes.windll.winmm.timeBeginPeriod(1)
            _timer_set = True
        except Exception:
            pass


def monotonic_clock(start: float | None = None) -> Callable[[], float]:
    t0 = time.monotonic() if start is None else start
    return lambda: time.monotonic() - t0
