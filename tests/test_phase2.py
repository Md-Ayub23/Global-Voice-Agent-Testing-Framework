"""Phase 2 checks: voice mode.

The end-to-end tests run real calls over a WebSocket in real time, with the
tone codec standing in for speech (no model downloads, exact transcripts), so
they take a few minutes. The audio path, timing, latency, barge-in, noise and
read-back logic are the same as with Piper and Whisper.
"""

import asyncio
import json
from pathlib import Path

import numpy as np
import pytest

from vatf.audio.noise import make_noise
from vatf.audio.pcm import from_pcm16, read_wav, rms_dbfs, set_level, to_pcm16
from vatf.audio.recorder import Recorder
from vatf.audio.vad import speech_spans, trim
from vatf.checks import evaluate_check, spoken_numbers
from vatf.cli import EXIT_CHECK_VOICE
from vatf.contracts.models import AudioClip, Result, TraceEvent
from vatf.decision import LocalDecisionEngine
from vatf.latency import percentile, turn_latencies
from vatf.loader import load_project, merge, schema_errors
from vatf.orchestrator import Orchestrator, RunConfig
from vatf.speech import SpeechConfig
from vatf.speech.codec import CodecSTT, CodecTTS
from zoo.clinic.voice import speak_text
from zoo.voice.gateway import Endpointer

ROOT = Path(__file__).resolve().parent.parent
CLINIC = ROOT / "examples" / "clinic"
CODEC = SpeechConfig(engine="codec")
SR = 16000


def voice_project(**agent_options):
    return load_project(CLINIC, "profile-voice.yaml").with_agent_options(
        {"brain": "rules", "bugs": "", "speech": "codec", **agent_options})


def run(tmp_path, proj, **config):
    cfg = RunConfig(root=ROOT, runs_dir=tmp_path / "runs", offline=True, speech=CODEC, repeats=1, **config)
    orch = Orchestrator(proj, cfg, log=lambda *_: None)
    return orch, asyncio.run(orch.run())


def calls_of(orch, decision):
    out = {}
    for c in decision["calls"]:
        folder = orch.store.call_dir(decision["run_id"], c["test_id"])
        events = [json.loads(line) for line in (folder / "events.jsonl").read_text("utf-8").splitlines()]
        out[c["scenario_id"]] = (json.loads((folder / "verdict.json").read_text("utf-8")), events, folder)
    return out


def check(verdict, check_id):
    return next(c for c in verdict["checks"] if c["check_id"] == check_id)


# ---- end to end -----------------------------------------------------------------------------

@pytest.fixture(scope="module")
def clean_voice_run(tmp_path_factory):
    orch, decision = run(tmp_path_factory.mktemp("voice"), voice_project())
    return orch, decision, calls_of(orch, decision)


def test_clean_voice_run_goes(clean_voice_run):
    _, decision, calls = clean_voice_run
    assert decision["decision"] == "GO", decision["reasons"]
    assert set(calls) == {"clinic.book-checkup", "clinic.book-checkup.tool-timeout", "clinic.confirm-before-booking",
                          "clinic.book-checkup.noisy-line", "clinic.caller-interrupts", "clinic.asks-for-person"}
    assert all(c["status"] == "PASS" for c in decision["calls"])
    assert decision["latency"]["turns_measured"] >= 10 and decision["latency"]["within_limits"] is True


def test_voice_files_match_their_schemas_and_recording_lines_up(clean_voice_run):
    orch, decision, calls = clean_voice_run
    run_dir = orch.store.runs_dir / decision["run_id"]
    assert not schema_errors("run_decision", json.loads((run_dir / "decision.json").read_text("utf-8")))
    for verdict, events, folder in calls.values():
        assert not schema_errors("call_verdict", verdict)
        for e in events:
            assert not schema_errors("trace_event", e), e
        ended = next(e for e in events if e["type"] == "call_ended")["data"]["recording"]
        audio, rate = read_wav(folder / ended["file"])
        assert rate == SR and abs(len(audio) / SR * 1000 - ended["duration_ms"]) < 5
        # Every agent turn's audio span really holds the agent's sound at that time in the file.
        with __import__("wave").open(str(folder / ended["file"])) as w:
            stereo = from_pcm16(w.readframes(w.getnframes())).reshape(-1, 2)
        for e in events:
            span = e["data"].get("audio")
            if e["type"] == "agent_turn" and span:
                a, b = span["start_ms"] * SR // 1000, span["end_ms"] * SR // 1000
                assert np.abs(stereo[a:a + 160, 1]).max() > 0.01           # sound right at the start
                assert np.abs(stereo[max(0, a - 400):a - 80, 1]).max() < 0.01  # silence just before
                assert b > a


def test_reference_is_read_back_and_checked_twice(clean_voice_run):
    _, _, calls = clean_voice_run
    verdict, events, _ = calls["clinic.book-checkup"]
    assert check(verdict, "reads-back-reference")["result"] == "PASS"
    assert any(e["type"] == "stt_text" for e in events)  # the careful second listen ran


def test_barge_in_stops_the_agent_quickly(clean_voice_run):
    _, _, calls = clean_voice_run
    verdict, events, _ = calls["clinic.caller-interrupts"]
    c = check(verdict, "agent-stops-when-interrupted")
    assert c["result"] == "PASS", c["message"]
    interrupted = next(e for e in events if e["type"] == "agent_turn" and e["data"].get("interrupted"))
    assert 200 <= interrupted["data"]["stopped_after_ms"] <= 600  # the gateway needs 300 ms of speech to react
    barge = next(e for e in events if e["type"] == "caller_turn" and e["data"].get("barge_in"))
    assert barge["seq"] == interrupted["seq"] + 1


def test_injected_tool_delay_is_waited_out_but_not_counted_as_latency(clean_voice_run):
    _, _, calls = clean_voice_run
    verdict, events, _ = calls["clinic.book-checkup.tool-timeout"]
    rows = verdict["metrics"]["latency_turns"]
    delayed = [r for r in rows if not r["counted"]]
    assert delayed and all("on purpose" in r["note"] for r in delayed)
    assert all(r["ms"] >= 5000 for r in delayed)   # the 5 s timeout really happened on the line
    assert check(verdict, "outcome")["result"] == "PASS"


def test_misread_reference_is_caught_only_in_voice(tmp_path):
    orch, decision = run(tmp_path, voice_project(bugs="misread_reference"), scenario_ids=("clinic.book-checkup",))
    verdict, _, _ = calls_of(orch, decision)["clinic.book-checkup"]
    c = check(verdict, "reads-back-reference")
    assert c["result"] == "FAIL" and "heard saying" in c["message"], c["message"]
    assert decision["decision"] == "NO_GO"
    assert EXIT_CHECK_VOICE["misread_reference"] == "clinic.book-checkup"


def test_latency_matches_the_reference_agent_within_50ms(tmp_path):
    proj = load_project(ROOT / "examples" / "latency-ref")
    orch, decision = run(tmp_path, proj)
    verdict, _, _ = calls_of(orch, decision)["latency-ref.three-answers"]
    measured = [r["ms"] for r in verdict["metrics"]["latency_turns"]]
    assert len(measured) == 3
    for got, want in zip(measured, [500, 900, 1300]):
        assert abs(got - want) <= 50, (measured, "vs 500, 900, 1300")


def test_text_runs_skip_voice_only_scenarios():
    proj = load_project(CLINIC)
    ids = {s["id"] for s in Orchestrator(proj, RunConfig(root=ROOT)).scenarios()}
    assert ids == {"clinic.book-checkup", "clinic.book-checkup.tool-timeout", "clinic.confirm-before-booking",
                   "clinic.asks-for-person"}


# ---- parts ----------------------------------------------------------------------------------

def test_speech_edges_are_found_to_the_sample():
    x = np.zeros(SR * 2, np.float32)
    x[4000:9000] = 0.2 * np.sin(np.arange(5000) / 3)
    x[20000:24000] = 0.2 * np.sin(np.arange(4000) / 3)
    spans = speech_spans(x, SR)
    assert len(spans) == 2
    assert abs(spans[0][0] - 4000) <= 3 and abs(spans[0][1] - 9000) <= 3
    assert abs(spans[1][0] - 20000) <= 3 and abs(spans[1][1] - 24000) <= 3
    assert abs(len(trim(x, SR)) - 20000) <= 6


def test_codec_survives_noise_and_resampling():
    tts, stt = CodecTTS(), CodecSTT()
    text = "Your booking reference is 482193."
    x = set_level(from_pcm16(tts.synthesize(text).pcm16))
    x = np.concatenate([np.zeros(2000, np.float32), x, np.zeros(2000, np.float32)])
    for kind in ("office", "street", "cafe", "car"):
        noisy = x + make_noise(kind, 5, SR, kind)[: len(x)] * 10 ** ((-20 - 10) / 20)
        assert stt.transcribe(AudioClip(to_pcm16(noisy), SR)).text == text, kind


def test_noise_level_follows_snr():
    n = make_noise("office", 3, SR, "seed")
    assert abs(rms_dbfs(n)) < 0.5   # unit level; load_noise scales it to speech - snr
    assert np.array_equal(n, make_noise("office", 3, SR, "seed"))  # same seed, same noise


def test_recorder_plays_agent_audio_in_order_and_clear_drops_the_rest():
    t = [1.0]
    rec = Recorder(SR, lambda: t[0])
    first = rec.agent_audio(np.ones(SR, np.float32) * 0.1)        # 1 s of audio arriving at 1.0 s
    second = rec.agent_audio(np.ones(SR, np.float32) * 0.1)       # queued behind it
    assert (first, second) == (SR, 2 * SR)
    t[0] = 1.5
    rec.clear()                                                    # barge-in at 1.5 s
    assert rec.cursor == int(1.5 * SR) and rec.clears[0].dropped_samples == int(1.5 * SR)
    assert rec.agent.length == int(1.5 * SR) and rec.agent._buf[int(1.5 * SR):3 * SR].max() == 0
    assert rec.agent_audio(np.ones(10, np.float32)) == int(1.5 * SR)  # the next answer plays at once


def test_endpointer_finds_utterances_in_background_noise():
    tts = CodecTTS()
    speech = set_level(from_pcm16(tts.synthesize("Hello, I need an appointment.").pcm16))
    noise = make_noise("office", 6, SR, "e") * 10 ** ((-20 - 15) / 20)
    line = noise.copy()
    line[SR:SR + len(speech)] += speech
    ep, events = Endpointer(SR), []
    for i in range(0, len(line) - 320, 320):
        kind, audio = ep.feed(line[i:i + 320])
        if kind:
            events.append((kind, i))
    assert [k for k, _ in events] == ["start", "end"], events
    assert abs(events[0][1] - SR) < 0.1 * SR
    assert abs(events[1][1] - (SR + len(speech) + 0.6 * SR)) < 0.15 * SR


def test_spoken_numbers():
    assert spoken_numbers("Your reference is 4 8 2, 1 9 3.") == ["482193"]
    assert spoken_numbers("reference four eight two one nine three") == ["482193"]
    assert spoken_numbers("ref 482-193, booked for 9:30") == ["482193", "930"]
    assert spoken_numbers("four eight two oh one nine") == ["482019"]
    assert spoken_numbers("I'll put you down for nine") == ["9"]   # "for" is not a 4 here


def _ev(seq, type_, data, turn=None):
    return TraceEvent(seq, seq * 100, "x", type_, data, turn)


def test_readback_passes_on_either_listen_and_fails_on_a_wrong_number():
    resp = _ev(1, "tool_response", {"tool": "book_appointment", "ok": True, "body": {"reference": "482193"}})
    chk = {"type": "readback", "tool": "book_appointment", "field": "reference", "severity": "major"}

    def result(heard, second=None):
        events = [resp, _ev(2, "agent_turn", {"heard_text": heard}, 2)]
        if second is not None:
            events.append(_ev(3, "stt_text", {"agent_turn": 2, "text": second}))
        return evaluate_check(chk, "rb", events, events, {}, "salt", "voice")

    assert result("Your reference is 4 8 2, 1 9 3.").result == Result.PASS
    assert result("Your reference is 4 8 2, 1 nine tree.", "Your reference is 482193.").result == Result.PASS
    wrong = result("Your reference is 4 8 1, 2 9 3.", "Your reference is 481293.")
    assert wrong.result == Result.FAIL and "481293" in wrong.message
    assert result("You're booked. Goodbye.").result == Result.FAIL
    none = evaluate_check(chk, "rb", [_ev(1, "agent_turn", {"heard_text": "hi"})], [], {}, "s", "voice")
    assert none.result == Result.NOT_APPLICABLE


def test_clinic_speech_layer_reads_references_digit_by_digit():
    assert speak_text("Your booking reference is 482193.") == "Your booking reference is 4 8 2, 1 9 3."
    assert speak_text("Your booking reference is 482193.", misread_reference=True) == \
        "Your booking reference is 4 8 1, 2 9 3."
    assert speak_text("Born 1985-03-14, at 9:30am") == "Born 1985-03-14, at 9:30 a m"


def test_latency_rows_say_why_a_turn_does_not_count():
    events = [
        {"seq": 1, "type": "agent_turn", "turn": 1, "data": {"heard_text": "hi"}},
        {"seq": 2, "type": "agent_turn", "turn": 2, "data": {"heard_text": "a", "latency_ms": 700}},
        {"seq": 3, "type": "fault_injected", "turn": 3, "data": {"fault": {"type": "timeout"}}},
        {"seq": 4, "type": "agent_turn", "turn": 3, "data": {"heard_text": "b", "latency_ms": 6100}},
        {"seq": 5, "type": "agent_turn", "turn": 4, "data": {"heard_text": "c", "latency_ms": -200}},
        {"seq": 6, "type": "agent_turn", "turn": 5, "data": {"heard_text": "", "silent": True}},
    ]
    rows = turn_latencies(events)
    assert [(r["turn"], r["counted"]) for r in rows] == [(2, True), (3, False), (4, False), (5, False)]
    assert percentile([100, 200, 300, 400], 50) == 200 and percentile(list(range(1, 21)), 95) == 19


def test_slow_answers_make_a_voice_run_no_go():
    def verdict(i, lat):
        return {"test_id": f"t{i}", "scenario_id": "s", "repeat": i, "status": "PASS", "checks": [],
                "metrics": {"latency_ms": lat, "latency_turns": []}}
    profile = {"id": "p", "thresholds": {"latency_p50_ms": 800, "latency_p95_ms": 1500, "repeats": 2}}
    scen = [{"id": "s", "criticality": "critical"}]
    fast = LocalDecisionEngine().decide("r", [verdict(1, [600, 700]), verdict(2, [650, 900])], scen, profile)
    slow = LocalDecisionEngine().decide("r", [verdict(1, [600, 700]), verdict(2, [650, 2600])], scen, profile)
    assert fast["decision"] == "GO" and fast["latency"]["within_limits"] is True
    assert slow["decision"] == "NO_GO" and slow["reasons"][0]["rule"] == "latency"
    assert slow["reasons"][0]["refs"] == ["t2"]


def test_voice_profile_extends_the_text_profile():
    text, voice = load_project(CLINIC).profile, load_project(CLINIC, "profile-voice.yaml").profile
    assert voice["connection"]["type"] == "websocket_audio"
    assert voice["tools"] == text["tools"] and voice["backend"] == text["backend"]
    assert voice["connection"]["options"]["brain"] == text["connection"]["options"]["brain"]
    assert voice["thresholds"]["repeats"] == text["thresholds"]["repeats"]
    assert merge({"a": {"b": 1, "c": [1]}}, {"a": {"c": [2]}}) == {"a": {"b": 1, "c": [2]}}


def test_whisper_loads_when_windows_blocks_pyav(tmp_path, monkeypatch):
    """Smart App Control can block PyAV's DLLs; faster-whisper must still import (vatf never needs PyAV)."""
    import sys

    from vatf.speech.local import _import_faster_whisper, blocked_hint

    (tmp_path / "av").mkdir()
    (tmp_path / "av" / "__init__.py").write_text(
        'raise ImportError("DLL load failed while importing layout: An Application Control policy has blocked this file.")')
    monkeypatch.syspath_prepend(str(tmp_path))
    for name in [m for m in sys.modules if m == "av" or m.startswith(("av.", "faster_whisper"))]:
        monkeypatch.delitem(sys.modules, name)
    assert _import_faster_whisper().__name__ == "WhisperModel"
    with pytest.raises(ImportError, match="PyAV is not available"):
        sys.modules["av"].open
    assert "Smart App Control" in blocked_hint(ImportError("An Application Control policy has blocked this file."))


def test_slow_model_loading_neither_hides_the_greeting_nor_delays_the_recording(tmp_path, monkeypatch):
    """First run on a laptop: Whisper downloads for longer than no_reply_ms. The greeting must still be
    heard, every turn timed, and the loading done before the call clock starts (Ram's run 2026-10-28)."""
    import time as _time

    from vatf.speech.codec import CodecSTT

    loads = []

    def slow_first_load(self):
        if not loads:
            _time.sleep(2.5)
        loads.append(1)

    monkeypatch.setattr(CodecSTT, "warm_up", slow_first_load)
    proj = load_project(ROOT / "examples" / "latency-ref")
    proj.profile.setdefault("limits", {})["no_reply_ms"] = 2000   # longer than the 1300 ms answer, shorter than the load
    orch, decision = run(tmp_path, proj)
    verdict, events, _ = calls_of(orch, decision)["latency-ref.three-answers"]
    greeting = next(e for e in events if e["type"] == "agent_turn")
    assert not greeting["data"].get("silent"), "the greeting was missed"
    assert greeting["data"]["audio"]["start_ms"] < 1000, "the recording opens with the loading time"
    measured = [r["ms"] for r in verdict["metrics"]["latency_turns"]]
    assert len(measured) == 3, measured
    for got, want in zip(measured, [500, 900, 1300]):
        assert abs(got - want) <= 50, measured


def test_latency_check_wants_enough_timed_answers():
    """A call where most turns could not be timed must not pass the latency check on what is left."""
    from vatf.checks import evaluate_check

    from vatf.contracts.models import TraceEvent

    events = [TraceEvent(2, 300, "speech", "agent_turn", {}, 1),
              TraceEvent(4, 3000, "speech", "agent_turn", {"latency_ms": -2364}, 2),
              TraceEvent(6, 5000, "speech", "agent_turn", {"latency_ms": 494}, 3)]
    check_ = {"type": "latency", "p95_ms": 2000, "min_answers": 3}
    c = evaluate_check(check_, "answers-timed", events, events, {}, "", "voice")
    assert c.result.value == "NOT_VERIFIED", c
    assert "Only 1 answer(s) could be timed, 3 needed" in c.message and "before the caller had finished" in c.message
    c = evaluate_check({**check_, "min_answers": 1}, "answers-timed", events, events, {}, "", "voice")
    assert c.result.value == "PASS"


def test_outcome_claim_uses_the_second_listen():
    """Ram's run 2026-10-28: Whisper base.en heard "You're booked" as "Your book"; small.en heard it right."""
    from vatf.contracts.models import TraceEvent
    from vatf.outcome import detect_claim

    claim = {"success_patterns": [r"\b(booked|confirmed)\b"], "failure_patterns": [r"\b(couldn't|could not)\b"]}
    turn = TraceEvent(16, 45000, "speech", "agent_turn",
                      {"heard_text": "Your book with Dr. Patel on Tuesday. Your booking reference is 701 829."}, 5)
    assert detect_claim([turn], claim, 14)[0] == "unclear"
    said, event, text = detect_claim([turn], claim, 14, {16: "You're booked with Dr. Patel on Tuesday."})
    assert (said, event.seq) == ("success", 16) and text.startswith("You're booked")
    # Hearings that contradict each other stay unclear.
    booked = TraceEvent(16, 45000, "speech", "agent_turn", {"heard_text": "You're booked with Dr. Patel."}, 5)
    assert detect_claim([booked], claim, 14, {16: "Sorry, I couldn't book that."})[0] == "unclear"


def test_clinic_agent_understands_times_as_whisper_writes_them():
    from zoo.clinic.brain_rules import find_time

    assert find_time("930 works.") == (9, 30)
    assert find_time("1130 am please") == (11, 30)
    assert find_time("Nine thirty works.") == (9, 30)
    assert find_time("born 14 March 1985") is None and find_time("reference 701829") is None


def test_not_booked_is_not_a_success_claim():
    """Ram's live run: "I could not complete the booking ... so you are not booked yet" was judged unclear."""
    from vatf.contracts.models import TraceEvent
    from vatf.outcome import detect_claim

    claim = {"success_patterns": [r"\b(booked|confirmed)\b"], "failure_patterns": [r"\b(couldn't|could not)\b"]}
    said = lambda text: detect_claim([TraceEvent(17, 1, "speech", "agent_turn", {"heard_text": text}, 5)], claim, 14)[0]
    assert said("I could not complete the booking because our system timed out, so you are not booked yet.") == "failure"
    assert said("Sorry, it isn't confirmed.") == "unclear"          # negated success, nothing else: still unclear
    assert said("That's all booked. It is confirmed.") == "success"
    assert said("You're booked, nothing more needed.") == "success"


def test_a_misheard_name_does_not_leave_the_real_one_unmasked():
    from vatf.evidence import fact_patterns

    profile = {"tools": [{"name": "find_patient", "pii_args": ["full_name"]}]}
    patterns = fact_patterns(profile, [("full_name", "Maria"), ("full_name", "Maria Lopez")])
    text = "Hi, I'm Maria Lopez, born 14 March 1985."
    for name, p in patterns:
        text = p.sub(f"[{name}]", text)
    assert "Lopez" not in text and text.startswith("Hi, I'm [full_name],"), text


def test_said_check_uses_the_second_listen():
    from vatf.checks import evaluate_check
    from vatf.contracts.models import TraceEvent

    turn = TraceEvent(14, 1, "speech", "agent_turn", {"heard_text": "Your book with Dr. Patel."}, 4)
    careful = TraceEvent(16, 2, "recorder", "stt_text", {"agent_turn": 14, "text": "You're booked with Dr. Patel."}, 4)
    said = {"type": "said", "pattern": r"(?i)\bbooked\b"}
    assert evaluate_check(said, "c", [turn], [turn, careful], {}, "", "voice").result.value == "PASS"
    assert evaluate_check(said, "c", [turn], [turn], {}, "", "voice").result.value == "FAIL"
    not_said = {"type": "not_said", "pattern": r"(?i)\bbook\b"}
    assert evaluate_check(not_said, "c", [turn], [turn, careful], {}, "", "voice").result.value == "PASS"
