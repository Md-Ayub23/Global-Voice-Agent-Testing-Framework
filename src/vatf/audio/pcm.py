"""PCM16 mono audio as numpy float32 arrays in [-1, 1], plus WAV files and resampling.

Everything inside vatf works on float32 at one sample rate (the profile's
connection.audio.sample_rate_hz, 16 kHz by default). Bytes only appear at the
edges: the wire (PCM16 little-endian) and WAV files.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

SPEECH_DBFS = -20.0  # every synthesized caller utterance is levelled to this RMS


def from_pcm16(data: bytes) -> np.ndarray:
    return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0


def to_pcm16(x: np.ndarray) -> bytes:
    return (np.clip(x, -1.0, 32767 / 32768) * 32768.0).astype("<i2").tobytes()


def resample(x: np.ndarray, from_hz: int, to_hz: int) -> np.ndarray:
    """Band-limited enough for speech: low-pass when going down, then linear interpolation."""
    if from_hz == to_hz or len(x) == 0:
        return x.astype(np.float32)
    if to_hz < from_hz:
        # Windowed-sinc low-pass at the new Nyquist, so downsampling does not alias.
        cutoff = 0.5 * to_hz / from_hz
        n = np.arange(-32, 33)
        taps = np.sinc(2 * cutoff * n) * np.hamming(len(n))
        x = np.convolve(x, taps / taps.sum(), mode="same")
    n_out = int(round(len(x) * to_hz / from_hz))
    t = np.arange(n_out) * (from_hz / to_hz)
    return np.interp(t, np.arange(len(x)), x).astype(np.float32)


def rms_dbfs(x: np.ndarray) -> float:
    if len(x) == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))
    return 20 * np.log10(max(rms, 1e-6))


def set_level(x: np.ndarray, dbfs: float = SPEECH_DBFS) -> np.ndarray:
    """Scale to a target RMS level, measured over the voiced part only."""
    voiced = x[np.abs(x) > 0.02 * (np.max(np.abs(x)) if len(x) else 1)]
    current = rms_dbfs(voiced if len(voiced) else x)
    return np.clip(x * 10 ** ((dbfs - current) / 20), -1, 1).astype(np.float32)


def write_wav(path: Path, channels: list[np.ndarray], sample_rate: int) -> None:
    """One or more equal-length float channels -> 16-bit WAV (stereo when two channels)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    length = max((len(c) for c in channels), default=0)
    padded = [np.pad(c, (0, length - len(c))) for c in channels]
    frames = np.stack(padded, axis=1).reshape(-1) if len(padded) > 1 else padded[0]
    with wave.open(str(path), "wb") as w:
        w.setnchannels(len(channels))
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(to_pcm16(frames))


def read_wav(path: Path, sample_rate: int | None = None) -> tuple[np.ndarray, int]:
    """Mono float audio (channels averaged), resampled to sample_rate when given."""
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError(f"{path}: only 16-bit WAV files are supported")
        rate, n_ch = w.getframerate(), w.getnchannels()
        x = from_pcm16(w.readframes(w.getnframes()))
    if n_ch > 1:
        x = x.reshape(-1, n_ch).mean(axis=1)
    if sample_rate and sample_rate != rate:
        return resample(x, rate, sample_rate), sample_rate
    return x, rate
