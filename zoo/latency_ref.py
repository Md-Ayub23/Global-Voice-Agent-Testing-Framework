"""Latency reference agent: answers every caller turn after an exact, known delay.

It is a measuring stick for vatf's own latency figures (Phase 2 exit criterion:
vatf's numbers must match a reference within 50 ms). It understands nothing:
it waits for the caller to stop talking, then plays a 0.8 s tone exactly
`delays_ms[i]` after the last sound of the caller's i-th utterance (cycling
through the list). The greeting tone plays 300 ms after the call starts.

It times everything on its own clock from the audio it receives: the frame
holding samples [s, s + 320) arrives when its last sample was spoken, so the
end of the caller's speech is known to the millisecond, independent of vatf.

Profile: examples/latency-ref (connection.launch: "zoo.latency_ref:launch").
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Mapping

import numpy as np

from zoo.voice.gateway import Launched

SILENCE_TO_END_S = 0.3
AMPLITUDE = 0.01


def tone(sample_rate: int, seconds: float = 0.8, freq: float = 440.0) -> np.ndarray:
    t = np.arange(int(seconds * sample_rate)) / sample_rate
    return (0.3 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


async def launch(*, tools: Any, options: Mapping[str, Any]) -> Launched:
    import websockets

    delays = [int(d) for d in str(options.get("delays_ms", "500,900,1300")).split(",")]
    log: list[dict] = []

    async def handle(ws):
        from vatf.audio.pcm import from_pcm16, to_pcm16

        start = json.loads(await ws.recv())
        sr = int(start.get("sample_rate", 16000))
        beep = to_pcm16(tone(sr))
        await asyncio.sleep(0.3)
        await ws.send(beep)
        last_sound: float | None = None   # monotonic time of the last caller sample above AMPLITUDE
        answered = 0
        busy_until = time.monotonic() + len(beep) / 2 / sr
        async for msg in ws:
            if isinstance(msg, str):
                if json.loads(msg).get("type") == "stop":
                    break
                continue
            arrived = time.monotonic()
            x = from_pcm16(msg)
            loud = np.flatnonzero(np.abs(x) >= AMPLITUDE)
            if len(loud) and arrived > busy_until:
                # This frame's last sample was spoken at `arrived`; sample i was spoken (len - 1 - i) / sr earlier.
                last_sound = arrived - (len(x) - 1 - int(loud[-1])) / sr
            elif last_sound is not None and arrived - last_sound >= SILENCE_TO_END_S:
                delay = delays[answered % len(delays)] / 1000
                answered += 1
                wait = last_sound + delay - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                sent = time.monotonic()
                await ws.send(beep)
                log.append({"turn": answered, "delay_ms": round(delay * 1000),
                            "actual_ms": round((sent - last_sound) * 1000)})
                busy_until = sent + len(beep) / 2 / sr
                last_sound = None

    server = await websockets.serve(handle, "127.0.0.1", 0, max_size=None, ping_interval=None)
    launched = Launched(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}", server)
    launched.log = log  # what the agent itself measured, for the tests
    return launched
