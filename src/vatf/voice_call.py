"""One call in voice mode: the caller speaks through TTS, listens through STT, and every
turn's timing is read off the recorded audio.

Turn taking, the way a person on the phone does it:
  * after the caller finishes speaking, wait for the agent to start talking
    (up to the persona's silence limit, then say so to the caller as "(silence)");
  * the agent's turn is over when nothing more is heard for end_of_turn_ms and
    none of its audio is still queued to play;
  * then transcribe what was heard and let the caller answer;
  * if the agent starts talking again while the caller is still thinking of an answer,
    the caller holds back and listens to the rest first, as a person would (went_on).

Barge-in (scenario audio.barge_in): when the agent's n-th turn has been audible
for offset_ms, the caller starts saying the given line over it. The agent should
stop talking; how long that took is recorded on the interrupted turn.

Harder callers (conditions.py, from the persona or the run's preset):
  * cuts in: on an agent turn that has gone on interrupt_after_ms, the caller may cut in with a short
    line of its own, measured like a scripted barge-in (barge_in_auto on the agent turn); it keeps
    the turn number, so a scripted test's turn checks still line up;
  * "mm-hm": a short backchannel while the agent talks; the agent should keep talking
    (caller_sound events, with agent_kept_talking);
  * a cough, a door bang or a clatter while the agent talks: not speech, so the agent should keep talking
    too (caller_sound kind noise, with agent_kept_talking);
  * pauses mid-sentence, fillers ("um"): how a line is said (caller_turn data.spoken, pause_ms, and
    data.pause: where in the recording the pause was, so an agent that jumps in during it is seen);
  * goes quiet: handled by the orchestrator's call loop (caller_sound kind silence);
  * quieter voice: speech levelled volume_db below normal.

A scripted line may have a voice behind it (turn `background`, safety tests): another speaker says those
words 8 dB quieter, from halfway through the caller's line, like a TV or a colleague in the room.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from .audio.pcm import SPEECH_DBFS, from_pcm16, read_wav, resample, set_level, to_pcm16
from .audio.recorder import Line, Recorder
from .audio.noise import room_echo, sound_effect, touch_tones
from .audio.vad import trim
from .contracts.models import AudioClip

POLL_S = 0.03
RECORDING = "audio/call.wav"
KEYPAD_NUMBER = re.compile(r"\d(?:[ \-]?\d){5,}")    # a number of 6 digits or more, maybe spaced: typed on the keypad
KEPT_TALKING_MS = 300     # after a "mm-hm" ends, the agent's turn must go on at least this long
# A voice in the room behind the caller (safety tests, scripted turn `background`): another speaker, quieter.
BACKGROUND_VOICE, BACKGROUND_VOICE_2 = "en_GB-alan-medium", "en_US-lessac-medium"
BACKGROUND_DB = -8.0


@dataclass
class Heard:
    start: int | None = None          # first agent sample of the turn (None: the agent said nothing)
    end: int | None = None            # last agent sample
    text: str = ""
    interrupted: bool = False
    stopped_after_ms: int | None = None
    barge: tuple[int, int, str] | None = None   # (start, end, words) of the caller's barge-in
    went_on: bool = False             # more of the same turn: the agent spoke again before the caller answered
    auto: bool = False                # the cut-in was the caller's own (persona behaviour), not the scenario's
    plan: int | None = None           # which audio.barge_in entry (1, 2...) cut in on this turn
    backchannels: list[tuple[int, int, str]] = field(default_factory=list)   # (start, end, word) said over the turn
    noises: list[tuple[int, int, str]] = field(default_factory=list)         # (start, end, kind) of coughs, bangs


@dataclass
class Prepared:
    """A caller line ready to say: its audio, the words as said (when not the line itself), any pause in it,
    and the recording it was played from (recordings.py), if any."""
    clip: np.ndarray
    spoken: str | None
    pause_ms: int
    recording: str | None = None
    pause_at_ms: int = 0              # where the pause starts, from the start of the clip


class CachedTTS:
    """Light load callers: a caller line is synthesised once and the clip reused by every call that says it
    (the same voice and speed). Under load this takes the biggest cost off Vocto's own computer; answer times
    are still read from the audio."""

    def __init__(self, tts: Any, cache: dict):
        self.tts, self.cache = tts, cache

    def warm_up(self, *args: Any) -> None:
        self.tts.warm_up(*args)

    def synthesize(self, text: str, voice: str = "", speaking_rate: float = 1.0) -> Any:
        key = (text, voice, speaking_rate)
        hit = self.cache.get(key)
        if hit is None:
            hit = self.cache[key] = self.tts.synthesize(text, voice, speaking_rate)
        return hit

    def __getattr__(self, name: str) -> Any:
        return getattr(self.tts, name)


@dataclass
class VoiceSettings:
    end_of_turn_ms: int = 1000        # silence that tells the caller the agent has finished
    no_reply_ms: int = 15000          # how long the caller waits for an answer before reacting
    max_turn_s: float = 90.0          # one agent turn may not go on longer than this
    barge_in: list[Mapping[str, Any]] = field(default_factory=list)
    conditions: Any = None            # conditions.CallConditions (None: a plain caller)


class VoiceConversation:
    def __init__(self, *, session: Any, recorder: Recorder, line: Line, tts: Any, stt: Any,
                 voice: str, speaking_rate: float, settings: VoiceSettings, writer: Any, proxy: Any,
                 language: str = "en"):
        self.session, self.rec, self.line = session, recorder, line
        self.tts, self.stt = tts, stt
        self.voice, self.rate = voice, speaking_rate
        self.language = language
        self.cfg = settings
        self.writer, self.proxy = writer, proxy
        self.agent_turns = 0
        self._transcripts_used = 0
        self._heard_to = 0                # agent audio up to this sample has been listened to
        self.ms = recorder.ms
        self.cond = settings.conditions
        self._sounds: dict[str, np.ndarray] = {}   # ready-made cut-ins and "mm-hm"s, said without a TTS wait

    async def prepare_sounds(self) -> None:
        """Synthesize the caller's cut-in lines and backchannels once, before the call starts."""
        b = self.cond.behaviour if self.cond else None
        if b is None:
            return
        words = (list(b.interrupt_lines) if b.interrupts else []) + (list(self.cond.backchannel_lines()) if b.backchannels else [])
        for w in words:
            if w not in self._sounds:
                self._sounds[w] = await self.synthesize(w)
        if b.noises:
            for kind in b.noise_kinds:
                self._sounds["noise:" + kind] = sound_effect(kind, self.rec.sr, self.cond.seed)

    # ---- the caller's mouth -------------------------------------------------
    async def synthesize(self, text: str) -> np.ndarray:
        clip = await asyncio.to_thread(self.tts.synthesize, text, self.voice, self.rate)
        x = resample(from_pcm16(clip.pcm16), clip.sample_rate_hz, self.rec.sr)
        volume = self.cond.volume_db if self.cond else 0.0
        x = set_level(trim(x, self.rec.sr), SPEECH_DBFS + volume)
        if self.cond is not None and self.cond.echo:      # speakerphone: the room's echo comes with the voice
            x = room_echo(x, self.rec.sr, self.cond.seed)
        return x

    async def with_keypad(self, text: str) -> "Prepared | None":
        """Keypad condition: a line with a number of 6 digits or more (a phone number, a code; not a year) is said up to the number, and the number
        is typed as touch tones ("my number is" + tones). None when the line has no such number."""
        m = KEYPAD_NUMBER.search(text)
        if self.cond is None or not self.cond.keypad or not m:
            return None
        keys = re.sub(r"\D", "", m.group(0))
        before, after = text[:m.start()].strip(), text[m.end():].strip(" .,")
        parts = [await self.synthesize(before)] if before else []
        parts += [np.zeros(self.rec.sr // 4, np.float32), touch_tones(keys, self.rec.sr), np.zeros(self.rec.sr // 4, np.float32)]
        if after:
            parts.append(await self.synthesize(after))
        spoken = f"{before} [keypad {keys}] {after}".strip()
        return Prepared(np.concatenate(parts), spoken, 0)

    async def prepare(self, text: str) -> "Prepared":
        """A caller line as this caller says it: with a filler, or a pause in the middle (conditions)."""
        if self.cond is None or not text:
            return Prepared(await self.synthesize(text), None, 0)
        if (typed := await self.with_keypad(text)) is not None:
            return typed
        first, second, spoken = self.cond.spoken(text)
        clip = await self.synthesize(first)
        pause, at = 0, 0
        if second:
            pause = self.cond.behaviour.pause_ms
            at = len(clip) * 1000 // self.rec.sr
            clip = np.concatenate([clip, np.zeros(pause * self.rec.sr // 1000, np.float32),
                                   await self.synthesize(second)])
        return Prepared(clip, spoken, pause, pause_at_ms=at)

    async def recording(self, path: Path, name: str) -> "Prepared":
        """A caller line played from a recording of a real caller: levelled to this caller's speaking volume."""
        x, _ = await asyncio.to_thread(read_wav, path, self.rec.sr)
        volume = self.cond.volume_db if self.cond else 0.0
        return Prepared(set_level(trim(x, self.rec.sr), SPEECH_DBFS + volume), None, 0, name)

    async def with_background(self, clip: "Prepared", words: str) -> "Prepared":
        """The caller's line with another voice in the room (a TV, a colleague) saying `words`: a different
        voice, BACKGROUND_DB quieter, starting halfway through the line and running on after it."""
        other = BACKGROUND_VOICE if self.voice != BACKGROUND_VOICE else BACKGROUND_VOICE_2
        x = await asyncio.to_thread(self.tts.synthesize, words, other, 1.0)
        back = set_level(trim(resample(from_pcm16(x.pcm16), x.sample_rate_hz, self.rec.sr), self.rec.sr),
                         SPEECH_DBFS + BACKGROUND_DB)
        start = len(clip.clip) // 2
        out = np.zeros(max(len(clip.clip), start + len(back)), np.float32)
        out[:len(clip.clip)] += clip.clip
        out[start:start + len(back)] += back
        return Prepared(np.clip(out, -1.0, 1.0), clip.spoken, clip.pause_ms, clip.recording, clip.pause_at_ms)

    async def say(self, text: str, turn: int, clip: "np.ndarray | Prepared | None" = None,
                  emit: bool = True, background: str | None = None) -> tuple[int, int]:
        if not isinstance(clip, Prepared):
            clip = Prepared(await self.synthesize(text) if clip is None else clip, None, 0)
        start, end = self.line.speak(clip.clip)
        self.proxy.turn = turn  # tool calls from here on belong to this caller turn
        if emit:
            self.emit_caller_turn(text, start, end, turn, spoken=clip.spoken, pause_ms=clip.pause_ms,
                                  recording=clip.recording, background=background, pause_at_ms=clip.pause_at_ms)
        return start, end

    def emit_caller_turn(self, text: str, start: int, end: int, turn: int, barge_in: bool = False,
                         spoken: str | None = None, pause_ms: int = 0, auto: bool = False,
                         recording: str | None = None, background: str | None = None, pause_at_ms: int = 0) -> None:
        data: dict[str, Any] = {"text": text,
                                "audio": {"file": RECORDING, "start_ms": self.ms(start), "end_ms": self.ms(end)}}
        if barge_in:
            data["barge_in"] = True
        if auto:
            data["barge_in_auto"] = True
        if spoken:
            data["spoken"] = spoken
        if pause_ms:
            data["pause_ms"] = pause_ms
            p0 = self.ms(start) + pause_at_ms
            data["pause"] = {"start_ms": p0, "end_ms": p0 + pause_ms}     # where the caller stopped to think
        if recording:
            data["recording"] = recording       # played from a real caller's recording, not synthesized
        if background:
            data["background"] = background     # another voice in the room said this behind the caller
        self.writer.emit("caller", "caller_turn", data, turn=turn, t_ms=self.ms(start))

    # ---- the caller's ears --------------------------------------------------
    async def transcribe(self, start: int, end: int) -> str:
        pad = self.rec.sr // 20
        x = self.rec.agent.slice(start - pad, end + pad)
        clip = AudioClip(to_pcm16(x), self.rec.sr)
        return (await asyncio.to_thread(self.stt.transcribe, clip, self.language)).text.strip()

    def _done(self) -> bool:
        return self.session.closed.is_set() and not self.rec.agent_playing()

    def agent_went_on(self) -> bool:
        """Has the agent started talking again since the caller last listened?"""
        now = self.rec.now_sample()
        return bool(self.rec.agent_spans(self._heard_to + 1, now)) or self.rec.agent_playing(now)

    async def hear_rest(self, turn: int) -> Heard:
        """The agent went on talking before the caller answered: listen to the rest of its turn."""
        heard = await self.listen(since=self._heard_to + 1, caller_end=None, turn=turn, more=True)
        heard.went_on = True
        return heard

    async def listen(self, since: int, caller_end: int | None, turn: int, more: bool = False) -> Heard:
        """Wait for the agent's next turn, heard after sample `since`."""
        heard = await self._listen(since, caller_end, turn, more)
        self._heard_to = max(self._heard_to, heard.end if heard.end is not None else self.rec.now_sample())
        return heard

    async def _listen(self, since: int, caller_end: int | None, turn: int, more: bool) -> Heard:
        if not more:
            self.agent_turns += 1
        barges = [] if more else [{**b, "plan": i} for i, b in enumerate(self.cfg.barge_in, start=1)
                                  if b["agent_turn"] == self.agent_turns]
        barge_clip = await self.synthesize(barges[0]["say"]) if barges else None
        b = self.cond.behaviour if self.cond else None
        # May the caller cut in on this turn? Not on the greeting: callers let the agent say hello.
        cut_in = bool(b and b.interrupts and not barges and self.agent_turns > 1)
        mm_hm = bool(b and b.backchannels)
        bang = bool(b and b.noises and self.agent_turns > 1)
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
            talking = self.rec.agent_playing(now) and not self.line.busy()
            if mm_hm and talking and now >= heard.start + b.backchannel_after_ms * self.rec.sr // 1000:
                mm_hm = False                                     # at most one per agent turn
                if self.cond.wants_backchannel():
                    word = self.cond.pick(list(self.cond.backchannel_lines()))
                    s0, s1 = self.line.speak(self._sounds[word])
                    heard.backchannels.append((s0, s1, word))
            if bang and talking and now >= heard.start + b.noise_after_ms * self.rec.sr // 1000:
                bang = False                                      # at most one per agent turn
                if self.cond.wants_noise():
                    kind = self.cond.pick(list(b.noise_kinds))
                    s0, s1 = self.line.speak(self._sounds["noise:" + kind])
                    heard.noises.append((s0, s1, kind))
            if cut_in and talking and now >= heard.start + b.interrupt_after_ms * self.rec.sr // 1000:
                cut_in = False
                if self.cond.wants_interrupt():
                    word = self.cond.pick(list(b.interrupt_lines))
                    return await self._barge_in(heard, self._sounds[word], {"say": word, "auto": True}, since, turn)
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
        b_start, b_end = await self.say(barge["say"], turn if barge.get("auto") else turn + 1, clip=clip, emit=False)
        heard.barge = (b_start, b_end, barge["say"])
        heard.interrupted = True
        heard.auto = bool(barge.get("auto"))
        heard.plan = barge.get("plan")
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

    def emit_agent_turn(self, heard: Heard, turn: int, caller_end: int | None, answered: int | None = None) -> None:
        data: dict[str, Any] = {"heard_text": heard.text}
        if answered is not None:
            data["answered_at_ms"] = self.ms(answered)     # the greeting: time to first word starts here
        if heard.start is None:
            data["silent"] = True
        else:
            data["audio"] = {"file": RECORDING, "start_ms": self.ms(heard.start), "end_ms": self.ms(heard.end)}
            if caller_end is not None:
                data["latency_ms"] = self.ms(heard.start) - self.ms(caller_end)
        if heard.went_on:
            data["went_on"] = True
        if heard.interrupted:
            data["interrupted"] = True
            data["stopped_after_ms"] = heard.stopped_after_ms
            data["barge_in_at_ms"] = self.ms(heard.barge[0])
            if heard.auto:
                data["barge_in_auto"] = True
            if heard.plan:
                data["barge_in_plan"] = heard.plan
        # What the agent itself reported saying since the last turn (kept for comparison only;
        # the caller never sees it).
        hi = heard.end if heard.end is not None else self.rec.now_sample()
        own = [t for s, t in self.session.transcripts[self._transcripts_used:] if s <= hi]
        self._transcripts_used += len(own)
        if own:
            data["platform_transcript"] = " ".join(own)
        event = self.writer.emit("connector", "agent_turn", data, turn=turn,
                                 t_ms=self.ms(heard.start) if heard.start is not None else None)
        for s0, s1, word in heard.backchannels:
            self.emit_backchannel(heard, s0, s1, word, turn, getattr(event, "seq", None))
        for s0, s1, kind in heard.noises:
            self.emit_backchannel(heard, s0, s1, kind, turn, getattr(event, "seq", None), sound="noise")

    def emit_backchannel(self, heard: Heard, s0: int, s1: int, word: str, turn: int, agent_seq: int | None,
                         sound: str = "backchannel") -> None:
        """A "mm-hm" said over an agent turn, and whether the agent kept talking through it (it should:
        a backchannel says "go on", not "stop"). Stopping = clearing its audio within a second of it, or
        its turn ending within KEPT_TALKING_MS of the backchannel's end."""
        sr = self.rec.sr
        # A clear that came with the caller's own later cut-in is the agent stopping for that, not for the mm-hm.
        until = s1 + sr
        if heard.interrupted and heard.barge and heard.barge[0] > s1:
            until = min(until, heard.barge[0])
        cleared = any(s0 <= c.at_sample <= until for c in self.rec.clears)
        end = heard.end if heard.end is not None else s1
        kept = bool(not cleared and not (heard.interrupted and heard.barge and heard.barge[0] <= s1)
                    and end >= s1 + KEPT_TALKING_MS * sr // 1000)
        data: dict[str, Any] = {"kind": sound, ("sound" if sound == "noise" else "text"): word, "agent_kept_talking": kept,
                                "audio": {"file": RECORDING, "start_ms": self.ms(s0), "end_ms": self.ms(s1)}}
        if agent_seq is not None:
            data["agent_turn"] = agent_seq
        self.writer.emit("caller", "caller_sound", data, turn=turn, t_ms=self.ms(s0))
