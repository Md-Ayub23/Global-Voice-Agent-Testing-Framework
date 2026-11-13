"""Background noise for the caller's line (scenario audio.noise + audio.snr_db).

The built-in noises are synthetic, so they need no downloads and are the same
on every machine; each call uses its test id as the random seed, so a call can
be replayed exactly. For real recordings, give audio.noise_file (a WAV file).

  office  pink noise (air conditioning, distant voices)
  street  brown and pink noise with slow swells (traffic passing)
  cafe    babble: noise shaped and pulsed at syllable rate like many voices
  car     low engine rumble with a faint hum
  tv      a TV or radio in the room: one speech-like voice in phrases, with quiet music under it
  chatter other people talking nearby: three voiced talkers (a pitch with its harmonics, shaped into
          syllables and phrases), the hardest kind of noise for speech recognition
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np

from .pcm import SPEECH_DBFS, read_wav, rms_dbfs

KINDS = ("office", "street", "cafe", "car", "tv", "chatter")


def _pink(rng: np.random.Generator, n: int) -> np.ndarray:
    spec = np.fft.rfft(rng.standard_normal(n))
    f = np.arange(len(spec), dtype=np.float64)
    f[0] = 1
    return np.fft.irfft(spec / np.sqrt(f), n)


def _brown(rng: np.random.Generator, n: int) -> np.ndarray:
    x = np.cumsum(rng.standard_normal(n))
    return x - np.convolve(x, np.ones(2001) / 2001, mode="same")  # remove the drift


def _swell(rng: np.random.Generator, n: int, sr: int, rate_hz: float, depth: float) -> np.ndarray:
    t = np.arange(n) / sr
    phase = rng.uniform(0, 2 * np.pi)
    return 1 - depth + depth * (0.5 + 0.5 * np.sin(2 * np.pi * rate_hz * t + phase))


def make_noise(kind: str, seconds: float, sample_rate: int, seed: str) -> np.ndarray:
    """Unit-level noise (RMS 1.0); mix_level() scales it."""
    rng = np.random.default_rng(int(hashlib.sha256(seed.encode()).hexdigest()[:12], 16))
    n = max(1, int(seconds * sample_rate))
    if kind == "office":
        x = _pink(rng, n)
    elif kind == "street":
        x = (_brown(rng, n) / 40 + _pink(rng, n)) * _swell(rng, n, sample_rate, 0.12, 0.6)
    elif kind == "cafe":
        # Babble: speech-band noise, pulsed at 3 to 6 Hz like overlapping syllables.
        x = np.zeros(n)
        for _ in range(6):
            band = _pink(rng, n)
            band = np.convolve(band, np.ones(4) / 4, mode="same")
            x += band * _swell(rng, n, sample_rate, rng.uniform(3, 6), 0.9)
    elif kind == "car":
        t = np.arange(n) / sample_rate
        x = _brown(rng, n) / 40 + 0.3 * np.sin(2 * np.pi * 95 * t) * _pink(rng, n) * 0.2
        x = np.convolve(x, np.ones(16) / 16, mode="same")
    elif kind == "tv":
        # One talker: speech-band noise pulsed at syllable rate, in phrases of a few seconds with gaps,
        # over a soft chord that drifts like background music.
        t = np.arange(n) / sample_rate
        voice = np.convolve(_pink(rng, n), np.ones(6) / 6, mode="same")
        voice *= _swell(rng, n, sample_rate, rng.uniform(4, 5), 0.95)
        voice *= np.clip(_swell(rng, n, sample_rate, 0.3, 1.0) * 1.6 - 0.3, 0, 1)   # phrases and pauses
        chord = sum(np.sin(2 * np.pi * f * t + rng.uniform(0, 6.3)) for f in (220.0, 277.2, 329.6))
        x = voice + 0.08 * chord * _swell(rng, n, sample_rate, 0.07, 0.5)
    elif kind == "chatter":
        t = np.arange(n) / sample_rate
        x = np.zeros(n)
        for _ in range(3):
            f0 = rng.uniform(100, 230) * (1 + 0.08 * np.sin(2 * np.pi * rng.uniform(0.2, 0.5) * t + rng.uniform(0, 6.3)))
            phase = 2 * np.pi * np.cumsum(f0) / sample_rate
            voiced = sum(np.sin(k * phase) / k for k in range(1, 16))            # a voice's buzz and harmonics
            voiced = np.convolve(voiced, np.ones(3) / 3, mode="same")            # softened, like a mouth
            hiss = np.convolve(_pink(rng, n), np.ones(2) / 2, mode="same") * 0.3 # consonants
            talk = (voiced + hiss) * _swell(rng, n, sample_rate, rng.uniform(3.5, 5.5), 0.95)
            talk *= np.clip(_swell(rng, n, sample_rate, rng.uniform(0.2, 0.35), 1.0) * 1.5 - 0.25, 0, 1)
            x += talk / max(np.sqrt(np.mean(talk * talk)), 1e-9)
    else:
        raise ValueError(f"unknown noise {kind!r} (use one of {', '.join(KINDS)} or audio.noise_file)")
    x = x - x.mean()
    return (x / max(np.sqrt(np.mean(x * x)), 1e-9)).astype(np.float32)


def load_noise(audio: dict, seconds: float, sample_rate: int, seed: str, root: Path) -> np.ndarray | None:
    """The scenario's noise bed, levelled for its snr_db against speech at SPEECH_DBFS. None for no noise."""
    kind = audio.get("noise", "none")
    file = audio.get("noise_file")
    if kind == "none" and not file:
        return None
    if file:
        x, _ = read_wav((root / file) if not Path(file).is_absolute() else Path(file), sample_rate)
        reps = int(np.ceil(seconds * sample_rate / max(len(x), 1)))
        x = np.tile(x, reps)[: int(seconds * sample_rate)]
        x = x / max(10 ** (rms_dbfs(x) / 20), 1e-9)
    else:
        x = make_noise(kind, seconds, sample_rate, seed)
    snr = float(audio.get("snr_db", 15))
    return (x * 10 ** ((SPEECH_DBFS - snr) / 20)).astype(np.float32)


# ---- short sounds that are not speech (Timing: the caller coughs, a door bangs) ------------------
SOUNDS = ("cough", "door", "clatter")


def _burst(rng: np.random.Generator, n: int, sr: int, attack_ms: float, decay_ms: float) -> np.ndarray:
    t = np.arange(n) / sr * 1000
    return np.minimum(1.0, t / max(attack_ms, 0.1)) * np.exp(-t / decay_ms)


def sound_effect(kind: str, sample_rate: int, seed: str) -> np.ndarray:
    """A short non-speech sound at about speech level (SPEECH_DBFS), for the caller's line while the agent
    talks. The agent should treat it as noise and keep talking, not stop as if the caller had cut in.

      cough    two harsh bursts of breath noise (~0.5 s)
      door     a low thump with a rattle (~0.4 s)
      clatter  a few sharp knocks, like dishes or keys dropped (~0.5 s)
    """
    rng = np.random.default_rng(int(hashlib.sha256(f"{kind}:{seed}".encode()).hexdigest()[:12], 16))
    sr = sample_rate
    if kind == "cough":
        parts = []
        for dur in (0.22, 0.17):
            n = int(dur * sr)
            breath = np.convolve(rng.standard_normal(n), np.ones(3) / 3, mode="same")     # harsh, speech-band
            parts += [breath * _burst(rng, n, sr, 8, 70), np.zeros(int(0.09 * sr))]
        x = np.concatenate(parts)
    elif kind == "door":
        n = int(0.4 * sr)
        t = np.arange(n) / sr
        thump = np.sin(2 * np.pi * 70 * t) * _burst(rng, n, sr, 2, 90)
        rattle = rng.standard_normal(n) * _burst(rng, n, sr, 1, 25) * 0.5
        x = thump + rattle
    elif kind == "clatter":
        n = int(0.5 * sr)
        x = np.zeros(n)
        for _ in range(4):
            at = int(rng.uniform(0, 0.38) * sr)
            m = int(0.08 * sr)
            x[at:at + m] += rng.standard_normal(m) * _burst(rng, m, sr, 0.5, 12)[: n - at] * rng.uniform(0.5, 1.0)
    else:
        raise ValueError(f"unknown sound {kind!r} (use one of {', '.join(SOUNDS)})")
    x = x - x.mean()
    peak_rms = max(np.sqrt(np.mean(x * x)), 1e-9)
    return (x / peak_rms * 10 ** (SPEECH_DBFS / 20)).astype(np.float32)


# ---- the caller's room and keypad (Speech conditions) ----------------------------------------------
def room_echo(x: np.ndarray, sample_rate: int, seed: str, rt60_s: float = 0.6, wet: float = 0.5) -> np.ndarray:
    """A voice on speakerphone in a hard-walled room: the dry voice plus its reverberation (an exponentially
    decaying noise impulse response with this RT60), at the same level as the dry voice."""
    rng = np.random.default_rng(int(hashlib.sha256(f"room:{seed}".encode()).hexdigest()[:12], 16))
    n = int(rt60_s * sample_rate)
    t = np.arange(n) / sample_rate
    ir = rng.standard_normal(n) * np.exp(-6.9 * t / rt60_s)            # -60 dB at rt60
    ir[: int(0.005 * sample_rate)] = 0                                 # the first reflection after 5 ms
    ir /= max(np.sqrt(np.sum(ir * ir)), 1e-9)
    size = 1 << int(np.ceil(np.log2(len(x) + n)))
    tail = np.fft.irfft(np.fft.rfft(x, size) * np.fft.rfft(ir, size), size)[: len(x) + n]
    out = np.concatenate([x, np.zeros(n, np.float32)]) + wet * tail
    rms_in = max(np.sqrt(np.mean(x.astype(np.float64) ** 2)), 1e-9)
    out *= rms_in / max(np.sqrt(np.mean(out[: len(x)] ** 2)), 1e-9)
    return np.clip(out, -1, 1).astype(np.float32)


DTMF = {"1": (697, 1209), "2": (697, 1336), "3": (697, 1477), "4": (770, 1209), "5": (770, 1336), "6": (770, 1477),
        "7": (852, 1209), "8": (852, 1336), "9": (852, 1477), "*": (941, 1209), "0": (941, 1336), "#": (941, 1477)}


def touch_tones(keys: str, sample_rate: int, tone_ms: int = 100, gap_ms: int = 100) -> np.ndarray:
    """Keypad touch tones (DTMF) for the keys, each tone_ms long with gap_ms between, at speech level."""
    on, off = int(tone_ms * sample_rate / 1000), np.zeros(int(gap_ms * sample_rate / 1000), np.float32)
    t = np.arange(on) / sample_rate
    parts = []
    for k in keys:
        if k in DTMF:
            lo, hi = DTMF[k]
            parts += [(0.5 * np.sin(2 * np.pi * lo * t) + 0.5 * np.sin(2 * np.pi * hi * t)).astype(np.float32), off]
    if not parts:
        return np.zeros(0, np.float32)
    x = np.concatenate(parts)
    return (x / max(np.sqrt(np.mean(x[x != 0] ** 2)), 1e-9) * 10 ** (SPEECH_DBFS / 20)).astype(np.float32)
