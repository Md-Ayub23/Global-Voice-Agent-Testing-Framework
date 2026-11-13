"""Resilience (step 3): failure drills (drills.py), the new tool faults, an unsteady network
(audio/line.py NetworkJitter) and recovery after a load spike (loadstats.py)."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import numpy as np

from vatf import readiness
from vatf.audio.line import NetworkJitter
from vatf.conditions import resolve
from vatf.contracts.models import CallContext
from vatf.drills import DEFAULT, FAILURES, by_failure, drillable, expand, make, split
from vatf.load import settings, stages
from vatf.loader import load_project, schema_errors
from vatf.loadstats import summarise
from vatf.orchestrator import Orchestrator, RunConfig
from vatf.proxy import InProcessToolProxy
from vatf.speech import SpeechConfig

ROOT = Path(__file__).resolve().parents[1]
SIM = {"schema_version": "0.1", "kind": "scenario", "id": "shop.order", "title": "Order a pizza", "profile": "shop",
       "style": "simulated", "criticality": "critical",
       "caller": {"persona": "calm", "goal": "Order a pizza", "facts": {"full_name": "Asha Rao"}},
       "setup": {"faults": [{"tool": "lookup", "type": "slow", "delay_ms": 500},
                            {"tool": "place_order", "type": "http_error"}]},
       "outcome": {"expected": "success", "action": {"tool": "place_order"},
                   "record": {"probe": "orders", "expect_count": 1}},
       "checks": [{"id": "said-total", "type": "said", "pattern": "total"},
                  {"id": "no-leak", "type": "no_secrets"},
                  {"id": "name-right", "type": "entity_fidelity", "entities": ["full_name"]}]}


def test_a_drill_breaks_the_order_tool_once_and_keeps_the_failure_safe_checks():
    d = make(SIM, "rate_limited")
    assert d["id"] == "shop.order.drill.rate_limited" and split(d["id"]) == ("shop.order", "rate_limited")
    assert d["title"] == "Order a pizza · Too many requests" and "drill" in d["tags"]
    # The test's own fault on the order tool is replaced; other tools' faults stay.
    assert d["setup"]["faults"] == [{"tool": "lookup", "type": "slow", "delay_ms": 500},
                                    {"tool": "place_order", "on_call": 1, "type": "rate_limited", "retry_after_s": 2}]
    assert d["outcome"]["expected"] == "recover_or_honest" and d["outcome"]["record"]["expect_count"] == 1
    # Happy-path checks are left out; a drill adds "stops retrying" and the way-forward judge question.
    assert [c["id"] for c in d["checks"]] == ["no-leak", "stops-retrying", "way-forward"]
    assert SIM["outcome"]["expected"] == "success" and len(SIM["checks"]) == 3   # the original is untouched
    lost = make(SIM, "saved_lost")
    assert lost["outcome"]["expected"] == "success" and lost["setup"]["faults"][-1]["forward"] is True
    assert make(SIM, "garbage")["setup"]["faults"][-1]["forward"] is False
    assert schema_errors("scenario", d) == [] and schema_errors("scenario", lost) == []
    # Scripted lines can't react to a failure, and a test with nothing to save has nothing to drill.
    assert drillable({**SIM, "style": "scripted"}) and drillable({**SIM, "outcome": {"expected": "no_action", "action": {"tool": "x"}}})
    assert drillable({k: v for k, v in SIM.items() if k != "outcome"}) and drillable(d)
    assert drillable(SIM) is None
    assert [x["id"] for x in expand([SIM, {**SIM, "id": "s2", "style": "scripted"}], DEFAULT)] == \
        [f"shop.order.drill.{f}" for f in DEFAULT]


class _Sink:
    def __init__(self):
        self.events = []

    def emit(self, source, type_, data, turn=None):
        self.events.append((type_, data))


def _proxy(faults):
    profile = {"tools": [{"name": "book", "upstream": "python:tests.test_resilience:_ok"}]}
    p = InProcessToolProxy(profile, faults)
    sink = _Sink()
    asyncio.run(p.attach(CallContext(test_id="t1", run_id="r1", profile=profile, scenario={}, repeat=1, attempt=1,
                                     mode="text"), sink))
    return p, sink


CALLS: list[dict] = []


def _ok(args, headers):
    CALLS.append(dict(args))
    return 201, {"reference": "R1"}


def test_the_new_tool_faults():
    import importlib
    calls = importlib.import_module("tests.test_resilience").CALLS   # the copy the proxy imports
    calls.clear()
    p, sink = _proxy([{"tool": "book", "type": "rate_limited", "on_call": 1},
                      {"tool": "book", "type": "auth_expired", "on_call": 2},
                      {"tool": "book", "type": "malformed_response", "on_call": 3, "forward": False},
                      {"tool": "book", "type": "malformed_response", "on_call": 4}])
    r1, r2, r3, r4, r5 = (p.call("book", slot=i) for i in range(5))
    assert (r1["status"], r1["body"]["retry_after_s"]) == (429, 2) and not r1["ok"]
    assert r2["status"] == 401 and "expired" in r2["body"]["error"]
    assert not r3["ok"] and not r4["ok"] and r5["ok"]
    # Only the 4th call (rubbish answer, forwarded as before) and the 5th reached the backend.
    assert [c["slot"] for c in calls] == [3, 4]
    reached = [d["reached_backend"] for t, d in sink.events if t == "tool_response"]
    assert reached == [False, False, False, True, True]


def _run(tmp_path, failures, bugs=""):
    shutil.copytree(ROOT / "examples" / "clinic", tmp_path / "clinic", ignore=shutil.ignore_patterns("runs"))
    proj = load_project(tmp_path / "clinic", "profile.yaml")
    proj = proj.with_agent_options({"brain": "rules", "bugs": bugs})
    cfg = RunConfig(root=ROOT, runs_dir=tmp_path / "runs", offline=True, repeats=1, judges=False,
                    test_mode="resilience", drills=tuple(failures), scenario_ids=("clinic.book-checkup",),
                    baseline="none")
    orch = Orchestrator(proj, cfg, log=lambda *_: None)
    assert len(orch.plan()) == len(orch.plan()) == len(failures)     # planning twice doesn't drill the drills
    decision = asyncio.run(orch.run())
    return decision, tmp_path / "runs" / decision["run_id"]


def test_failure_drills_end_to_end_on_the_clinic_agent(tmp_path):
    """The clean clinic agent is honest when booking fails, but a booking saved with its answer lost
    is reported to the caller as failed: a real finding, with advice for that case."""
    decision, run_dir = _run(tmp_path, ["timeout", "rate_limited", "auth_expired", "slow", "saved_lost"])
    info = json.loads((run_dir / "run.json").read_text("utf-8"))
    assert info["drills"] == ["timeout", "rate_limited", "auth_expired", "slow", "saved_lost"]
    assert [p["scenario_id"] for p in info["plan"]] == [f"clinic.book-checkup.drill.{f}" for f in info["drills"]]
    assert schema_errors("run_decision", {k: v for k, v in decision.items() if not k.startswith("_")}) == []
    rows = {r["failure"]: r for r in decision["by_failure"]}
    assert [r["failure"] for r in decision["by_failure"]] == ["timeout", "rate_limited", "auth_expired", "slow", "saved_lost"]
    assert all(rows[f]["passed"] == 1 and rows[f]["honest"] == 1 for f in ("timeout", "rate_limited", "auth_expired"))
    assert rows["slow"]["passed"] == 1 and rows["slow"]["recovered"] == 1
    assert rows["saved_lost"]["failed"] == 1 and rows["saved_lost"]["false_claims"] == 1
    assert decision["decision"] == "NO_GO"

    report = readiness.build(run_dir, titles={"clinic.book-checkup": "Book a check-up"})
    fails = {f["key"]: f for f in report["failures"]}
    assert fails["saved_lost"]["status"] == "broke" and fails["timeout"]["status"] == "handled"
    assert fails["saved_lost"]["failed_tests"] == ["Book a check-up"]
    row = next(r for r in report["scorecard"] if r["area"] == "resilience")
    assert row["value"] == "Handled 4 of 5 kinds of failure" and row["status"] == "fail"
    assert "Saved, but the answer was lost" in row["detail"]
    top = report["findings"][0]
    assert "saved" in top["fix"] and "request id" in top["fix"]
    assert any(line.startswith("With its tools failing on purpose (5 kinds of failure)")
               for line in report["summary"])
    assert "1 test case" in report["summary"][0]                 # drills count as their test case


def test_drills_catch_a_lying_and_a_double_booking_agent(tmp_path):
    decision, _ = _run(tmp_path / "a", ["timeout", "saved_lost"], bugs="lie_on_failure")
    rows = {r["failure"]: r for r in decision["by_failure"]}
    assert rows["timeout"]["false_claims"] == 1 and rows["timeout"]["failed"] == 1
    decision, _ = _run(tmp_path / "b", ["saved_lost"], bugs="double_booking")
    assert decision["by_failure"][0]["duplicates"] == 1


def test_an_unsteady_network_delays_and_reorders_but_keeps_every_frame():
    net = NetworkJitter(8000, jitter_ms=80, reorder_pct=10, seed="call-1")
    due = [i * 0.02 for i in range(2000)]
    at = [net.arrival(d) for d in due]
    assert all(b >= a for a, b in zip(at, at[1:]))                 # in order on the wire
    assert all(a >= d for a, d in zip(at, due)) and 50 < net.max_delay_ms < 600
    swaps = sum(net.swap() for _ in range(2000))
    assert 120 < swaps < 290 and net.reordered == swaps
    again = NetworkJitter(8000, jitter_ms=80, reorder_pct=10, seed="call-1")
    assert [again.arrival(d) for d in due] == at                   # seeded: a call can be replayed
    assert not NetworkJitter(8000).active

    c = resolve({}, {"audio": {}}, preset="jittery")
    assert (c.jitter_ms, c.reorder_pct) == (80, 1) and "unsteady network" in c.label()
    assert c.summary()["jitter_ms"] == 80


def test_the_line_sends_every_frame_through_an_unsteady_network():
    from vatf.audio.recorder import Line, Recorder

    async def go():
        sent = []
        clock = {"t": 0.0}
        rec = Recorder(8000, lambda: int(clock["t"] * 1000))
        rec.clock = lambda: clock["t"]                             # a fake call clock

        async def send(x):
            sent.append(int(round(x[0] * 1000)))

        line = Line(rec, send, network=NetworkJitter(8000, jitter_ms=40, reorder_pct=20, seed="s"))
        line._wire = asyncio.Queue()                               # as start() makes it
        frames = [np.full(160, i / 1000, dtype=np.float32) for i in range(50)]
        for i, f in enumerate(frames):
            line._post(f, i * 0.02)
        line._wire.put_nowait((0.0, None))
        out = []
        while True:
            at, x = line._wire.get_nowait()
            if x is None:
                break
            out.append(int(round(x[0] * 1000)))
        return out

    out = asyncio.run(go())
    held_last = 49 not in out             # a frame held for reordering waits for the next one
    assert sorted(out + ([49] if held_last else [])) == list(range(50))
    assert out != sorted(out)             # some frames swapped places


def _lv(stage, answers, outcome="ok"):
    return {"test_id": f"t-{stage}-{answers[0][0] if answers else 0}", "status": "PASS", "checks": [], "metrics": {},
            "load": {"stage": stage, "outcome": outcome, "why": "", "answers": [list(a) for a in answers],
                     "blockers": [], "missed": [], "first_word_ms": 100, "tool_ms": [], "tool_calls": 0,
                     "tool_errors": 0, "answered_turns": len(answers), "unanswered_turns": 0}}


def test_recovery_after_a_spike():
    s = settings({"load": {"shape": "spike", "calls_at_once": 4, "start_at": 1, "before_minutes": 1, "minutes": 1,
                           "after_minutes": 1}})
    st = stages(s)
    assert [x.kind for x in st] == ["normal", "spike", "recovery"]
    samples = [(t / 10, 1.0, 4 if 600 <= t < 1200 else 1) for t in range(1800)]
    before = [_lv(0, [(5 + i, 400)]) for i in range(5)]
    spike = [_lv(1, [(65 + i, 1500)]) for i in range(5)]
    back = [_lv(2, [(125 + 5 * i, 1400 if i < 2 else 450)]) for i in range(6)]
    out = summarise(s, st, before + spike + back, samples, None, {}, "voice")
    rec = out["comparisons"]["recovery"]
    assert rec["recovered"] and rec["before_p95_ms"] == 400 and rec["back_to_normal_s"] == 15.0
    assert not any(w["rule"] == "recovery" for w in out["warnings"])
    stuck = [_lv(2, [(125 + 5 * i, 1400)]) for i in range(6)]
    out = summarise(s, st, before + spike + stuck, samples, None, {}, "voice")
    assert not out["comparisons"]["recovery"]["recovered"]
    assert any(w["rule"] == "recovery" and "didn't come back" in w["message"] for w in out["warnings"])
    blocking = settings({"load": {**s, "limits": {"recovery": {"blocks": True}}}})
    out = summarise(blocking, st, before + spike + stuck, samples, None, {}, "voice")
    assert any(b["rule"] == "recovery" for b in out["blocking"])
