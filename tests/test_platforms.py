"""Hosted voice platforms (Vapi, ElevenLabs, Retell, LiveKit), against fakes of their APIs.

The fakes (platform_fakes.py) run the zoo clinic agent behind each platform's own protocol, and run the
agent's tools themselves, as the platform would. So Vocto learns about the tool calls only from the
platform's record of the call, and the checks must pass from that alone.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from vatf.loader import load_project
from vatf.orchestrator import Orchestrator, RunConfig
from vatf.speech import SpeechConfig

from platform_fakes import (ROOT, FakeElevenLabs, FakeRetellAPI, FakeRoom, FakeVapi, platform_profile)

CODEC = SpeechConfig(engine="codec")
SCENARIO = "clinic.book-checkup"


def project_with(profile: dict):
    proj = load_project(ROOT / "examples" / "clinic", "profile.yaml")
    clone = copy.deepcopy(proj)
    clone.profile = profile
    return clone


def run(tmp_path, profile, scenarios=(SCENARIO,)):
    cfg = RunConfig(root=ROOT, runs_dir=tmp_path / "runs", offline=True, speech=CODEC, repeats=1,
                    scenario_ids=tuple(scenarios), judges=False)
    orch = Orchestrator(project_with(profile), cfg, log=lambda *_: None)
    decision = asyncio.run(orch.run())
    calls = {}
    for c in decision["calls"]:
        folder = orch.store.call_dir(decision["run_id"], c["test_id"])
        events = [json.loads(line) for line in (folder / "events.jsonl").read_text("utf-8").splitlines()]
        calls[c["scenario_id"]] = (json.loads((folder / "verdict.json").read_text("utf-8")), events, folder)
    return decision, calls


def failed_checks(verdict):
    return [(c["check_id"], c.get("detail") or c.get("reason")) for c in verdict["checks"] if c.get("status") == "FAIL"]


def schema_problems(verdict, events):
    from vatf.loader import schema_errors

    out = schema_errors("call_verdict", verdict)
    for e in events:
        out += [f"{e['type']}: {x}" for x in schema_errors("trace_event", e)]
    return out


def all_text(folder: Path) -> str:
    return "\n".join(p.read_text("utf-8", errors="replace") for p in folder.rglob("*")
                     if p.is_file() and p.suffix in (".json", ".jsonl", ".md", ".txt", ".html"))


# ---- Vapi ------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def vapi_run(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    fake = FakeVapi(ready_after_reads=2)
    mp.setenv("VOCTO_VAPI_API_BASE", fake.base)
    mp.setenv("VAPI_TEST_KEY", FakeVapi.KEY)
    try:
        prof = platform_profile("vapi", "asst_1", api_key="${VAPI_TEST_KEY}", report_wait_s=20)
        decision, calls = run(tmp_path_factory.mktemp("vapi"), prof)
        yield fake, decision, calls
    finally:
        fake.close()
        mp.undo()


def test_a_vapi_call_passes_from_the_platform_record_alone(vapi_run):
    fake, decision, calls = vapi_run
    verdict, events, folder = calls[SCENARIO]
    assert not failed_checks(verdict), failed_checks(verdict)
    assert schema_problems(verdict, events) == []
    assert decision["calls"][0]["status"] == "PASS", decision["reasons"]
    reqs = [e for e in events if e["type"] == "tool_request"]
    assert reqs and all(e["source"] == "platform" and e["data"]["reported_by"] == "vapi" for e in reqs)
    assert "book_appointment" in {e["data"]["tool"] for e in reqs}
    # each tool call sits in the caller turn it belongs to, before the agent's answer that used it
    book = next(e for e in reqs if e["data"]["tool"] == "book_appointment")
    after = [e for e in events if e["type"] == "agent_turn" and e["seq"] > book["seq"]]
    assert after, "the booking was placed after every agent turn"
    seqs = [e["seq"] for e in events]
    assert seqs == list(range(1, len(events) + 1))
    # the Vapi record: the test id reached the agent, the call was created over the websocket transport
    create = next(b for m, p, b in fake.requests if m == "POST" and p == "/call")
    assert create["transport"]["provider"] == "vapi.websocket"
    assert create["assistantId"] == "asst_1"
    assert create["assistantOverrides"]["variableValues"]["vocto_test_id"] == decision["calls"][0]["test_id"]


def test_the_vapi_bill_and_summary_are_kept_and_the_key_is_not(vapi_run):
    _, decision, calls = vapi_run
    verdict, events, folder = calls[SCENARIO]
    ended = next(e for e in events if e["type"] == "call_ended")
    plat = ended["data"]["platform"]
    assert plat["platform"] == "vapi" and abs(plat["cost"] - 0.0731) < 1e-9
    assert FakeVapi.KEY not in all_text(folder.parent.parent)


def test_a_wrong_vapi_key_fails_the_call_in_plain_words(tmp_path, monkeypatch):
    fake = FakeVapi()
    monkeypatch.setenv("VOCTO_VAPI_API_BASE", fake.base)
    monkeypatch.setenv("VAPI_TEST_KEY", "not-the-key-999")
    try:
        from vatf.connectors.platforms import check
        from vatf.connectors.platforms.base import PlatformError

        prof = platform_profile("vapi", "asst_1", api_key="${VAPI_TEST_KEY}")
        with pytest.raises(PlatformError) as e:
            asyncio.run(check(prof))
        assert "401" in str(e.value) or "key" in str(e.value).lower()
        assert "not-the-key-999" not in str(e.value)
        monkeypatch.setenv("VAPI_TEST_KEY", FakeVapi.KEY)
        ok = asyncio.run(check(prof))
        assert ok["ok"] is True
    finally:
        fake.close()


def test_a_key_typed_into_the_profile_is_refused(monkeypatch):
    from vatf.connectors.platforms import check
    from vatf.connectors.platforms.base import PlatformError

    prof = platform_profile("vapi", "asst_1", api_key=FakeVapi.KEY)
    with pytest.raises(PlatformError) as e:
        asyncio.run(check(prof))
    assert "saved secret" in str(e.value) and FakeVapi.KEY not in str(e.value)


def test_a_platform_address_cannot_come_from_the_profile(monkeypatch):
    from vatf.connectors.platforms.base import api_base

    monkeypatch.delenv("VOCTO_VAPI_API_BASE", raising=False)
    assert api_base("vapi", "https://api.vapi.ai") == "https://api.vapi.ai"


# ---- ElevenLabs --------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def eleven_run(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    fake = FakeElevenLabs(client_tools=("list_free_slots",))
    mp.setenv("VOCTO_ELEVENLABS_API_BASE", fake.base)
    mp.setenv("XI_TEST_KEY", FakeElevenLabs.KEY)
    try:
        prof = platform_profile("elevenlabs", "agent_1", api_key="${XI_TEST_KEY}", report_wait_s=20)
        decision, calls = run(tmp_path_factory.mktemp("eleven"), prof)
        yield fake, decision, calls
    finally:
        fake.close()
        mp.undo()


def test_an_elevenlabs_call_with_a_client_tool_answered_by_vocto(eleven_run):
    fake, decision, calls = eleven_run
    verdict, events, folder = calls[SCENARIO]
    assert not failed_checks(verdict), failed_checks(verdict)
    assert schema_problems(verdict, events) == []
    reqs = [e for e in events if e["type"] == "tool_request"]
    by_tool = {}
    for e in reqs:
        by_tool.setdefault(e["data"]["tool"], []).append(e)
    # list_free_slots is a client tool: Vocto's own tool gateway answered it, so it is there once, not twice
    assert len(by_tool.get("list_free_slots", [])) >= 1
    assert all(e["source"] != "platform" for e in by_tool["list_free_slots"])
    assert fake.client_results and all(not r.get("is_error") for r in fake.client_results)
    # book_appointment ran on the platform (a server tool): known only from its record
    assert by_tool["book_appointment"] and all(e["data"].get("reported_by") == "elevenlabs" for e in by_tool["book_appointment"])
    assert fake.inits[0]["dynamic_variables"]["vocto_test_id"] == decision["calls"][0]["test_id"]
    text = all_text(folder.parent.parent)
    assert FakeElevenLabs.KEY not in text and "sig-SECRET-42" not in text


# ---- Retell and LiveKit (rooms) -------------------------------------------------------------------------

@pytest.fixture(scope="module")
def retell_run(tmp_path_factory):
    from vatf.connectors.platforms import livekit_room

    mp = pytest.MonkeyPatch()
    api = FakeRetellAPI()
    mp.setenv("VOCTO_RETELL_API_BASE", api.base)
    mp.setenv("VOCTO_RETELL_ROOM_URL", "ws://127.0.0.1:1")
    mp.setenv("RETELL_TEST_KEY", FakeRetellAPI.KEY)
    mp.setattr(livekit_room, "ROOM_LINK", lambda: FakeRoom("retell", shim_for=api.shim_for))
    try:
        prof = platform_profile("retell", "agent_r1", api_key="${RETELL_TEST_KEY}", report_wait_s=20)
        decision, calls = run(tmp_path_factory.mktemp("retell"), prof)
        yield api, decision, calls
    finally:
        api.close()
        mp.undo()


def test_a_retell_web_call_passes_from_its_record(retell_run):
    api, decision, calls = retell_run
    verdict, events, folder = calls[SCENARIO]
    assert not failed_checks(verdict), failed_checks(verdict)
    assert schema_problems(verdict, events) == []
    reqs = [e for e in events if e["type"] == "tool_request"]
    assert reqs and all(e["data"].get("reported_by") == "retell" for e in reqs)
    plat = next(e for e in events if e["type"] == "call_ended")["data"]["platform"]
    assert abs(plat["cost"] - 0.095) < 1e-9
    call = next(iter(api.calls.values()))
    assert call["metadata"]["vocto_test_id"] == decision["calls"][0]["test_id"]
    assert "eyJ-room-token" not in all_text(folder.parent.parent)


@pytest.fixture(scope="module")
def livekit_run(tmp_path_factory):
    from vatf.connectors.platforms import livekit_agent, livekit_room

    mp = pytest.MonkeyPatch()
    rooms: list[FakeRoom] = []

    def make():
        r = FakeRoom("livekit", report_tools=True)
        rooms.append(r)
        return r

    deleted: list[str] = []

    async def fake_delete(profile, room, secrets):
        deleted.append(room)

    mp.setattr(livekit_room, "ROOM_LINK", make)
    mp.setattr(livekit_agent, "delete_room", fake_delete)
    mp.setenv("LK_TEST_KEY", "APIabcdef")
    mp.setenv("LK_TEST_SECRET", "s3cr3t-livekit-secret-value-0123456789")
    try:
        prof = platform_profile("livekit", "clinic-agent", url="ws://127.0.0.1:7880", api_key="${LK_TEST_KEY}",
                                api_secret="${LK_TEST_SECRET}")
        decision, calls = run(tmp_path_factory.mktemp("livekit"), prof)
        yield rooms, deleted, decision, calls
    finally:
        mp.undo()


def test_a_livekit_agent_reports_its_tools_and_the_room_is_deleted(livekit_run):
    rooms, deleted, decision, calls = livekit_run
    verdict, events, folder = calls[SCENARIO]
    assert not failed_checks(verdict), failed_checks(verdict)
    assert schema_problems(verdict, events) == []
    reqs = [e for e in events if e["type"] == "tool_request"]
    assert reqs and all(e["data"].get("reported_by") == "livekit" for e in reqs)
    assert deleted and deleted[0].startswith("vocto-")
    # the caller token asks LiveKit to send the named agent, and carries the test id
    import jwt as _jwt  # noqa: F401  (pyjwt comes with livekit-api)

    claims = _jwt.decode(rooms[0].token, options={"verify_signature": False})
    assert claims["roomConfig"]["agents"][0]["agentName"] == "clinic-agent"
    assert json.loads(claims["metadata"])["vocto_test_id"] == decision["calls"][0]["test_id"]
    text = all_text(folder.parent.parent)
    assert "s3cr3t-livekit-secret" not in text and rooms[0].token not in text


# ---- pieces --------------------------------------------------------------------------------------------

def test_a_stream_of_silence_does_not_keep_the_agent_talking():
    """Phone and WebRTC streams carry the far end all the time; silence must not count as the agent talking."""
    from vatf.audio.recorder import Recorder, play_agent

    now = [0.0]
    rec = Recorder(16000, lambda: now[0])
    quiet = np.zeros(320, dtype=np.float32)
    hiss = np.full(320, 0.001, dtype=np.float32)
    for _ in range(50):                     # one second of silence and line hiss
        assert play_agent(rec, quiet, 800) is False
        assert play_agent(rec, hiss, 800) is False
        now[0] += 0.02
    assert rec.chunks == 0 and not rec.agent_playing()
    tone = (0.3 * np.sin(np.arange(320) / 5)).astype(np.float32)
    assert play_agent(rec, tone, 800)
    assert rec.agent_playing()
    now[0] += 0.021                         # the next chunk is a little late: it joins on without a gap
    assert play_agent(rec, tone, 800)
    assert rec.cursor == int(1.0 * 16000) + 640
    now[0] += 1.0
    assert not rec.agent_playing()


def _ev(seq, t, type_, data=None, turn=None, source="vocto"):
    return SimpleNamespace(seq=seq, t_ms=t, type=type_, data=data or {}, turn=turn, source=source)


def test_platform_tool_calls_go_where_they_happened_and_only_once():
    from vatf import reconcile

    events = [_ev(1, 0, "call_started"), _ev(2, 400, "agent_turn", {"audio": {"start_ms": 1400}}, 1),
              _ev(3, 3000, "caller_turn", {}, 1), _ev(4, 3500, "tool_request", {"tool": "find_patient", "args": {"dob": "1"}}, 1, "gateway"),
              _ev(5, 6000, "caller_turn", {}, 2), _ev(6, 9000, "agent_turn", {"audio": {"start_ms": 9000}}, 2),
              _ev(7, 12000, "call_ended")]
    call = {"items": [{"kind": "turn", "role": "agent", "text": "hi", "start_ms": 900},
                      {"kind": "tool", "id": "a", "name": "find_patient", "args": {"dob": "1"}, "at_ms": 2000},
                      {"kind": "tool", "id": "b", "name": "book", "args": {"slot": 3}, "at_ms": 6500, "result": "ok"},
                      {"kind": "tool", "id": "c", "name": "live_one", "args": {}},
                      {"kind": "tool", "id": "d", "name": "client_one", "args": {}},
                      {"kind": "tool", "id": "e", "name": "no_time", "args": {}}],
            "transferred": True, "ended_reason": "assistant-forwarded-call"}
    session = SimpleNamespace(t0_ms=1000, live_tools={"c": 7000}, handled_tools={"d"})
    report = SimpleNamespace(platform="vapi", call=call)
    assert reconcile.clock_offset(call, events, 1000) == 500     # their 900 ms greeting = our 1400 ms
    out = reconcile.platform_events(report, session, events)
    reqs = [o for o in out if o[2] == "tool_request"]
    assert [r[3]["tool"] for r in reqs] == ["book", "live_one", "no_time"]      # find_patient: the gateway had it
    book, live, late = reqs
    assert book[0] == 7000 and book[4] == 2                                      # 6500 + 500, caller turn 2
    assert live[0] == 7000
    assert late[0] == 11999 and late[4] == 2
    resp = next(o for o in out if o[2] == "tool_response" and o[3]["tool"] == "book")
    assert resp[3]["body"] == {"result": "ok"} and resp[3]["reported_by"] == "vapi"
    handoff = [o for o in out if o[2] == "handoff"]
    assert handoff and handoff[0][3]["reason"] == "assistant-forwarded-call"


def test_a_greeting_that_does_not_line_up_falls_back_to_the_session_start():
    from vatf import reconcile

    events = [_ev(1, 0, "agent_turn", {"audio": {"start_ms": 60000}})]
    call = {"items": [{"kind": "turn", "role": "agent", "start_ms": 100}]}
    assert reconcile.clock_offset(call, events, 2000) == 2000
    assert reconcile.clock_offset({"clock": "session"}, events, 2000) == 2000


def test_inserting_events_keeps_time_order_and_renumbers_the_references(tmp_path):
    from vatf.evidence import LocalCallWriter

    rec = {"test_id": "t-1", "events_file": "events.jsonl", "llm_log_dir": "llm"}
    w = LocalCallWriter(tmp_path, rec, {})
    import time

    w.emit("vocto", "caller_turn", {"text": "hi"}, turn=1)
    time.sleep(0.01)
    w.emit("vocto", "agent_turn", {"heard_text": "hello"}, turn=1)
    time.sleep(0.01)
    w.emit("vocto", "judge_note", {"agent_turn": 2, "caller_turn": 1})
    t = [e.t_ms for e in w.events]
    added = w.insert([(t[1], "platform", "tool_request", {"tool": "x"}, 1)])
    assert [e.type for e in w.events] == ["caller_turn", "agent_turn", "tool_request", "judge_note"]
    assert [e.seq for e in w.events] == [1, 2, 3, 4] and added[0].seq == 3
    note = w.events[3]
    assert note.data == {"agent_turn": 2, "caller_turn": 1}
    added = w.insert([(-5, "platform", "tool_request", {"tool": "first"}, 1)])
    assert w.events[0].data["tool"] == "first" and w.events[4].data == {"agent_turn": 3, "caller_turn": 2}
    lines = [json.loads(x) for x in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert [x["seq"] for x in lines] == [1, 2, 3, 4, 5]


@pytest.mark.parametrize("events, how", [
    ([{"type": "handoff", "data": {"to": "billing"}}], "platform"),
    ([{"type": "call_ended", "data": {"platform": {"ended_reason": "assistant-forwarded-call"}}}], "platform"),
    ([{"type": "tool_request", "data": {"tool": "transferCall", "call_id": "1", "reported_by": "vapi"}},
      {"type": "tool_response", "data": {"call_id": "1", "ok": True}}], "platform"),
    ([{"type": "tool_request", "data": {"tool": "escalate_to_human", "call_id": "1"}},
      {"type": "tool_response", "data": {"call_id": "1", "ok": True}}], "tool"),
    ([{"type": "agent_turn", "data": {"heard_text": "Sure, I'll transfer you to the front desk now."}}], "speech"),
    ([{"type": "agent_turn", "data": {"heard_text": "Let me connect you with a colleague."}}], "speech"),
])
def test_hand_offs_are_found_with_how_strong_the_proof_is(events, how):
    from vatf.handoff import detect

    found = detect(events)
    assert found["handed_off"] and found["how"] == how


@pytest.mark.parametrize("events", [
    [{"type": "tool_request", "data": {"tool": "transfer_to_human", "call_id": "1"}},
     {"type": "tool_response", "data": {"call_id": "1", "ok": False}}],
    [{"type": "tool_request", "data": {"tool": "book_callback", "call_id": "1"}},
     {"type": "tool_response", "data": {"call_id": "1", "ok": True}}],
    [{"type": "agent_turn", "data": {"heard_text": "I can't transfer you, but I can book a callback."}}],
    [{"type": "call_ended", "data": {"reason": "customer-ended-call"}}],
])
def test_no_hand_off_when_none_happened(events):
    from vatf.handoff import detect

    assert detect(events)["handed_off"] is False


def test_the_handoff_check_takes_expect_and_the_weakest_proof_it_accepts():
    from vatf.checks import evaluate_check
    from vatf.contracts.models import Result, TraceEvent

    said = [TraceEvent(1, 100, "vocto", "agent_turn", {"heard_text": "I'm transferring you to a nurse now."}, 1),
            TraceEvent(2, 200, "vocto", "call_ended", {}, None)]

    def status(check):
        return evaluate_check(check, "c", said, said, {}, "salt", "voice").result

    assert status({"type": "handoff"}) == Result.PASS
    assert status({"type": "handoff", "min_evidence": "tool"}) == Result.FAIL
    assert status({"type": "handoff", "expect": False}) == Result.FAIL
    assert status({"type": "handoff", "to": "a person"}) == Result.PASS


def test_an_elevenlabs_conversation_is_read_with_its_tool_calls():
    from vatf.monitor.intake import from_doc

    doc = {"conversation_id": "conv_1", "status": "done", "metadata": {"call_duration_secs": 42, "termination_reason": "transfer_to_number"},
           "transcript": [{"role": "agent", "message": "Hello", "time_in_call_secs": 0},
                          {"role": "agent", "message": None, "time_in_call_secs": 7,
                           "tool_calls": [{"request_id": "r1", "tool_name": "book", "params_as_json": "{\"slot\": 2}", "type": "webhook"}],
                           "tool_results": [{"request_id": "r1", "tool_name": "book", "result_value": "{\"ok\": true}", "is_error": False}]},
                          {"role": "user", "message": "Thanks", "time_in_call_secs": 9}]}
    call = from_doc(doc)
    tool = next(i for i in call["items"] if i["kind"] == "tool")
    assert tool["args"] == {"slot": 2} and tool["at_ms"] == 7000 and tool["ok"] is True and tool["tool_type"] == "webhook"
    assert call["transferred"] is True and call["duration_ms"] == 42000
    wrapped = from_doc({"type": "post_call_transcription", "data": doc})
    assert wrapped["external_id"] == "conv_1"


def test_platform_bills_are_used_for_costs():
    from vatf import costs

    def v(cost=None, cur="USD"):
        m = {"duration_ms": 60000, "agent_turns": 4, "tool_calls": 1}
        if cost is not None:
            m.update(platform_cost=cost, platform_currency=cur)
        return {"status": "PASS", "metrics": m, "outcome": {}}

    s = costs.summary([v(0.10), v(0.20)], None)
    assert s["source"] == "platform" and abs(s["total"] - 0.30) < 1e-9
    assert costs.summary([v(0.10), v()], None) is None           # one call has no bill: no misleading total
    p = costs.prices({"costs": {"per_minute": 0.05}})
    s = costs.summary([v(0.10), v()], p)
    assert s["source"] == "platform+prices" and abs(s["total"] - 0.15) < 1e-9


def test_mysql_addresses_scripts_and_guards(monkeypatch):
    from vatf import probe

    assert probe.backend_kind({"backend": {"access": "sql", "connection": "mysql://u:p@db:3306/x"}}) == "mysql"
    assert probe.backend_kind({"backend": {"access": "sql", "connection": "mariadb://u@db/x"}}) == "mysql"
    assert "hunter2" not in probe._redacted("mysql://u:hunter2@db:3306/x")
    script = "INSERT INTO t VALUES ('a;b'); -- note; here\nINSERT INTO t VALUES (\"c\");/* x; y */ DELETE FROM t WHERE n = 1;"
    assert probe._statements(script) == ["INSERT INTO t VALUES ('a;b')", "INSERT INTO t VALUES (\"c\")",
                                         "DELETE FROM t WHERE n = 1"]
    monkeypatch.setenv("VOCTO_URL_POLICY", "server")
    with pytest.raises(probe.ProbeSetupError):
        probe._db_host_allowed("mysql://u:p@169.254.169.254:3306/x", 3306)


def test_mysql_snapshot_names_are_quoted():
    from vatf import statewatch

    assert statewatch._quote_mysql("ord`ers") == "`ord``ers`"


def test_moving_a_secret_to_another_address_needs_an_admin():
    from vatf.web.auth import code_settings

    base = {"connection": {"type": "platform", "platform": "vapi", "target": "asst_1",
                           "options": {"api_key": "${VAPI_KEY}"}}}
    other_agent = copy.deepcopy(base)
    other_agent["connection"]["target"] = "asst_2"
    assert code_settings(base) == code_settings(other_agent)          # a new agent id is anyone's change
    lk = {"connection": {"type": "platform", "platform": "livekit", "target": "a",
                         "options": {"url": "wss://a.livekit.cloud", "api_key": "${K}", "api_secret": "${S}"}}}
    moved = copy.deepcopy(lk)
    moved["connection"]["options"]["url"] = "wss://evil.example.com"
    assert code_settings(lk) != code_settings(moved)
    ws = {"connection": {"type": "websocket", "target": "wss://agent.example.com/ws", "options": {"headers": {"Authorization": "${T}"}}}}
    moved = copy.deepcopy(ws)
    moved["connection"]["target"] = "wss://evil.example.com/ws"
    assert code_settings(ws) != code_settings(moved)


def test_the_connection_test_can_make_a_short_vapi_call(monkeypatch):
    from vatf.web.setup import _test_platform

    fake = FakeVapi()
    monkeypatch.setenv("VOCTO_VAPI_API_BASE", fake.base)
    monkeypatch.setenv("VAPI_TEST_KEY", FakeVapi.KEY)
    try:
        prof = platform_profile("vapi", "asst_1", api_key="${VAPI_TEST_KEY}")
        out = asyncio.run(_test_platform(prof, call=True))
        text = json.dumps(out)
        assert out["ok"] is True, out
        assert "Speech heard" in text and "call-" in text and FakeVapi.KEY not in text
    finally:
        fake.close()


def test_a_mysql_probe_reads_in_a_read_only_transaction_over_verified_tls(monkeypatch):
    import pymysql

    from vatf import probe

    seen: dict = {"sql": []}

    class Cur:
        description = [("n",)]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, args=None):
            seen["sql"].append((sql, args))

        def fetchmany(self, n):
            return [{"n": 2}]

    class Conn:
        def cursor(self):
            return Cur()

        def rollback(self):
            seen["rolled_back"] = True

        def close(self):
            seen["closed"] = True

    def connect(**kw):
        seen["kw"] = kw
        return Conn()

    monkeypatch.setattr(pymysql, "connect", connect)
    monkeypatch.setenv("MYSQL_TEST_PW", "p@ss/w?rd#1")
    profile = {"backend": {"access": "sql", "connection": "mysql://reader:${MYSQL_TEST_PW}@127.0.0.1:3307/clinic?ssl=true",
                           "probes": [{"id": "booked", "query": "SELECT COUNT(*) AS n FROM bookings WHERE test_id = :test_id AND note LIKE '5%'"}]}}
    rows = probe.BackendProbe(profile, ROOT)._mysql(profile["backend"]["probes"][0]["query"], {"test_id": "t-1"})
    assert rows == [{"n": 2}]
    assert seen["kw"]["ssl_verify_cert"] is True and seen["kw"]["ssl_verify_identity"] is True
    assert seen["kw"]["port"] == 3307 and seen["kw"]["database"] == "clinic" and seen["kw"]["password"] == "p@ss/w?rd#1" and seen["kw"]["host"] == "127.0.0.1"
    assert seen["sql"][0][0] == "START TRANSACTION READ ONLY"
    assert seen["sql"][1] == ("SELECT COUNT(*) AS n FROM bookings WHERE test_id = %(test_id)s AND note LIKE '5%%'", {"test_id": "t-1"})
    assert seen["rolled_back"] and seen["closed"]
