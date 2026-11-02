"""Phone-line effects on what the caller sends (scenario audio.line, or a run's caller conditions).

  phone          the sound of a phone call: band-limited to 300-3400 Hz (the telephone band) and
                 quantized with G.711 mu-law (audio/g711.py), the codec most phone lines and
                 Twilio carry
  packet loss    a share of the 20 ms frames never arrives. Losses come in short bursts like on a
                 real network (two-state Gilbert model, mean burst two frames), from the call's own
                 seed, so a call can be replayed exactly. The receiving end conceals a gap the way
                 G.711 Appendix I does: it replays the last frame that arrived, fading out (20% per
                 10 ms after the first 10 ms, silent after 60 ms). Plain zeros would be a digital
                 silence no real phone line has, and would fool an agent's noise floor

Effects run frame by frame on the line (audio/recorder.py Line), after speech and background noise
are mixed, so the agent hears exactly what a phone would deliver and call.wav records that. The
filter keeps its state across frames, so frame edges add no clicks.
"""

from __future__ import annotations

import hashlib

import numpy as np

from .g711 import pcm16_to_ulaw, ulaw_to_pcm16
from .pcm import from_pcm16, to_pcm16

BAND_HZ = (300.0, 3400.0)
TAPS = 129
BURST_MEAN_FRAMES = 2.0


def band_taps(sample_rate: int, low: float = BAND_HZ[0], high: float = BAND_HZ[1], n: int = TAPS) -> np.ndarray:
    """Windowed-sinc band-pass FIR (linear phase). Above Nyquist the top edge is the Nyquist."""
    high = min(high, 0.49 * sample_rate)
    m = np.arange(n) - (n - 1) / 2

    def lowpass(fc: float) -> np.ndarray:
        return 2 * fc / sample_rate * np.sinc(2 * fc / sample_rate * m)

    h = (lowpass(high) - lowpass(low)) * np.hamming(n)
    return h.astype(np.float64)


class LineEffects:
    """Stateful per-frame processing: feed frames in order with process()."""

    def __init__(self, sample_rate: int, *, phone: bool = False, packet_loss_pct: float = 0.0, seed: str = ""):
        self.sr = sample_rate
        self.phone = phone
        self.loss = max(0.0, min(float(packet_loss_pct or 0), 50.0)) / 100.0
        self._taps = band_taps(sample_rate) if phone else None
        self._ctx = np.zeros(TAPS - 1, dtype=np.float64)
        self._rng = np.random.default_rng(int(hashlib.sha256(f"line:{seed}".encode()).hexdigest()[:12], 16))
        self._lost = False
        r = 1.0 / BURST_MEAN_FRAMES                      # P(lost -> arrives)
        self._p_start = self.loss * r / (1 - self.loss) if self.loss < 1 else 1.0  # P(arrives -> lost)
        self._p_end = r
        self.frames = 0
        self.lost_frames = 0
        self._last: np.ndarray | None = None   # last frame that arrived, for concealment
        self._run = 0                           # frames lost in a row

    @property
    def active(self) -> bool:
        return self.phone or self.loss > 0

    def process(self, frame: np.ndarray) -> np.ndarray:
        x = frame.astype(np.float64)
        if self._taps is not None:
            buf = np.concatenate([self._ctx, x])
            self._ctx = buf[-(TAPS - 1):]
            x = np.convolve(buf, self._taps, mode="valid")         # exactly len(frame) samples
            x = from_pcm16(ulaw_to_pcm16(pcm16_to_ulaw(to_pcm16(x.astype(np.float32))))).astype(np.float64)
        self.frames += 1
        if self.loss > 0:
            self._lost = (self._rng.random() >= self._p_end) if self._lost else (self._rng.random() < self._p_start)
            if self._lost:
                self.lost_frames += 1
                self._run += 1
                return self._conceal(len(frame))
        self._run = 0
        self._last = x
        return x.astype(np.float32)

    def _conceal(self, n: int) -> np.ndarray:
        """Lost frame: the last good frame again, at a gain that falls linearly from 1 at 10 ms into the
        gap to 0 at 60 ms (G.711 Appendix I's fade)."""
        if self._last is None or len(self._last) != n:
            return np.zeros(n, dtype=np.float32)
        frame_ms = 1000.0 * n / self.sr
        t = (self._run - 1) * frame_ms + np.arange(n) * frame_ms / n    # ms since the gap began
        gain = np.clip(1.0 - (t - 10.0) / 50.0, 0.0, 1.0)
        return (self._last * gain).astype(np.float32)
