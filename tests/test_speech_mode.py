"""Speech mode: every test once under several caller conditions, side by side, and how much of the
caller the agent understood (when it reports what it heard)."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from pathlib import Path

import pytest

from vatf import readiness
from vatf.conditions import ACCENT_PREFIX, PRESETS, SPEECH_DEFAULT, known, resolve, sweep_list, title
from vatf.loader import load_project, schema_errors
from vatf.logs import heard_in_log
from vatf.metrics import by_condition, call_quality
from vatf.orchestrator import Orchestrator, RunConfig
from vatf.speech import SpeechConfig

ROOT = Path(__file__).resolve().parent.parent
CODEC = SpeechConfig(engine="codec")


def test_speech_conditions_change_one_thing_each():
    persona = {"speech": {"tts_voice": "en_US-amy-medium", "speaking_rate": 1.0}}
    quiet = resolve(persona, {}, preset="quiet")
    assert quiet.volume_db == -12 and quiet.phone and quiet.noise == "none"
    fast = resolve(persona, {}, preset="fast")
    assert fast.speaking_rate == 1.3 and not fast.phone
    street = resolve(persona, {}, preset="street")
    assert (street.noise, street.snr_db) == ("street", 5)
    lossy = resolve(persona, {}, preset="lossy")
    assert lossy.phone and lossy.packet_loss_pct == 5
    # An accent condition wins over the persona's own voice.
    indian = resolve(persona, {}, preset="accent:indian")
    assert indian.accent == "indian" and indian.voice == "en_GB-vctk-medium#p248"
    assert indian.summary()["title"] == "Indian English accent"
    assert known("accent:indian") and not known("accent:martian") and not known("loud")
    with pytest.raises(ValueError, match="unknown caller conditions"):
        resolve(persona, {}, preset="accent:martian")


def test_the_dashboard_list_of_conditions():
    items = sweep_list()
    ids = [c["id"] for c in items]
    assert ids[:2] == ["as_written", "phone"] and "hard" not in ids          # "hard" mixes many changes
    assert {c["id"] for c in items if c["default"]} == set(SPEECH_DEFAULT)
    assert all(c["title"] and c["about"] for c in items)
    assert sum(c["group"] == "accent" for c in items) == len([i for i in ids if i.startswith(ACCENT_PREFIX)]) >= 10
    assert all(title(k) == PRESETS[k]["title"] for k in PRESETS)


def test_heard_text_from_a_log_line():
    rx = re.compile(r"User said: (?P<text>.+)")
    assert heard_in_log(rx, '2026-11-05 10:00:01 INFO User said: "I need a check-up"') == "I need a check-up"
    assert heard_in_log(rx, "INFO agent replied: hello") is None
    assert heard_in_log(re.compile(r"STT final \[(.*?)\]"), "STT final [book me in]") == "book me in"


def test_caller_words_misheard_by_the_agent():
    ev = [{"type": "caller_turn", "data": {"text": "I need a check-up on Tuesday"}, "seq": 1, "t_ms": 0},
          {"type": "caller_turn", "data": {"text": "My date of birth is 4 May 1980"}, "seq": 3, "t_ms": 900},
          {"type": "agent_heard", "data": {"text": "I need a checkup on Thursday", "source": "agent"}, "seq": 2, "t_ms": 500},
          {"type": "agent_heard", "data": {"text": "my date of birth is four may 1980", "source": "log"}, "seq": 4, "t_ms": 1500},
          {"type": "agent_turn", "data": {"heard_text": "Thanks, anything else?"}, "seq": 5, "t_ms": 2000, "turn": 2},
          # The goodbye the agent never answered is left out, not counted as missed words.
          {"type": "caller_turn", "data": {"text": "No that's all, bye"}, "seq": 6, "t_ms": 3000}]
    q = call_quality(ev, "voice")["caller_wer"]
    # "Tuesday" -> "Thursday" and "4" -> "four" are the two errors in 14 words.
    assert q == {"value": round(100 * 2 / 14, 1), "n": 14, "errors": 2}
    assert "caller_wer" not in call_quality(ev[:2], "voice")        # nothing reported: no number made up


def test_what_the_agent_heard_is_masked_like_the_caller():
    from vatf.contracts.models import TraceEvent
    from vatf.evidence import mask_events
    profile = {"pii": {"mask_patterns": [{"name": "phone", "regex": r"\d{10}"}]},
               "tools": [{"name": "book", "pii_args": ["full_name"]}]}
    facts = {"full_name": "Maria Lopez"}
    ev = [TraceEvent(1, 0, "caller", "caller_turn", {"text": "I'm Maria Lopez, 9876543210"}, 1),
          TraceEvent(2, 9, "platform", "agent_heard", {"text": "im maria lopez 9876543210", "source": "agent"}, None)]
    out = mask_events(ev, profile, facts, "salt")
    assert out[0].data["text"] == "I'm [full_name], [phone]"
    assert out[1].data["text"] == "im [full_name] [phone]"


def _verdict(preset, status, ent=None):
    v = {"test_id": f"t-{preset}-{status}", "scenario_id": "s1", "status": status,
         "conditions": {"preset": preset, "title": title(preset), "label": "phone line" if preset == "phone" else "clean line"},
         "checks": [], "metrics": {"latency_ms": [700]}}
    if ent:
        v["checks"].append({"type": "entity_fidelity", "result": ent})
    return v


def test_results_by_condition_group_a_sweep_by_condition():
    rows = by_condition([_verdict("phone", "FAIL", "FAIL"), _verdict("as_written", "PASS", "PASS"),
                         _verdict("phone", "PASS", "PASS")])
    assert [r["key"] for r in rows] == ["as_written", "phone"]        # the clean result first
    phone = rows[1]
    assert phone["label"] == "Phone line" and (phone["passed"], phone["failed"]) == (1, 1)
    assert (phone["details_right"], phone["details_checked"]) == (1, 2)
    assert phone["failed_calls"] == ["t-phone-FAIL"]
    # Without a sweep (one run preset), calls still group by what the line sounded like.
    one = by_condition([_verdict("as_written", "PASS")])
    assert one[0]["label"] == "clean line"


def test_plan_runs_every_test_under_every_condition():
    proj = load_project(ROOT / "examples" / "clinic", "profile-voice.yaml")
    cfg = RunConfig(root=ROOT, offline=True, speech=CODEC, repeats=2, test_mode="speech",
                    scenario_ids=("clinic.book-checkup",), sweep=("as_written", "phone", "accent:indian"))
    plan = Orchestrator(proj, cfg, log=lambda *_: None).plan()
    assert [(p.conditions, p.repeat) for p in plan] == [("as_written", 1), ("as_written", 2), ("phone", 1),
                                                         ("phone", 2), ("accent:indian", 1), ("accent:indian", 2)]
    text = load_project(ROOT / "examples" / "clinic", "profile.yaml")
    plan = Orchestrator(text, RunConfig(root=ROOT, offline=True, repeats=1, sweep=("phone",),
                                        scenario_ids=("clinic.book-checkup",)), log=lambda *_: None).plan()
    assert [p.conditions for p in plan] == [None]                    # text runs have no caller conditions


def test_a_speech_mode_run_compares_conditions_side_by_side(tmp_path):
    """The zoo clinic agent (rules brain, test-tone speech) under three conditions. It reports what it
    heard, so the caller's words it misheard are measured too."""
    shutil.copytree(ROOT / "examples" / "clinic", tmp_path / "clinic", ignore=shutil.ignore_patterns("runs"))
    proj = load_project(tmp_path / "clinic", "profile-voice.yaml").with_agent_options(
        {"brain": "rules", "bugs": "", "speech": "codec"})
    cfg = RunConfig(root=ROOT, runs_dir=tmp_path / "runs", offline=True, speech=CODEC, repeats=1, judges=False,
                    test_mode="speech", scenario_ids=("clinic.book-checkup",),
                    sweep=("as_written", "phone", "accent:indian"), baseline="none")
    orch = Orchestrator(proj, cfg, log=lambda *_: None)
    decision = asyncio.run(orch.run())
    run_dir = tmp_path / "runs" / decision["run_id"]
    info = json.loads((run_dir / "run.json").read_text("utf-8"))
    assert info["sweep"] == ["as_written", "phone", "accent:indian"] and len(info["plan"]) == 3
    assert [p["conditions"] for p in info["plan"]] == ["as_written", "phone", "accent:indian"]
    verdicts = [json.loads((run_dir / "calls" / c["test_id"] / "verdict.json").read_text("utf-8")) for c in decision["calls"]]
    assert [v["conditions"]["preset"] for v in verdicts] == ["as_written", "phone", "accent:indian"]
    for v in verdicts:
        assert schema_errors("call_verdict", v) == []
        events = [json.loads(x) for x in (run_dir / "calls" / v["test_id"] / "events.jsonl").read_text("utf-8").splitlines()]
        assert all(schema_errors("trace_event", e) == [] for e in events)
        heard = [e for e in events if e["type"] == "agent_heard"]
        assert heard and all(e["data"]["source"] == "agent" for e in heard)
        assert v["metrics"]["quality"]["caller_wer"]["n"] > 0
    rows = decision["by_condition"]
    assert [r["label"] for r in rows] == ["As written", "Phone line", "Indian English accent"]
    assert all(r["calls"] == 1 and r["understood_wer"] is not None for r in rows)
    assert schema_errors("run_decision", {k: v for k, v in decision.items() if not k.startswith("_")}) == []

    report = readiness.build(run_dir, titles={"clinic.book-checkup": "Book a check-up"})
    assert report["mode"]["id"] == "speech"
    conds = report["conditions"]
    assert [c["title"] for c in conds] == ["As written", "Phone line", "Indian English accent"]
    assert all(c["status"] in ("handled", "struggled", "broke") and c["result"].endswith("passed") for c in conds)
    speech_row = next(r for r in report["scorecard"] if r["area"] == "speech")
    handled = sum(c["status"] == "handled" for c in conds)
    assert speech_row["value"] == f"Handled {handled} of 3 caller conditions"
    assert any(line.startswith(f"The agent handled {handled} of 3 caller conditions") for line in report["summary"])
