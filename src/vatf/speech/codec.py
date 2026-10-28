"""Tone codec: a stand-in for speech in the framework's own tests.

Text becomes a short sync tone followed by one 20 ms tone per half-byte of the
UTF-8 text (16 frequencies). The decoder finds each sound with the same level
gate as the recorder and reads the tones back. Several utterances in one clip
(separated by silence) are decoded one by one and joined with a space.

A sentence of 40 characters lasts about 1.7 seconds, close to real speech, so
timings in tests look like real calls.
"""

from __future__ import annotations

import numpy as np

from ..audio.pcm import from_pcm16, resample, to_pcm16
from ..contracts.models import AudioClip
from ..contracts.speech import Transcript

RATE = 16000
SYMBOL_S = 0.020
SYNC_S = 0.040
SYNC_HZ = 500.0
TONES = 800.0 + 100.0 * np.arange(16)
MIN_AMPLITUDE = 0.02


def _tone(freq: float, seconds: float) -> np.ndarray:
    n = int(round(seconds * RATE))
    x = 0.3 * np.sin(2 * np.pi * freq * np.arange(n) / RATE)
    ramp = min(32, n // 4)
    x[:ramp] *= np.linspace(0, 1, ramp)
    x[-ramp:] *= np.linspace(1, 0, ramp)
    return x


class CodecTTS:
    def warm_up(self, voice: str = "") -> None:
        pass

    def synthesize(self, text: str, voice: str = "", speaking_rate: float = 1.0) -> AudioClip:
        parts = [_tone(SYNC_HZ, SYNC_S)]
        for byte in text.encode("utf-8"):
            parts += [_tone(TONES[byte >> 4], SYMBOL_S), _tone(TONES[byte & 15], SYMBOL_S)]
        return AudioClip(to_pcm16(np.concatenate(parts).astype(np.float32)), RATE)


def _magnitude(x: np.ndarray, freq: float, length: int) -> np.ndarray:
    """|tone content| of every window x[k:k+length], as an amplitude estimate."""
    kernel = np.exp(2j * np.pi * freq * np.arange(length) / RATE)
    return np.abs(np.convolve(x, kernel, mode="valid")) * 2 / length


def _find_sync(x: np.ndarray, start: int) -> int | None:
    length = int(0.030 * RATE)
    seg = x[start:]
    if len(seg) < length:
        return None
    m = _magnitude(seg, SYNC_HZ, length)
    above = np.flatnonzero(m > MIN_AMPLITUDE)
    if not len(above):
        return None
    k0 = int(above[0])
    peak = float(m[k0:k0 + int(0.040 * RATE)].max())
    k = k0 + int(np.flatnonzero(m[k0:] >= 0.95 * peak)[0])
    return start + k


def _decode_from(x: np.ndarray, sync: int) -> tuple[str, int]:
    length = int(0.014 * RATE)
    t = np.arange(length) / RATE
    bank = np.exp(-2j * np.pi * TONES[:, None] * t[None, :])
    ref = float(_magnitude(x[sync:sync + int(0.035 * RATE)], SYNC_HZ, int(0.030 * RATE)).max())
    pos, nibbles = sync + int(SYNC_S * RATE), []
    while pos + int(SYMBOL_S * RATE) <= len(x):
        w = x[pos + int(0.003 * RATE): pos + int(0.003 * RATE) + length]
        mags = np.abs(bank @ w) * 2 / length
        if mags.max() < 0.4 * ref:
            break
        nibbles.append(int(np.argmax(mags)))
        pos += int(SYMBOL_S * RATE)
    data = bytes((nibbles[i] << 4) | nibbles[i + 1] for i in range(0, len(nibbles) - 1, 2))
    return data.decode("utf-8", errors="replace").strip(), pos


class CodecSTT:
    def warm_up(self) -> None:
        pass

    def transcribe(self, clip: AudioClip, language: str = "en") -> Transcript:
        x = resample(from_pcm16(clip.pcm16), clip.sample_rate_hz, RATE)
        texts, i = [], 0
        while (sync := _find_sync(x, i)) is not None:
            text, i = _decode_from(x, sync)
            i = max(i, sync + int(SYNC_S * RATE))
            if text:
                texts.append(text)
        return Transcript(" ".join(texts))
