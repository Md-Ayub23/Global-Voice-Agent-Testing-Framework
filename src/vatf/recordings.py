"""Recorded caller audio (Step 4): a scripted test case's line can be played from a recording of a real
caller instead of Vocto's synthetic voice (scenario turns[].audio, a WAV file in the agent's folder).

Real voices, accents, mumbling and line noise test the agent's hearing in a way synthetic speech can't.
The line's `caller` text stays: it is what is said in the recording, so the checks and the misheard-words
score still know what the agent should have understood. Use recordings only with the speaker's consent.

The recording is levelled to the caller's normal speaking volume (recordings vary a lot), then goes on
the call's phone line like any caller line, so line conditions (phone, noise, packet loss, unsteady
network, quiet caller) still apply. Conditions that change the voice itself (an accent, a fast talker)
can't change a recording.
"""

from __future__ import annotations

import io
import re
import wave
from pathlib import Path
from typing import Any, Mapping

FOLDER = "recordings"
MAX_BYTES = 20 * 1024 * 1024
MAX_SECONDS = 60


def wav_problem(raw: bytes) -> str | None:
    """Why these bytes can't be a caller line, or None when they can."""
    if len(raw) > MAX_BYTES:
        return f"the file is over {MAX_BYTES // (1024 * 1024)} MB"
    try:
        with wave.open(io.BytesIO(raw), "rb") as w:
            width, rate, frames = w.getsampwidth(), w.getframerate(), w.getnframes()
    except (wave.Error, EOFError) as e:
        why = f" ({e})" if str(e) else ""
        return f"it isn't a WAV file Vocto can read{why}: save it as WAV, 16-bit PCM"
    if width != 2:
        return "it isn't 16-bit: save it as WAV, 16-bit PCM"
    if rate < 8000:
        return "its sample rate is below 8000 Hz"
    seconds = frames / rate
    if seconds < 0.2:
        return "it is empty or shorter than 0.2 seconds"
    if seconds > MAX_SECONDS:
        return f"it is {seconds:.0f} s long; one caller line can be at most {MAX_SECONDS} s"
    return None


def seconds(raw: bytes) -> float:
    with wave.open(io.BytesIO(raw), "rb") as w:
        return round(w.getnframes() / w.getframerate(), 1)


def safe_name(name: str) -> str:
    stem = re.sub(r"[^a-z0-9]+", "-", Path(name).stem.lower()).strip("-")[:60] or "recording"
    return f"{stem}.wav"


def resolve(folder: Path, rel: str) -> Path:
    """A recording's path inside the agent's folder; never outside it."""
    base = folder.resolve()
    path = (base / rel).resolve()
    if base not in path.parents:
        raise ValueError(f"{rel}: a recording must be inside the agent's folder")
    if path.suffix.lower() != ".wav":
        raise ValueError(f"{rel}: a recording must be a .wav file")
    return path


def problems(folder: Path, scenarios: list[Mapping[str, Any]]) -> list[str]:
    """Missing or unreadable recordings, one line each (checked before a voice run starts)."""
    out = []
    for s in scenarios:
        for i, t in enumerate(s.get("turns") or [], 1):
            rel = t.get("audio")
            if not rel:
                continue
            try:
                path = resolve(folder, rel)
                why = wav_problem(path.read_bytes()) if path.is_file() else "the file is missing"
            except ValueError as e:
                why = str(e)
            if why:
                out.append(f"{s['id']} line {i}: recording {rel}: {why}")
    return out


def save(folder: Path, name: str, raw: bytes) -> dict[str, Any]:
    """Saves an uploaded recording as recordings/<name>.wav (a new name if taken)."""
    why = wav_problem(raw)
    if why:
        raise ValueError(f"{name}: {why}")
    target_dir = folder / FOLDER
    target_dir.mkdir(exist_ok=True)
    base = safe_name(name)
    path, n = target_dir / base, 2
    while path.exists():
        if path.read_bytes() == raw:          # the same file again: reuse it
            break
        path, n = target_dir / f"{base[:-4]}-{n}.wav", n + 1
    path.write_bytes(raw)
    return {"path": f"{FOLDER}/{path.name}", "seconds": seconds(raw)}
