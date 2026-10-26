"""Speech adapters (L3 caller's mouth and ears). Voice mode only, Phase 2."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .models import AudioClip


@dataclass(frozen=True)
class Transcript:
    text: str
    confidence: float | None = None


class TextToSpeech(Protocol):
    """Implementations: Piper, Windows SAPI, later a cloud TTS."""

    def synthesize(self, text: str, voice: str, speaking_rate: float = 1.0) -> AudioClip: ...


class SpeechToText(Protocol):
    """Implementations: faster-whisper tiny, Vosk, later a cloud STT.

    Used for the caller's ears: the caller only ever hears the agent through this.
    """

    def transcribe(self, clip: AudioClip, language: str = "en") -> Transcript: ...
