"""Listen live: a copy of a voice call's audio, sent to the dashboard while the call runs.

The dashboard starts each run as its own process and passes a local UDP port in VATF_LISTEN_PORT.
Every 100 ms the tap mixes what is on the line so far (the caller's frames as sent, the agent's
audio as the caller heard it) and sends it to that port as one datagram. It only reads the
recorder's tracks, never waits for anyone, and drops audio when sending fails, so the call and its
timing are the same with or without a listener. Without the variable (a run from the terminal) there
is no tap at all.

Datagram: b"VTP1", sample rate (u32), first sample on the call clock (u64), label length (u16),
label (utf-8: "<test id> <scenario id>"), then 16-bit little-endian mono samples.
"""

from __future__ import annotations

import asyncio
import os
import socket
import struct

import numpy as np

MAGIC = b"VTP1"
HEADER = struct.Struct(">4sIQH")
EVERY_S = 0.1
LAG_MS = 60                 # the caller frame being spoken now is written when it is sent
MAX_SAMPLES = 8000          # samples per datagram, so one stays well under the UDP limit
PORT_ENV = "VATF_LISTEN_PORT"


def pack(sample_rate: int, start: int, label: str, x: np.ndarray) -> bytes:
    name = label.encode("utf-8")[:200]
    pcm = (np.clip(x, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    return HEADER.pack(MAGIC, sample_rate, start, len(name)) + name + pcm


def unpack(packet: bytes) -> tuple[int, int, str, bytes] | None:
    """(sample rate, start sample, label, pcm16 bytes), or None for anything that isn't a tap datagram."""
    if len(packet) < HEADER.size:
        return None
    magic, sr, start, n = HEADER.unpack_from(packet)
    if magic != MAGIC or not 4000 <= sr <= 96000 or len(packet) < HEADER.size + n or (len(packet) - HEADER.size - n) % 2:
        return None
    try:
        label = packet[HEADER.size:HEADER.size + n].decode("utf-8")
    except UnicodeDecodeError:
        return None
    return sr, start, label, packet[HEADER.size + n:]


class LiveTap:
    def __init__(self, recorder, label: str, port: int):
        self.rec = recorder
        self.label = label
        self.addr = ("127.0.0.1", port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        self.sent = recorder.now_sample()
        self._task: asyncio.Task | None = None

    @classmethod
    def from_env(cls, recorder, label: str) -> "LiveTap | None":
        try:
            port = int(os.environ.get(PORT_ENV, ""))
        except ValueError:
            return None
        if not 0 < port < 65536:
            return None
        try:
            tap = cls(recorder, label, port)
        except OSError:
            return None
        tap._task = asyncio.create_task(tap._run())
        return tap

    def flush(self, lag: bool = True) -> None:
        """Send what is on the line since the last datagram, a little behind the call so every
        caller frame is in (never raises)."""
        try:
            now = self.rec.now_sample() - (self.rec.sr * LAG_MS // 1000 if lag else 0)
            while self.sent < now:
                end = min(now, self.sent + MAX_SAMPLES)
                caller = self.rec.caller.slice(self.sent, end)
                agent = self.rec.agent.slice(self.sent, end)
                x = np.zeros(end - self.sent, dtype=np.float32)
                x[:len(caller)] += caller
                x[:len(agent)] += agent
                try:
                    self.sock.sendto(pack(self.rec.sr, self.sent, self.label, x), self.addr)
                except OSError:
                    pass            # nobody listening, or the buffer is full: this bit is dropped
                self.sent = end
        except Exception:  # noqa: BLE001 - listening must never break a call
            self.sent = self.rec.now_sample()

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(EVERY_S)
            self.flush()

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        self.flush(lag=False)
        try:
            self.sock.close()
        except OSError:
            pass
