"""Phase 7: load tests. Settings and stages, the tool gateway with many calls at once, the numbers per
stage, the load decision, the load baseline, CI reports, the dashboard API, and two end-to-end runs:

  - the load reference agent (zoo/load_ref.py: 300 ms + 250 ms per extra call), over the tone codec,
    must come out with capacity exactly 3 against a 1000 ms p95 limit, and NO-GO;
  - the clinic agent in text mode, offline: GO, a planted double booking under load is NO-GO.

Never run two of these at the same time as another clinic run: they share zoo/clinic/clinic.db.
"""

import asyncio
import json
import shutil
import threading
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from vatf import alerts
from vatf.cireport import junit_xml, markdown
from vatf.cli import main
from vatf.compare import compare
from vatf.decision import apply_load
from vatf.gateway import ToolGateway
from vatf.load import LoadSettingsError, levels, scenario_ids, settings, stages
from vatf.loader import load_project, schema_errors
from vatf.loadrun import LoadOrchestrator
from vatf.loadstats import call_facts, must_never, stage_stats, summarise
from vatf.orchestrator import RunConfig
from vatf.snapshot import snapshot
from vatf.speech import SpeechConfig
from vatf.util import read_json

ROOT = Path(__file__).resolve().parent.parent


# ---- load.py ----------------------------------------------------------------------------------

def test_settings_defaults_overrides_and_checks():
    s = settings({})
    assert s["shape"] == "ramp" and s["calls_at_once"] == 5 and s["caller"] == "free" and not s["judges"]
    assert s["limits"]["max_failed_pct"] == 2 and s["limits"]["slowdown"] == {"blocks": False, "pct": 50, "ms": 300}
    s = settings({"load": {"shape": "spike", "limits": {"slowdown": {"blocks": True}}}}, calls_at_once=9, minutes=None)
    assert s["calls_at_once"] == 9 and s["limits"]["slowdown"] == {"blocks": True, "pct": 50, "ms": 300}
    for bad in ({"shape": "wave"}, {"calls_at_once": 0}, {"calls_at_once": 500}, {"start_at": 9, "calls_at_once": 3},
                {"caller": "robot"}, {"minutes": -1}, {"limits": {"p95_ms": 0}}, {"steps": 2.5}):
        with pytest.raises(LoadSettingsError):
            settings({"load": bad})


def test_levels_and_stages_for_every_shape():
    assert levels(1, 5, 4) == [1, 2, 4, 5]
    assert levels(1, 3, 10) == [1, 2, 3]          # never the same level twice
    assert levels(4, 4, 3) == [4]
    ramp = stages(settings({"load": {"calls_at_once": 5, "steps": 4, "step_minutes": 1}}))
    assert [(s.level, s.start_s, s.seconds) for s in ramp] == [(1, 0, 60), (2, 60, 60), (4, 120, 60), (5, 180, 60)]
    spike = stages(settings({"load": {"shape": "spike", "calls_at_once": 8, "start_at": 2, "minutes": 1,
                                      "before_minutes": 0.5, "after_minutes": 2}}))
    assert [(s.kind, s.level, s.seconds) for s in spike] == [("normal", 2, 30), ("spike", 8, 60), ("recovery", 2, 120)]
    soak = stages(settings({"load": {"shape": "soak", "minutes": 12, "window_minutes": 5}}))
    assert [(s.name, s.seconds) for s in soak] == [("Window 1 of 3", 300), ("Window 2 of 3", 300), ("Window 3 of 3", 120)]
    steady = stages(settings({"load": {"shape": "steady", "minutes": 3}}))
    assert len(steady) == 1 and steady[0].level == 5 and steady[0].end_s == 180
    with pytest.raises(LoadSettingsError, match="no time"):
        stages(settings({"load": {"shape": "steady", "minutes": 0}}))


def test_default_tests_leave_out_planted_faults_and_safety_attacks():
    proj = load_project(ROOT / "examples" / "clinic")
    ids = scenario_ids(settings({}), proj.scenarios, "text")
    assert "clinic.book-checkup" in ids and "clinic.book-checkup.tool-timeout" not in ids
    assert all(not i.startswith("safety.") for i in ids)
    assert "clinic.book-checkup-noisy-line" not in ids            # voice only
    picked = scenario_ids(settings({"load": {"scenarios": ["clinic.book-checkup.tool-timeout"]}}), proj.scenarios, "text")
    assert picked == ["clinic.book-checkup.tool-timeout"]          # chosen on purpose: allowed
    with pytest.raises(LoadSettingsError, match="not found"):
        scenario_ids(settings({"load": {"scenarios": ["nope"]}}), proj.scenarios, "text")


# ---- the tool gateway with several calls at once ----------------------------------------------

class FakeProxy:
    def __init__(self, name):
        self.name, self.seen = name, []

    def call_with(self, tool, args):
        self.seen.append((tool, dict(args)))
        return {"ok": True, "status": 200, "body": {"by": self.name}}


def _post(port, body, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/tools/find", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_gateway_routes_each_tool_call_to_its_own_call_by_test_id():
    fallback = FakeProxy("fallback")
    gw = ToolGateway(port=0, header="X-Test-Id", fallback=fallback).start()
    a, b = FakeProxy("a"), FakeProxy("b")
    try:
        gw.attach(a, "t-a")
        assert _post(gw.port, {"x": 1})[1] == {"by": "a"}                 # one call: no id needed
        gw.attach(b, "t-b")
        assert _post(gw.port, {"x": 1}, {"X-Test-Id": "t-b"})[1] == {"by": "b"}
        assert _post(gw.port, {"x": 1, "test_id": "t-a"})[1] == {"by": "a"}   # id in the body works too
        assert a.seen[-1][1] == {"x": 1, "test_id": "t-a"}                 # and is left in the arguments
        assert _post(gw.port, {"x": 2})[1] == {"by": "fallback"} and gw.unattributed == 1
        status, body = _post(gw.port, {"x": 3}, {"X-Test-Id": "t-gone"})
        assert status == 503 and "not a call in progress" in body["error"] and gw.unknown_ids == 1
        gw.detach("t-a")
        gw.detach("t-b")
        assert _post(gw.port, {})[0] == 503
    finally:
        gw.stop()


def test_gateway_one_call_at_a_time_works_as_before_phase_7():
    gw = ToolGateway(port=0).start()
    p = FakeProxy("only")
    try:
        gw.attach(p, "t-1")
        assert _post(gw.port, {}, {"X-Test-Id": "t-stale"})[1] == {"by": "only"}   # an old id: still that call
        gw.attach(FakeProxy("two"), "t-2")
        status, body = _post(gw.port, {})
        assert status == 503 and "X-Test-Id" in body["error"]            # no fallback outside load tests
    finally:
        gw.stop()


# ---- loadstats.py ------------------------------------------------------------------------------

def ev(type_, data=None, t=0, turn=None, seq=0):
    return {"seq": seq, "t_ms": t, "source": "x", "type": type_, "data": data or {}, "turn": turn}


def test_call_facts_sort_calls_into_ok_failed_and_vocto_errors():
    ok = call_facts([ev("call_started", t=0), ev("agent_turn", {"heard_text": "hi"}, 120, 1, 1),
                     ev("caller_turn", {}, 500, 2, 2), ev("agent_turn", {}, 900, 2, 3),
                     ev("tool_response", {"call_id": "c1", "duration_ms": 40, "reached_backend": True, "status": 200}),
                     ev("call_ended", {"ended_by": "caller", "reason": "the script ended"})], {"status": "PASS", "checks": []})
    assert ok["outcome"] == "ok" and ok["first_word_ms"] == 120 and ok["timed"] == [[900, 400]] and ok["tool_ms"] == [40]
    refused = call_facts([ev("harness_event", {"level": "error", "code": "connect_failed"})], {"status": "ERROR", "checks": []})
    assert (refused["outcome"], refused["why"]) == ("failed", "couldn't connect")
    ours = call_facts([ev("harness_event", {"level": "error", "code": "speech_setup_failed"})], {"status": "ERROR", "checks": []})
    assert ours["outcome"] == "harness"
    quiet = call_facts([ev("call_ended", {"ended_by": "caller", "reason": "the agent stopped answering"})],
                       {"status": "FAIL", "checks": []})
    assert quiet["why"] == "stopped answering"
    slow = call_facts([ev("call_ended", {"ended_by": "timeout"})], {"status": "FAIL", "checks": []})
    assert slow["why"] == "ran too long"


def test_only_must_never_failures_block_a_load_test():
    v = lambda cls: {"outcome": {"class": cls}}  # noqa: E731
    out = {"check_id": "outcome", "result": "FAIL", "severity": "blocker"}
    assert must_never(out, v("FALSE_SUCCESS")) and must_never(out, v("SILENT_DATA_BUG"))
    assert not must_never(out, v("NO_ACTION")) and not must_never(out, v("HONEST_FAILURE"))   # slot taken / data used up
    twice = {"check_id": "t3", "type": "tool_called", "message": "book_appointment was called 2 time(s); expected exactly 1 time(s)."}
    never = {"check_id": "t3", "type": "tool_called", "message": "book_appointment was called 0 time(s); expected exactly 1 time(s)."}
    assert must_never(twice, {}) and not must_never(never, {})
    assert must_never({"type": "tool_order"}, {}) and must_never({"type": "not_said"}, {}) and not must_never({"type": "said"}, {})


def lv(stage, outcome="ok", status="PASS", answers=(), blockers=(), why=""):
    return {"test_id": f"t-{stage}-{len(answers)}-{outcome}-{status}", "status": status, "checks": [], "metrics": {},
            "load": {"stage": stage, "outcome": outcome, "why": why, "answers": [list(a) for a in answers],
                     "blockers": list(blockers), "missed": [], "first_word_ms": 100, "tool_ms": [], "tool_calls": 0,
                     "tool_errors": 0, "answered_turns": len(answers), "unanswered_turns": 0}}


def test_stage_numbers_capacity_slowdown_and_trust():
    # Small fixed stages: the minimums lowered (the defaults, 10 calls and 20 answers, are tested in test_load_upgrade).
    s = settings({"load": {"calls_at_once": 3, "steps": 3, "step_minutes": 1,
                           "limits": {"p95_ms": 1000, "min_calls": 3, "min_answers": 1}}})
    st = stages(s)
    samples = [(t / 10, 1.0, 1 if t < 600 else (2 if t < 1200 else 3)) for t in range(1800)]
    verdicts = ([lv(0, answers=[(5 + i, 300)]) for i in range(4)]
                + [lv(1, answers=[(65 + i, 600)]) for i in range(4)]
                + [lv(2, answers=[(125 + i, 1200)]) for i in range(3)] + [lv(2, "failed", "ERROR", why="couldn't connect")])
    out = summarise(s, st, verdicts, samples, 1000, {}, "voice")
    rows = out["stages"]
    assert [r["latency"]["p95_ms"] for r in rows] == [300, 600, 1200]
    assert [r["meets_limits"] for r in rows] == [True, True, False]
    assert rows[2]["failed_pct"] == 25.0 and rows[2]["failed_why"] == {"couldn't connect": 1}
    assert out["capacity"] == 2 and out["breaking"]["level"] == 3
    assert out["warnings"][0]["rule"] == "slowdown"                 # 300 -> 1200 ms: warns by default
    d = apply_load(_decision(), out)
    assert d["decision"] == "NO_GO" and d["exit_code"] == 1
    assert {r["rule"] for r in d["reasons"]} >= {"load_failed_calls", "load_latency", "load_warnings"}
    assert schema_errors("run_decision", d) == []
    # Vocto's own computer lagging makes a stage untrustworthy: not enough evidence, never NO-GO.
    lag = [(t, 400.0, n) for t, _, n in samples]
    out = summarise(s, st, [v for v in verdicts if v["load"]["stage"] < 2], lag, 1000, {}, "voice")
    assert not out["stages"][0]["trusted"] and "couldn't keep up" in out["stages"][0]["untrusted_why"]
    d = apply_load(_decision(), out)
    assert d["decision"] == "NO_GO_INSUFFICIENT_EVIDENCE" and d["exit_code"] == 2


def test_answers_count_in_the_stage_they_were_given_in():
    """A call that starts in one stage and answers in the next is timed under the next stage's load;
    after a spike, the recovery stage counts only answers given once the spike's calls are done."""
    s = settings({"load": {"shape": "spike", "calls_at_once": 4, "start_at": 1, "minutes": 1,
                           "before_minutes": 1, "after_minutes": 1}})
    st = stages(s)
    samples = [(t / 10, 0.0, 1 if t < 600 else (4 if t < 1250 else 1)) for t in range(1800)]
    v = lv(0, answers=[(30, 300), (61, 1000), (122, 1000), (130, 300)])
    rows = summarise(s, st, [v], samples, None, {}, "voice")["stages"]
    assert rows[0]["latency"]["turns"] == 1 and rows[1]["latency"]["turns"] == 1
    assert rows[2]["latency"]["turns"] == 1 and rows[2]["latency"]["p95_ms"] == 300   # 122 s: spike calls still going


def test_unmatched_tool_calls_turn_blockers_into_not_enough_evidence():
    s = settings({"load": {"shape": "steady", "calls_at_once": 2, "minutes": 1, "limits": {"min_calls": 1}}})
    st = stages(s)
    out = summarise(s, st, [lv(0, status="FAIL", answers=[(1, 10)], blockers=["verify-first"])], [(0.5, 0.0, 2)],
                    None, {}, "text", gateway={"unattributed": 3})
    assert out["unattributed_tool_calls"] == 3 and "test id" in out["notes"][-1]
    d = apply_load(_decision(), out)
    assert d["decision"] == "NO_GO_INSUFFICIENT_EVIDENCE"
    out = summarise(s, st, [lv(0, status="FAIL", answers=[(1, 10)], blockers=["verify-first"])], [(0.5, 0.0, 2)], None, {}, "text")
    assert apply_load(_decision(), out)["decision"] == "NO_GO"


def test_soak_drift_is_found():
    s = settings({"load": {"shape": "soak", "calls_at_once": 2, "minutes": 3, "window_minutes": 1}})
    st = stages(s)
    samples = [(t / 10, 0.0, 2) for t in range(1800)]
    vs = [lv(0, answers=[(5 + i, 400)]) for i in range(4)] + [lv(2, answers=[(125 + i, 900)]) for i in range(4)]
    vs += [lv(1, answers=[(65 + i, 500)]) for i in range(4)]
    out = summarise(s, st, vs, samples, None, {}, "voice")
    assert out["comparisons"]["drift"] == {"first_p95_ms": 400, "last_p95_ms": 900, "first_failed_pct": 0.0,
                                           "last_failed_pct": 0.0}
    assert any(w["rule"] == "drift" for w in out["warnings"])


def _decision():
    from vatf.decision import LocalDecisionEngine

    return LocalDecisionEngine().decide("r-1", [], [], {"id": "p"})


# ---- load baseline and comparison --------------------------------------------------------------

def _load_snap(run_id, p95, failed_pct=0.0, capacity=3, shape="ramp", target=5):
    return {"kind": "run_snapshot", "run_id": run_id, "profile_id": "p", "profile_file": "profile.yaml",
            "decision": "GO", "how": {"mode": "voice", "conditions": "as_written", "speech_engine": "local",
                                      "offline": False, "kind": "load", "load_shape": f"{shape} to {target} at once"},
            "tests": {}, "scores": {}, "latency": None, "quality": {}, "judges": {}, "safety": None,
            "load": {"capacity": capacity, "levels": {"1": {"level": 1, "p95_ms": 300, "failed_pct": 0.0, "pass_rate": 100.0},
                                                      "3": {"level": 3, "p95_ms": p95, "failed_pct": failed_pct, "pass_rate": 100.0}}}}


def test_load_runs_compare_level_by_level_and_never_with_normal_runs():
    c = compare(_load_snap("a", 800), _load_snap("b", 1300, failed_pct=10.0, capacity=2))
    by = {x["id"]: x for x in c["changes"]}
    assert by["p95@3"]["direction"] == "worse" and by["failed@3"]["direction"] == "worse"
    assert by["capacity"]["message"].startswith("Capacity went from 3 to 2")
    assert c["like_for_like"] and c["blocking"] == 0                # warns by default
    assert compare(_load_snap("a", 800), _load_snap("b", 1300), {"regression": {"load": {"blocks": True}}})["blocking"] == 1
    other = compare(_load_snap("a", 800), _load_snap("b", 1300, shape="spike"), {"regression": {"load": {"blocks": True}}})
    assert not other["like_for_like"] and other["blocking"] == 0
    normal = {**_load_snap("n", 800), "how": {"mode": "voice", "conditions": "as_written", "speech_engine": "local",
                                              "offline": False}}
    normal.pop("load")
    assert not compare(normal, _load_snap("b", 900))["like_for_like"]   # an old baseline has no kind: a normal run


# ---- end to end -------------------------------------------------------------------------------

def test_load_reference_agent_capacity_is_found_exactly(tmp_path):
    """Phase 7 exit criterion: against the reference agent's known slowdown, Vocto measures every level's
    answer time within 50 ms, finds capacity 3 (p95 limit 1000 ms) and decides NO-GO for 4 at once."""
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
        # 15 s stages finish a handful of calls: the minimums are lowered for this quick check.
        proj.profile.setdefault("load", {}).setdefault("limits", {}).update(min_calls=2, min_answers=2)
        s = settings(proj.profile, calls_at_once=4, step_minutes=0.25)
        cfg = RunConfig(root=ROOT, runs_dir=tmp_path / "runs", offline=True, speech=SpeechConfig(engine="codec"),
                        baseline="none")
        orch = LoadOrchestrator(proj, cfg, s, log=lambda *_: None)
        decision = asyncio.run(orch.run())
    finally:
        box["loop"].call_soon_threadsafe(box["loop"].stop)
    run = tmp_path / "runs" / decision["run_id"]
    load = read_json(run / "load.json")
    rows = {r["level"]: r for r in load["stages"]}
    for level, want in ((1, 300), (2, 550), (3, 800), (4, 1050)):
        lat = rows[level]["latency"]
        assert lat["turns"] >= 2, (level, lat)
        assert abs(lat["p50_ms"] - want) <= 50 and abs(lat["p95_ms"] - want) <= 50, (level, lat)
    assert load["capacity"] == 3 and load["breaking"]["level"] == 4
    assert decision["decision"] == "NO_GO" and any(r["rule"] == "load_latency" for r in decision["reasons"])
    assert box["agent"].peak == 4                                    # never more calls at once than asked
    assert load["lag_p95_ms"] < 100 and all(r["trusted"] for r in load["stages"])
    info = read_json(run / "run.json")
    assert info["kind"] == "load" and info["load"]["stages"][0]["level"] == 1 and info["status"] == "finished"
    v = read_json(next((run / "calls").glob("t-*")) / "verdict.json")
    assert schema_errors("call_verdict", v) == []
    assert schema_errors("run_decision", read_json(run / "decision.json")) == []


@pytest.fixture
def clinic(tmp_path):
    shutil.copytree(ROOT / "examples" / "clinic", tmp_path / "clinic",
                    ignore=shutil.ignore_patterns("runs", "baselines"))
    return tmp_path / "clinic"


def load_cli(clinic, runs, *extra):
    return main(["load", str(clinic), "--offline", "--shape", "steady", "--calls", "3", "--minutes", "0.12",
                 "--root", str(ROOT), "--runs-dir", str(runs), *extra])


def newest(runs):
    return sorted(p for p in runs.iterdir() if p.name.startswith("r-"))[-1]


def test_clinic_load_go_then_double_booking_no_go_with_baseline_ci_and_alerts(clinic, tmp_path):
    runs = tmp_path / "runs"
    junit, summary = tmp_path / "ci" / "load.xml", tmp_path / "ci" / "load.md"
    assert load_cli(clinic, runs, "--junit", str(junit), "--summary", str(summary)) == 0
    good = newest(runs)
    decision = read_json(good / "decision.json")
    # The offline practice agent answers in about a millisecond, so calls never pile up to 3 at once: capacity is
    # only counted up to the calls really in progress, not the level that was asked for.
    assert decision["decision"] == "GO" and decision["load"]["capacity"] == 1
    assert "ended so fast" in decision["load"]["headroom"]["text"]
    assert decision["reasons"][0]["rule"] == "load_ok"
    load = read_json(good / "load.json")
    assert load["calls"] >= 6 and load["stages"][0]["failed"] == 0
    root = ET.parse(junit).getroot()
    assert [c.get("name").split(" (")[0] for c in root.iter("testcase")] == ["Hold", "decision"]
    assert "| Hold | 3 |" in summary.read_text("utf-8")
    # the data note: one copy of the test data for the whole load, so later bookings can find slots taken
    assert any("loaded once" in n for n in load["notes"]) or load["stages"][0]["pass_rate"] == 100.0

    # A load run becomes the load baseline, never the normal one.
    assert main(["baseline", "set", good.name, "--root", str(ROOT), "--runs-dir", str(runs)]) == 0
    assert (clinic / "baselines" / "profile-load.json").is_file() and not (clinic / "baselines" / "profile.json").exists()

    assert load_cli(clinic, runs, "--agent-opt", "bugs=double_booking") == 1
    bad = newest(runs)
    d = read_json(bad / "decision.json")
    assert d["decision"] == "NO_GO"
    msg = next(r["message"] for r in d["reasons"] if r["rule"] == "load_blocker")
    assert "must never do" in msg and "DUPLICATE_ACTION" in msg, msg
    comparison = read_json(bad / "comparison.json")
    assert comparison["source"] == "load baseline" and comparison["baseline"]["run_id"] == good.name

    # A normal run doesn't compare with the load baseline.
    assert main(["run", str(clinic), "--offline", "--quiet", "--repeats", "1", "--scenario", "clinic.asks-for-person",
                 "--root", str(ROOT), "--runs-dir", str(runs)]) == 0
    assert not (newest(runs) / "comparison.json").exists()
    # Alerts: a load test recovers from an earlier load test only.
    info = read_json(bad / "run.json")
    fake = runs / "r-99999999-000000-0000"
    fake.mkdir()
    (fake / "run.json").write_text(json.dumps({"project": info["project"], "profile_file": "profile.yaml",
                                               "decision": "NO_GO", "agent_options": {}}), "utf-8")
    later = "r-99999999-999999-ffff"
    assert alerts.previous_decision(runs, later, info["project"], "profile.yaml", "load") == "GO"     # the good load run
    assert alerts.previous_decision(runs, later, info["project"], "profile.yaml") == "NO_GO"         # the normal run


def test_load_cli_refuses_bad_settings(clinic, tmp_path, capsys):
    assert main(["load", str(clinic), "--offline", "--calls", "0", "--root", str(ROOT),
                 "--runs-dir", str(tmp_path / "runs")]) == 3
    assert "calls_at_once" in capsys.readouterr().err
    assert not (tmp_path / "runs").exists()


def test_snapshot_of_a_load_run_keeps_the_load_not_the_tests():
    decision = {**_decision(), "reliability": [{"scenario_id": "s", "criticality": "critical", "k": 9, "passed": 3,
                                                 "failed": 6, "unverified": 0}],
                "load": {"shape": "ramp", "calls_at_once": 3, "capacity": 2, "calls": 9, "failed": 1,
                         "stages": [{"level": 1, "finished": 3, "failed": 0, "p95_ms": 300, "pass_rate": 100.0},
                                    {"level": 3, "finished": 6, "failed": 1, "p95_ms": 900, "pass_rate": 50.0}]}}
    snap = snapshot(decision, {"kind": "load", "load": {"shape": "ramp", "calls_at_once": 3}, "mode": "voice"})
    assert snap["tests"] == {} and snap["how"]["kind"] == "load" and snap["how"]["load_shape"] == "ramp to 3 at once"
    assert snap["load"]["levels"]["3"] == {"level": 3, "finished": 6, "failed": 1, "p95_ms": 900, "pass_rate": 50.0,
                                          "failed_pct": 16.7}


def test_ci_reports_for_a_load_run():
    d = {**_decision(), "decision": "NO_GO", "exit_code": 1,
         "reasons": [{"rule": "load_latency", "message": "Step 2 of 2 (3 calls at once): answer time p95 1,200 ms."}],
         "load": {"shape": "ramp", "calls_at_once": 3, "capacity": 1, "calls": 8, "failed": 0,
                  "stages": [{"index": 0, "name": "Step 1 of 2", "kind": "step", "level": 1, "calls": 4, "finished": 4,
                              "failed": 0, "failed_pct": 0.0, "pass_rate": 100.0, "trusted": True, "meets_limits": True,
                              "p50_ms": 300, "p95_ms": 310},
                             {"index": 1, "name": "Step 2 of 2", "kind": "step", "level": 3, "calls": 4, "finished": 4,
                              "failed": 0, "failed_pct": 0.0, "pass_rate": 100.0, "trusted": True, "meets_limits": False,
                              "p50_ms": 1100, "p95_ms": 1200}]}}
    root = ET.fromstring(junit_xml(d, []).split("\n", 1)[1])
    failures = [c.get("name") for c in root.iter("testcase") if c.find("failure") is not None]
    assert failures == ["Step 2 of 2 (3 at once, 4 calls)", "decision"]
    md = markdown(d, {"profile_name": "Agent", "mode": "voice"})
    assert "| Step 2 of 2 | 3 | 4 | 0.0% | 1100 ms | 1200 ms | 100.0% | **broke a limit** |" in md
    assert "Capacity | 1 calls at once" in md


# ---- dashboard API -----------------------------------------------------------------------------

def test_dashboard_load_page_save_and_job(tmp_path):
    from vatf.web import data, loadtest
    from vatf.web.jobs import JobError, JobManager

    root = tmp_path / "root"
    shutil.copytree(ROOT / "examples" / "load-ref", root / "examples" / "load-ref")
    key = data.project_keys(root)["examples/load-ref"]
    page = loadtest.load_page(root, root / "runs", key)
    assert page["mode"] == "voice" and page["settings"]["calls_at_once"] == 5 and page["p95_limit_ms"] == 1000
    assert [s["level"] for s in page["plan"]["stages"]] == [1, 2, 3, 4, 5] and page["tests_used"] == ["load-ref.two-answers"]
    assert page["agent"]["agent_process"] and not page["agent"]["launched_per_call"]
    bad = loadtest.save(root, key, "profile.yaml", {"shape": "ramp", "calls_at_once": 2, "start_at": 4})
    assert not bad["saved"] and "start_at" in bad["problems"][0]
    ok = loadtest.save(root, key, "profile.yaml", {"shape": "spike", "calls_at_once": 6, "minutes": 1, "scenarios": [],
                                                   "limits": {"p95_ms": None, "slowdown": {"blocks": True}}})
    assert ok["saved"], ok
    page = loadtest.load_page(root, root / "runs", key)
    assert page["saved"] == {"shape": "spike", "calls_at_once": 6, "minutes": 1, "limits": {"slowdown": {"blocks": True}}}
    assert [s["kind"] for s in page["plan"]["stages"]] == ["normal", "spike", "recovery"]
    # The section is written whole: a field set back to its default is gone from the file, not kept.
    assert loadtest.save(root, key, "profile.yaml", {"shape": "spike", "calls_at_once": 6})["saved"]
    assert loadtest.load_page(root, root / "runs", key)["saved"] == {"shape": "spike", "calls_at_once": 6}
    assert loadtest.save(root, key, "profile.yaml", {})["saved"]
    assert loadtest.load_page(root, root / "runs", key)["saved"] == {}
    jobs = JobManager(root, Path("runs"))
    args, command = jobs.build({"command": "load", "profile": "profile.yaml", "speech": "codec", "start_agent": True},
                               data.project_by_key(root, key))
    assert args[:4] == ["load", "examples/load-ref", "--profile", "profile.yaml"] and "--start-agent" in args
    assert command.startswith("uv run python -m vatf load examples/load-ref") and "--quiet" not in command
    with pytest.raises(JobError):
        jobs.build({"command": "load", "profile": "profile.yaml", "speech": "robot"}, data.project_by_key(root, key))


def test_load_stops_at_once_when_the_agent_is_not_running(tmp_path, capsys):
    """Nothing listening at the agent's address: one clear message and exit 3, no run, no failed calls."""
    import socket

    from vatf.load import unreachable

    with socket.socket() as s:          # a port nothing listens on
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    folder = tmp_path / "load-ref"
    shutil.copytree(ROOT / "examples" / "load-ref", folder)
    prof = folder / "profile.yaml"
    prof.write_text(prof.read_text(encoding="utf-8").replace("ws://127.0.0.1:8792", f"ws://127.0.0.1:{port}")
                    .replace("start_agent: true", "start_agent: false"), encoding="utf-8")
    profile = load_project(folder).profile
    why = unreachable(profile)
    assert why and f"ws://127.0.0.1:{port}" in why and "Start the agent for me" in why
    assert unreachable({"connection": {"type": "text_python", "target": "zoo.clinic.agent:build_agent"}}) is None
    runs = tmp_path / "runs"
    assert main(["load", str(folder), "--speech", "codec", "--root", str(ROOT), "--runs-dir", str(runs), "--quiet"]) == 3
    assert "the agent isn't running" in capsys.readouterr().err
    assert not runs.exists() or not any(runs.glob("r-*"))
