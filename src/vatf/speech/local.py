"""Local speech engines: Piper (text to speech) and faster-whisper (speech to text). CPU only."""

from __future__ import annotations

import logging
import os
import sys
import threading
import types
from pathlib import Path

import numpy as np

from ..audio.pcm import from_pcm16, resample, to_pcm16
from ..contracts.models import AudioClip
from ..contracts.speech import Transcript

log = logging.getLogger(__name__)
SENTENCE_PAUSE_S = 0.25


class PiperTTS:
    """Piper voices such as en_US-lessac-medium (see https://rhasspy.github.io/piper-samples/)."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self._voices: dict[str, object] = {}
        self._lock = threading.Lock()

    def _voice(self, name: str):
        with self._lock:
            if name not in self._voices:
                from piper import PiperVoice
                from piper.download_voices import download_voice

                self.folder.mkdir(parents=True, exist_ok=True)
                model = self.folder / f"{name}.onnx"
                if not model.exists() or not (self.folder / f"{name}.onnx.json").exists():
                    print(f"vatf: downloading Piper voice {name} into {self.folder} (once only)...")
                    download_voice(name, self.folder)
                self._voices[name] = PiperVoice.load(model)
            return self._voices[name]

    def warm_up(self, voice: str) -> None:
        """Download and load a voice now, so it never happens in the middle of a call."""
        self._voice(voice)

    def synthesize(self, text: str, voice: str, speaking_rate: float = 1.0) -> AudioClip:
        from piper import SynthesisConfig

        v = self._voice(voice)
        cfg = SynthesisConfig(length_scale=1.0 / max(speaking_rate, 0.1))
        parts, rate = [], v.config.sample_rate
        for chunk in v.synthesize(text, syn_config=cfg):
            rate = chunk.sample_rate
            if parts:
                parts.append(np.zeros(int(SENTENCE_PAUSE_S * rate), dtype=np.float32))
            parts.append(chunk.audio_float_array.astype(np.float32))
        x = np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)
        return AudioClip(to_pcm16(x), rate)


def _import_faster_whisper():
    """Import faster-whisper without needing PyAV.

    faster-whisper imports PyAV (FFmpeg) at the top of its audio module, but only uses it to
    decode audio files; vatf always passes it samples. On Windows, Smart App Control can block
    PyAV's DLLs ("An Application Control policy has blocked this file"), so if PyAV cannot be
    loaded a placeholder stands in for it and Whisper works as normal.
    """
    # Windows without Developer Mode can't make the symlinks the Hugging Face cache prefers; it copies
    # instead, which works. Keep its long warning about that off the run output.
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    try:
        import av  # noqa: F401
    except (ImportError, OSError) as e:
        log.info("PyAV unavailable (%s); faster-whisper runs without it", e)
        cause = str(e)
        placeholder = types.ModuleType("av")
        placeholder.__getattr__ = lambda name: _no_pyav(name, cause)  # type: ignore[method-assign]
        sys.modules["av"] = placeholder
    from faster_whisper import WhisperModel
    return WhisperModel


def _no_pyav(name: str, cause: str):
    raise ImportError(f"PyAV is not available ({cause}); vatf only passes audio samples to Whisper")


def blocked_hint(e: BaseException) -> str:
    """Plain-English advice when Windows blocks a speech library's DLL."""
    if "Application Control" in str(e) or "blocked this file" in str(e):
        return (" Windows blocked one of the speech library's files (Smart App Control or a company"
                " App Control policy). The file is named in the error above.")
    return ""


class WhisperSTT:
    """faster-whisper on the CPU (int8). English-only .en models are the most accurate for their size."""

    def __init__(self, model: str, folder: Path, beam_size: int = 1):
        self.model_name = model
        self.folder = Path(folder)
        self.beam_size = beam_size
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        if self._model is None:
            WhisperModel = _import_faster_whisper()

            self.folder.mkdir(parents=True, exist_ok=True)
            print(f"vatf: loading whisper {self.model_name} (downloads into {self.folder} the first time)...")
            self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8",
                                       download_root=str(self.folder))
        return self._model

    def warm_up(self) -> None:
        with self._lock:
            self._load()

    def transcribe(self, clip: AudioClip, language: str = "en") -> Transcript:
        x = resample(from_pcm16(clip.pcm16), clip.sample_rate_hz, 16000)
        if len(x) < 1600:  # under 0.1 s: nothing to hear
            return Transcript("")
        x = np.concatenate([np.zeros(1600, np.float32), x, np.zeros(3200, np.float32)])
        with self._lock:  # one decode at a time per model; CTranslate2 uses all cores anyway
            model = self._load()
            segments, _ = model.transcribe(
                x, language=None if self.model_name.endswith(".en") else language,
                beam_size=self.beam_size, vad_filter=False, condition_on_previous_text=False,
                without_timestamps=True, temperature=0.0)
            text = " ".join(s.text.strip() for s in segments).strip()
        return Transcript(text)
