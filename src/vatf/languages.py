"""The languages a simulated caller can speak: English and Tamil.

A call's language is the persona's own `language` when it gives one, else the agent's first language
(profile `languages`). It decides:
  - the caller's voice (unless the persona names its own tts_voice; accents are English voices),
  - the words the caller adds itself: cut-ins, "mm-hm"s and fillers,
  - how Vocto's caller hears the agent: Whisper's English-only ".en" models can't hear Tamil, so a
    Tamil call uses the multilingual model of at least the size listed here,
  - what Claude is told when it plays the caller.

Tamil voices are community Piper voices (not in Piper's own list), trained on the female and male
speakers of AI4Bharat's Rasa expressive speech dataset, CC-BY-4.0:
  https://huggingface.co/tinisoft/piper-ta_IN-rasa_female-medium
  https://huggingface.co/tinisoft/piper-ta_IN-rasa_male-medium
They use eSpeak's Tamil phonemes (bundled with piper-tts), which also read English words and digits
mixed into Tamil, as Tamil speakers say them.
"""

from __future__ import annotations

from typing import Any, Mapping

# Voices that are not in Piper's own voices.json: where to download the .onnx and .onnx.json.
COMMUNITY_VOICES: dict[str, str] = {
    "ta_IN-rasa_female-medium": "https://huggingface.co/tinisoft/piper-ta_IN-rasa_female-medium/resolve/main/",
    "ta_IN-rasa_male-medium": "https://huggingface.co/tinisoft/piper-ta_IN-rasa_male-medium/resolve/main/",
}

LANGUAGES: dict[str, dict[str, Any]] = {
    "en": {
        "name": "English",
        "voices": [],                     # English: the run's caller voice, or the persona's accent
        "interrupt_lines": ("Sorry, can I just say something?", "Wait, sorry, one thing.", "Hang on a second.",
                            "Sorry to cut in."),
        "backchannels": ("Mm-hm.", "Okay.", "Right.", "Yeah."),
        "fillers": ("Um,", "Uh,", "Hmm,"),
        "ears": None, "check": None,
        "caller_note": "",
    },
    "ta": {
        "name": "Tamil",
        "voices": [("ta_IN-rasa_female-medium", "Tamil, female voice"),
                   ("ta_IN-rasa_male-medium", "Tamil, male voice")],
        # "Sorry, I need to say one thing." / "One minute." / "Wait a little." / "One minute, may I say?"
        "interrupt_lines": ("மன்னிக்கவும், ஒரு விஷயம் சொல்லணும்.", "ஒரு நிமிஷம்.", "கொஞ்சம் இருங்க.",
                            "ஒரு நிமிஷம், சொல்லட்டுமா?"),
        # "Mm." / "Okay." / "Yes."
        "backchannels": ("ம்ம்.", "சரி.", "ஆமா."),
        # "That is...", "Mm,", "Then,"
        "fillers": ("அது வந்து,", "ம்ம்,", "அப்புறம்,"),
        # Whisper's multilingual models; base hears Tamil poorly, so a Tamil call uses small or larger.
        "ears": "small", "check": "medium",
        "caller_note": """
Speak only Tamil: everyday spoken Tamil, written in Tamil script, the way people in Tamil Nadu talk on
the phone. English words Tamil speakers normally use (appointment, doctor, booking, OK) are fine.
Say personal names, email addresses and reference codes exactly as written in your facts. If the agent
speaks English, keep speaking Tamil unless your extra rules say otherwise.""",
    },
}

WHISPER_SIZES = ("tiny", "base", "small", "medium", "large-v1", "large-v2", "large-v3")


def known(code: str | None) -> bool:
    return (code or "en") in LANGUAGES


def name(code: str | None) -> str:
    return LANGUAGES.get(code or "en", {}).get("name", code or "English")


def call_language(profile: Mapping[str, Any] | None, persona: Mapping[str, Any] | None) -> str:
    """The persona's own language, else the agent's first language, else English."""
    own = (persona or {}).get("language")
    if own:
        return own
    langs = (profile or {}).get("languages") or ["en"]
    return langs[0].split("-")[0]


def default_voice(code: str) -> str | None:
    voices = LANGUAGES.get(code, {}).get("voices") or []
    return voices[0][0] if voices else None


def voice_language(voice: str | None) -> str:
    """The language a Piper voice speaks: its name starts with it ("ta_IN-...", "en_GB-...")."""
    return (voice or "en").split("_")[0].split("-")[0] or "en"


def phrases(code: str, kind: str) -> tuple[str, ...]:
    return tuple(LANGUAGES.get(code, LANGUAGES["en"])[kind])


def hearing_model(model: str, code: str, careful: bool = False) -> str:
    """The Whisper model to hear a call in this language with. English: the model as chosen. Another
    language: the multilingual model of the same size (".en" dropped), at least the language's minimum."""
    if code == "en":
        return model
    base = model[:-3] if model.endswith(".en") else model
    floor = LANGUAGES.get(code, {}).get("check" if careful else "ears")
    if floor and base in WHISPER_SIZES and WHISPER_SIZES.index(base) < WHISPER_SIZES.index(floor):
        return floor
    return base


def language_list() -> list[dict[str, Any]]:
    return [{"id": k, "name": v["name"],
             "voices": [{"id": vid, "label": label} for vid, label in v["voices"]]}
            for k, v in LANGUAGES.items()]
