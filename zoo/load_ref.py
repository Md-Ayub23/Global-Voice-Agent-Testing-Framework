"""Load reference agent: one shared voice server that gets slower, by a known amount, the more calls it has.

The measuring stick for Vocto's load tests (Phase 7), as zoo/latency_ref.py is for response times.
It understands nothing. Every caller turn gets a 0.6 s tone after

    delay = base_ms + per_call_ms * (calls in progress - 1)

so with the defaults (300 ms, +250 ms per extra call) one call is answered after 300 ms, two after
550, three after 800, four after 1050. Against a p95 limit of 1000 ms its capacity is exactly 3 calls
at once. Above max_calls it refuses new calls (the connection is closed at once), which a load test
must count as failed calls.

Unlike the other zoo agents it is one server for all calls, the way a real deployed agent is:

    python -m zoo.load_ref --port 8792 [--base-ms 300] [--per-call-ms 250] [--max-calls 6]

Profile: examples/load-ref (agent_process starts it; connection.target dials it).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time

import numpy as np

SILENCE_TO_END_S = 0.3
AMPLITUDE = 0.01


def tone(sample_rate: int, seconds: float = 0.6, freq: float = 523.0) -> np.ndarray:
    t = np.arange(int(seconds * sample_rate)) / sample_rate
    return (0.3 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


class LoadRef:
    def __init__(self, base_ms: int = 300, per_call_ms: int = 250, max_calls: int = 6):
        self.base_ms, self.per_call_ms, self.max_calls = base_ms, per_call_ms, max_calls
        self.active = 0
        self.peak = 0
        self.refused = 0
        self.log: list[dict] = []

    def delay_ms(self) -> int:
        return self.base_ms + self.per_call_ms * max(0, self.active - 1)

    async def handle(self, ws) -> None:
        from vatf.audio.pcm import from_pcm16, to_pcm16

        if self.active >= self.max_calls:
            self.refused += 1
            await ws.close(code=1013, reason="busy: too many calls")
            return
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            start = json.loads(await ws.recv())
            sr = int(start.get("sample_rate", 16000))
            beep = to_pcm16(tone(sr))
            await asyncio.sleep(0.3)
            await ws.send(beep)
            last_sound: float | None = None
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
                    last_sound = arrived - (len(x) - 1 - int(loud[-1])) / sr
                elif last_sound is not None and arrived - last_sound >= SILENCE_TO_END_S:
                    delay = self.delay_ms()
                    wait = last_sound + delay / 1000 - time.monotonic()
                    if wait > 0:
                        await asyncio.sleep(wait)
                    sent = time.monotonic()
                    await ws.send(beep)
                    self.log.append({"active": self.active, "delay_ms": delay,
                                     "actual_ms": round((sent - last_sound) * 1000)})
                    busy_until = sent + len(beep) / 2 / sr
                    last_sound = None
        except Exception:  # noqa: BLE001 - the caller hung up mid-turn; nothing to report
            pass
        finally:
            self.active -= 1


async def serve(port: int = 8792, host: str = "127.0.0.1", **kw):
    """Starts the server; returns (agent, server). The tests use port 0 and read the real port."""
    import websockets

    agent = LoadRef(**kw)
    server = await websockets.serve(agent.handle, host, port, max_size=None, ping_interval=None)
    return agent, server


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--port", type=int, default=8792)
    p.add_argument("--base-ms", type=int, default=300)
    p.add_argument("--per-call-ms", type=int, default=250)
    p.add_argument("--max-calls", type=int, default=6)
    a = p.parse_args()

    async def run() -> None:
        agent, server = await serve(a.port, base_ms=a.base_ms, per_call_ms=a.per_call_ms, max_calls=a.max_calls)
        print(f"load reference agent on ws://127.0.0.1:{a.port}: {a.base_ms} ms + {a.per_call_ms} ms per extra call, "
              f"at most {a.max_calls} calls", flush=True)
        await server.wait_closed()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
