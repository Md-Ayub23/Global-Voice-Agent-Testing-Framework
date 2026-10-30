"""Twilio Media Streams connector, the agent process vatf starts, and planted bugs.

Vocto plays the part of Twilio, so these tests stand in for the agent: a small WebSocket server
that speaks the Twilio format, the zoo clinic in its Twilio mode (test-tone speech, offline), and a
dummy agent process. The Pipecat food agent itself needs its own environment (targets/pipecat-food);
the test that reads its planted bugs runs only where Pipecat is installed.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import socket
import sys
import textwrap
import time
from pathlib import Path

import numpy as np
import pytest

from vatf.agent_process import AgentProcess, AgentProcessError
from vatf.audio.g711 import pcm16_to_ulaw, ulaw_to_pcm16
from vatf.audio.pcm import from_pcm16, to_pcm16
from vatf.audio.recorder import Recorder
from vatf.connectors import build_connector, is_voice
from vatf.connectors.twilio_stream import SAMPLE_RATE, TwilioStreamConnector, TwilioStreamSession, start_messages
from vatf.contracts.models import CallContext
from vatf.loader import load_project, schema_errors
from vatf.orchestrator import Orchestrator, RunConfig
from vatf.speech import SpeechConfig
from vatf.web import data
from vatf.web.jobs import JobError, JobManager

ROOT = Path(__file__).resolve().parent.parent
CLINIC = ROOT / "examples" / "clinic"
FOOD = ROOT / "examples" / "pipecat-food"


# ---- G.711 mu-law -------------------------------------------------------------------------

def test_mulaw_is_bit_exact_with_the_reference_codec():
    # Hashes of Python's audioop (removed in 3.13) over every 16-bit sample and every mu-law byte.
    every_sample = np.arange(-32768, 32768, dtype="<i2").tobytes()
    assert hashlib.sha256(pcm16_to_ulaw(every_sample)).hexdigest() == \
        "81d633c9e6972a18c74a58720b96cb8ca0bdd096d4060b646dd708c3b846019a"
    assert hashlib.sha256(ulaw_to_pcm16(bytes(range(256)))).hexdigest() == \
        "3dab54339e520bb2c924826e3b72a917a2b612e9fd12fc867500f1d983a75827"


def test_mulaw_round_trip_keeps_speech_level_audio_close():
    t = np.arange(SAMPLE_RATE) / SAMPLE_RATE
    x = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    y = from_pcm16(ulaw_to_pcm16(pcm16_to_ulaw(to_pcm16(x))))
    assert len(pcm16_to_ulaw(to_pcm16(x))) == len(x)          # one byte per sample
    assert np.max(np.abs(x - y)) < 0.01


# ---- the start of a stream --------------------------------------------------------------

def _profile(**options):
    return {"id": "t", "connection": {"type": "twilio_media_stream", "target": "ws://x", "options": options},
            "test_id": {"propagation": ["proxy_header", "session_metadata"]}}


def test_start_messages_carry_custom_parameters_test_id_and_time(monkeypatch):
    monkeypatch.setenv("SHOP_ID", "shop-42")
    ctx = CallContext(test_id="t-1", run_id="r-1", profile={}, scenario={"setup": {"now": "2026-11-02T18:00:00+00:00"}},
                      repeat=1, attempt=1, mode="voice")
    connected, start = start_messages(ctx, _profile(custom_parameters={"shop": "${SHOP_ID}"}),
                                      stream_sid="MZ1", call_sid="CA1", account_sid="AC1")
    assert connected == {"event": "connected", "protocol": "Call", "version": "1.0.0"}
    assert start["event"] == "start" and start["streamSid"] == "MZ1"
    s = start["start"]
    assert s["customParameters"] == {"shop": "shop-42", "now": "2026-11-02T18:00:00+00:00", "test_id": "t-1"}
    assert s["mediaFormat"] == {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1}
    assert (s["callSid"], s["accountSid"], s["tracks"]) == ("CA1", "AC1", ["inbound"])
    # Without session_metadata the test id is not sent.
    prof = _profile()
    prof["test_id"]["propagation"] = ["proxy_header"]
    assert "test_id" not in start_messages(ctx, prof, stream_sid="MZ1", call_sid="CA1", account_sid="AC1")[1]["start"]["customParameters"]


def test_twilio_profiles_are_voice_and_validate():
    for folder, file in ((CLINIC, "profile-twilio.yaml"), (ROOT / "examples" / "clinic-service", "profile-twilio.yaml"),
                         (FOOD, "profile.yaml")):
        proj = load_project(folder, file)
        assert is_voice(proj.profile) and not schema_errors("client_profile", proj.profile)
    assert isinstance(build_connector(load_project(FOOD).profile, ROOT, None), TwilioStreamConnector)


# ---- the session, against a stand-in agent ---------------------------------------------

class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class Agent:
    """A Twilio-format agent server: records what Vocto sends, sends what the test asks."""

    def __init__(self):
        self.got: list[dict] = []
        self.ws = None
        self.ready = asyncio.Event()

    async def handler(self, ws):
        self.ws = ws
        self.ready.set()
        try:
            async for raw in ws:
                self.got.append(json.loads(raw))
        except Exception:
            pass

    async def send(self, msg):
        await self.ws.send(json.dumps(msg))


def _media(seconds: float, level: float = 0.3) -> dict:
    n = int(seconds * SAMPLE_RATE)
    x = (level * np.sin(2 * np.pi * 300 * np.arange(n) / SAMPLE_RATE)).astype(np.float32)
    return {"event": "media", "streamSid": "MZ", "media": {"payload": base64.b64encode(pcm16_to_ulaw(to_pcm16(x))).decode()}}


async def _session(agent: Agent, clock, **options):
    import websockets

    server = await websockets.serve(agent.handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    prof = _profile(**options)
    prof["connection"]["target"] = f"ws://127.0.0.1:{port}"
    rec = Recorder(SAMPLE_RATE, clock)
    session = await TwilioStreamConnector(prof).open(None, rec)
    await asyncio.wait_for(agent.ready.wait(), 5)
    return server, session, rec


async def _until(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("timed out")
        await asyncio.sleep(0.01)


def test_session_speaks_twilio_both_ways():
    async def main():
        agent, clock = Agent(), FakeClock()
        server, session, rec = await _session(agent, clock, account_sid="ACtest")
        await _until(lambda: len(agent.got) >= 2)
        assert [m["event"] for m in agent.got[:2]] == ["connected", "start"]
        assert agent.got[1]["start"]["accountSid"] == "ACtest"

        # Caller audio: 20 ms frames, mu-law, chunk and timestamp counting up, sequence numbers rising.
        frame = np.full(160, 0.1, dtype=np.float32)
        for _ in range(3):
            await session.send_frame(frame)
        await _until(lambda: sum(m["event"] == "media" for m in agent.got) == 3)
        media = [m for m in agent.got if m["event"] == "media"]
        assert [m["media"]["chunk"] for m in media] == ["1", "2", "3"]
        assert [m["media"]["timestamp"] for m in media] == ["0", "20", "40"]
        assert all(len(base64.b64decode(m["media"]["payload"])) == 160 and m["media"]["track"] == "inbound" for m in media)
        assert all(m["streamSid"] == session.stream_sid for m in media)
        seqs = [int(m["sequenceNumber"]) for m in agent.got[1:]]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)

        # Agent audio is queued; its mark comes back only once that audio has played.
        await agent.send(_media(1.0))
        await agent.send({"event": "mark", "streamSid": "MZ", "mark": {"name": "said-hello"}})
        await _until(lambda: rec.chunks == 1 and session.marks)
        await asyncio.sleep(0.1)
        assert not [m for m in agent.got if m["event"] == "mark"]
        clock.t = 1.05
        await _until(lambda: [m for m in agent.got if m["event"] == "mark"])
        assert [m["mark"]["name"] for m in agent.got if m["event"] == "mark"] == ["said-hello"]

        # Clear (barge-in): the unplayed rest is dropped and pending marks come back at once.
        clock.t = 2.0
        await agent.send(_media(2.0))
        await agent.send({"event": "mark", "streamSid": "MZ", "mark": {"name": "long-answer"}})
        await _until(lambda: rec.chunks == 2 and session.marks)
        clock.t = 2.5
        await agent.send({"event": "clear", "streamSid": "MZ"})
        await _until(lambda: rec.clears)
        assert rec.clears[0].at_sample == int(2.5 * SAMPLE_RATE) and rec.cursor == int(2.5 * SAMPLE_RATE)
        await _until(lambda: [m for m in agent.got if m.get("mark", {}).get("name") == "long-answer"])

        # Protocol mistakes are noted, not fatal; the agent hanging up ends the call.
        await agent.ws.send(b"\x00\x01")
        await agent.send({"event": "media", "media": {"payload": "%%%"}})
        await _until(lambda: len(session.protocol_errors) == 2)
        await agent.ws.close()
        await asyncio.wait_for(session.closed.wait(), 5)
        assert session.closed_by_agent
        await session.close()
        server.close()
        await server.wait_closed()

    asyncio.run(main())


def test_vocto_hangs_up_with_a_stop_message():
    async def main():
        agent, clock = Agent(), FakeClock()
        server, session, _ = await _session(agent, clock)
        await session.close()
        await _until(lambda: agent.got and agent.got[-1]["event"] == "stop")
        assert agent.got[-1]["stop"]["callSid"] == session.call_sid
        server.close()
        await server.wait_closed()

    asyncio.run(main())


def test_jitter_buffer_joins_late_chunks_but_not_real_pauses():
    clock = FakeClock()
    rec = Recorder(SAMPLE_RATE, clock)
    session = TwilioStreamSession.__new__(TwilioStreamSession)     # play() needs only the recorder
    session.rec, session._jitter = rec, 60 * SAMPLE_RATE // 1000
    chunk = np.full(160, 0.2, dtype=np.float32)                     # 20 ms
    session.play(chunk)
    clock.t = 0.025                                                 # 5 ms after it finished: joined on
    assert session.play(chunk) is None and rec.cursor == 320
    clock.t = 0.040 + 0.061                                         # 61 ms late: a real pause, plays on arrival
    session.play(chunk)
    assert rec.cursor == int(0.101 * SAMPLE_RATE) + 160
    session._jitter = 0                                             # buffer off: every chunk plays on arrival
    start = rec.cursor
    clock.t = (start + 40.5) / SAMPLE_RATE                          # 5 ms after the queue ran dry
    session.play(chunk)
    assert rec.cursor == start + 40 + 160


def test_recorder_must_run_at_8khz():
    async def main():
        rec = Recorder(16000, FakeClock())
        with pytest.raises(Exception, match="8 kHz"):
            await TwilioStreamConnector(_profile()).open(None, rec)

    asyncio.run(main())


# ---- a whole run, and the connection test, against the clinic in Twilio mode ------------

@pytest.fixture(scope="module")
def clinic_twilio_run(tmp_path_factory):
    proj = load_project(CLINIC, "profile-twilio.yaml").with_agent_options({"brain": "rules", "bugs": "", "speech": "codec"})
    cfg = RunConfig(root=ROOT, runs_dir=tmp_path_factory.mktemp("twilio") / "runs", offline=True,
                    speech=SpeechConfig(engine="codec"), repeats=1)
    orch = Orchestrator(proj, cfg, log=lambda *_: None)
    return orch, asyncio.run(orch.run())


def test_clinic_over_twilio_goes_with_barge_in(clinic_twilio_run):
    orch, decision = clinic_twilio_run
    assert decision["decision"] == "GO", decision["reasons"]
    call = next(c for c in decision["calls"] if c["scenario_id"] == "clinic.caller-interrupts")
    folder = orch.store.call_dir(decision["run_id"], call["test_id"])
    events = [json.loads(line) for line in (folder / "events.jsonl").read_text("utf-8").splitlines()]
    cut = next(e for e in events if e["type"] == "agent_turn" and e["data"].get("interrupted"))
    assert cut["data"]["stopped_after_ms"] <= 600
    ended = next(e for e in events if e["type"] == "call_ended")["data"]["recording"]
    assert ended["sample_rate_hz"] == 8000


def test_connection_test_hears_the_greeting():
    from vatf.connectors.websocket_audio import launch_agent
    from vatf.web.setup import _test_twilio

    async def main():
        conn = {"type": "twilio_media_stream", "launch": "zoo.clinic.voice:launch",
                "options": {"brain": "rules", "speech": "codec"}}
        launched = await launch_agent(conn, ROOT, None)
        try:
            prof = {"connection": {"type": "twilio_media_stream", "target": launched.url}, "test_id": {}}
            return await _test_twilio(prof)
        finally:
            await launched.close()

    out = asyncio.run(main())
    assert out["ok"], out
    assert "Speech heard" in out["details"][0]
    bad = asyncio.run(_bad_address())
    assert not bad["ok"] and "ws://" in bad["message"]


async def _bad_address():
    from vatf.web.setup import _test_twilio

    return await _test_twilio({"connection": {"type": "twilio_media_stream", "target": "http://x"}})


# ---- the agent process vatf starts -------------------------------------------------------

DUMMY = textwrap.dedent("""
    import http.server, json, os, sys
    out = sys.argv[2]
    with open(out, "w") as f:
        json.dump({"bug": os.environ.get("DUMMY_BUG"), "extra": os.environ.get("DUMMY_EXTRA")}, f)
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
        def log_message(self, *a): pass
    print("dummy agent up", flush=True)
    http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
""")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _dummy_profile(tmp_path: Path, port: int) -> dict:
    script = tmp_path / "dummy_agent.py"
    script.write_text(DUMMY, encoding="utf-8")
    return {"id": "dummy", "connection": {"type": "twilio_media_stream", "target": f"ws://127.0.0.1:{port}/ws"},
            "agent_process": {"command": [sys.executable, str(script), str(port), str(tmp_path / "env.json")],
                              "ready_url": f"http://127.0.0.1:{port}/health", "ready_timeout_s": 20,
                              "planted_bug_env": "DUMMY_BUG", "env": {"DUMMY_EXTRA": "${DUMMY_EXTRA_SRC}"}}}


def test_agent_process_starts_with_the_planted_bug_and_stops(tmp_path, monkeypatch):
    monkeypatch.setenv("DUMMY_EXTRA_SRC", "from-secret")
    port = _free_port()
    prof = _dummy_profile(tmp_path, port)
    agent = AgentProcess(prof, tmp_path, Path("runs"), planted_bug="leaks_secret")
    with agent:
        assert json.loads((tmp_path / "env.json").read_text()) == {"bug": "leaks_secret", "extra": "from-secret"}
        # A second copy refuses to start: it would test whatever is already answering.
        with pytest.raises(AgentProcessError, match="already answering"):
            AgentProcess(prof, tmp_path, Path("runs")).start()
    with socket.socket() as s:
        assert s.connect_ex(("127.0.0.1", port)) != 0          # stopped, and the port is free again
    log = (tmp_path / "runs" / ".agents" / "dummy.log").read_text()
    assert "planted bug leaks_secret" in log and "dummy agent up" in log and "stopped" in log
    with AgentProcess(prof, tmp_path, Path("runs")):           # clean: the bug variable is empty
        assert json.loads((tmp_path / "env.json").read_text())["bug"] == ""


def test_agent_process_reports_a_crash_a_missing_program_and_a_missing_secret(tmp_path, monkeypatch):
    prof = _dummy_profile(tmp_path, _free_port())
    monkeypatch.delenv("DUMMY_EXTRA_SRC", raising=False)
    with pytest.raises(AgentProcessError, match="set DUMMY_EXTRA_SRC"):
        AgentProcess(prof, tmp_path, Path("runs")).start()
    monkeypatch.setenv("DUMMY_EXTRA_SRC", "x")
    prof["agent_process"]["command"] = [sys.executable, "-c", "import sys; print('no model file'); sys.exit(3)"]
    with pytest.raises(AgentProcessError, match="exit code 3") as e:
        AgentProcess(prof, tmp_path, Path("runs")).start()
    assert "no model file" in str(e.value)
    prof["agent_process"]["command"] = ["no-such-program-vocto"]
    with pytest.raises(AgentProcessError, match="can't find the program"):
        AgentProcess(prof, tmp_path, Path("runs")).start()
    with pytest.raises(AgentProcessError, match="no agent_process"):
        AgentProcess({"connection": {}}, tmp_path, Path("runs"))


# ---- planted bugs in the profile, the dashboard and the food agent ------------------------

def test_food_profile_lists_planted_bugs_for_the_dashboard():
    info = data.profile_info(ROOT, FOOD, "profile.yaml")
    assert info["ok"] and info["mode"] == "voice" and info["agent_process"]
    assert {b["id"]: b["scenario"] for b in info["planted_bugs"]} == {
        "skip_confirmation": "pipecat-food.order-pizza", "ignores_order_failure": "pipecat-food.order-system-down",
        "records_wrong_size": "pipecat-food.order-pizza", "rude_tone": "pipecat-food.order-pizza",
        "leaks_secret": "safety.secrets.persistent"}
    assert next(b for b in info["planted_bugs"] if b["id"] == "rude_tone")["judge"] == "polite-professional-tone"
    bad = load_project(FOOD).profile | {"planted_bugs": [{"id": "Bad Name", "caught_by": {}}]}
    assert schema_errors("client_profile", bad)


def test_new_run_can_start_the_agent_with_a_planted_bug(tmp_path):
    jobs = JobManager(ROOT, Path("runs"))
    project = {"path": "examples/pipecat-food", "profiles": [data.profile_info(ROOT, FOOD, "profile.yaml")]}
    args, command = jobs.build({"command": "run", "profile": "profile.yaml", "start_agent": True,
                                "planted_bug": "records_wrong_size", "offline": True}, project)
    assert args[:4] == ["run", "examples/pipecat-food", "--profile", "profile.yaml"]
    assert "--start-agent" in args and args[args.index("--planted-bug") + 1] == "records_wrong_size"
    assert "--planted-bug records_wrong_size" in command
    with pytest.raises(JobError, match="no planted bug"):
        jobs.build({"command": "run", "profile": "profile.yaml", "start_agent": True, "planted_bug": "nope"}, project)
    with pytest.raises(JobError, match="needs the agent to be started"):
        jobs.build({"command": "run", "profile": "profile.yaml", "planted_bug": "records_wrong_size"}, project)
    exit_args, _ = jobs.build({"command": "exit-check", "profile": "profile.yaml", "offline": True}, project)
    assert "--start-agent" not in exit_args       # the exit check starts and restarts the agent itself


def test_cli_refuses_a_planted_bug_it_does_not_know(capsys):
    from vatf.cli import main

    with pytest.raises(SystemExit, match="unknown planted bug"):
        main(["run", "examples/pipecat-food", "--start-agent", "--planted-bug", "nope", "--offline", "--root", str(ROOT)])
    with pytest.raises(SystemExit, match="needs --start-agent"):
        main(["run", "examples/pipecat-food", "--planted-bug", "rude_tone", "--offline", "--root", str(ROOT)])


def test_food_agent_planted_bugs_match_upstream_text():
    pytest.importorskip("pipecat.flows")
    sys.path.insert(0, str(ROOT / "targets" / "pipecat-food"))
    import importlib

    import planted_bugs

    for bug in [None, *planted_bugs.BUGS]:
        flow = importlib.reload(importlib.import_module("food_ordering"))
        planted_bugs.apply(flow, bug)          # raises if an upstream sentence it edits has changed
        text = json.dumps(flow.create_initial_node(), default=str)
        assert planted_bugs.CANARY in text


def test_an_agent_left_running_by_a_killed_run_is_stopped_first(tmp_path, monkeypatch):
    monkeypatch.setenv("DUMMY_EXTRA_SRC", "x")
    port = _free_port()
    prof = _dummy_profile(tmp_path, port)
    left = AgentProcess(prof, tmp_path, Path("runs")).start()
    left.proc = None              # as if vatf had been killed: nobody stops it, its pid file stays
    agent_pid = left.pid_path.read_text().split()[0]
    left.pid_path.write_text(f"{agent_pid} 999999999")     # ...and the vatf that started it is gone
    with AgentProcess(prof, tmp_path, Path("runs"), planted_bug="rude_tone"):
        assert json.loads((tmp_path / "env.json").read_text())["bug"] == "rude_tone"   # the new one answers
    assert not left.pid_path.exists()
    with socket.socket() as s:
        assert s.connect_ex(("127.0.0.1", port)) != 0


def test_stopping_while_the_agent_starts_does_not_leave_it_running(tmp_path, monkeypatch):
    import vatf.agent_process as ap

    monkeypatch.setenv("DUMMY_EXTRA_SRC", "x")
    port = _free_port()
    prof = _dummy_profile(tmp_path, port)
    prof["agent_process"]["ready_url"] = f"http://127.0.0.1:{_free_port()}/never"   # never ready
    agent = AgentProcess(prof, tmp_path, Path("runs"))
    calls = {"n": 0}

    def ready():
        calls["n"] += 1
        if calls["n"] > 6:        # about 3 s in, once the dummy is listening: the person presses Stop
            raise KeyboardInterrupt
        return False

    monkeypatch.setattr(agent, "_ready", ready)
    with pytest.raises(KeyboardInterrupt):
        agent.start()
    assert agent.proc is None and not agent.pid_path.exists()
    with socket.socket() as s:
        assert s.connect_ex(("127.0.0.1", port)) != 0
