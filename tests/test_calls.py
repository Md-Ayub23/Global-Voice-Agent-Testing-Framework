"""Outbound calls, voicemail and returning callers (calls.py, orchestrator._converse), on the clinic agent
offline (text mode)."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import yaml

from vatf import calls
from vatf.checks import evaluate_check
from vatf.connectors import call_metadata
from vatf.contracts.models import Result, TraceEvent
from vatf.loader import load_project, schema_errors
from vatf.orchestrator import Orchestrator, RunConfig
from vatf.rubrics import scenario_items
from vatf.story import story_rows

ROOT = Path(__file__).resolve().parents[1]
BASE = {"schema_version": "0.1", "kind": "scenario", "profile": "clinic-booking", "criticality": "critical",
        "setup": {"now": "2026-11-02T09:00:00+00:00", "fixtures": ["zoo/clinic/fixtures/base.sql"]},
        "caller": {"facts": {"full_name": "Maria Lopez", "date_of_birth": "1985-03-14"}}}
BOOK = [
    {"caller": "Hi, I'm Maria Lopez, born 14 March 1985. I need a check-up with Dr Patel tomorrow, "
               "Tuesday 3 November, in the morning."},
    {"caller": "Nine thirty works."},
    {"caller": "Yes please.", "expect": [{"type": "said", "pattern": "(?i)booked"}]},
]


def _ev(seq, type_, data, turn=1, t_ms=0):
    return TraceEvent(seq=seq, t_ms=t_ms, source="x", type=type_, data=data, turn=turn)


# ---- units ------------------------------------------------------------------------------------------

def test_call_metadata_tells_the_agent_it_is_outbound_and_who_it_calls():
    sc = {"direction": "outbound", "outbound": {"to": "$phone", "reason": "reminder", "for": ["$full_name"]},
          "caller_id": "$phone", "caller": {"facts": {"phone": "+15550001111", "full_name": "Maria"}}}
    meta = call_metadata(SimpleNamespace(scenario=sc))
    assert meta == {"direction": "outbound", "outbound": {"to": "+15550001111", "reason": "reminder", "for": ["Maria"]},
                    "from": "+15550001111"}
    assert call_metadata(SimpleNamespace(scenario={"caller": {}})) == {} and call_metadata(None) == {}


def test_legs_remember_and_fill():
    sc = {**BASE, "id": "s", "title": "Book", "style": "scripted", "turns": BOOK, "direction": "outbound",
          "remember": {"ref": r"reference is (\d{6})", "bad": "("},
          "returning": [{"turns": [{"caller": "About booking {ref}, please."}], "gap_s": 0},
                        {"goal": "Cancel booking $ref", "max_turns": 4}]}
    legs = calls.legs(sc)
    assert [l.get("call_number") for l in legs] == [None, 2, 3]
    assert legs[1]["style"] == "scripted" and "direction" not in legs[1] and legs[1]["follow_up"] is False
    assert legs[2]["style"] == "simulated" and legs[2]["caller"]["max_turns"] == 4
    assert "You called this agent before" in legs[2]["caller"]["rules"][-1]
    assert calls.gap_s(sc, 2) == 0 and calls.gap_s(sc, 3) == calls.DEFAULT_GAP_S
    events = [_ev(1, "agent_turn", {"heard_text": "Your booking reference is 123456."}),
              _ev(2, "tool_response", {"body": {"reference": "999999"}}),
              _ev(3, "agent_turn", {"heard_text": "Your booking reference is 654321."})]
    got = calls.remember(sc, events)
    assert got == {"ref": "654321"}                          # the latest wins; a bad pattern is skipped
    two, three = calls.with_remembered(legs[1], got), calls.with_remembered(legs[2], got)
    assert two["turns"][0]["caller"] == "About booking 654321, please."
    assert three["caller"]["goal"] == "Cancel booking 654321" and three["caller"]["facts"]["ref"] == "654321"
    assert calls.with_remembered(legs[1], {})["turns"][0]["caller"] == "About booking {ref}, please."


def test_beep_is_a_clean_tone():
    b = calls.beep(16000)
    assert b.dtype == np.float32 and len(b) == 7200 and abs(b[0]) < 1e-3 and abs(b[-1]) < 1e-2
    assert 0.3 < float(np.max(np.abs(b))) <= 0.35


def test_voicemail_check():
    greet = _ev(1, "caller_turn", {"text": "leave a message [beep]", "voicemail": True, "beep_end_ms": 5000})
    check = {"type": "voicemail", "must_say": [r"\b555\b"], "never_say": [r"\b\d{6}\b"], "max_seconds": 20}

    def run(text, start=5200, end=12000, mode="voice", c=check):
        events = [greet] + ([_ev(2, "agent_turn", {"heard_text": text, "audio": {"start_ms": start, "end_ms": end}})]
                            if text is not None else [])
        return evaluate_check(c, "vm", events, events, {}, "salt", mode)

    assert run("Hi, this is the clinic. Please call us back on 555 0100.").result == Result.PASS
    r = run("Hi, this is the clinic, call 555 0100.", start=3000)
    assert r.result == Result.FAIL and "before the beep" in r.message
    assert run("Call 555. Your reference is 123456.").result == Result.FAIL
    assert "never said" in run("Hi, this is the clinic.").message
    assert "ran 25 s" in run("Call 555 0100.", end=30200).message
    assert run(None).result == Result.FAIL                              # no message at all
    assert run(None, c={"type": "voicemail", "message": False}).result == Result.PASS
    assert run("hello", c={"type": "voicemail", "message": False}).result == Result.FAIL
    assert run("Call 555.", start=0, mode="text").result == Result.PASS  # no timing in text
    plain = [_ev(1, "caller_turn", {"text": "Hello?"})]
    assert evaluate_check(check, "vm", plain, plain, {}, "s", "text").result == Result.NOT_APPLICABLE


def test_call_kind_judge_items():
    ids = lambda sc: [i.id for i, _ in scenario_items({}, sc)]
    assert ids({"direction": "outbound"}) == ["outbound-opening"]
    assert ids({"direction": "outbound", "answered_by": "voicemail"}) == ["voicemail-message"]
    assert ids({"returning": [{"goal": "x"}]}) == ["returning-caller"]
    assert ids({}) == []


# ---- end to end, offline ----------------------------------------------------------------------------

def _run(tmp_path, scenario):
    shutil.copytree(ROOT / "examples" / "clinic", tmp_path / "clinic", ignore=shutil.ignore_patterns("runs"))
    assert schema_errors("scenario", scenario) == []
    (tmp_path / "clinic" / "scenarios" / "extra.yaml").write_text(yaml.safe_dump(scenario), "utf-8")
    proj = load_project(tmp_path / "clinic", "profile.yaml").with_agent_options({"brain": "rules"})
    cfg = RunConfig(root=ROOT, runs_dir=tmp_path / "runs", offline=True, repeats=1, judges=False,
                    scenario_ids=(scenario["id"],), baseline="none")
    decision = asyncio.run(Orchestrator(proj, cfg, log=lambda *_: None).run())
    run_dir = tmp_path / "runs" / decision["run_id"]
    call_dir = next(p for p in (run_dir / "calls").iterdir() if p.is_dir())
    events = [json.loads(l) for l in (call_dir / "events.jsonl").read_text("utf-8").splitlines() if l.strip()]
    verdict = json.loads((call_dir / "verdict.json").read_text("utf-8"))
    return events, verdict


def _check(result, check_id):
    return next(c for c in result["checks"] if c["check_id"] == check_id)


def test_outbound_call_answered_by_a_person(tmp_path):
    sc = {**BASE, "id": "clinic.outbound-person", "title": "Reminder call", "style": "scripted",
          "direction": "outbound", "outbound": {"reason": "appointment reminder"}, "turns": BOOK,
          "checks": [{"id": "booked", "type": "said", "pattern": "(?i)booked"}]}
    events, result = _run(tmp_path, sc)
    turns = [(e["type"], e["turn"], e["data"]) for e in events if e["type"] in ("caller_turn", "agent_turn")]
    # The clinic agent talks first, as inbound agents do: kept, and marked as said before the pickup.
    assert turns[0][0] == "agent_turn" and turns[0][2].get("before_pickup")
    assert turns[1][0] == "caller_turn" and turns[1][2]["text"] == "Hello?" and turns[1][2]["pickup"]
    assert turns[1][1] == 1 and turns[2] [:2] == ("agent_turn", 1)
    assert _check(result, "booked")["result"] == "PASS" and _check(result, "turn3.1")["result"] == "PASS"
    rows = story_rows(events)
    assert any("said before anyone picked up" in r[2] for r in rows)


def test_outbound_call_answered_by_voicemail(tmp_path):
    sc = {**BASE, "id": "clinic.outbound-voicemail", "title": "Reminder voicemail", "style": "simulated",
          "direction": "outbound", "answered_by": "voicemail",
          "caller": {**BASE["caller"], "persona": "calm-adult", "goal": "none"},
          "checks": [{"id": "vm", "type": "voicemail", "must_say": ["(?i)clinic"]}]}
    events, result = _run(tmp_path, sc)
    callers = [e for e in events if e["type"] == "caller_turn"]
    assert len(callers) == 1 and callers[0]["data"]["voicemail"] and callers[0]["data"]["text"].endswith("[beep]")
    msgs = [e for e in events if e["type"] == "agent_turn" and e["data"].get("voicemail_message")]
    assert len(msgs) == 1
    ended = next(e for e in events if e["type"] == "call_ended")
    assert "voicemail" in ended["data"]["reason"] or "message" in ended["data"]["reason"]
    assert _check(result, "vm")["result"] in ("PASS", "FAIL")   # judged on what the clinic agent said


def test_returning_caller_chain(tmp_path):
    sc = {**BASE, "id": "clinic.returning", "title": "Book, then ring back", "style": "scripted", "turns": BOOK,
          "remember": {"ref": r"reference is (\d{6})"},
          "returning": [{"gap_s": 0, "turns": [{"caller": "Hi, I'm calling about booking {ref}.",
                                                "expect": [{"type": "max_turns", "n": 5}]}]}],
          "checks": [{"id": "booked-on-call-1", "type": "said", "pattern": "(?i)booked", "call": 1},
                     {"id": "not-booked-on-call-2", "type": "tool_not_called", "tool": "book_appointment", "call": 2}]}
    events, result = _run(tmp_path, sc)
    nxt = [e for e in events if e["type"] == "harness_event" and e["data"]["code"] == "next_call"]
    assert len(nxt) == 1 and nxt[0]["data"]["remembered"] == ["ref"] and nxt[0]["data"]["first_turn"] == 5
    ref = next(e["data"]["heard_text"] for e in events if e["type"] == "agent_turn"
               and "reference is" in e["data"].get("heard_text", "")).split("reference is ")[1][:6]
    line = [e for e in events if e["type"] == "caller_turn"][-1]
    assert line["data"]["text"] == f"Hi, I'm calling about booking {ref}." and line["turn"] == 6
    greet2 = [e for e in events if e["type"] == "agent_turn" and e["turn"] == 5]
    assert greet2, "the agent answers the second call with a greeting of its own"
    assert _check(result, "booked-on-call-1")["result"] == "PASS"
    assert _check(result, "not-booked-on-call-2")["result"] == "PASS"
    assert _check(result, "turn3.1")["result"] == "PASS" and _check(result, "call2.turn1.1")["result"] == "PASS"
    assert sum(1 for e in events if e["type"] == "call_ended") == 1      # one test, one chain


def test_scripted_voicemail_needs_no_lines(tmp_path):
    sc = {**BASE, "id": "clinic.vm-scripted", "title": "Reminder voicemail", "style": "scripted",
          "direction": "outbound", "answered_by": "voicemail", "checks": [{"id": "vm", "type": "voicemail"}]}
    assert schema_errors("scenario", {**sc, "answered_by": "person"})          # a person call still needs lines
    events, result = _run(tmp_path, sc)
    assert [e for e in events if e["type"] == "agent_turn" and e["data"].get("voicemail_message")]
    assert _check(result, "vm")["result"] == "PASS"
