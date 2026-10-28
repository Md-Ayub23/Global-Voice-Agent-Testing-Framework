"""Speech engines (contracts/speech.py): the simulated caller's mouth (TTS) and ears (STT).

  local  Piper voices for speech and faster-whisper for listening. Runs on the
         laptop CPU, no API calls. Models download once into .models/ (about
         60 MB per Piper voice, 145 MB for whisper base.en, 480 MB for small.en).
  codec  A test double: text is sent as a sequence of tones and decoded back
         exactly. No models, no downloads. It exercises the whole audio path
         (timing, line, recorder, latency, barge-in) and is what the framework's
         own tests use. It is not speech: never use it to judge an agent.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ENGINES = ("local", "codec")


@dataclass(frozen=True)
class SpeechConfig:
    engine: str = "local"
    caller_voice: str = "en_US-lessac-medium"   # a persona's voice.tts_voice wins
    ears_model: str = "base.en"                 # what the caller hears with (fast)
    check_model: str = "small.en"               # second, careful listen for read-back checks
    models_dir: Path | None = None              # default: <root>/.models, or VATF_MODELS_DIR

    def resolved_models_dir(self, root: Path) -> Path:
        if self.models_dir:
            return Path(self.models_dir)
        env = os.environ.get("VATF_MODELS_DIR")
        return Path(env) if env else root / ".models"


_cache: dict[tuple, object] = {}


def make_tts(cfg: SpeechConfig, root: Path):
    key = ("tts", cfg.engine, str(cfg.resolved_models_dir(root)))
    if key not in _cache:
        if cfg.engine == "codec":
            from .codec import CodecTTS

            _cache[key] = CodecTTS()
        elif cfg.engine == "local":
            from .local import PiperTTS

            _cache[key] = PiperTTS(cfg.resolved_models_dir(root) / "piper")
        else:
            raise ValueError(f"unknown speech engine {cfg.engine!r} (use one of {', '.join(ENGINES)})")
    return _cache[key]


def make_stt(cfg: SpeechConfig, root: Path, careful: bool = False):
    model = cfg.check_model if careful else cfg.ears_model
    key = ("stt", cfg.engine, model, careful, str(cfg.resolved_models_dir(root)))
    if key not in _cache:
        if cfg.engine == "codec":
            from .codec import CodecSTT

            _cache[key] = CodecSTT()
        elif cfg.engine == "local":
            from .local import WhisperSTT

            _cache[key] = WhisperSTT(model, cfg.resolved_models_dir(root) / "whisper",
                                     beam_size=5 if careful else 1)
        else:
            raise ValueError(f"unknown speech engine {cfg.engine!r} (use one of {', '.join(ENGINES)})")
    return _cache[key]
