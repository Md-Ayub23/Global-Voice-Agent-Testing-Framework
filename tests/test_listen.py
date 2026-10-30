"""Listen live: the running voice call's audio reaches the dashboard without changing the call."""

import asyncio
import queue
import socket
import time
from pathlib import Path

import numpy as np

from vatf.audio.recorder import Recorder
from vatf.audio.tap import PORT_ENV, LiveTap, pack, unpack
from vatf.loader import load_project
from vatf.orchestrator import Orchestrator, RunConfig
from vatf.speech import SpeechConfig
from vatf.web.jobs import LiveAudio

ROOT = Path(__file__).resolve().parent.parent


def test_datagrams_round_trip_and_junk_is_ignored():
    x = np.array([0.0, 0.5, -0.5, 2.0], dtype=np.float32)
    sr, start, label, pcm = unpack(pack(16000, 123456, "t-1 clinic.book-checkup", x))
    assert (sr, start, label) == (16000, 123456, "t-1 clinic.book-checkup")
    assert np.frombuffer(pcm, "<i2").tolist() == [0, 16383, -16383, 32767]   # clipped, not wrapped
    assert unpack(b"hello") is None and unpack(b"XXXX" + pack(16000, 0, "", x)[4:]) is None


def test_a_listener_that_falls_behind_loses_audio_and_nobody_else_waits():
    audio = LiveAudio()
    slow, fast = audio.listen(), audio.listen()
    send = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    packet = pack(16000, 0, "t-1 s", np.zeros(160, dtype=np.float32))
    for _ in range(audio.QUEUE + 30):
        send.sendto(packet, ("127.0.0.1", audio.port))
    send.sendto(b"not audio", ("127.0.0.1", audio.port))
    time.sleep(0.3)
    assert slow.qsize() == fast.qsize() == audio.QUEUE    # full, the rest dropped; the sender never waited
    audio.leave(slow)
    assert audio.listeners == 1


def test_the_tap_sends_whats_on_the_line_and_never_raises():
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    rx.settimeout(1)
    t = [0.0]
    rec = Recorder(16000, lambda: t[0])
    tap = LiveTap(rec, "t-1 s", rx.getsockname()[1])
    rec.caller.write(0, np.full(8000, 0.25, dtype=np.float32))
    rec.agent_audio(np.full(4000, 0.25, dtype=np.float32), arrival_s=0.0)
    t[0] = 0.5
    tap.flush(lag=False)
    got = np.frombuffer(unpack(rx.recv(65536))[3], "<i2")
    assert len(got) == 8000 and got[0] == int(0.5 * 32767) and got[-1] == int(0.25 * 32767)
    tap.sock.close()
    t[0] = 1.0
    tap.flush()                                  # a closed socket: dropped, no error


def test_a_voice_call_streams_live_and_its_timing_is_unchanged(tmp_path, monkeypatch):
    audio = LiveAudio()
    listener = audio.listen()
    monkeypatch.setenv(PORT_ENV, str(audio.port))
    proj = load_project(ROOT / "examples" / "latency-ref")
    cfg = RunConfig(root=ROOT, runs_dir=tmp_path / "runs", offline=True, speech=SpeechConfig(engine="codec"), repeats=1)
    orch = Orchestrator(proj, cfg, log=lambda *_: None)
    decision = asyncio.run(orch.run())
    time.sleep(0.2)
    packets = []
    while True:
        try:
            packets.append(unpack(listener.get_nowait()))
        except queue.Empty:
            break
    assert packets and all(p is not None for p in packets)
    labels = {p[2] for p in packets}
    assert labels == {f"{decision['calls'][0]['test_id']} latency-ref.three-answers"}
    starts = [p[1] for p in packets]
    ends = [p[1] + len(p[3]) // 2 for p in packets]
    assert all(a == b for a, b in zip(ends, starts[1:]))          # one continuous stream, no gaps
    heard = np.concatenate([np.frombuffer(p[3], "<i2") for p in packets])
    assert np.abs(heard).max() > 1000                              # real sound, not silence
    from vatf.util import read_json
    verdict = read_json(orch.store.call_dir(decision["run_id"], decision["calls"][0]["test_id"]) / "verdict.json")
    measured = [r["ms"] for r in verdict["metrics"]["latency_turns"]]
    for got, want in zip(measured, [500, 900, 1300]):
        assert abs(got - want) <= 50, measured
