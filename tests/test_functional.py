"""Functional test type upgrade: nothing else changed, wrong tools, the AI claim reader, tasks with
several steps, expected answers, the coverage map, caller variations and the Functional report numbers."""

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from vatf import claimread, coverage, readiness, statewatch, variations
from vatf.contracts.models import Result, TraceEvent
from vatf.llm import ScriptedLLM
from vatf.loader import load_project, schema_errors
from vatf.outcome import claim_question, evaluate_more, evaluate_outcome
from vatf.rubrics import EXPECTED_ANSWER, items_for_call

from test_phase1 import CLINIC, project, run, verdicts_of

HAPPY = "clinic.book-checkup"


def ev(seq, type_, data, source="connector"):
    return TraceEvent(seq, seq * 100, source, type_, data)


# ---- nothing else changed ------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    path = tmp_path / "shop.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE orders (id INTEGER PRIMARY KEY, customer TEXT, item TEXT, status TEXT, updated_at TEXT);
        INSERT INTO orders VALUES (1, 'ann', 'tea', 'open', 't0'), (2, 'bob', 'cake', 'open', 't0');
        CREATE TABLE audit_log (id INTEGER PRIMARY KEY, line TEXT);
    """)
    conn.commit()
    conn.close()
    profile = {"backend": {"access": "sql", "connection": f"sqlite:///{path}", "probes": [
        {"id": "orders_for_test", "query": "SELECT id, item FROM orders WHERE customer = :name"}]},
        "tools": [{"name": "place_order", "effect": "write"}, {"name": "cancel_order", "effect": "write"},
                  {"name": "find_order", "effect": "read"}]}
    return path, profile


def change(path, sql):
    conn = sqlite3.connect(path)
    conn.executescript(sql)
    conn.commit()
    conn.close()


SCEN = {"outcome": {"expected": "success", "action": {"tool": "place_order"},
                    "record": {"probe": "orders_for_test"}}}


def watched_events(profile, before, after, probe_rows=(), tool_calls=()):
    diff = statewatch.diff(before, after, statewatch.ignored_columns(profile))
    events = []
    for i, (tool, args, ok) in enumerate(tool_calls):
        events += [ev(10 + 2 * i, "tool_request", {"tool": tool, "args": args, "call_id": f"c{i}"}, "tool_proxy"),
                   ev(11 + 2 * i, "tool_response", {"tool": tool, "call_id": f"c{i}", "ok": ok}, "tool_proxy")]
    events.append(ev(50, "probe_result", {"probe": "orders_for_test", "ok": True, "rows": list(probe_rows)}, "probe"))
    events.append(ev(51, "state_diff", {"watched": list(before), **diff}, "probe"))
    events.append(ev(52, "call_ended", {"ended_by": "caller", "reason": "done"}))
    return events


def test_tables_watched_by_default_are_the_ones_the_probes_read(db):
    _, profile = db
    assert statewatch.tables_of(profile) == ["orders"]
    assert statewatch.tables_of({**profile, "backend": {**profile["backend"], "watch": False}}) == []
    assert statewatch.tables_of({**profile, "backend": {**profile["backend"], "watch": ["orders", "audit_log"],
                                                        "watch_ignore": ["audit_log"]}}) == ["orders"]
    assert statewatch.tables_of({"backend": {"access": "rest", "probes": [{"id": "x", "query": "GET /a"}]}}) == []


def test_the_asked_for_record_passes_and_another_change_fails(db):
    path, profile = db
    before = statewatch.snapshot(profile, Path("/"), ["orders"])
    change(path, "INSERT INTO orders VALUES (3, 'cat', 'pie', 'open', 't1');")
    after = statewatch.snapshot(profile, Path("/"), ["orders"])
    events = watched_events(profile, before, after, [{"id": 3, "item": "pie"}], [("place_order", {"item": "pie"}, True)])
    ok = statewatch.side_effects_check(SCEN, profile, events)
    assert ok.result == Result.PASS and "1 expected change" in ok.message

    change(path, "UPDATE orders SET status = 'cancelled' WHERE id = 2;")
    worse = statewatch.snapshot(profile, Path("/"), ["orders"])
    events = watched_events(profile, before, worse, [{"id": 3, "item": "pie"}], [("place_order", {"item": "pie"}, True)])
    bad = statewatch.side_effects_check(SCEN, profile, events)
    assert bad.result == Result.FAIL and bad.severity == "blocker"
    assert "orders row 2 changed (status)" in bad.message and "row 3" not in bad.message


def test_counting_probe_still_explains_the_expected_new_row(db):
    path, profile = db
    before = statewatch.snapshot(profile, Path("/"), ["orders"])
    change(path, "INSERT INTO orders VALUES (3, 'cat', 'pie', 'open', 't1'); INSERT INTO orders VALUES (4, 'cat', 'pie', 'open', 't1');")
    after = statewatch.snapshot(profile, Path("/"), ["orders"])
    events = watched_events(profile, before, after, [], [("place_order", {"item": "pie"}, True)])
    res = statewatch.side_effects_check(SCEN, profile, events)
    assert res.result == Result.FAIL and "row 4 was added" in res.message   # one row expected, the second is extra


def test_a_cancel_of_the_named_row_is_expected_and_ignored_columns_never_count(db):
    path, profile = db
    profile = {**profile, "backend": {**profile["backend"], "watch_ignore": ["orders.updated_at"]}}
    scen = {"outcome": {"expected": "success", "action": {"tool": "cancel_order"}}}
    before = statewatch.snapshot(profile, Path("/"), ["orders"])
    change(path, "UPDATE orders SET status = 'cancelled' WHERE id = 1; UPDATE orders SET updated_at = 't9' WHERE id = 2;")
    after = statewatch.snapshot(profile, Path("/"), ["orders"])
    events = watched_events(profile, before, after, [], [("cancel_order", {"order_id": 1}, True)])
    res = statewatch.side_effects_check(scen, profile, events)
    assert res.result == Result.PASS, res.message
    allowed = statewatch.side_effects_check({**SCEN, "allow_changes": ["orders"]}, profile,
                                            watched_events(profile, before, after))
    assert allowed.result == Result.PASS


def test_a_snapshot_that_failed_is_not_verified(db):
    _, profile = db
    events = [ev(1, "state_diff", {"watched": ["orders"], "error": "SQLite: no such table"}, "probe")]
    res = statewatch.side_effects_check(SCEN, profile, events)
    assert res.result == Result.NOT_VERIFIED and "no such table" in res.message


def test_wrong_tool_is_a_blocker_only_when_it_went_through(db):
    _, profile = db
    calls = lambda ok: [ev(1, "tool_request", {"tool": "cancel_order", "args": {}, "call_id": "a"}, "tool_proxy"),
                        ev(2, "tool_response", {"tool": "cancel_order", "call_id": "a", "ok": ok}, "tool_proxy"),
                        ev(3, "call_ended", {"ended_by": "caller", "reason": "x"})]
    went = statewatch.wrong_tool_check(SCEN, profile, calls(True))
    tried = statewatch.wrong_tool_check(SCEN, profile, calls(False))
    assert went.result == tried.result == Result.FAIL
    assert went.severity == "blocker" and tried.severity == "major"
    # Allowed: then every write tool is expected, so there is nothing left to check.
    assert statewatch.wrong_tool_check({**SCEN, "allow_tools": ["cancel_order"]}, profile, calls(True)) is None
    assert statewatch.wrong_tool_check({}, profile, calls(True)) is None          # no outcome: not checked


def test_clinic_clean_run_passes_and_the_cancel_other_bug_is_caught(tmp_path):
    orch, decision = run(tmp_path, scenario_ids=(HAPPY,), repeats=1)
    v = verdicts_of(orch, decision)[0]
    checks = {c["check_id"]: c for c in v["checks"]}
    assert decision["decision"] == "GO"
    assert checks["nothing-else-changed"]["result"] == "PASS" and checks["only-expected-writes"]["result"] == "PASS"
    orch, decision = run(tmp_path / "bug", project(bugs="cancel_other"), scenario_ids=(HAPPY,), repeats=1)
    v = verdicts_of(orch, decision)[0]
    checks = {c["check_id"]: c for c in v["checks"]}
    assert decision["decision"] == "NO_GO" and v["outcome"]["class"] == "TRUE_SUCCESS"   # the outcome alone can't see it
    assert checks["nothing-else-changed"]["result"] == "FAIL" and "appointments row 1" in checks["nothing-else-changed"]["message"]
    assert checks["only-expected-writes"]["result"] == "FAIL"
    report = readiness.build(orch.store.runs_dir / decision["run_id"])
    titles = {f["title"] for f in report["findings"]}
    assert "Changed something else in your system" in titles and report["functional"]["other_changes"]["calls"] == 1


# ---- the AI claim reader --------------------------------------------------------------------------

UNCLEAR = {"outcome": {"expected": "success", "action": {"tool": "place_order"}, "record": {"probe": "orders_for_test"},
                       "claim": {"success_patterns": ["\\bbooked\\b"], "failure_patterns": ["couldn't"]}}}


def unclear_call(reply="Lovely, that's all sorted for Friday then."):
    return [ev(1, "tool_request", {"tool": "place_order", "args": {}, "call_id": "a"}, "tool_proxy"),
            ev(2, "tool_response", {"tool": "place_order", "call_id": "a", "ok": True, "status": 201}, "tool_proxy"),
            ev(3, "agent_turn", {"heard_text": reply, "text": reply}),
            ev(4, "probe_result", {"probe": "orders_for_test", "ok": True, "rows": [{"id": 3}]}, "probe")]


def reader(*answers):
    replies = list(answers)
    return ScriptedLLM(lambda req: json.dumps(replies.pop(0)))


def test_two_models_that_agree_decide_an_unclear_claim():
    events = unclear_call()
    qs = claim_question(UNCLEAR, events)
    assert [q["check_id"] for q in qs] == ["outcome"] and qs[0]["turns"] == [(3, "Lovely, that's all sorted for Friday then.")]
    llm = reader({"said": "success", "reply": 3, "quote": "all sorted for Friday"},
                 {"said": "success", "reply": 3, "quote": "that's all sorted"})
    readings = asyncio.run(claimread.read_claims(llm, qs, ["a", "b"]))
    assert readings["outcome"]["agreed"] and readings["outcome"]["said"] == "success"
    block, check = evaluate_outcome(UNCLEAR, {"backend": {"access": "sql"}}, events, {}, "s", readings)
    assert check.result == Result.PASS and block["said_heard_by"] == "ai" and "read by AI" in check.message
    assert schema_errors("call_verdict", {"schema_version": "0.1", "kind": "call_verdict", "test_id": "t", "run_id": "r",
                                          "scenario_id": "s", "repeat": 1, "status": "PASS", "checks": [],
                                          "outcome": block}) == []


def test_disagreeing_or_made_up_readings_stay_not_verified():
    events = unclear_call()
    qs = claim_question(UNCLEAR, events)
    split = asyncio.run(claimread.read_claims(reader({"said": "success", "reply": 3, "quote": "all sorted"},
                                                     {"said": "unclear", "reply": 3, "quote": "sorted"}), qs, ["a", "b"]))
    _, check = evaluate_outcome(UNCLEAR, {"backend": {"access": "sql"}}, events, {}, "s", split)
    assert check.result == Result.NOT_VERIFIED and "did not agree" in check.message
    invented = asyncio.run(claimread.read_claims(reader({"said": "success", "reply": 3, "quote": "your booking is confirmed"},
                                                        {"said": "success", "reply": 3, "quote": "all sorted"}), qs, ["a", "b"]))
    assert not invented["outcome"]["agreed"]          # a quote that isn't in the call never counts
    assert claim_question(UNCLEAR, unclear_call("You're booked in for Friday.")) == []   # clear: no AI asked


# ---- tasks with several steps, expected answers --------------------------------------------------

def test_each_further_step_is_its_own_outcome_check():
    scen = {"outcome": {"expected": "success", "action": {"tool": "place_order"},
                        "claim": {"success_patterns": ["placed"], "failure_patterns": ["couldn't"]}},
            "more_outcomes": [{"expected": "success", "action": {"tool": "add_note"},
                               "claim": {"success_patterns": ["note added"], "failure_patterns": ["couldn't"]}}]}
    events = [ev(1, "tool_request", {"tool": "place_order", "args": {}, "call_id": "a"}, "tool_proxy"),
              ev(2, "tool_response", {"tool": "place_order", "call_id": "a", "ok": True}, "tool_proxy"),
              ev(3, "agent_turn", {"heard_text": "Your order is placed."}),
              ev(4, "agent_turn", {"heard_text": "Anything else?"})]
    more = evaluate_more(scen, {"backend": {"access": "none"}}, events, {}, "s")
    assert len(more) == 1
    block, check = more[0]
    assert check.check_id == "outcome-2" and block["tool"] == "add_note" and block["did"]["write_calls"] == 0
    assert check.message.startswith("add_note: ")


def test_expected_answer_becomes_a_judge_question():
    proj = load_project(CLINIC)
    sc = {**proj.scenario(HAPPY), "expected_answer": "The front desk is open 8am to 6pm on weekdays."}
    items = {i.id: i for i in items_for_call(proj.profile, sc)}
    item = items[EXPECTED_ANSWER]
    assert "8am to 6pm on weekdays" in item.question and item.section == "accuracy"
    assert schema_errors("scenario", {k: v for k, v in sc.items() if not k.startswith("_")}) == []


# ---- coverage map and caller variations -----------------------------------------------------------

def write_plan(folder: Path, tests):
    d = folder / "plans"
    d.mkdir(parents=True, exist_ok=True)
    plan = {"test_type": "functional", "created_at": "2026-11-12T10:00:00+00:00",
            "analysis": {"intents": [{"id": "book_appointment", "description": "", "tools": []},
                                     {"id": "cancel_appointment", "description": "", "tools": []}],
                         "rules": [{"id": "R1", "text": "Confirm before booking.", "kind": "must"},
                                   {"id": "R2", "text": "Opening hours are 8 to 6.", "kind": "fact"}]},
            "tests": tests}
    (d / "plan-20261112T100000.json").write_text(json.dumps(plan), encoding="utf-8")


def test_coverage_lists_jobs_rules_tools_and_gaps(tmp_path):
    proj = load_project(CLINIC)
    write_plan(tmp_path, [{"id": HAPPY, "intent": "book_appointment", "category": "happy",
                           "rules": [{"id": "R9", "text": "Confirm before booking.", "kind": "must"}]}])
    c = coverage.functional_coverage(tmp_path, proj.profile, proj.scenarios)
    by_intent = {i["id"]: i for i in c["intents"]}
    assert by_intent["book_appointment"]["counts"]["happy"] == 1 and not any(by_intent["cancel_appointment"]["counts"].values())
    assert {r["id"]: len(r["cases"]) for r in c["rules"]}["R1"] == 1          # matched by its text, not its id
    tools = {t["name"]: t for t in c["tools"]}
    assert tools["book_appointment"]["works"] and tools["book_appointment"]["refuses"]
    texts = " | ".join(g["text"] for g in c["gaps"])
    assert "cancel_appointment has no test case" in texts and "R2" not in texts      # facts aren't gaps
    assert c["focus"].startswith("Write test cases for these gaps only")
    assert coverage.intent_of(tmp_path) == {HAPPY: "book_appointment"}


def test_variations_keep_facts_outcome_and_checks(tmp_path):
    import shutil
    folder = tmp_path / "clinic"
    shutil.copytree(CLINIC, folder)
    proj = load_project(folder)
    original = proj.scenario(HAPPY)
    llm = reader({"variations": [
        {"label": "Vague about the day", "goal": "Wants a check-up sometime early in the week.", "persona": original["caller"]["persona"],
         "caller_rules": ["Says 'early next week' before giving a day."], "why": "Real callers are vague."},
        {"label": "All details at once", "goal": "Book a check-up, gives everything in one go.", "persona": "rushed-vague",
         "caller_rules": ["Gives name, birth date and doctor in the first sentence."], "why": "Tests slot filling."}]})
    drafts = asyncio.run(variations.draft(proj, llm, HAPPY, count=2))
    assert len(drafts) == 2 and all(not d["problems"] for d in drafts), [d["problems"] for d in drafts]
    for d in drafts:
        doc = d["scenario"]
        assert doc["caller"]["facts"] == original["caller"]["facts"] and doc["outcome"] == original["outcome"]
        assert doc.get("checks") == original.get("checks") and "variation" in doc["tags"]
        assert doc["criticality"] != "critical" and doc["id"].startswith(HAPPY + ".v-")
    out = variations.add(folder, drafts)
    assert len(out["added"]) == 2
    again = load_project(folder)
    assert {d["id"] for d in drafts} <= {s["id"] for s in again.scenarios}
    with pytest.raises(Exception):
        asyncio.run(variations.draft(proj, llm, "clinic.confirm-before-booking"))   # scripted lines aren't varied
