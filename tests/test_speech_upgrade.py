"""Speech upgrade: details by type, the fair-test listen, read back before saving, honest drops, the
breaking-point ladder, and the new line conditions (echo, chatter, keypad)."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from vatf import metrics, speechcheck, stats
from vatf.audio.noise import make_noise, room_echo, touch_tones
from vatf.conditions import known, ladder_step, resolve, sweep_list, title
from vatf.contracts.models import CheckResult, EvidenceLevel, Result
from vatf.decision import LocalDecisionEngine
from vatf.loader import schema_errors
from vatf.orchestrator import _too_hard

ROOT = Path(__file__).resolve().parent.parent


def ev(seq, type_, data, turn=None):
    return {"seq": seq, "t_ms": 0, "source": "x", "type": type_, "data": data, "turn": turn}


SCENARIO = {"id": "s", "caller": {"facts": {"full_name": "Maria Lopez", "date_of_birth": "1985-03-14",
                                            "phone": "07700 900123", "preferred_time": "morning"}},
            "outcome": {"action": {"tool": "book_appointment"}}}


def test_fact_types_and_how_people_say_them():
    assert speechcheck.fact_type("full_name", "Maria Lopez") == "name"
    assert speechcheck.fact_type("date_of_birth", "1985-03-14") == "date"
    assert speechcheck.fact_type("phone", "07700 900123") == "phone"
    assert speechcheck.fact_type("email", "a.b@example.com") == "email"
    assert speechcheck.fact_type("booking_ref", "BX7-22P") == "code"
    assert speechcheck.fact_type("amount", "£42.50") == "amount"
    assert speechcheck.said_in("1985-03-14", "I was born on the 14th of March 1985")
    assert not speechcheck.said_in("1985-03-14", "born on the 4th of March 1985")
    assert speechcheck.said_in("07700 900123", "it's oh seven seven zero zero, 900 123")
    assert speechcheck.said_in("Maria Lopez", "Hi, Maria Lopez here")
    assert not speechcheck.said_in("Maria Lopez", "Hi, Mario Lopes here")


def call(name_saved="Maria Lopez", read_back=True, said_yes=True, heard="Hi, I'm Maria Lopez, born 14 March 1985"):
    evs = [
        ev(1, "caller_turn", {"text": "Hi, I'm Maria Lopez, born 14 March 1985", "audio": {"start_ms": 0, "end_ms": 2000}}, 1),
        ev(2, "caller_heard", {"caller_turn": 1, "text": heard}, 1),
        ev(3, "agent_turn", {"heard_text": "So that's Maria Lopez, born the 14th of March 1985, is that right?"
                             if read_back else "Thanks, booking you in now"}, 1),
        ev(4, "caller_turn", {"text": "Yes, that's right" if said_yes else "Can you book it"}, 2),
        ev(5, "tool_request", {"call_id": "c1", "tool": "book_appointment",
                               "args": {"full_name": name_saved, "date_of_birth": "1985-03-14"}}, 2),
    ]
    return evs


def test_details_read_back_and_fair_test():
    s = speechcheck.summary(SCENARIO, call(), "salt")
    assert [(r["fact"], r["type"], r["right"]) for r in s["details"]] == [("full_name", "name", True), ("date_of_birth", "date", True)]
    assert s["fair"] == {"heard": ["date_of_birth", "full_name"], "missed": []}
    assert s["read_back"]["ok"] and s["read_back"]["details"] == 2 and s["too_hard"] == []

    no_rb = speechcheck.summary(SCENARIO, call(read_back=False), "salt")
    assert not no_rb["read_back"]["ok"] and no_rb["read_back"]["read_back"] == 0
    no_yes = speechcheck.summary(SCENARIO, call(said_yes=False), "salt")
    assert no_yes["read_back"]["read_back"] == 2 and not no_yes["read_back"]["confirmed"]

    # Saved wrong, and Vocto's careful listener didn't hear the name either: too hard to hear, a test issue.
    hard = speechcheck.summary(SCENARIO, call(name_saved="Mario Lopes", heard="Hi, I'm Mario Lopes, born 14 March 1985"), "salt")
    assert hard["too_hard"] == ["full_name"] and hard["details"][0]["saved"] == "Mario Lopes"
    # Saved wrong although the line was clear: the agent's own mishearing.
    clear = speechcheck.summary(SCENARIO, call(name_saved="Mario Lopes"), "salt")
    assert clear["too_hard"] == []


def test_too_hard_turns_the_detail_failure_into_a_test_issue():
    fail = CheckResult(check_id="ent", origin="hard_check", result=Result.FAIL, severity="major", section="accuracy",
                       message="full_name in book_appointment arguments differs", evidence=(), evidence_level=EvidenceLevel.TOOL_TRAFFIC,
                       type="entity_fidelity")
    other = CheckResult(check_id="x", origin="hard_check", result=Result.FAIL, severity="major", section="accuracy",
                        message="something else", evidence=(), evidence_level=EvidenceLevel.TOOL_TRAFFIC, type="tool_called")
    out = _too_hard([fail, other], ["full_name"], {"full_name": "Maria Lopez"})
    assert out[0].result == Result.NOT_VERIFIED and "Too hard to hear" in out[0].message
    assert out[1].result == Result.FAIL


def test_keypad_numbers_are_not_held_against_the_listener():
    evs = [ev(1, "caller_turn", {"text": "my number is 07700 900123", "spoken": "my number is [keypad 07700900123]"}, 1),
           ev(2, "caller_heard", {"caller_turn": 1, "text": "my number is"}, 1)]
    assert speechcheck.fair_test(SCENARIO, evs) == {"heard": [], "missed": []}


def test_honest_drops_and_the_ladder():
    def v(i, preset, status, ladder=None, details=()):
        c = {"preset": preset, "title": title(preset), "label": preset, **({"ladder": ladder} if ladder else {})}
        return {"test_id": f"t{i}", "scenario_id": "s", "repeat": 1, "status": status, "conditions": c, "checks": [], "metrics": {},
                "speech": {"details": [{"fact": "full_name", "type": "name", "right": r} for r in details]}}
    vs = [v(i, "as_written", "PASS") for i in range(3)] + [v(10, "phone", "PASS"), v(11, "phone", "PASS"), v(12, "phone", "FAIL")]
    vs += [v(20, "snr:20", "PASS", {"kind": "noise", "level": 20}, [True]), v(21, "snr:10", "PASS", {"kind": "noise", "level": 10}, [True]),
           v(22, "snr:5", "FAIL", {"kind": "noise", "level": 5}, [False]), v(23, "snr:0", "PASS", {"kind": "noise", "level": 0}, [True])]
    rows = metrics.by_condition(vs)
    phone = next(r for r in rows if r["key"] == "phone")
    # 3/3 against 2/3 is not more than chance.
    assert phone["vs_clean"]["lower"] and not phone["vs_clean"]["worse"]
    assert phone["vs_clean"]["text"] == "no clear difference with this many calls"
    lad = metrics.ladder(rows)
    assert lad["noise"]["works_to"] == 10 and lad["noise"]["text"] == "works down to 10 dB"
    assert [s["level"] for s in lad["noise"]["levels"]] == [20, 10, 5, 0]
    # Ladder calls never decide the release: a failed 5 dB step doesn't block.
    d = LocalDecisionEngine().decide("r", vs[:3] + vs[6:], [{"id": "s", "criticality": "critical"}], {"id": "p", "thresholds": {"repeats": 3}})
    assert d["decision"] == "GO" and d["ladder"]["noise"]["works_to"] == 10
    assert len(d["calls"]) == 7
    assert stats.fisher_exact(10, 0, 2, 8) < 0.05            # a big gap is a real drop


def test_ladder_and_new_conditions():
    assert ladder_step("snr:5") == ("noise", 5.0) and ladder_step("loss:10") == ("loss", 10.0) and ladder_step("phone") is None
    assert known("snr:0") and known("loss:3") and not known("snr:99") and known("chatter") and known("echo") and known("keypad")
    assert title("snr:5") == "Cafe noise 5 dB" and title("loss:3") == "3% packets lost"
    c = resolve({}, {}, preset="snr:5", seed="x")
    assert (c.noise, c.snr_db) == ("cafe", 5.0) and c.summary()["ladder"] == {"kind": "noise", "level": 5.0}
    lossy = resolve({}, {}, preset="loss:10", seed="x")
    assert lossy.phone and lossy.packet_loss_pct == 10
    assert resolve({}, {}, preset="echo", seed="x").echo and resolve({}, {}, preset="keypad", seed="x").keypad
    assert {c["group"] for c in sweep_list()} == {"line", "ladder", "accent"}


def test_new_sounds():
    x = make_noise("chatter", 2, 16000, "s")
    assert abs(np.sqrt(np.mean(x.astype(np.float64) ** 2)) - 1) < 0.01
    voice = np.sin(2 * np.pi * 200 * np.arange(16000) / 16000).astype(np.float32) * 0.1
    wet = room_echo(voice, 16000, "s")
    assert len(wet) > len(voice) and abs(np.sqrt(np.mean(wet[:16000] ** 2)) - np.sqrt(np.mean(voice ** 2))) < 0.01
    tones = touch_tones("123", 8000)
    assert len(tones) == 3 * 1600
    spec = np.abs(np.fft.rfft(tones[:800]))
    peaks = sorted(np.argsort(spec)[-2:] * 8000 / 800)
    assert abs(peaks[0] - 697) < 15 and abs(peaks[1] - 1209) < 15        # key 1: 697 Hz + 1209 Hz


def test_run_summary_and_schemas():
    v = {"test_id": "t", "scenario_id": "s", "status": "FAIL", "conditions": {"title": "Phone line"},
         "speech": {"details": [{"fact": "full_name", "type": "name", "right": False, "said": "Maria Lopez", "saved": "Mario Lopes", "seq": 5},
                                {"fact": "date_of_birth", "type": "date", "right": True}],
                    "read_back": {"ok": True}, "voices": "synthetic", "too_hard": []}}
    s = speechcheck.run_summary([v])
    assert s["details"]["k"] == 1 and s["details"]["n"] == 2 and s["voices"] == "synthetic"
    assert s["wrong"][0]["saved"] == "Mario Lopes" and [t["type"] for t in s["by_type"]] == ["name", "date"]
    assert s["read_back"]["k"] == 1
    verdict = {"schema_version": "0.1", "kind": "call_verdict", "test_id": "t", "run_id": "r", "scenario_id": "s",
               "repeat": 1, "status": "PASS", "checks": [], "metrics": {"turns": 1, "duration_ms": 1}, "speech": v["speech"]}
    assert [e for e in schema_errors("call_verdict", verdict) if "speech" in e] == []
    assert schema_errors("trace_event", {"seq": 1, "t_ms": 0, "source": "recorder",
                                         "type": "caller_heard", "data": {"caller_turn": 1, "text": "hi"}}) == []
    assert schema_errors("trace_event", {"seq": 1, "t_ms": 0, "source": "recorder",
                                         "type": "caller_heard", "data": {"text": "hi"}}) != []


def test_a_speech_run_with_the_ladder_end_to_end(tmp_path):
    """The zoo clinic agent (rules brain, test-tone speech): clean line plus two ladder steps. The fair-test
    listen runs on the caller's lines, details and read-back are recorded, and the ladder never decides."""
    import asyncio
    import json
    import shutil

    from vatf import readiness
    from vatf.loader import load_project
    from vatf.orchestrator import Orchestrator, RunConfig
    from vatf.speech import SpeechConfig

    shutil.copytree(ROOT / "examples" / "clinic", tmp_path / "clinic", ignore=shutil.ignore_patterns("runs"))
    proj = load_project(tmp_path / "clinic", "profile-voice.yaml").with_agent_options(
        {"brain": "rules", "bugs": "", "speech": "codec"})
    cfg = RunConfig(root=ROOT, runs_dir=tmp_path / "runs", offline=True, speech=SpeechConfig(engine="codec"), repeats=2,
                    judges=False, test_mode="speech", scenario_ids=("clinic.book-checkup",),
                    sweep=("as_written", "snr:20", "loss:10"), baseline="none")
    orch = Orchestrator(proj, cfg, log=lambda *_: None)
    assert [(p.conditions, p.repeat) for p in orch.plan()] == [("as_written", 1), ("as_written", 2), ("snr:20", 1), ("loss:10", 1)]
    decision = asyncio.run(orch.run())
    run_dir = tmp_path / "runs" / decision["run_id"]
    assert len(decision["calls"]) == 4 and decision["coverage"]["planned_calls"] == 2     # ladder calls don't count
    first = decision["calls"][0]["test_id"]
    events = [json.loads(x) for x in (run_dir / "calls" / first / "events.jsonl").read_text("utf-8").splitlines()]
    assert any(e["type"] == "caller_heard" for e in events)
    assert all(schema_errors("trace_event", e) == [] for e in events)
    v = json.loads((run_dir / "calls" / first / "verdict.json").read_text("utf-8"))
    assert v["speech"]["voices"] == "synthetic" and "fair" in v["speech"]
    assert schema_errors("call_verdict", v) == []
    assert set(decision["ladder"]) == {"noise", "loss"}
    assert schema_errors("run_decision", {k: x for k, x in decision.items() if not k.startswith("_")}) == []
    report = readiness.build(run_dir, titles={})
    assert [c["title"] for c in report["conditions"]] == ["As written"]          # ladder steps have their own chart
    assert report["speech"]["ladder"]["noise"]["levels"][0]["level"] == 20
