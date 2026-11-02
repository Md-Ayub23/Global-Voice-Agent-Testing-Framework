"""Caller accents (persona speech.accent): each accent is a local Piper voice, and for Piper's
multi-speaker voices one speaker of it. A persona's own speech.tts_voice always wins over its accent.

A voice is written "<piper voice>" or "<piper voice>#<speaker>", for example
"en_GB-vctk-medium#p248". The speaker is looked up in the voice's speaker list when the voice is
loaded (speech/local.py), so a wrong name fails the call's speech setup with the list of names.

Sources for the accent labels:
  - en_GB-vctk-medium is trained on the CSTR VCTK corpus; its speaker-info.txt gives each speaker's
    accent (p248 and p251 Indian, p245 Irish, p234 Scottish, p294 American).
  - en_US-l2arctic-medium is trained on L2-ARCTIC: English read by speakers whose first language is
    Hindi (ASI, RRBI), Mandarin (BWC, LXC), Spanish (EBVS, ERMS), Arabic (ABA, SKA), Korean (HJK, HKK)
    or Vietnamese (HQTV, PNV).
These are accents to test speech recognition against, not claims about groups of people.
"""

from __future__ import annotations

from typing import Any, Mapping

# id -> (label shown in the UI, voice)
ACCENTS: dict[str, tuple[str, str]] = {
    "american": ("American English", "en_US-lessac-medium"),
    "american-2": ("American English (second voice)", "en_US-amy-medium"),
    "british": ("British English", "en_GB-alan-medium"),
    "northern-english": ("Northern English", "en_GB-northern_english_male-medium"),
    "scottish": ("Scottish English", "en_GB-alba-medium"),
    "irish": ("Irish English", "en_GB-vctk-medium#p245"),
    "indian": ("Indian English", "en_GB-vctk-medium#p248"),
    "indian-2": ("Indian English (second voice)", "en_GB-vctk-medium#p251"),
    "hindi-l1": ("English, first language Hindi", "en_US-l2arctic-medium#ASI"),
    "mandarin-l1": ("English, first language Mandarin", "en_US-l2arctic-medium#BWC"),
    "spanish-l1": ("English, first language Spanish", "en_US-l2arctic-medium#EBVS"),
    "arabic-l1": ("English, first language Arabic", "en_US-l2arctic-medium#ABA"),
    "korean-l1": ("English, first language Korean", "en_US-l2arctic-medium#HJK"),
    "vietnamese-l1": ("English, first language Vietnamese", "en_US-l2arctic-medium#HQTV"),
}


def split_voice(voice: str) -> tuple[str, str | None]:
    """"en_GB-vctk-medium#p248" -> ("en_GB-vctk-medium", "p248"); a plain voice has no speaker."""
    name, _, speaker = voice.partition("#")
    return name.strip(), (speaker.strip() or None)


def speaker_id(speaker: str | None, id_map: Mapping[str, int], voice: str) -> int | None:
    """The speaker's number in a multi-speaker voice: by exact name, by name ignoring case, by a name
    that ends with it (some voices store "p_ASI"), or a plain number."""
    if speaker is None:
        return None
    if speaker in id_map:
        return int(id_map[speaker])
    low = {k.lower(): v for k, v in id_map.items()}
    if speaker.lower() in low:
        return int(low[speaker.lower()])
    ends = [v for k, v in low.items() if k.endswith(speaker.lower()) or k.endswith("_" + speaker.lower())]
    if len(ends) == 1:
        return int(ends[0])
    if speaker.isdigit() and (not id_map or int(speaker) in set(id_map.values())):
        return int(speaker)
    names = ", ".join(sorted(id_map)[:40]) or "(this voice has one speaker)"
    raise ValueError(f"voice {voice} has no speaker {speaker!r}; its speakers are: {names}")


def accent_voice(accent: str | None) -> str | None:
    return ACCENTS[accent][1] if accent in ACCENTS else None


def caller_voice(persona: Mapping[str, Any], default: str) -> str:
    """The voice a persona speaks with: its own tts_voice, else its accent's voice, else the default."""
    speech = persona.get("speech", {}) or {}
    return speech.get("tts_voice") or accent_voice(speech.get("accent")) or default


def accent_list() -> list[dict[str, str]]:
    return [{"id": k, "label": label, "voice": voice} for k, (label, voice) in ACCENTS.items()]
