"""Load upgrade: calls per hour, find the limit, enough samples per stage, the headroom sentence, the
cross-call leak check, the phone-line warning and light load callers.

Fixed numbers for the rules; one end-to-end run against the load reference agent (known slowdown)."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from vatf.decision import apply_load
from vatf.load import LoadSettingsError, peak_from_hour, phone_warning, settings, stage_warnings, stages, summary
from vatf.loader import load_project, schema_errors
from vatf.loadrun import LoadOrchestrator
from vatf.loadstats import cross_call_leaks, headroom, identity_facts, summarise, unique_value
from vatf.orchestrator import RunConfig
from vatf.speech import SpeechConfig
from vatf.util import read_json

ROOT = Path(__file__).resolve().parent.parent


def lv(stage, n, outcome="ok", status="PASS", answers=(), why=""):
    return {"test_id": f"t-{stage}-{n}-{outcome}", "status": status, "checks": [], "metrics": {},
            "load": {"stage": stage, "outcome": outcome, "why": why, "answers": [list(a) for a in answers],
                     "blockers": [], "missed": [], "first_word_ms": 100, "tool_ms": [], "tool_calls": 0,
                     "tool_errors": 0, "answered_turns": len(answers), "unanswered_turns": 0}}


def decision():
    from vatf.decision import LocalDecisionEngine

    return LocalDecisionEngine().decide("r-1", [], [], {"id": "p"})


# ---- settings ------------------------------------------------------------------------------------------

def test_calls_per_hour_sets_calls_at_once_by_littles_law():
    assert peak_from_hour(120, 4) == 8 and peak_from_hour(10, 3) == 1 and peak_from_hour(100, 3) == 5
    s = settings({"load": {"peak_calls_per_hour": 120, "call_minutes": 4}})
    assert s["expected_peak"] == 8 and s["calls_at_once"] == 8
    f = settings({"load": {"shape": "find_limit", "peak_calls_per_hour": 120, "call_minutes": 4}})
    assert f["expected_peak"] == 8 and f["calls_at_once"] == 16        # find the limit tries up to twice the peak
    assert settings({"load": {"peak_calls_per_hour": 120, "call_minutes": 4}}, calls_at_once=3)["calls_at_once"] == 3
    with pytest.raises(LoadSettingsError, match="call length"):
        settings({"load": {"peak_calls_per_hour": 120}})
    with pytest.raises(LoadSettingsError):
        settings({"load": {"call_minutes": -1}})


def test_short_stages_are_warned_before_the_run():
    s = settings({"load": {"calls_at_once": 4, "steps": 4, "step_minutes": 2, "call_minutes": 1}})
    w = stage_warnings(s, stages(s))
    # 1 at once x 2 min / 1 min a call = 2 calls ... 4 at once = 8 calls: all under 10
    assert w and "4 stage(s) will finish fewer than 10 calls" in w[0]
    assert stage_warnings(settings({"load": {"shape": "steady", "calls_at_once": 5, "minutes": 10, "call_minutes": 1}}),
                          stages(settings({"load": {"shape": "steady", "calls_at_once": 5, "minutes": 10}}))) == []
    assert summary(s, stages(s))["expected_calls"] == 20


def test_phone_line_load_warns_about_cost_and_the_providers_limit():
    prof = {"connection": {"type": "phone_call", "options": {"provider": "twilio"}},
            "costs": {"currency": "USD", "per_minute": 0.05, "per_call": 0.01}}
    s = settings({"load": {"shape": "steady", "calls_at_once": 3, "minutes": 10, "call_minutes": 2}})
    w = phone_warning(prof, s, stages(s))
    assert w["calls"] == 15 and w["at_once"] == 3 and w["cost"] == round(15 * (0.01 + 0.05 * 2), 2)
    assert "Twilio" in w["text"] and "at least 3" in w["text"]
    assert phone_warning({"connection": {"type": "websocket_audio"}}, s, stages(s)) is None
    unknown = phone_warning({"connection": {"type": "phone_call"}}, settings({}), stages(settings({})))
    assert unknown["calls"] == 200 and not unknown["calls_known"] and unknown["cost"] is None


# ---- stages: enough samples ---------------------------------------------------------------------------------

def test_a_stage_needs_ten_calls_and_twenty_answers():
    s = settings({"load": {"calls_at_once": 2, "steps": 2, "step_minutes": 1, "limits": {"p95_ms": 1000}}})
    st = stages(s)
    samples = [(t / 10, 1.0, 1 if t < 600 else 2) for t in range(1200)]
    low = [lv(0, i, answers=[(1 + i, 400)]) for i in range(4)]                        # 4 calls: too few
    top = [lv(1, i, answers=[(61 + i, 400), (61.5 + i, 400)]) for i in range(12)]       # 12 calls, 24 answers
    out = summarise(s, st, low + top, samples, 1000, {}, "voice")
    r0, r1 = out["stages"]
    assert r0["too_few"] and not r0["trusted"] and "too few to judge" in r0["untrusted_why"]
    assert r1["trusted"] and r1["meets_limits"] and out["capacity"] == 2
    d = apply_load(decision(), out)
    assert d["decision"] == "GO" and any(r["rule"] == "load_too_few" for r in d["reasons"])
    assert schema_errors("run_decision", d) == []

    # The top level with too few calls: not enough evidence.
    out = summarise(s, st, low + top[:5], samples, 1000, {}, "voice")
    assert apply_load(decision(), out)["decision"] == "NO_GO_INSUFFICIENT_EVIDENCE"

    # 12 calls but a slow p95 over only 12 answers decides nothing; over 24 answers it breaks the limit.
    slow = [lv(1, i, answers=[(61 + i, 1500)]) for i in range(12)]
    row = summarise(s, st, low + slow, samples, 1000, {}, "voice")["stages"][1]
    assert row["p95_enough"] is False and row["meets_limits"] is True
    slow2 = [lv(1, i, answers=[(61 + i, 1500), (61.5 + i, 1500)]) for i in range(12)]
    row = summarise(s, st, low + slow2, samples, 1000, {}, "voice")["stages"][1]
    assert row["meets_limits"] is False and row["problems"][0]["rule"] == "latency"


def test_headroom_sentence():
    assert headroom(12, {"level": 13}, 13, 8)["text"] == (
        "Handles 12 calls at once within your limits (it broke a limit at 13). Your peak is 8, so you have 50% headroom.")
    h = headroom(5, None, 5, 5)
    assert h["text"].startswith("Handles at least 5 calls at once") and "at least 0% headroom" in h["text"]
    assert headroom(3, {"level": 4}, 4, 8)["text"].endswith("so it is 5 calls at once short.")
    assert headroom(0, {"level": 1}, 1, None)["text"].startswith("It broke a limit at 1 call at once")


# ---- cross-call leaks ------------------------------------------------------------------------------------

def test_identity_facts_and_unique_values():
    facts = {"full_name": "Maria Lopez", "date_of_birth": "1985-03-14", "doctor": "Dr Patel", "phone": "07700 900123",
             "preferred_time": "morning", "booking_ref": "AB12", "pet_name": "Rex"}
    assert identity_facts(facts) == {"full_name": "Maria Lopez", "date_of_birth": "1985-03-14",
                                     "phone": "07700 900123", "booking_ref": "AB12"}
    names = {unique_value("full_name", "Maria Lopez", n) for n in range(300)}
    assert len(names) == 300 and unique_value("phone", "x", 7) == "07700 900007"
    assert unique_value("email", "x", 3) == "caller3@example.com" and unique_value("member_id", "M1", 4) == "M1-4"


def call(tid, sid, own, said=(), tools=(), context=""):
    return {"test_id": tid, "scenario_id": sid, "own": own, "context": context.lower(),
            "said": list(enumerate(said, 1)), "tools": list(enumerate(tools, 1))}


def test_a_callers_details_in_another_call_is_a_leak():
    a = call("t-a", "book", {"full_name": "Maria Lopez"}, said=["Thanks Maria, you're booked"])
    b = call("t-b", "cancel", {"full_name": "Tom Baker", "date_of_birth": "1990-05-04"},
             said=["I've found the booking for Maria Lopez on Tuesday"])
    c = call("t-c", "move", {"full_name": "Ana Silva"}, tools=['{"full_name": "Tom Baker", "dob": "1990-05-04"}'])
    d = call("t-d", "asks-for-person", {"full_name": "Ana Silva"}, said=["I can't share Maria Lopez's details"],
             context="caller asks about Maria Lopez")       # named in its own test case: not a leak
    leaks = cross_call_leaks([a, b, c, d])
    got = {(l["in_test"], l["from_test"], l["fact"], l["where"]) for l in leaks}
    assert got == {("t-b", "t-a", "full_name", "said"), ("t-c", "t-b", "full_name", "tool"),
                   ("t-c", "t-b", "date_of_birth", "tool")}
    assert "Maria" not in leaks[0]["text"] and "Maria" in cross_call_leaks([a, b], show=True)[0]["text"]

    s = settings({"load": {"shape": "steady", "calls_at_once": 1, "minutes": 1, "limits": {"min_calls": 1, "min_answers": 1}}})
    out = summarise(s, stages(s), [lv(0, 1, answers=[(1, 300)])], [(0.5, 1.0, 1), (1.0, 1.0, 1)], None, {}, "text",
                    leaks=leaks)
    d = apply_load(decision(), out)
    leak = next(r for r in d["reasons"] if r["rule"] == "load_leak")
    assert d["decision"] == "NO_GO" and leak["refs"] == ["t-b", "t-c"] and len(d["load"]["leaks"]) == 3
    assert schema_errors("run_decision", d) == []


# ---- light callers --------------------------------------------------------------------------------------

def test_light_callers_synthesise_each_line_once():
    from vatf.voice_call import CachedTTS

    class Count:
        n = 0

        def synthesize(self, text, voice="", speaking_rate=1.0):
            Count.n += 1
            return (text, voice)

    cache: dict = {}
    a, b = CachedTTS(Count(), cache), CachedTTS(Count(), cache)
    assert a.synthesize("hello", "v") == b.synthesize("hello", "v") and Count.n == 1
    b.synthesize("hello", "other")
    assert Count.n == 2


# ---- find the limit, end to end ------------------------------------------------------------------------------

def test_find_the_limit_stops_at_the_breaking_point(tmp_path):
    """The reference agent answers in 300 ms + 250 ms per extra call: p95 limit 1000 ms holds up to 3 at once.
    Find the limit steps 1..6 and must stop after 4 (it never tries 5 or 6); with a peak of 2, that is GO
    with 50% headroom."""
    from zoo.load_ref import serve

    ready = threading.Event()
    box = {}

    def server():
        loop = asyncio.new_event_loop()
        agent, srv = loop.run_until_complete(serve(0, base_ms=300, per_call_ms=250, max_calls=10))
        box.update(agent=agent, port=srv.sockets[0].getsockname()[1], loop=loop, srv=srv)
        ready.set()
        loop.run_forever()

    threading.Thread(target=server, daemon=True).start()
    assert ready.wait(10)
    try:
        proj = load_project(ROOT / "examples" / "load-ref")
        proj.profile["connection"]["target"] = f"ws://127.0.0.1:{box['port']}"
        proj.profile["load"] = {"shape": "find_limit", "start_at": 1, "calls_at_once": 6, "steps": 6,
                                "step_minutes": 0.25, "expected_peak": 2,
                                "limits": {"min_calls": 2, "min_answers": 2}}
        s = settings(proj.profile)
        cfg = RunConfig(root=ROOT, runs_dir=tmp_path / "runs", offline=True, speech=SpeechConfig(engine="codec"),
                        baseline="none")
        orch = LoadOrchestrator(proj, cfg, s, log=lambda *_: None)
        assert orch.light
        d = asyncio.run(orch.run())
    finally:
        box["loop"].call_soon_threadsafe(box["loop"].stop)
    load = read_json(tmp_path / "runs" / d["run_id"] / "load.json")
    assert [r["level"] for r in load["stages"]] == [1, 2, 3, 4]        # stopped: 5 and 6 never tried
    assert box["agent"].peak <= 4
    assert load["capacity"] == 3 and load["breaking"]["level"] == 4 and load["headroom"]["exact"]
    assert load["headroom"]["text"].endswith("Your peak is 2, so you have 50% headroom.")
    assert d["decision"] == "GO" and d["reasons"][0]["rule"] == "load_ok" and "Handles 3 calls" in d["reasons"][0]["message"]
    assert any("Find the limit stopped after Step 4 of 6" in n for n in load["notes"])
    saved = read_json(tmp_path / "runs" / d["run_id"] / "decision.json")
    assert load["light"] and load["leaks"] == [] and schema_errors("run_decision", saved) == []
