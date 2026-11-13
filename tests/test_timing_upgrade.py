"""Timing upgrade: plain vs tool answers, the full wait, the p95 range, coughs and bangs, repeated
questions, cut off in a pause, and where answer time went from the agent's logs.

Fixed timelines, so every number can be checked by hand."""

from __future__ import annotations

import numpy as np

from vatf import metrics, stats
from vatf.audio.noise import SOUNDS, sound_effect
from vatf.audio.pcm import SPEECH_DBFS
from vatf.conditions import resolve
from vatf.latency import run_timing, split_waits, stage_times, turn_latencies
from vatf.loader import schema_errors


def ev(seq, type_, data, turn=None, t=0):
    return {"seq": seq, "t_ms": t, "source": "x", "type": type_, "data": data, "turn": turn}


def audio(a, b):
    return {"file": "audio/call.wav", "start_ms": a, "end_ms": b}


# A call: a plain answer (500 ms), then a tool answer that says "let me check" after 400 ms and gives the
# real answer after the tool answered (full wait 2600 ms), then a pause the agent jumps into.
CALL = [
    ev(1, "agent_turn", {"heard_text": "Hello, how can I help you today", "audio": audio(200, 1800)}, 1),
    ev(2, "caller_turn", {"text": "Hi there", "audio": audio(2000, 2600)}, 2),
    ev(3, "agent_turn", {"heard_text": "Hi, what can I do for you", "audio": audio(3100, 4000), "latency_ms": 500}, 2),
    ev(4, "caller_turn", {"text": "Is Tuesday free", "audio": audio(5000, 6000)}, 3),
    ev(5, "tool_request", {"call_id": "c1", "tool": "check_availability", "args": {}}, 3, t=6300),
    ev(6, "agent_turn", {"heard_text": "Let me check", "audio": audio(6400, 7000), "latency_ms": 400}, 3),
    ev(7, "tool_response", {"call_id": "c1", "tool": "check_availability", "ok": True}, 3, t=8000),
    ev(8, "agent_turn", {"heard_text": "Tuesday at nine is free", "audio": audio(8600, 9800), "went_on": True}, 3),
    ev(9, "caller_turn", {"text": "I'd like to book it, under the name Priya Raman please",
                          "audio": audio(10500, 14000), "pause_ms": 1200, "pause": {"start_ms": 11800, "end_ms": 13000}}, 4),
    ev(10, "agent_turn", {"heard_text": "Sorry, what name", "audio": audio(12900, 13600), "latency_ms": -1100}, 4),
    ev(11, "caller_sound", {"kind": "noise", "sound": "cough", "agent_kept_talking": False,
                            "audio": audio(13200, 13700), "agent_turn": 10}, 4),
    ev(12, "call_ended", {"ended_by": "caller", "reason": "x"}, 4, t=15000),
]


def test_plain_and_tool_answers_are_timed_apart_with_the_full_wait():
    rows = turn_latencies(CALL)
    plain, tool = rows[0], rows[1]
    assert plain["tool"] is False and plain["ms"] == 500
    assert tool["tool"] is True and tool["ms"] == 400
    # The caller stopped at 6000; the first sound after the tool answered (8000) is 8600.
    assert tool["full_wait_ms"] == 2600
    split = split_waits(rows)
    assert split["plain"] == {"n": 1, "p50_ms": 500, "p95_ms": 500}
    assert split["tool"]["n"] == 1 and split["tool"]["full_p50_ms"] == 2600


def test_cut_off_in_a_pause_is_its_own_number_not_talked_over():
    q = metrics.call_quality(CALL, "voice", {})
    assert q["paused_cut_off"] == {"value": 100.0, "n": 1, "hits": 1}
    assert q["talked_over"]["value"] is False        # the -1100 ms answer was the pause, counted above
    assert q["noise_kept"] == {"value": 0.0, "n": 1, "kept": 0}


def test_repeated_questions_counted_but_not_when_asked_to_repeat():
    loop = [
        ev(1, "agent_turn", {"heard_text": "Can I have your full name and date of birth please"}, 1),
        ev(2, "caller_turn", {"text": "Priya Raman, fourth of May 1990"}, 2),
        ev(3, "agent_turn", {"heard_text": "Can I have your full name and date of birth, please?"}, 2),
        ev(4, "caller_turn", {"text": "I just told you"}, 3),
        ev(5, "agent_turn", {"heard_text": "Could I have your full name and date of birth please"}, 3),
    ]
    reps = metrics.repeated_turns(loop)
    assert [r["seq"] for r in reps] == [3, 5] and reps[0]["first_seq"] == 1
    q = metrics.call_quality(loop, "text", {})
    assert q["repeated_question"]["value"] is True and q["repeated_question"]["n"] == 2

    asked = [loop[0], ev(2, "caller_turn", {"text": "Sorry, can you say that again?"}, 2), loop[2]]
    assert metrics.repeated_turns(asked) == []
    # A caller who asks the same thing again gets the same answer, rightly.
    same = [ev(1, "caller_turn", {"text": "What time do you open on Saturday"}, 1),
            ev(2, "agent_turn", {"heard_text": "We open at nine on Saturday morning"}, 1),
            ev(3, "caller_turn", {"text": "What time do you open on Saturday?"}, 2),
            ev(4, "agent_turn", {"heard_text": "We open at nine on Saturday morning"}, 2)]
    assert metrics.repeated_turns(same) == []
    # Short turns repeat naturally.
    short = [ev(1, "agent_turn", {"heard_text": "Okay, anything else?"}, 1), ev(2, "caller_turn", {"text": "no"}, 2),
             ev(3, "agent_turn", {"heard_text": "Okay, anything else?"}, 2)]
    assert metrics.repeated_turns(short) == []


def test_noise_sounds_are_levelled_repeatable_and_on_in_hard():
    for kind in SOUNDS:
        a, b = sound_effect(kind, 16000, "t1"), sound_effect(kind, 16000, "t1")
        assert np.array_equal(a, b) and 0.2 < len(a) / 16000 < 0.7
        rms = 20 * np.log10(np.sqrt(np.mean(a.astype(np.float64) ** 2)))
        assert abs(rms - SPEECH_DBFS) < 0.5
    hard = resolve({}, {}, preset="hard", seed="x")
    assert hard.behaviour.noises and "noises" in hard.summary()["behaviours"]
    picks = sum(hard.wants_noise() for _ in range(10))
    assert picks <= hard.behaviour.noise_max


def test_p95_range_is_honest_about_few_answers():
    assert stats.percentile_range(list(range(1, 11)), 95) == (8, None)        # at least 8, maybe slower
    low, high = stats.percentile_range(list(range(1, 201)), 95)
    assert low < 190 < high


def test_stage_times_from_the_agents_logs():
    events = [ev(1, "platform_event", {"kind": "log", "text": "stt_ms=120 llm_latency=640 tts_first=210"}),
              ev(2, "platform_event", {"kind": "log", "text": "llm_latency=900"}),
              ev(3, "platform_event", {"kind": "log", "text": "nothing here"})]
    prof = {"logs": {"timing": {"stt": r"stt_ms=(?P<ms>\d+)", "llm": r"llm_latency=(?P<ms>\d+)",
                                "tts": r"tts_first=(?P<ms>\d+)"}}}
    assert stage_times(events, prof) == {"stt": [120], "llm": [640, 900], "tts": [210]}
    assert stage_times(events, {}) == {}


def test_run_timing_lists_moments_to_listen_to():
    rows = turn_latencies(CALL)
    v = {"test_id": "t1", "scenario_id": "s", "status": "PASS",
         "metrics": {"latency_turns": rows, "stages": {"llm": [640, 900]},
                     "quality": {"barge_in_stop_ms": {"value": 1400, "each": [1400, 300], "seqs": [10, 20]}}}}
    t = run_timing([v])
    assert [w["ms"] for w in t["slowest"]] == [500, 400] and t["slowest"][1]["tool"] is True
    assert t["slow_stops"] == [{"test_id": "t1", "scenario_id": "s", "seq": 10, "ms": 1400}]
    assert t["stages"]["llm"]["p95_ms"] == 900 and t["stages"]["llm"]["title"] == "AI model"
    assert t["p95_range"]["high_ms"] is None


def test_run_quality_shows_p90_without_a_second_rule():
    timed = list(range(100, 1100, 10))           # 100 answers: enough for p90 and p99
    v = {"test_id": "t", "status": "PASS", "metrics": {"latency_ms": timed,
                                                        "quality": {"response_p90_ms": {"value": 3000, "band": "bad"}}}}
    rows = {r["id"]: r for r in metrics.run_quality([v], {})}
    assert rows["response_p90_ms"]["band"] is None and "Pass rules" in rows["response_p90_ms"]["detail"]["note"]


def test_schemas_take_the_new_fields():
    from pathlib import Path
    import yaml
    prof = yaml.safe_load((Path(__file__).resolve().parent.parent / "examples/clinic/profile.yaml").read_text())
    prof.setdefault("logs", {"source": "none"})["timing"] = {"llm": "x=(?P<ms>\\d+)"}
    assert schema_errors("client_profile", prof) == []
    rows = turn_latencies(CALL)
    q = metrics.with_bands(metrics.call_quality(CALL, "voice", {}), {})
    v = {"schema_version": "0.1", "kind": "call_verdict", "test_id": "t", "run_id": "r", "scenario_id": "s",
         "repeat": 1, "status": "PASS", "checks": [],
         "metrics": {"turns": 3, "duration_ms": 15000, "latency_turns": rows, "quality": q, "stages": {"llm": [640]}}}
    errs = [e for e in schema_errors("call_verdict", v) if "metrics" in e]
    assert errs == []
