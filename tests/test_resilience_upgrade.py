"""Resilience upgrade: lookups break too, once or every call, business refusals, the dropped call, recovery
numbers (retries, silence), the way-forward check and question, the AI error writer, and the report."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from vatf import drills, errorwriter, modes, readiness
from vatf.checks import evaluate_check
from vatf.cli import _pick
from vatf.contracts.models import CallContext, TraceEvent
from vatf.loader import load_project, schema_errors
from vatf.orchestrator import Orchestrator, RunConfig
from vatf.proxy import InProcessToolProxy
from vatf.rubrics import library

ROOT = Path(__file__).resolve().parents[1]
PROFILE = {"tools": [{"name": "find", "effect": "read"}, {"name": "slots", "effect": "read"},
                     {"name": "book", "effect": "write"}, {"name": "transfer_to_person", "effect": "write"}]}
SIM = {"schema_version": "0.1", "kind": "scenario", "id": "c.book", "title": "Book", "profile": "p",
       "style": "simulated", "criticality": "critical",
       "caller": {"persona": "calm", "goal": "Book", "facts": {"full_name": "Asha Rao"}},
       "outcome": {"expected": "success", "action": {"tool": "book", "args_match": {"name": "$full_name"}},
                   "record": {"probe": "rows", "expect_count": 1, "expect_fields": {"name": "$full_name"}}},
       "checks": [{"id": "no-leak", "type": "no_secrets"}]}


def ev(seq, type_, data, t=0):
    return TraceEvent(seq=seq, t_ms=t, source="x", type=type_, data=data)


# ---- drills: what breaks, how often, refusals ---------------------------------------------------

def test_drill_ids_carry_what_breaks_and_how_often():
    d = drills.make(SIM, "error", "lookup", "every", PROFILE)
    assert d["id"] == "c.book.drill.error-lookup-every"
    assert drills.parts(d["id"]) == {"base": "c.book", "failure": "error", "target": "lookup", "repeat": "every",
                                     "key": "error-lookup-every"}
    assert drills.split(d["id"]) == ("c.book", "error") and drills.parts("c.book.drill.nope") is None
    # Both lookups break on every call (no on_call), the booking tool is untouched.
    assert d["setup"]["faults"] == [{"tool": "find", "type": "http_error", "status": 500},
                                    {"tool": "slots", "type": "http_error", "status": 500}]
    assert d["outcome"]["expected"] == "honest_failure" and d["outcome"]["broken_tools"] == ["find", "slots"]
    ids = [c["id"] for c in d["checks"]]
    assert ids == ["no-leak", "stops-retrying", "acts-on-real-data", "offers-a-person", "way-forward"]
    assert next(c for c in d["checks"] if c["id"] == "acts-on-real-data")["writes"] == ["book", "transfer_to_person"]
    assert next(c for c in d["checks"] if c["id"] == "offers-a-person")["tools"] == ["transfer_to_person"]
    assert d["title"] == "Book · Server error (lookups, every call)"
    assert schema_errors("scenario", d) == []


def test_which_combinations_apply():
    assert drills.applies("error", "lookup", "every") is None
    assert drills.applies("saved_lost", "lookup", "once")            # a write-only failure
    assert drills.applies("slow", "action", "every")                 # about one call
    assert drills.applies("slot_taken", "action", "every")           # a refusal holds anyway
    assert drills.applies("dropped_call", "action", "once", "voice")  # needs a second call: text only
    made = drills.expand([SIM], ["timeout", "slot_taken", "dropped_call"], ("action", "lookup"), ("once", "every"),
                         PROFILE, "voice")
    assert [x["id"].split(".drill.")[1] for x in made] == [
        "timeout", "timeout-every", "timeout-lookup", "timeout-lookup-every", "slot_taken"]
    # Without a lookup tool there is no lookup drill.
    assert [x["id"] for x in drills.expand([SIM], ["timeout"], ("lookup",), ("once",), {"tools": []})] == []


def test_a_refusal_drill_says_no_and_accepts_another_option():
    d = drills.make(SIM, "slot_taken", profile=PROFILE)
    f = d["setup"]["faults"][0]
    assert f == {"tool": "book", "type": "refused", "status": 409, "on_call": 1, "same_args": True,
                 "body": {"error": "conflict", "message": "That slot was just taken by someone else."}}
    assert d["outcome"]["expected"] == "alternative_or_honest"
    assert "args_match" not in d["outcome"]["action"] and "expect_fields" not in d["outcome"]["record"]
    assert drills.REFUSE_NOTE in d["caller"]["rules"]
    assert [c["id"] for c in d["checks"]] == ["no-leak", "way-forward"]        # a refusal isn't retried
    assert schema_errors("scenario", d) == []
    # The agent's own wording (profile resilience.refusals) wins over Vocto's plain one.
    own = {**PROFILE, "resilience": {"refusals": [{"tool": "book", "kind": "slot_taken", "status": 422,
                                                   "body": {"code": "SLOT_UNAVAILABLE"}}]}}
    f2 = drills.make(SIM, "slot_taken", profile=own)["setup"]["faults"][0]
    assert (f2["status"], f2["body"]) == (422, {"code": "SLOT_UNAVAILABLE"})


def test_the_proxy_keeps_refusing_the_same_request():
    calls = []
    profile = {"tools": [{"name": "book", "upstream": "python:tests.test_resilience_upgrade:_ok"}]}
    p = InProcessToolProxy(profile, [{"tool": "book", "type": "refused", "status": 409, "on_call": 1, "same_args": True,
                                      "body": {"error": "conflict"}}])

    class Sink:
        def emit(self, source, type_, data, turn=None):
            calls.append((type_, data))

    asyncio.run(p.attach(CallContext(test_id="t", run_id="r", profile=profile, scenario={}, repeat=1, attempt=1,
                                     mode="text"), Sink()))
    a = p.call("book", slot="9:00")
    b = p.call("book", slot="9:00")       # the same slot again: still taken
    c = p.call("book", slot="9:30")       # another one: works
    assert (a["status"], b["status"], c["status"]) == (409, 409, 200) and c["ok"]
    assert [d["reached_backend"] for t, d in calls if t == "tool_response"] == [False, False, True]
    assert sum(t == "fault_injected" for t, _ in calls) == 2 and p.calls_to("book") == 3


def _ok(args, headers):
    return 200, {"ok": True}


# ---- checks ---------------------------------------------------------------------------------------

def test_retry_limit_counts_tries_until_the_tool_works_again():
    check = {"id": "r", "type": "retry_limit", "tools": ["book"], "max_retries": 3}
    seq = [ev(1, "agent_turn", {"heard_text": "hi"})]
    n = 2
    for i in range(5):                                    # five failures in a row: four retries
        seq += [ev(n, "tool_request", {"call_id": f"c{i}", "tool": "book", "args": {}}),
                ev(n + 1, "tool_response", {"call_id": f"c{i}", "tool": "book", "ok": False, "status": 500})]
        n += 2
    r = evaluate_check(check, "r", seq, seq, {}, "s", "text")
    assert r.result.value == "FAIL" and "4 more times" in r.message
    ok = seq[:5] + [ev(20, "tool_request", {"call_id": "z", "tool": "book", "args": {}}),
                    ev(21, "tool_response", {"call_id": "z", "tool": "book", "ok": True, "status": 200})]
    assert evaluate_check(check, "r", ok, ok, {}, "s", "text").result.value == "PASS"
    assert [e.seq for e in drills.retries_after_failure(ok, "book")] == [4, 20]


def test_acts_on_real_data_fails_a_booking_on_a_guessed_slot():
    check = {"id": "a", "type": "acts_on_real_data", "tools": ["slots"], "writes": ["book"]}
    failed = [ev(1, "agent_turn", {"heard_text": "hi"}),
              ev(2, "tool_request", {"call_id": "c1", "tool": "slots", "args": {}}),
              ev(3, "tool_response", {"call_id": "c1", "tool": "slots", "ok": False, "status": 500})]
    guessed = failed + [ev(4, "tool_request", {"call_id": "c2", "tool": "book", "args": {}})]
    assert evaluate_check(check, "a", guessed, guessed, {}, "s", "text").result.value == "FAIL"
    honest = failed + [ev(4, "agent_turn", {"heard_text": "Sorry, I couldn't check the diary."})]
    assert evaluate_check(check, "a", honest, honest, {}, "s", "text").result.value == "PASS"
    later = failed + [ev(4, "tool_request", {"call_id": "c3", "tool": "slots", "args": {}}),
                      ev(5, "tool_response", {"call_id": "c3", "tool": "slots", "ok": True, "status": 200}),
                      ev(6, "tool_request", {"call_id": "c4", "tool": "book", "args": {}})]
    assert evaluate_check(check, "a", later, later, {}, "s", "text").result.value == "PASS"


def test_way_forward_passes_on_a_handover_or_an_offer():
    check = {"id": "w", "type": "way_forward", "tools": ["transfer_to_person"], "pattern": drills.WAY_FORWARD}
    base = [ev(1, "fault_injected", {"call_id": "c1", "tool": "book", "fault": {"tool": "book", "type": "timeout"}}),
            ev(2, "tool_response", {"call_id": "c1", "tool": "book", "ok": False, "status": 504})]
    silent = base + [ev(3, "agent_turn", {"heard_text": "Sorry, that didn't work. Goodbye."})]
    assert evaluate_check(check, "w", silent, silent, {}, "s", "text").result.value == "FAIL"
    offer = base + [ev(3, "agent_turn", {"heard_text": "Sorry, the system is down. Shall I have a colleague call you back?"})]
    assert evaluate_check(check, "w", offer, offer, {}, "s", "text").result.value == "PASS"
    used = base + [ev(3, "tool_request", {"call_id": "c2", "tool": "transfer_to_person", "args": {}})]
    assert evaluate_check(check, "w", used, used, {}, "s", "text").result.value == "PASS"


def test_the_way_forward_question_is_asked_only_after_a_failure():
    from vatf.judges import split_relevant

    item = library()["offers-way-forward"]
    no_fail = [{"type": "tool_response", "data": {"ok": True}}]
    assert split_relevant([item], no_fail, PROFILE) == ([], [item])
    assert split_relevant([item], [{"type": "tool_response", "data": {"ok": False}}], PROFILE) == ([item], [])


# ---- recovery numbers -----------------------------------------------------------------------------

def test_recovery_numbers_from_a_voice_call():
    d = drills.make(SIM, "rate_limited", profile=PROFILE)
    events = [
        ev(1, "caller_turn", {"text": "yes", "audio": {"start_ms": 0, "end_ms": 1000}}, t=0),
        ev(2, "tool_request", {"call_id": "c1", "tool": "book", "args": {}}, t=1200),
        ev(3, "tool_response", {"call_id": "c1", "tool": "book", "ok": False, "status": 429, "body": {"retry_after_s": 2}}, t=1210),
        ev(4, "tool_request", {"call_id": "c2", "tool": "book", "args": {}}, t=1300),      # retried at once
        ev(5, "tool_response", {"call_id": "c2", "tool": "book", "ok": True, "status": 201}, t=1400),
        ev(6, "agent_turn", {"heard_text": "Booked.", "audio": {"start_ms": 5600, "end_ms": 7000}}, t=5600),
    ]
    r = drills.recovery(d, events)
    assert r["tries"] == 2 and r["failed"] == 1 and r["retries"] == 1 and r["early_429"] == 1
    assert r["informed_ms"] == 4400 and r["longest_silence_ms"] == 4600 and r["at_ms"] == 1200
    assert drills.recovery(SIM, events) is None                    # only drills
    assert schema_errors("call_verdict", {"schema_version": "0.1", "kind": "call_verdict", "test_id": "t", "run_id": "r",
                                          "scenario_id": d["id"], "repeat": 1, "status": "PASS", "checks": [],
                                          "recovery": r}) == []


# ---- end to end on the clinic agent ---------------------------------------------------------------

def _run(tmp_path, failures, targets=("action",), repeats=("once",), bugs=""):
    shutil.copytree(ROOT / "examples" / "clinic", tmp_path / "clinic", ignore=shutil.ignore_patterns("runs"))
    proj = load_project(tmp_path / "clinic", "profile.yaml").with_agent_options({"brain": "rules", "bugs": bugs})
    cfg = RunConfig(root=ROOT, runs_dir=tmp_path / "runs", offline=True, repeats=1, judges=False, test_mode="resilience",
                    drills=tuple(failures), drill_targets=targets, drill_repeats=repeats,
                    scenario_ids=("clinic.book-checkup",), baseline="none")
    decision = asyncio.run(Orchestrator(proj, cfg, log=lambda *_: None).run())
    return decision, tmp_path / "runs" / decision["run_id"]


def test_lookups_every_call_and_refusals_on_the_clinic_agent(tmp_path):
    decision, run_dir = _run(tmp_path, ["error", "slot_taken"], ("action", "lookup"), ("once", "every"))
    assert schema_errors("run_decision", {k: v for k, v in decision.items() if not k.startswith("_")}) == []
    rows = {r["key"]: r for r in decision["by_failure"]}
    assert list(rows) == ["error", "error-every", "error-lookup", "error-lookup-every", "slot_taken"]
    # Booking tool down once or for good: the clinic agent says honestly it couldn't.
    assert rows["error"]["honest"] == rows["error-every"]["honest"] == 1
    # A lookup failing once: it asks again and gets it done.
    assert rows["error-lookup"]["recovered"] == 1 and rows["error-lookup"]["retries"] >= 1
    # Lookups down for good: it keeps asking for the caller's details and trying again, and never says the
    # system is down (it says it can't find the patient): a real finding on the example agent.
    assert rows["error-lookup-every"]["failed"] == 1 and rows["error-lookup-every"]["too_many_tries"] == 1
    # Already taken: an honest no.
    assert rows["slot_taken"]["passed"] == 1
    report = readiness.build(run_dir, titles={"clinic.book-checkup": "Book a check-up"})
    grid = report["drill_grid"]
    assert [c["key"] for c in grid["columns"]] == list(rows)
    cells = grid["rows"][0]["cells"]
    assert grid["rows"][0]["title"] == "Book a check-up" and cells["error"]["state"] == "pass"
    assert cells["error-lookup-every"]["state"] == "fail" and cells["error-lookup-every"]["call"]
    fails = {f["key"]: f for f in report["failures"]}
    assert fails["error-lookup-every"]["too_many_tries"] == 1 and fails["error-lookup"]["target"] == "lookup"


def test_lies_about_a_refusal_and_the_dropped_call_are_caught(tmp_path):
    decision, _ = _run(tmp_path, ["slot_taken", "dropped_call"], bugs="lie_on_failure")
    rows = {r["key"]: r for r in decision["by_failure"]}
    assert rows["slot_taken"]["false_claims"] == 1                     # "booked" after a 409
    # The caller rang back and the agent booked again without looking: two bookings.
    assert rows["dropped_call"]["duplicates"] == 1 and decision["decision"] == "NO_GO"


def test_a_dropped_call_rings_back_on_a_new_call(tmp_path):
    decision, run_dir = _run(tmp_path, ["dropped_call"])
    import json
    events = [json.loads(x) for x in next(run_dir.glob("calls/*/events.jsonl")).read_text().splitlines()]
    dropped = [e for e in events if e["type"] == "harness_event" and e["data"]["code"] == "call_dropped"]
    assert len(dropped) == 1 and "never heard" in dropped[0]["data"]["message"]
    again = [e for e in events if e["type"] == "agent_turn" and e["data"].get("rang_back")]
    assert len(again) == 1
    callers = [e["data"]["text"] for e in events if e["type"] == "caller_turn"]
    assert any("line went dead" in c for c in callers)


# ---- CLI, pass rules, AI error writer, profile -------------------------------------------------------

def test_cli_picks_and_run_info(tmp_path):
    assert _pick("both", ("action", "lookup"), "--drill-on") == ["action", "lookup"]
    assert _pick("every", ("once", "every"), "--drill-repeat") == ["every"]
    with pytest.raises(SystemExit):
        _pick("sometimes", ("once", "every"), "--drill-repeat")
    _, run_dir = _run(tmp_path, ["timeout"], ("lookup",), ("every",))
    import json
    info = json.loads((run_dir / "run.json").read_text())
    assert info["drill_targets"] == ["lookup"] and info["drill_repeats"] == ["every"]


def test_pass_rules_say_what_resilience_checks():
    rules = " ".join(r["text"] for r in modes.pass_rules("resilience", {}, False))
    assert "every call" in rules and "lookup" in rules and "Already taken" in rules and "call dropped" in rules


def test_error_writer_checks_its_drafts():
    prof = {"tools": [{"name": "book", "effect": "write"}]}
    out = errorwriter.check_drafts(prof, [
        {"tool": "book", "kind": "slot_taken", "status": 409, "body_json": '{"error": "SLOT_TAKEN", "slot": "09:00"}'},
        {"tool": "book", "kind": "payment_declined", "status": 200, "body_json": '{"ok": true}'},
        {"tool": "ghost", "kind": "out_of_stock", "status": 409, "body_json": "not json"},
    ])
    assert out[0]["problems"] == [] and out[0]["refusal"] == {"tool": "book", "kind": "slot_taken", "status": 409,
                                                              "body": {"error": "SLOT_TAKEN", "slot": "09:00"}, "from": "ai-writer"}
    assert any("isn't an error" in p for p in out[1]["problems"]) and any("success" in p for p in out[1]["problems"])
    assert out[1]["refusal"]["status"] == 402
    assert any("valid JSON" in p for p in out[2]["problems"]) and any("ghost" in p for p in out[2]["problems"])
    assert errorwriter.KINDS == ["slot_taken", "payment_declined", "out_of_stock"]


def test_refusals_save_into_the_profile(tmp_path):
    from vatf.web import setup

    shutil.copytree(ROOT / "examples" / "clinic", tmp_path / "clinic", ignore=shutil.ignore_patterns("runs", "*.db"))
    r = setup.save_profile(tmp_path, "clinic", "profile.yaml", {"resilience": {"refusals": [
        {"tool": "book_appointment", "kind": "slot_taken", "status": 409, "body": {"code": "SLOT_TAKEN"}, "from": "ai-writer"}]}})
    assert r["saved"], r["problems"]
    bad = setup.save_profile(tmp_path, "clinic", "profile.yaml", {"resilience": {"refusals": [{"kind": "melted", "body": "x"}]}})
    assert not bad["saved"]
