"""Phase 5: harder callers (conditions, accents, phone line, behaviours) and call quality metrics.

Unit tests on fixed audio and fixed timelines, so every number can be checked by hand, plus voice
runs end to end with the tone codec against the zoo agents.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import numpy as np
import pytest

from vatf import metrics
from vatf.accents import ACCENTS, caller_voice, speaker_id, split_voice
from vatf.audio.line import LineEffects, band_taps
from vatf.audio.noise import make_noise
from vatf.checks import barge_in_checks
from vatf.conditions import PRESETS, judge_note, resolve, split_line
from vatf.contracts.models import Result, TraceEvent
from vatf.decision import LocalDecisionEngine
from vatf.loader import load_project, schema_errors
from vatf.orchestrator import Orchestrator, RunConfig
from vatf.speech import SpeechConfig

ROOT = Path(__file__).resolve().parent.parent
SR = 16000


# ---- conditions -------------------------------------------------------------------------------

PERSONA = {"id": "p", "speech": {"accent": "indian", "speaking_rate": 1.1, "volume_db": -6},
           "behaviour": {"backchannels": True, "pause_ms": 900}}
SCENARIO = {"id": "s", "audio": {"noise": "office", "snr_db": 20, "line": {"codec": "phone"}}}


def test_conditions_join_persona_test_and_run_preset():
    c = resolve(PERSONA, SCENARIO, seed="t-1")
    assert c.voice == "en_GB-vctk-medium#p248" and c.accent == "indian" and c.speaking_rate == 1.1
    assert (c.noise, c.snr_db, c.phone, c.packet_loss_pct) == ("office", 20, True, 0)
    assert c.behaviour.backchannels and not c.behaviour.interrupts and c.behaviour.pause_ms == 900
    assert c.label() == "office noise 20 dB, phone line, Indian English, voice -6 dB"

    hard = resolve(PERSONA, SCENARIO, preset="hard", seed="t-1")
    # The preset's line and noise win over the test's; it turns behaviours on, never off.
    assert (hard.noise, hard.snr_db, hard.phone, hard.packet_loss_pct) == ("street", 8, True, 3)
    assert hard.behaviour.interrupts and hard.behaviour.backchannels and hard.behaviour.pauses_mid_sentence
    assert hard.summary()["behaviours"] == ["interrupts", "backchannels", "pauses_mid_sentence"]
    with pytest.raises(ValueError):
        resolve(PERSONA, SCENARIO, preset="tornado")


def test_own_voice_beats_accent_and_default():
    assert caller_voice({"speech": {"tts_voice": "en_US-amy-medium", "accent": "scottish"}}, "d") == "en_US-amy-medium"
    assert caller_voice({"speech": {"accent": "scottish"}}, "d") == "en_GB-alba-medium"
    assert caller_voice({}, "en_US-lessac-medium") == "en_US-lessac-medium"


def test_random_choices_replay_exactly_from_the_call_seed():
    p = {"behaviour": {"goes_quiet": True, "interrupts": True, "fillers": True, "pauses_mid_sentence": True}}
    a, b = resolve(p, {}, seed="t-42"), resolve(p, {}, seed="t-42")
    assert a.quiet_turn == b.quiet_turn and a.quiet_turn in (2, 3, 4)
    line = "I would like to book a check-up with Dr Patel tomorrow, please"
    assert [a.spoken(line) for _ in range(5)] == [b.spoken(line) for _ in range(5)]
    assert [a.wants_interrupt() for _ in range(6)] == [b.wants_interrupt() for _ in range(6)]
    assert a.interruptions <= a.behaviour.interrupt_max


def test_a_line_splits_where_a_person_would_pause():
    assert split_line("My name is Maria Lopez, and I was born in 1985") == ("My name is Maria Lopez,", "and I was born in 1985")
    first, second = split_line("one two three four five six")
    assert (first, second) == ("one two three", "four five six")


def test_judges_are_told_the_conditions():
    note = judge_note(resolve(PERSONA, SCENARIO, preset="hard").summary())
    assert note.startswith("Caller conditions: street noise 8 dB, phone line, 3% packets lost, Indian English")
    assert "cuts in on long answers" in note and "not the agent's fault" in note


def test_every_preset_resolves_and_has_words_for_the_ui():
    for key, p in PRESETS.items():
        assert p["title"] and p["about"]
        resolve({}, {}, preset=key)


# ---- accents --------------------------------------------------------------------------------

def test_accent_voices_name_a_piper_voice_and_speaker():
    for label, voice in ACCENTS.values():
        name, speaker = split_voice(voice)
        assert name.count("-") == 2 and name.split("-")[0] in ("en_US", "en_GB"), voice
    assert split_voice("en_GB-vctk-medium#p248") == ("en_GB-vctk-medium", "p248")
    assert split_voice("en_US-amy-medium") == ("en_US-amy-medium", None)


def test_speaker_lookup_by_name_case_suffix_or_number():
    ids = {"p225": 0, "p248": 7, "p_ASI": 3}
    assert speaker_id("p248", ids, "v") == 7
    assert speaker_id("P248", ids, "v") == 7
    assert speaker_id("ASI", ids, "v") == 3
    assert speaker_id("7", ids, "v") == 7
    assert speaker_id(None, ids, "v") is None
    with pytest.raises(ValueError, match="no speaker 'p999'.*p225"):
        speaker_id("p999", ids, "v")


def test_unknown_accent_is_a_project_problem(tmp_path):
    import shutil

    from vatf.loader import ProjectError

    shutil.copytree(ROOT / "examples" / "clinic", tmp_path / "clinic", ignore=shutil.ignore_patterns("runs"))
    (tmp_path / "clinic" / "personas" / "odd.yaml").write_text(
        'schema_version: "0.1"\nkind: persona\nid: odd\ndescription: x\nspeech: {accent: martian}\n', "utf-8")
    with pytest.raises(ProjectError, match="unknown accent 'martian'"):
        load_project(tmp_path / "clinic")


# ---- phone line -----------------------------------------------------------------------------

def tone(freq, seconds=1.0, sr=SR):
    return (0.3 * np.sin(2 * np.pi * freq * np.arange(int(seconds * sr)) / sr)).astype(np.float32)


def run_frames(fx, x, frame=320):
    return np.concatenate([fx.process(x[i:i + frame]) for i in range(0, len(x) - frame + 1, frame)])


def rms(x):
    return float(np.sqrt(np.mean(np.square(x[2000:], dtype=np.float64))))


def test_phone_line_keeps_the_telephone_band_only():
    for freq, low, high in ((1000, 0.85, 1.1), (100, 0.0, 0.15), (6000, 0.0, 0.1)):
        x = tone(freq)
        y = run_frames(LineEffects(SR, phone=True), x)
        assert low <= rms(y) / rms(x) <= high, (freq, rms(y) / rms(x))


def test_frame_by_frame_filtering_equals_one_pass():
    """The filter keeps its state between frames, so 20 ms frames add no clicks at their edges."""
    x = np.random.default_rng(1).standard_normal(SR).astype(np.float32) * 0.1
    whole = np.convolve(np.concatenate([np.zeros(128), x]), band_taps(SR), mode="valid")
    framed = run_frames(LineEffects(SR, phone=True), x)
    # Only mu-law rounding differs (under 1% of full scale for this level).
    assert np.max(np.abs(framed - whole[:len(framed)])) < 0.01


def _arrived(fx: LineEffects, n: int) -> list[bool]:
    out = []
    for _ in range(n):
        before = fx.lost_frames
        fx.process(np.ones(320, np.float32))
        out.append(fx.lost_frames == before)
    return out


def test_packet_loss_rate_bursts_and_replay():
    out = _arrived(LineEffects(SR, packet_loss_pct=5, seed="t-1"), 20000)
    lost = out.count(False) / len(out)
    assert 0.04 <= lost <= 0.06, lost
    runs, n = [], 0
    for ok in out + [True]:
        if not ok:
            n += 1
        elif n:
            runs.append(n)
            n = 0
    assert 1.6 <= np.mean(runs) <= 2.4, np.mean(runs)     # bursts of about two frames
    assert _arrived(LineEffects(SR, packet_loss_pct=5, seed="t-1"), 500) == out[:500]
    assert not LineEffects(SR).active


def test_lost_frames_are_concealed_not_zeroed():
    """A gap replays the last frame that arrived, fading from 10 ms to silence at 60 ms (G.711
    Appendix I), so a line with background noise never drops to digital silence."""
    fx = LineEffects(SR, packet_loss_pct=50, seed="plc")
    gaps = []
    for i in range(4000):
        x = np.full(320, 0.1 if i % 2 else -0.1, np.float32)
        before = fx.lost_frames
        y = fx.process(x)
        if fx.lost_frames > before:
            gaps.append((fx._run, y))
    first = [y for run, y in gaps if run == 1]
    assert first and all(np.allclose(np.abs(y[:80]), 0.1) for y in first)   # first 10 ms at full level
    assert all(abs(y[-1]) < 0.1 for y in first)                              # then fading
    assert all(not np.any(y) for run, y in gaps if run >= 4)                 # silent after 60 ms
    assert all(np.any(y) for run, y in gaps if run <= 2)                      # never silent early in a gap


def test_tv_noise_is_levelled_and_repeatable():
    a, b = make_noise("tv", 3, SR, "x"), make_noise("tv", 3, SR, "x")
    assert np.array_equal(a, b) and abs(np.sqrt(np.mean(a.astype(np.float64) ** 2)) - 1) < 0.01


# ---- metrics on a fixed timeline -----------------------------------------------------------------

def ev(seq, type_, data, turn=None, t=0):
    return {"seq": seq, "t_ms": t, "source": "x", "type": type_, "data": data, "turn": turn}


def audio(a, b):
    return {"file": "audio/call.wav", "start_ms": a, "end_ms": b}


TIMELINE = [
    ev(1, "call_started", {"test_id": "t", "mode": "voice", "conditions": {"label": "phone line", "behaviours": ["backchannels"]}}),
    ev(2, "agent_turn", {"heard_text": "Hello, clinic, how can I help", "audio": audio(400, 2400), "answered_at_ms": 100}, 1),
    ev(3, "caller_turn", {"text": "I want to book", "audio": audio(3000, 4000)}, 2),
    ev(4, "agent_turn", {"heard_text": "Sure, let me check the diary for you", "audio": audio(4600, 7000), "latency_ms": 600,
                         "platform_transcript": "Sure, let me check the diary for you"}, 2),
    ev(5, "caller_sound", {"kind": "backchannel", "text": "Mm-hm.", "agent_kept_talking": True, "audio": audio(5500, 5900)}, 2),
    ev(6, "agent_turn", {"heard_text": "Okay I found a slot at nine", "audio": audio(11000, 13000), "went_on": True,
                         "platform_transcript": "Okay, I found a slot at nine"}, 2),     # a 4 s pause mid-answer
    ev(7, "caller_turn", {"text": "Nine is fine thanks", "audio": audio(14000, 15500)}, 3),
    ev(8, "agent_turn", {"heard_text": "Great, booking it", "audio": audio(15200, 16500), "latency_ms": -300,
                         "interrupted": True, "stopped_after_ms": 700, "barge_in_at_ms": 15800, "barge_in_auto": True}, 3),
    ev(9, "caller_turn", {"text": "Sorry to cut in.", "audio": audio(15800, 16600), "barge_in": True, "barge_in_auto": True}, 3),
    ev(10, "agent_turn", {"heard_text": "Go ahead", "audio": audio(16400, 17000), "latency_ms": -200}, 3),
    ev(11, "tool_request", {"call_id": "c1", "tool": "book_appointment", "args": {}}, 3),
    ev(12, "tool_response", {"call_id": "c1", "tool": "book_appointment", "ok": True}, 3),
    ev(13, "caller_sound", {"kind": "silence", "text": "", "quiet_ms": 12000, "agent_spoke_after_ms": None,
                            "audio": audio(17000, 29000)}, 3),
    ev(14, "call_ended", {"ended_by": "caller", "reason": "x"}, 3, t=30000),
]
GOAL = {"outcome": {"expected": "success", "action": {"tool": "book_appointment"}}}


def test_call_quality_numbers_by_hand():
    q = metrics.call_quality(TIMELINE, "voice", GOAL)
    assert q["ttfw_ms"]["value"] == 300                                  # 400 - 100
    assert q["response_p90_ms"]["value"] == 600 and q["response_p90_ms"]["n"] == 1   # negative ones don't count
    assert q["longest_silence_ms"]["value"] == 4000                     # 11000 - 7000, the pause mid-answer
    # -300 ms on turn 3 is the agent starting over the caller; -200 ms answers the caller's cut-in, so it's left out.
    assert q["talked_over"] == {"value": True, "n": 1}
    assert q["barge_in_stop_ms"]["value"] == 700
    assert q["backchannel_kept"]["value"] == 100.0 and q["backchannel_kept"]["n"] == 1
    assert q["checkin_ms"]["value"] is None and q["checkin_ms"]["never"] is True
    agent_ms, caller_ms = 2000 + 2400 + 2000 + 1300 + 600, 1000 + 1500 + 800
    assert q["talk_share"]["value"] == round(100 * agent_ms / (agent_ms + caller_ms), 1)
    words = 6 + 8 + 7 + 3 + 2
    assert q["agent_wpm"]["value"] == round(words / (agent_ms / 60000))
    assert q["heard_wer"]["value"] == 0.0 and q["heard_wer"]["n"] == 15   # punctuation is not a word error
    assert q["turns_to_goal"]["value"] == 2                              # caller turns 2 and 3
    assert q["escalated"]["value"] is False and q["duration_s"]["value"] == 30.0


def test_bands_per_call():
    b = metrics.with_bands(metrics.call_quality(TIMELINE, "voice", GOAL), {})
    assert b["ttfw_ms"]["band"] == "good" and b["longest_silence_ms"]["band"] == "warn"
    assert b["talked_over"]["band"] == "bad" and b["checkin_ms"]["band"] == "bad"     # never checked in
    assert b["turns_to_goal"]["band"] is None                                        # no default band
    own = metrics.with_bands(metrics.call_quality(TIMELINE, "voice", GOAL),
                             {"metrics": {"longest_silence_ms": {"good": 5000, "warn": 8000}}})
    assert own["longest_silence_ms"]["band"] == "good"


def test_text_calls_get_only_text_metrics():
    q = metrics.call_quality([e for e in TIMELINE if e["type"] != "caller_sound"], "text", GOAL)
    assert set(q) == {"duration_s", "escalated", "turns_to_goal"}


def test_word_error_rate():
    assert metrics.wer("book it for nine", "book it for nine") == (0, 4)
    assert metrics.wer("book it for nine", "look it for") == (2, 4)
    assert metrics.wer("", "hello") == (1, 0)


def test_escalation_is_a_handoff_or_a_transfer_tool():
    base = [ev(1, "call_ended", {"ended_by": "agent", "reason": "x"}, t=1000)]
    assert metrics.call_quality(base + [ev(2, "handoff", {})], "text")["escalated"]["value"] is True
    tool = ev(2, "tool_request", {"call_id": "1", "tool": "transfer_to_human", "args": {}})
    assert metrics.call_quality(base + [tool], "text")["escalated"]["value"] is True


# ---- run level: decision and schemas -----------------------------------------------------------

def verdict(tid, status, quality, latency=(), label="clean line"):
    return {"test_id": tid, "run_id": "r", "scenario_id": "s", "repeat": 1, "status": status, "checks": [],
            "metrics": {"latency_ms": list(latency), "latency_turns": [], "quality": quality},
            "conditions": {"label": label, "behaviours": []}}


def q(**values):
    return metrics.with_bands({k: {"value": v} for k, v in values.items()}, {})


PROFILE = {"id": "agent", "thresholds": {"repeats": 1, "latency_p50_ms": 5000, "latency_p95_ms": 9000}}
SCENARIOS = [{"id": "s", "criticality": "normal"}]


def test_run_quality_and_a_blocking_metric():
    vs = [verdict("a", "PASS", q(longest_silence_ms=2000, talked_over=False, response_p90_ms=700), [500, 700]),
          verdict("b", "PASS", q(longest_silence_ms=9000, talked_over=True, response_p90_ms=800), [800],
                  label="cafe noise 10 dB")]
    rows = {r["id"]: r for r in metrics.run_quality(vs, PROFILE)}
    assert rows["longest_silence_ms"]["value"] == 9000 and rows["longest_silence_ms"]["band"] == "bad"
    assert rows["talked_over"]["value"] == 50.0 and rows["talked_over"]["band"] == "bad"
    assert rows["response_p90_ms"]["value"] == 800 and rows["response_p90_ms"]["detail"]["turns"] == 3
    # 3 answers are too few for a p90 (10 needed): shown, never banded, so it can never block.
    assert rows["response_p90_ms"]["detail"]["too_few"] and rows["response_p90_ms"]["band"] is None

    d = LocalDecisionEngine().decide("r", vs, SCENARIOS, PROFILE)
    assert d["decision"] == "GO"                                   # nothing blocks by default
    warn = next(r for r in d["reasons"] if r["rule"] == "metric_warnings")
    assert "Longest silence from the agent 9,000 ms (bad)" in warn["message"] and warn["refs"] == ["b"]
    assert [c["label"] for c in d["by_condition"]] == ["clean line", "cafe noise 10 dB"]
    assert schema_errors("run_decision", d) == []

    blocking = {**PROFILE, "metrics": {"longest_silence_ms": {"blocks": True}}}
    d = LocalDecisionEngine().decide("r", vs, SCENARIOS, blocking)
    assert d["decision"] == "NO_GO" and "call_quality" in d["basis"]["decided_by"]
    reason = next(r for r in d["reasons"] if r["rule"] == "metric")
    assert reason["message"].startswith("Longest silence from the agent: 9,000 ms is in the bad band")
    assert schema_errors("run_decision", d) == []


def test_never_checking_in_is_bad_for_the_run():
    vs = [verdict("a", "PASS", {"checkin_ms": {"value": None, "never": True, "band": "bad"}})]
    row = metrics.run_quality(vs, PROFILE)[0]
    assert row["id"] == "checkin_ms" and row["band"] == "bad" and row["detail"]["note"] == "the agent never checked in"


def test_backchannels_and_cut_ins_pool_over_the_run():
    vs = [verdict("a", "PASS", {"backchannel_kept": {"value": 50.0, "n": 2, "kept": 1, "band": None},
                                 "barge_in_stop_ms": {"value": 1200, "n": 2, "each": [300, 1200], "band": None}}),
          verdict("b", "PASS", {"backchannel_kept": {"value": 100.0, "n": 2, "kept": 2, "band": None}})]
    rows = {r["id"]: r for r in metrics.run_quality(vs, PROFILE)}
    assert rows["backchannel_kept"]["value"] == 75.0 and rows["backchannel_kept"]["band"] == "warn"
    assert rows["barge_in_stop_ms"]["value"] == 1200 and rows["barge_in_stop_ms"]["detail"]["cut_ins"] == 2


def test_profile_metrics_section_is_checked_by_the_schema():
    base = load_project(ROOT / "examples" / "clinic").profile
    good = {**base, "metrics": {"ttfw_ms": {"good": 1000, "warn": 2000, "blocks": True}}}
    assert schema_errors("client_profile", good) == []
    assert schema_errors("client_profile", {**base, "metrics": {"nonsense": {}}})


# ---- barge-in check regression ----------------------------------------------------------------

def test_barge_in_check_counts_listened_turns_not_went_on_parts():
    """A "went on" part of the greeting used to shift the agent's turn count, so the check read the
    wrong turn. Now it reads the turn the cut-in was made on."""
    scenario = {"audio": {"barge_in": [{"agent_turn": 2, "offset_ms": 700, "say": "x", "stop_within_ms": 1000}]}}
    events = [TraceEvent(1, 0, "c", "agent_turn", {"heard_text": "hi"}, 1),
              TraceEvent(2, 0, "c", "agent_turn", {"heard_text": "more", "went_on": True}, 1),
              TraceEvent(3, 0, "c", "agent_turn", {"heard_text": "ans", "interrupted": True, "stopped_after_ms": 400,
                                                   "barge_in_plan": 1}, 2)]
    [r] = barge_in_checks(scenario, events)
    assert r.result == Result.PASS and "after 400 ms" in r.message
    old = [TraceEvent(e.seq, 0, "c", "agent_turn", {k: v for k, v in e.data.items() if k != "barge_in_plan"}, e.turn)
           for e in events]
    assert barge_in_checks(scenario, old)[0].result == Result.PASS     # older traces: counted the same way


# ---- end to end, tone codec -------------------------------------------------------------------

CODEC = SpeechConfig(engine="codec")


def run_voice(tmp_path, proj, **config):
    cfg = RunConfig(root=ROOT, runs_dir=tmp_path / "runs", offline=True, speech=CODEC, repeats=1, **config)
    orch = Orchestrator(proj, cfg, log=lambda *_: None)
    decision = asyncio.run(orch.run())
    calls = {}
    for c in decision["calls"]:
        folder = orch.store.call_dir(decision["run_id"], c["test_id"])
        events = [json.loads(x) for x in (folder / "events.jsonl").read_text("utf-8").splitlines()]
        calls[c["scenario_id"]] = (json.loads((folder / "verdict.json").read_text("utf-8")), events, folder)
    return decision, calls


def test_latency_reference_agent_under_a_phone_line_with_lost_packets(tmp_path):
    """The reference agent answers on its own clock; the phone line and packet loss must not change
    vatf's timing figures (still within 50 ms), and the conditions are written down with the call."""
    proj = load_project(ROOT / "examples" / "latency-ref")
    decision, calls = run_voice(tmp_path, proj, conditions="phone")
    verdict, events, folder = calls["latency-ref.three-answers"]
    started = events[0]
    assert started["data"]["conditions"]["label"] == "phone line, 1% packets lost"
    assert verdict["conditions"]["preset"] == "phone"
    measured = [r["ms"] for r in verdict["metrics"]["latency_turns"]]
    for got, want in zip(measured, [500, 900, 1300]):
        assert abs(got - want) <= 50, measured
    quality = verdict["metrics"]["quality"]
    assert abs(quality["ttfw_ms"]["value"] - 300) <= 60, quality["ttfw_ms"]
    assert quality["response_p99_ms"]["value"] == max(measured)
    assert schema_errors("call_verdict", verdict) == []
    assert all(schema_errors("trace_event", e) == [] for e in events)
    assert decision["by_condition"][0]["label"] == "phone line, 1% packets lost"
    assert schema_errors("run_decision", {k: v for k, v in decision.items() if not k.startswith("_")}) == []


def test_clinic_agent_with_a_caller_who_cuts_in_and_says_mm_hm(tmp_path):
    """The zoo clinic agent stops when talked over and talks on through a short "mm-hm"."""
    import shutil

    shutil.copytree(ROOT / "examples" / "clinic", tmp_path / "clinic", ignore=shutil.ignore_patterns("runs"))
    persona = tmp_path / "clinic" / "personas" / "calm-adult.yaml"
    persona.write_text(persona.read_text("utf-8").replace(
        "behaviour: {interrupts: false}",
        "behaviour: {interrupts: true, interrupt_after_ms: 1200, interrupt_chance: 1, interrupt_max: 1,\n"
        "            backchannels: true, backchannel_after_ms: 500, backchannel_chance: 1, backchannel_max: 2}"), "utf-8")
    proj = load_project(tmp_path / "clinic", "profile-voice.yaml").with_agent_options(
        {"brain": "rules", "bugs": "", "speech": "codec"})
    decision, calls = run_voice(tmp_path, proj, scenario_ids=("clinic.book-checkup",))
    verdict, events, _ = calls["clinic.book-checkup"]
    cut = [e for e in events if e["type"] == "agent_turn" and e["data"].get("barge_in_auto")]
    bcs = [e for e in events if e["type"] == "caller_sound" and e["data"]["kind"] == "backchannel"]
    assert len(cut) == 1 and cut[0]["data"]["stopped_after_ms"] <= 1000, [e["data"] for e in cut]
    caller_cut = next(e for e in events if e["type"] == "caller_turn" and e["data"].get("barge_in_auto"))
    assert caller_cut["turn"] == cut[0]["turn"]                  # the caller's own cut-in keeps the turn
    assert bcs and verdict["metrics"]["quality"]["backchannel_kept"]["n"] == len(bcs)
    assert verdict["metrics"]["quality"]["barge_in_stop_ms"]["value"] == cut[0]["data"]["stopped_after_ms"]
    assert schema_errors("call_verdict", verdict) == []
    assert all(schema_errors("trace_event", e) == [] for e in events)


def test_word_error_rate_writes_numbers_and_times_one_way():
    assert metrics.wer("Your reference is 606465 at 9:30am.", "your reference is 6 0 6, 4 6 5 at 9:30 a m") == (0, 7)
    assert metrics.wer("at 9:30 pm", "at 9 30 p.m.") == (0, 3)
    assert metrics.wer("I am here", "I am here") == (0, 3)


def test_a_caller_who_goes_quiet_and_pauses_on_a_noisy_phone_line(tmp_path):
    """Goes quiet once (the clinic agent never checks in: a real finding about the practice agent), pauses
    mid-sentence, says um: the call still completes and every event matches its schema."""
    import shutil

    shutil.copytree(ROOT / "examples" / "clinic", tmp_path / "clinic", ignore=shutil.ignore_patterns("runs"))
    persona = tmp_path / "clinic" / "personas" / "calm-adult.yaml"
    persona.write_text(persona.read_text("utf-8").replace(
        "behaviour: {interrupts: false}",
        "behaviour: {goes_quiet: true, quiet_ms: 4000, pauses_mid_sentence: true, pause_chance: 1, pause_ms: 700,\n"
        "            fillers: true, filler_chance: 1}"), "utf-8")
    proj = load_project(tmp_path / "clinic", "profile-voice.yaml").with_agent_options(
        {"brain": "rules", "bugs": "", "speech": "codec"})
    decision, calls = run_voice(tmp_path, proj, scenario_ids=("clinic.book-checkup",), conditions="noisy")
    verdict, events, _ = calls["clinic.book-checkup"]
    assert verdict["conditions"]["label"] == "cafe noise 10 dB"
    quiet = [e for e in events if e["type"] == "caller_sound" and e["data"]["kind"] == "silence"]
    assert len(quiet) == 1
    q = quiet[0]["data"]
    took = q["audio"]["end_ms"] - q["audio"]["start_ms"]
    assert q["agent_spoke_after_ms"] is None and abs(took - 4000) <= 300, q   # wall clock: allow for a busy machine
    assert verdict["metrics"]["quality"]["checkin_ms"]["never"] is True
    paused = [e for e in events if e["type"] == "caller_turn" and e["data"].get("pause_ms")]
    assert paused and all(e["data"]["spoken"].startswith(("Um,", "Uh,", "Hmm,")) for e in paused)
    assert all(schema_errors("trace_event", e) == [] for e in events)
    assert schema_errors("call_verdict", verdict) == []


def test_a_scripted_cut_in_on_the_greeting_is_written_down_and_answered(tmp_path):
    """audio.barge_in agent_turn 1 cuts in on the greeting. The caller's words used to be lost there."""
    import shutil

    shutil.copytree(ROOT / "examples" / "latency-ref", tmp_path / "ref")
    sc = tmp_path / "ref" / "scenarios" / "three-answers.yaml"
    sc.write_text(sc.read_text("utf-8").replace("checks:", "audio:\n  barge_in:\n    - {agent_turn: 1, offset_ms: 300, say: Hello there}\nchecks:"), "utf-8")
    decision, calls = run_voice(tmp_path, load_project(tmp_path / "ref"))
    verdict, events, _ = calls["latency-ref.three-answers"]
    greeting = next(e for e in events if e["type"] == "agent_turn")
    assert greeting["data"].get("interrupted") and greeting["data"]["barge_in_plan"] == 1
    cut = next(e for e in events if e["type"] == "caller_turn")
    assert cut["data"]["text"] == "Hello there" and cut["data"]["barge_in"] and cut["turn"] == 2
    check = next(c for c in verdict["checks"] if c["check_id"] == "agent-stops-when-interrupted")
    assert check["result"] in ("PASS", "FAIL") and "turn 1" in check["message"]


def test_wer_on_a_cut_turn_counts_only_what_was_spoken():
    full = "Thanks. Dr Patel has 9:30am or 11am on Tuesday 3 November. Which would you like?"
    heard = "Thanks. Dr Patel has 9:30 a m or 11"
    assert metrics.wer(full, heard, cut=True) == (0, 8)            # the unspoken rest is not an error
    assert metrics.wer(full, heard)[0] > 0                          # a full turn that lost its end still counts
    assert metrics.wer(full, "Thanks. Dr Patel has 9:30 a m or", cut=True) == (0, 7)
    assert metrics.wer("book it for Tuesday", "look it for", cut=True) == (1, 3)
