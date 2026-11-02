"""Caller conditions: everything about how hard one voice call is for the agent, in one place.

Three inputs, read once per call and turned into one CallConditions:

  persona   who calls: voice / accent, speaking speed, volume, and behaviours
            (cuts in, says "mm-hm", pauses mid-sentence, fillers, goes quiet once)
  scenario  where they call from: audio.noise / noise_file / snr_db and audio.line
            (phone codec, packet loss) - plus the exact barge-ins of audio.barge_in
  run       a preset laid over every test (RunConfig.conditions): as_written, phone, noisy, hard

The voice call (voice_call.py, audio/line.py) only reads CallConditions; the metrics
(metrics.py) and the report only read what the call wrote down (call_started.data.conditions),
so the three never depend on each other's inputs. Text runs have no conditions.

Random choices (which turn the caller goes quiet on, whether it cuts in on a long answer) use the
call's test id as the seed, so a call can be replayed exactly.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Mapping

from .accents import ACCENTS, caller_voice

INTERRUPT_LINES = ("Sorry, can I just say something?", "Wait, sorry, one thing.", "Hang on a second.",
                   "Sorry to cut in.")
BACKCHANNEL_LINES = ("Mm-hm.", "Okay.", "Right.", "Yeah.")
FILLERS = ("Um,", "Uh,", "Hmm,")

PRESETS: dict[str, dict[str, Any]] = {
    "as_written": {"title": "As written",
                   "about": "Each test's own audio and each caller's own voice and behaviour."},
    "phone": {"title": "Phone line", "about": "Every call on a narrowband phone line with 1% of packets lost.",
              "line": {"codec": "phone", "packet_loss_pct": 1}},
    "noisy": {"title": "Noisy", "about": "Every call from a busy cafe (10 dB speech-to-noise).",
              "noise": "cafe", "snr_db": 10},
    "hard": {"title": "Hard", "about": "Street noise at 8 dB on a phone line with 3% packet loss, and every caller "
                                       "cuts in, says \"mm-hm\" and pauses mid-sentence.",
             "noise": "street", "snr_db": 8, "line": {"codec": "phone", "packet_loss_pct": 3},
             "behaviour": {"interrupts": True, "backchannels": True, "pauses_mid_sentence": True}},
}


@dataclass
class Behaviour:
    interrupts: bool = False
    interrupt_after_ms: int = 2500      # the agent has been talking this long
    interrupt_chance: float = 0.5       # on each agent turn that long
    interrupt_max: int = 2              # cut-ins per call
    interrupt_lines: tuple[str, ...] = INTERRUPT_LINES
    backchannels: bool = False
    backchannel_after_ms: int = 1800
    backchannel_chance: float = 0.6
    backchannel_max: int = 3
    pauses_mid_sentence: bool = False
    pause_ms: int = 1200
    pause_chance: float = 0.5           # of caller lines of 6 words or more
    fillers: bool = False
    filler_chance: float = 0.4
    goes_quiet: bool = False
    quiet_ms: int = 12000               # the caller says nothing this long, once per call

    def any(self) -> bool:
        return self.interrupts or self.backchannels or self.pauses_mid_sentence or self.fillers or self.goes_quiet


@dataclass
class CallConditions:
    preset: str = "as_written"
    voice: str = ""
    accent: str | None = None
    speaking_rate: float = 1.0
    volume_db: float = 0.0
    noise: str = "none"
    noise_file: str | None = None
    snr_db: float = 15.0
    phone: bool = False
    packet_loss_pct: float = 0.0
    behaviour: Behaviour = field(default_factory=Behaviour)
    seed: str = ""
    quiet_turn: int | None = None       # the caller turn (2nd or later) the caller goes quiet before

    def __post_init__(self) -> None:
        self._rng = random.Random(f"conditions:{self.seed}")
        b = self.behaviour
        self.quiet_turn = (2 + self._rng.randrange(3)) if b.goes_quiet else None
        self.interruptions = 0
        self.backchannels = 0

    # ---- choices made during the call (seeded) ------------------------------
    def roll(self, chance: float) -> bool:
        return self._rng.random() < chance

    def pick(self, options) -> str:
        return options[self._rng.randrange(len(options))]

    def wants_interrupt(self) -> bool:
        b = self.behaviour
        if not b.interrupts or self.interruptions >= b.interrupt_max or not self.roll(b.interrupt_chance):
            return False
        self.interruptions += 1
        return True

    def wants_backchannel(self) -> bool:
        b = self.behaviour
        if not b.backchannels or self.backchannels >= b.backchannel_max or not self.roll(b.backchannel_chance):
            return False
        self.backchannels += 1
        return True

    def spoken(self, text: str) -> tuple[str, str | None, str | None]:
        """How the caller says a line: (first part, second part after a pause or None, the words as said
        when they differ from the line)."""
        b = self.behaviour
        first = text
        if b.fillers and text and self.roll(b.filler_chance):
            first = f"{self.pick(FILLERS)} {text}"
        second = None
        words = first.split()
        if b.pauses_mid_sentence and len(words) >= 6 and self.roll(b.pause_chance):
            first, second = split_line(first)
        said = first if second is None else f"{first} ... {second}"
        return first, second, (said if said != text else None)

    # ---- what the call writes down ------------------------------------------
    def label(self) -> str:
        """Short name of the line and voice, for grouping results: "cafe noise 10 dB, phone line"."""
        parts = []
        if self.noise_file:
            parts.append(f"recorded noise {self.snr_db:g} dB")
        elif self.noise != "none":
            parts.append(f"{self.noise} noise {self.snr_db:g} dB")
        if self.phone:
            parts.append("phone line")
        if self.packet_loss_pct:
            parts.append(f"{self.packet_loss_pct:g}% packets lost")
        if self.accent:
            parts.append(ACCENTS[self.accent][0] if self.accent in ACCENTS else self.accent)
        if self.volume_db:
            parts.append(f"voice {self.volume_db:+g} dB")
        return ", ".join(parts) or "clean line"

    def summary(self) -> dict[str, Any]:
        b = self.behaviour
        out: dict[str, Any] = {"label": self.label(), "preset": self.preset, "voice": self.voice,
                               "speaking_rate": self.speaking_rate, "noise": self.noise, "snr_db": self.snr_db,
                               "phone": self.phone, "packet_loss_pct": self.packet_loss_pct}
        if self.accent:
            out["accent"] = self.accent
        if self.noise_file:
            out["noise_file"] = self.noise_file
        if self.volume_db:
            out["volume_db"] = self.volume_db
        on = [k for k in ("interrupts", "backchannels", "pauses_mid_sentence", "fillers", "goes_quiet") if getattr(b, k)]
        out["behaviours"] = on
        return out

    def judge_note(self) -> str:
        return judge_note(self.summary())


BEHAVIOUR_WORDS = {"interrupts": "cuts in on long answers", "backchannels": "says \"mm-hm\" while the agent talks",
                   "pauses_mid_sentence": "pauses in the middle of sentences", "fillers": "says \"um\" and \"uh\"",
                   "goes_quiet": "goes quiet once"}


def judge_note(summary: Mapping[str, Any]) -> str:
    """One line for the AI judges, so a noisy line or a caller who cuts in is not blamed on the agent."""
    bits = [summary.get("label", "clean line")] + [BEHAVIOUR_WORDS.get(k, k) for k in summary.get("behaviours", [])]
    return ("Caller conditions: " + "; ".join(bits) + ". Judge the agent, not the line: speech recognition "
            "mistakes caused by noise or a phone line are not the agent's fault.")


def split_line(text: str) -> tuple[str, str]:
    """Split a caller line where a person would pause: at a comma near the middle, else mid-way."""
    words = text.split()
    commas = [i for i, w in enumerate(words[:-2], start=1) if w.endswith(",") and i >= 2]
    mid = len(words) // 2
    cut = min(commas, key=lambda i: abs(i - mid)) if commas else mid
    return " ".join(words[:cut]), " ".join(words[cut:])


def _merge(base: Mapping[str, Any], over: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out.get(k) or {}, v) if isinstance(v, Mapping) else v
    return out


def resolve(persona: Mapping[str, Any] | None, scenario: Mapping[str, Any], *, preset: str = "as_written",
            default_voice: str = "en_US-lessac-medium", seed: str = "") -> CallConditions:
    """The conditions of one voice call. The run preset is laid over the test's audio (its noise and
    line win over the test's) and turns behaviours on (it never turns a persona's behaviour off)."""
    persona = persona or {}
    if preset not in PRESETS:
        raise ValueError(f"unknown caller conditions {preset!r} (use one of {', '.join(PRESETS)})")
    p = PRESETS[preset]
    audio = dict(scenario.get("audio", {}) or {})
    if "noise" in p:
        audio.update(noise=p["noise"], snr_db=p["snr_db"])
        audio.pop("noise_file", None)
    line = _merge(audio.get("line", {}) or {}, p.get("line", {}))
    speech = persona.get("speech", {}) or {}
    raw = _merge(persona.get("behaviour", {}) or {}, p.get("behaviour", {}))
    known = {f for f in Behaviour.__dataclass_fields__}
    b = Behaviour(**{k: (tuple(v) if k == "interrupt_lines" else v) for k, v in raw.items() if k in known and v is not None})
    if not b.interrupt_lines:
        b.interrupt_lines = INTERRUPT_LINES
    return CallConditions(
        preset=preset, voice=caller_voice(persona, default_voice), accent=speech.get("accent"),
        speaking_rate=float(speech.get("speaking_rate", 1.0)), volume_db=float(speech.get("volume_db", 0) or 0),
        noise=audio.get("noise", "none") or "none", noise_file=audio.get("noise_file"),
        snr_db=float(audio.get("snr_db", 15)), phone=line.get("codec") == "phone",
        packet_loss_pct=float(line.get("packet_loss_pct", 0) or 0), behaviour=b, seed=seed)


def noise_settings(c: CallConditions) -> dict[str, Any]:
    """The audio dict load_noise() reads."""
    out: dict[str, Any] = {"noise": c.noise, "snr_db": c.snr_db}
    if c.noise_file:
        out["noise_file"] = c.noise_file
    return out


def preset_list() -> list[dict[str, str]]:
    return [{"id": k, "title": v["title"], "about": v["about"]} for k, v in PRESETS.items()]

