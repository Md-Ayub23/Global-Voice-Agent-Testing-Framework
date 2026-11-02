"""Background noise for the caller's line (scenario audio.noise + audio.snr_db).

The built-in noises are synthetic, so they need no downloads and are the same
on every machine; each call uses its test id as the random seed, so a call can
be replayed exactly. For real recordings, give audio.noise_file (a WAV file).

  office  pink noise (air conditioning, distant voices)
  street  brown and pink noise with slow swells (traffic passing)
  cafe    babble: noise shaped and pulsed at syllable rate like many voices
  car     low engine rumble with a faint hum
  tv      a TV or radio in the room: one speech-like voice in phrases, with quiet music under it
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np

from .pcm import SPEECH_DBFS, read_wav, rms_dbfs

KINDS = ("office", "street", "cafe", "car", "tv")


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
