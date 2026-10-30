"""G.711 mu-law, the 8 kHz telephone codec (what Twilio Media Streams and most phone lines carry).

Python 3.13 removed the audioop module, so the codec is done here with two lookup tables:
every 16-bit sample to its mu-law byte, and every byte back to a sample. This is the standard
ITU-T G.711 mu-law, byte for byte what audioop.lin2ulaw / ulaw2lin gave.
"""

from __future__ import annotations

import numpy as np

_BIAS = 0x84
_SEG_END = np.array([0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF])


def _encode_table() -> np.ndarray:
    # The classic Sun g711.c encoder on 14-bit samples, as audioop.lin2ulaw (and Pipecat) do it.
    s = np.arange(-32768, 32768, dtype=np.int32)
    v = s >> 2
    mask = np.where(v < 0, 0x7F, 0xFF)
    mag = np.minimum(np.abs(v), 8159) + (_BIAS >> 2)
    seg = np.searchsorted(_SEG_END, mag)                            # first segment whose end >= mag
    byte = np.where(seg >= 8, 0x7F, (seg << 4) | ((mag >> (seg + 1)) & 0x0F)) ^ mask
    table = np.empty(65536, dtype=np.uint8)
    table[s.astype(np.int16).view(np.uint16)] = byte                 # index by the sample's raw 16 bits
    return table


def _decode_table() -> np.ndarray:
    u = ~np.arange(256, dtype=np.int32) & 0xFF
    sign, exponent, mantissa = u & 0x80, (u >> 4) & 0x07, u & 0x0F
    mag = (((mantissa << 3) + _BIAS) << exponent) - _BIAS
    return np.where(sign != 0, -mag, mag).astype(np.int16)


_ENCODE = _encode_table()
_DECODE = _decode_table()


def pcm16_to_ulaw(pcm: bytes) -> bytes:
    """PCM16 little-endian mono -> mu-law bytes (one byte per sample)."""
    samples = np.frombuffer(pcm, dtype="<i2")
    return _ENCODE[samples.view(np.uint16)].tobytes()


def ulaw_to_pcm16(data: bytes) -> bytes:
    """mu-law bytes -> PCM16 little-endian mono."""
    return _DECODE[np.frombuffer(data, dtype=np.uint8)].astype("<i2").tobytes()
