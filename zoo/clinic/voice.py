"""The clinic agent on the phone: the text agent behind the zoo voice gateway.

Named in the voice profile as connection.launch: "zoo.clinic.voice:launch".
vatf calls launch(tools=..., options=connection.options) for each call; it
returns the WebSocket URL to dial.

Options (on top of the text agent's brain / model / bugs):
  speech     local (Piper + faster-whisper, default) or codec (tones; framework tests only)
  voice      Piper voice the agent speaks with (default en_US-amy-medium)
  stt_model  faster-whisper model the agent listens with (default base.en)

Bug switch misread_reference lives here, in the speech layer: the booking
reference is read out with its 3rd and 4th digits swapped. The text the brain
wrote is right; only what the caller hears is wrong. That is the kind of bug
only a voice test catches.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any, Mapping

from zoo.voice.gateway import VoiceGateway

from .agent import build_agent, parse_bugs

ROOT = Path(__file__).resolve().parents[2]
REFERENCE = re.compile(r"(?<![\d-])\d(?:[ -]?\d){5}(?![\d-])")


def speak_text(text: str, misread_reference: bool = False) -> str:
    """Written reply -> words for the speech engine: references digit by digit, "9:30am" -> "9:30 a m"."""
    def digits(m: re.Match) -> str:
        ref = re.sub(r"\D", "", m.group(0))
        if misread_reference:
            ref = ref[:2] + ref[3] + ref[2] + ref[4:]
        return " ".join(ref[:3]) + ", " + " ".join(ref[3:])

    text = REFERENCE.sub(digits, text)
    return re.sub(r"(\d)\s?([ap])\.?m\.?\b", lambda m: f"{m.group(1)} {m.group(2)} m", text, flags=re.I)


async def launch(*, tools: Any, options: Mapping[str, Any]):
    from vatf.speech import SpeechConfig, make_stt, make_tts

    options = dict(options)
    bugs = parse_bugs(options)
    cfg = SpeechConfig(engine=options.get("speech", "local"), ears_model=options.get("stt_model", "base.en"))
    tts, stt, voice = make_tts(cfg, ROOT), make_stt(cfg, ROOT), options.get("voice", "en_US-amy-medium")
    # Load the models before the call is answered, as a real deployment keeps them loaded.
    await asyncio.to_thread(lambda: (tts.warm_up(voice), stt.warm_up()))
    gateway = VoiceGateway(
        lambda session: build_agent(tools=tools, session=session, options=options),
        tts=tts, stt=stt, voice=voice,
        speak_text=lambda text: speak_text(text, "misread_reference" in bugs))
    return await gateway.launch()
