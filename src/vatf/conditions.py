"""Caller conditions: everything about how hard one voice call is for the agent, in one place.

Three inputs, read once per call and turned into one CallConditions:

  persona   who calls: voice / accent, speaking speed, volume, and behaviours
            (cuts in, says "mm-hm", pauses mid-sentence, fillers, goes quiet once)
  scenario  where they call from: audio.noise / noise_file / snr_db and audio.line
            (phone codec, packet loss) - plus the exact barge-ins of audio.barge_in
  run       a preset laid over every test (RunConfig.conditions): as_written, phone, noisy, hard,
            quiet, fast, street, lossy - or "accent:<id>", the caller speaking with one accent.
            A Speech mode run sweeps several of them (RunConfig.sweep): every test once per condition.

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
from .languages import default_voice as lang_voice, name as language_name, phrases, voice_language

INTERRUPT_LINES = phrases("en", "interrupt_lines")
BACKCHANNEL_LINES = phrases("en", "backchannels")
FILLERS = phrases("en", "fillers")

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
    # Speech conditions (Speech mode sweeps these): each changes one thing about how the caller sounds.
    "quiet": {"title": "Quiet caller", "about": "The caller speaks softly (12 dB quieter) on a phone line.",
              "volume_db": -12, "line": {"codec": "phone"}},
    "fast": {"title": "Fast talker", "about": "The caller speaks about 30% faster than normal.",
             "speaking_rate": 1.3},
    "street": {"title": "Street noise", "about": "Traffic and street noise loud enough to compete with the "
                                                 "caller (5 dB speech-to-noise).",
               "noise": "street", "snr_db": 5},
    "lossy": {"title": "Bad mobile signal", "about": "A phone line that loses 5% of its audio packets.",
              "line": {"codec": "phone", "packet_loss_pct": 5}},
    # Network conditions (Resilience): how the audio travels, not how the caller sounds.
    "jittery": {"title": "Unsteady network", "about": "The caller's audio arrives in bursts with gaps (delay varying "
                                                      "by about 80 ms), and 1% of packets arrive out of order.",
                "line": {"jitter_ms": 80, "reorder_pct": 1}},
}
ACCENT_PREFIX = "accent:"


def known(preset: str) -> bool:
    """A run preset or Speech mode condition: a PRESETS id, or "accent:<id>" for one of ACCENTS."""
    if preset.startswith(ACCENT_PREFIX):
        return preset[len(ACCENT_PREFIX):] in ACCENTS
    return preset in PRESETS


def title(preset: str) -> str:
    """How a condition is named in reports: "Phone line", "Indian English accent"."""
    if preset.startswith(ACCENT_PREFIX):
        acc = preset[len(ACCENT_PREFIX):]
        return f"{ACCENTS[acc][0]} accent" if acc in ACCENTS else preset
    return PRESETS[preset]["title"] if preset in PRESETS else preset


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
    jitter_ms: float = 0.0              # an unsteady network: the spread of the audio's delay
    reorder_pct: float = 0.0            # and the share of packets that arrive out of order
    behaviour: Behaviour = field(default_factory=Behaviour)
    seed: str = ""
    quiet_turn: int | None = None       # the caller turn (2nd or later) the caller goes quiet before
    language: str = "en"                # the language the caller speaks (languages.py)

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

    def backchannel_lines(self) -> tuple[str, ...]:
        return phrases(self.language, "backchannels")

    def spoken(self, text: str) -> tuple[str, str | None, str | None]:
        """How the caller says a line: (first part, second part after a pause or None, the words as said
        when they differ from the line)."""
        b = self.behaviour
        first = text
        if b.fillers and text and self.roll(b.filler_chance):
            first = f"{self.pick(phrases(self.language, 'fillers'))} {text}"
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
        if self.jitter_ms or self.reorder_pct:
            parts.append(f"unsteady network ({self.jitter_ms:g} ms jitter"
                         + (f", {self.reorder_pct:g}% out of order)" if self.reorder_pct else ")"))
        if self.accent:
            parts.append(ACCENTS[self.accent][0] if self.accent in ACCENTS else self.accent)
        if self.volume_db:
            parts.append(f"voice {self.volume_db:+g} dB")
        if self.language != "en":
            parts.append(f"{language_name(self.language)} caller")
        return ", ".join(parts) or "clean line"

    def summary(self) -> dict[str, Any]:
        b = self.behaviour
        out: dict[str, Any] = {"label": self.label(), "preset": self.preset, "title": title(self.preset),
                               "voice": self.voice,
                               "speaking_rate": self.speaking_rate, "noise": self.noise, "snr_db": self.snr_db,
                               "phone": self.phone, "packet_loss_pct": self.packet_loss_pct}
        if self.accent:
            out["accent"] = self.accent
        if self.language != "en":
            out["language"] = self.language
        if self.noise_file:
            out["noise_file"] = self.noise_file
        if self.volume_db:
            out["volume_db"] = self.volume_db
        if self.jitter_ms or self.reorder_pct:
            out.update(jitter_ms=self.jitter_ms, reorder_pct=self.reorder_pct)
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
    lang = summary.get("language")
    said = (f" The call is in {language_name(lang)}: judge what was said in that language, and quote it as said."
            if lang and lang != "en" else "")
    return ("Caller conditions: " + "; ".join(bits) + ". Judge the agent, not the line: speech recognition "
            "mistakes caused by noise or a phone line are not the agent's fault." + said)


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
            default_voice: str = "en_US-lessac-medium", seed: str = "", language: str = "en") -> CallConditions:
    """The conditions of one voice call. The run preset is laid over the test's audio (its noise and
    line win over the test's) and turns behaviours on (it never turns a persona's behaviour off)."""
    persona = persona or {}
    if not known(preset):
        raise ValueError(f"unknown caller conditions {preset!r} (use one of {', '.join(PRESETS)}, or accent:<id>)")
    accent = preset[len(ACCENT_PREFIX):] if preset.startswith(ACCENT_PREFIX) else None
    p = PRESETS.get(preset, {})
    audio = dict(scenario.get("audio", {}) or {})
    if "noise" in p:
        audio.update(noise=p["noise"], snr_db=p["snr_db"])
        audio.pop("noise_file", None)
    line = _merge(audio.get("line", {}) or {}, p.get("line", {}))
    speech = dict(persona.get("speech", {}) or {})
    if accent:          # the condition's accent wins over the persona's own voice
        speech.pop("tts_voice", None)
        speech["accent"] = accent
        persona = {**persona, "speech": speech}
    raw = _merge(persona.get("behaviour", {}) or {}, p.get("behaviour", {}))
    fields = {f for f in Behaviour.__dataclass_fields__}
    b = Behaviour(**{k: (tuple(v) if k == "interrupt_lines" else v) for k, v in raw.items() if k in fields and v is not None})
    if not raw.get("interrupt_lines"):        # the persona's own cut-in lines, else the language's
        b.interrupt_lines = phrases(language, "interrupt_lines")
    voice, accent_id = caller_voice(persona, default_voice), speech.get("accent")
    if language != "en":
        # Accents are English voices, and an English voice can't speak Tamil: a caller in another language
        # speaks with its own voice only if that voice speaks the language, else the language's voice.
        own = speech.get("tts_voice")
        voice = own if own and voice_language(own) == language else (lang_voice(language) or voice)
        accent_id = None
    return CallConditions(
        preset=preset, voice=voice, accent=accent_id, language=language,
        speaking_rate=float(p.get("speaking_rate") or speech.get("speaking_rate", 1.0)),
        volume_db=float(p.get("volume_db") or speech.get("volume_db", 0) or 0),
        noise=audio.get("noise", "none") or "none", noise_file=audio.get("noise_file"),
        snr_db=float(audio.get("snr_db", 15)), phone=line.get("codec") == "phone",
        packet_loss_pct=float(line.get("packet_loss_pct", 0) or 0),
        jitter_ms=float(line.get("jitter_ms", 0) or 0), reorder_pct=float(line.get("reorder_pct", 0) or 0),
        behaviour=b, seed=seed)


def noise_settings(c: CallConditions) -> dict[str, Any]:
    """The audio dict load_noise() reads."""
    out: dict[str, Any] = {"noise": c.noise, "snr_db": c.snr_db}
    if c.noise_file:
        out["noise_file"] = c.noise_file
    return out


RUN_PRESETS = ("as_written", "phone", "noisy", "hard", "jittery")     # the run-wide choice on New run (any mode)


def preset_list() -> list[dict[str, str]]:
    return [{"id": k, "title": PRESETS[k]["title"], "about": PRESETS[k]["about"]} for k in RUN_PRESETS]


# The Speech mode's choice of conditions: how the caller sounds, one change at a time. "hard" mixes
# several and adds caller behaviours, so it is left to the run-wide presets.
SPEECH_SWEEP = ("as_written", "phone", "noisy", "street", "quiet", "fast", "lossy", "jittery")
SPEECH_DEFAULT = ("as_written", "phone", "noisy", "quiet")


def sweep_list() -> list[dict[str, Any]]:
    """Speech mode conditions for the dashboard: line and voice conditions, then accents."""
    out = [{"id": k, "title": PRESETS[k]["title"], "about": PRESETS[k]["about"], "group": "line",
            "default": k in SPEECH_DEFAULT} for k in SPEECH_SWEEP]
    out += [{"id": ACCENT_PREFIX + k, "title": f"{label} accent", "about": f"The caller speaks {label} (voice {voice}).",
             "group": "accent", "default": False} for k, (label, voice) in ACCENTS.items()]
    return out

