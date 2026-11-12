"""Tamil callers (languages.py): the call's language, its voice and words, how Vocto hears it, the
caller's instructions, and Tamil words in the scores and checks."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import yaml

from vatf.caller import caller_system_prompt
from vatf.checks import evaluate_check
from vatf.conditions import resolve
from vatf.contracts.models import Result, TraceEvent
from vatf.languages import COMMUNITY_VOICES, call_language, hearing_model, language_list
from vatf.loader import load_project, schema_errors
from vatf.metrics import normalise, wer
from vatf.orchestrator import Orchestrator, RunConfig
from vatf.speech import SpeechConfig

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = {"id": "s", "caller": {"goal": "book", "facts": {}}, "audio": {}}


def test_the_call_language_is_the_persona_s_else_the_agent_s():
    assert call_language({"languages": ["en"]}, {}) == "en"
    assert call_language({"languages": ["ta", "en"]}, {}) == "ta"
    assert call_language({"languages": ["en", "ta"]}, {"language": "ta"}) == "ta"
    assert call_language({"languages": ["ta"]}, {"language": "en"}) == "en"
    assert call_language({}, None) == "en"
    assert call_language({"languages": ["en-IN"]}, {}) == "en"


def test_a_tamil_caller_speaks_with_a_tamil_voice_and_tamil_words():
    english_voice = {"speech": {"tts_voice": "en_GB-alba-medium", "accent": "indian"},
                     "behaviour": {"interrupts": True, "backchannels": True, "fillers": True, "filler_chance": 1.0}}
    c = resolve(english_voice, SCENARIO, language="ta", seed="x")
    assert c.voice == "ta_IN-rasa_female-medium" and c.voice in COMMUNITY_VOICES
    assert c.accent is None and c.language == "ta"            # accents are English voices
    assert all(any("஀" <= ch <= "௿" for ch in w) for w in c.behaviour.interrupt_lines)
    assert all(any("஀" <= ch <= "௿" for ch in w) for w in c.backchannel_lines())
    first, _, said = c.spoken("எனக்கு ஒரு appointment வேணும்")
    assert first.split()[0] in ("அது", "ம்ம்,", "அப்புறம்,") and said
    assert c.label() == "Tamil caller" and c.summary()["language"] == "ta"
    assert "The call is in Tamil" in c.judge_note()
    own = resolve({"speech": {"tts_voice": "ta_IN-rasa_male-medium"}}, SCENARIO, language="ta")
    assert own.voice == "ta_IN-rasa_male-medium"
    # English callers are unchanged.
    en = resolve(english_voice, SCENARIO, seed="x")
    assert en.voice == "en_GB-alba-medium" and en.accent == "indian" and en.backchannel_lines()[0] == "Mm-hm."
    assert "language" not in en.summary() and "Tamil" not in en.judge_note()


def test_vocto_hears_tamil_with_a_multilingual_model():
    assert hearing_model("base.en", "en") == "base.en"
    assert hearing_model("base.en", "ta") == "small"            # base hears Tamil poorly
    assert hearing_model("small.en", "ta") == "small"
    assert hearing_model("small.en", "ta", careful=True) == "medium"
    assert hearing_model("large-v3", "ta", careful=True) == "large-v3"
    assert hearing_model("distil-large-v3", "ta") == "distil-large-v3"   # unknown sizes are left alone
    assert [l["id"] for l in language_list()] == ["en", "ta"]


def test_claude_is_told_to_speak_tamil():
    scenario = {"caller": {"goal": "Book a check-up", "facts": {"full_name": "Kavitha Raman"}}, "setup": {}}
    ta = caller_system_prompt(scenario, {}, "the clinic", voice=True, language="ta")
    assert "Speak only Tamil" in ta and "Tamil script" in ta and "exactly as written in your facts" in ta
    assert "Tamil" not in caller_system_prompt(scenario, {}, "the clinic", voice=True)


def test_tamil_words_are_scored_as_words():
    said = "நான் ஒரு சந்திப்பு பதிவு செய்ய வேண்டும்"
    assert normalise(said) == said.split()                         # vowel signs stay inside the word
    assert wer(said, "நான் ஒரு சந்திப்பு பதிவு செய்ய வேணும்") == (1, 6)
    assert normalise("எண் ௭௭௩௧") == ["எண்", "7731"]                  # Tamil digits as digits
    assert normalise("Don't book the check-up at 9:30am") == ["don't", "book", "the", "checkup", "at", "930", "am"]


def _ev(seq, type_, data, turn=None):
    return TraceEvent(seq, seq * 100, "x", type_, data, turn)


def test_a_reference_read_back_in_tamil():
    def result(reference, heard):
        resp = _ev(1, "tool_response", {"tool": "book", "ok": True, "body": {"reference": reference}})
        events = [resp, _ev(2, "agent_turn", {"heard_text": heard}, 2)]
        chk = {"type": "readback", "tool": "book", "field": "reference", "severity": "major"}
        return evaluate_check(chk, "rb", events, events, {}, "salt", "voice")

    assert result("7731", "உங்கள் எண் ஏழு ஏழு மூணு ஒண்ணு").result == Result.PASS
    assert result("7731", "உங்கள் எண் 7731").result == Result.PASS
    wrong = result("7731", "உங்கள் எண் ஏழு ஏழு மூணு ரெண்டு")
    assert wrong.result == Result.FAIL and "7732" in wrong.message
    # A code with letters said in Tamil can't be matched with certainty: left for a person, never failed.
    letters = result("R7731", "உங்கள் எண் ஆர் ஏழாயிரத்து எழுநூற்று முப்பத்தொன்று")
    assert letters.result == Result.NOT_VERIFIED and "listen to the recording" in letters.message
    assert result("R7731", "Your reference is R 7 7 3 2.").result == Result.FAIL   # English: still checked


def test_a_tamil_voice_run_end_to_end(tmp_path):
    """The zoo clinic agent (rules brain, test-tone speech) called by a Tamil caller: the voice, the
    caller's language and the hearing models are recorded, and the call is checked as usual."""
    shutil.copytree(ROOT / "examples" / "clinic", tmp_path / "clinic", ignore=shutil.ignore_patterns("runs"))
    persona = tmp_path / "clinic" / "personas" / "calm-adult.yaml"
    doc = yaml.safe_load(persona.read_text("utf-8"))
    doc["language"] = "ta"
    doc["behaviour"] = {"fillers": True, "filler_chance": 1.0}
    persona.write_text(yaml.safe_dump(doc, allow_unicode=True), "utf-8")
    proj = load_project(tmp_path / "clinic", "profile-voice.yaml").with_agent_options(
        {"brain": "rules", "bugs": "", "speech": "codec"})
    assert schema_errors("persona", doc) == []
    cfg = RunConfig(root=ROOT, runs_dir=tmp_path / "runs", offline=True, speech=SpeechConfig(engine="codec"),
                    repeats=1, judges=False, scenario_ids=("clinic.book-checkup",), baseline="none")
    decision = asyncio.run(Orchestrator(proj, cfg, log=lambda *_: None).run())
    run_dir = tmp_path / "runs" / decision["run_id"]
    info = json.loads((run_dir / "run.json").read_text("utf-8"))
    assert info["languages"] == ["ta"]
    call = decision["calls"][0]
    verdict = json.loads((run_dir / "calls" / call["test_id"] / "verdict.json").read_text("utf-8"))
    assert verdict["conditions"]["voice"] == "ta_IN-rasa_female-medium"
    assert verdict["conditions"]["language"] == "ta" and verdict["conditions"]["label"] == "Tamil caller"
    assert schema_errors("call_verdict", verdict) == []
    events = [json.loads(x) for x in (run_dir / "calls" / call["test_id"] / "events.jsonl").read_text("utf-8").splitlines()]
    spoken = [e["data"].get("spoken", "") for e in events if e["type"] == "caller_turn"]
    assert any(s.startswith(("அது வந்து,", "ம்ம்,", "அப்புறம்,")) for s in spoken)


def test_a_run_can_make_every_caller_speak_tamil(tmp_path):
    """The Run tab's Caller's language: no caller file changed, every call is in Tamil with a Tamil voice."""
    shutil.copytree(ROOT / "examples" / "clinic", tmp_path / "clinic", ignore=shutil.ignore_patterns("runs"))
    proj = load_project(tmp_path / "clinic", "profile-voice.yaml").with_agent_options(
        {"brain": "rules", "bugs": "", "speech": "codec"})
    cfg = RunConfig(root=ROOT, runs_dir=tmp_path / "runs", offline=True, speech=SpeechConfig(engine="codec"),
                    repeats=1, judges=False, scenario_ids=("clinic.book-checkup",), baseline="none", language="ta")
    decision = asyncio.run(Orchestrator(proj, cfg, log=lambda *_: None).run())
    run_dir = tmp_path / "runs" / decision["run_id"]
    info = json.loads((run_dir / "run.json").read_text("utf-8"))
    assert info["languages"] == ["ta"] and info["caller_language"] == "ta"
    call = decision["calls"][0]
    verdict = json.loads((run_dir / "calls" / call["test_id"] / "verdict.json").read_text("utf-8"))
    assert verdict["conditions"]["language"] == "ta"
    assert verdict["conditions"]["voice"].startswith("ta_IN-")
