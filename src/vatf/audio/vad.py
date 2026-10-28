"""Where speech starts and stops in a recorded track.

Used on clean audio only: the agent's channel as the caller heard it, and the
caller's own synthesized speech before noise is mixed in. A simple level gate
is exact on such audio, and it is the same thing a person sees when they
measure the gap by eye in an audio editor (the waveform rising from the flat line).
"""

from __future__ import annotations

import numpy as np

WINDOW_MS = 10
THRESHOLD_DBFS = -45.0      # a 10 ms window louder than this counts as sound
ONSET_AMPLITUDE = 0.01      # the first / last single sample above this marks the exact edge


def _window_levels(x: np.ndarray, win: int) -> np.ndarray:
    n = len(x) // win
    if n == 0:
        return np.zeros(0)
    frames = x[: n * win].reshape(n, win).astype(np.float64)
    rms = np.sqrt(np.mean(frames * frames, axis=1))
    return 20 * np.log10(np.maximum(rms, 1e-6))


def speech_spans(x: np.ndarray, sample_rate: int, *, merge_gap_ms: int = 300, min_ms: int = 30,
                 threshold_dbfs: float = THRESHOLD_DBFS) -> list[tuple[int, int]]:
    """[(first_sample, last_sample + 1), ...] of sound, with pauses shorter than merge_gap_ms joined."""
    win = max(1, sample_rate * WINDOW_MS // 1000)
    loud = _window_levels(x, win) > threshold_dbfs
    spans: list[list[int]] = []
    gap = merge_gap_ms // WINDOW_MS
    for i in np.flatnonzero(loud):
        if spans and i - spans[-1][1] <= gap:
            spans[-1][1] = i + 1
        else:
            spans.append([int(i), int(i) + 1])
    out = []
    for a, b in spans:
        if (b - a) * WINDOW_MS < min_ms:
            continue
        start, end = _refine(x, a * win, b * win, win)
        out.append((start, end))
    return out


def _refine(x: np.ndarray, start: int, end: int, win: int) -> tuple[int, int]:
    """Sample-exact edges: first and last sample above ONSET_AMPLITUDE near the window edges."""
    lo = max(0, start - win)
    head = np.flatnonzero(np.abs(x[lo:start + win]) >= ONSET_AMPLITUDE)
    s = lo + int(head[0]) if len(head) else start
    hi = min(len(x), end + win)
    tail = np.flatnonzero(np.abs(x[max(0, end - win):hi]) >= ONSET_AMPLITUDE)
    e = max(0, end - win) + int(tail[-1]) + 1 if len(tail) else end
    return s, max(e, s + 1)


def trim(x: np.ndarray, sample_rate: int) -> np.ndarray:
    """Cut leading and trailing silence, so a clip's first and last samples are speech."""
    spans = speech_spans(x, sample_rate, merge_gap_ms=10_000, min_ms=10)
    if not spans:
        return x[:0]
    return x[spans[0][0]: spans[-1][1]]
