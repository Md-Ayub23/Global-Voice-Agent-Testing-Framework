"""The agent's baseline: a known-good run's snapshot, saved in the project folder (Phase 6).

    <project>/baselines/<profile file stem>.json      e.g. examples/clinic/baselines/profile-voice.json
    <project>/baselines/<profile file stem>-load.json a load test's baseline (Phase 7): load runs are
                                                      compared only with load runs

Kept in the project folder (not runs/) so it is committed with the project and CI can compare
against it without the old run. One baseline per profile file, because a text profile and a voice
profile of the same agent are measured differently.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .evidence import utc_now
from .snapshot import snapshot
from .util import read_json, write_json


class BaselineError(Exception):
    pass


def baseline_path(project_folder: Path, profile_file: str = "profile.yaml", kind: str = "run") -> Path:
    suffix = "-load" if kind == "load" else ""
    return Path(project_folder) / "baselines" / f"{Path(profile_file).stem}{suffix}.json"


def snapshot_kind(snap: Mapping[str, Any]) -> str:
    """run (a normal test run) or load (a load test)."""
    return "load" if (snap.get("how") or {}).get("kind") == "load" else "run"


def load_baseline(project_folder: Path, profile_file: str = "profile.yaml", kind: str = "run") -> dict[str, Any] | None:
    path = baseline_path(project_folder, profile_file, kind)
    if not path.is_file():
        return None
    try:
        doc = read_json(path)
    except (OSError, ValueError) as e:
        raise BaselineError(f"the baseline file {path.name} could not be read: {e}") from e
    if not isinstance(doc, dict) or doc.get("kind") != "run_snapshot":
        raise BaselineError(f"{path.name} is not a Vocto baseline")
    if snapshot_kind(doc) != kind:
        raise BaselineError(f"{path.name} is the baseline of a {snapshot_kind(doc)} run, not of a {kind} run")
    return doc


def run_snapshot(run_dir: Path, titles: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Snapshot of a finished run folder."""
    decision_file, info_file = run_dir / "decision.json", run_dir / "run.json"
    if not decision_file.is_file():
        raise BaselineError(f"run {run_dir.name} has no decision yet (it did not finish)")
    info = read_json(info_file) if info_file.is_file() else {}
    return snapshot(read_json(decision_file), info, titles)


def problems_as_baseline(snap: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """(errors, warnings) for using this run as the baseline."""
    errors, warnings = [], []
    how = snap.get("how") or {}
    if how.get("bugs"):
        errors.append(f"it ran with bug switches on ({how['bugs']}), so it is meant to fail")
    if how.get("offline"):
        warnings.append("it was an offline dry run (rule-based caller), so it only compares with other dry runs")
    if snap.get("decision") != "GO":
        warnings.append(f"its decision was {snap.get('decision')}, not GO: tests that failed in it can't show as "
                        "new failures later")
    return errors, warnings


def set_baseline(project_folder: Path, run_dir: Path, titles: Mapping[str, str] | None = None,
                 note: str = "") -> dict[str, Any]:
    snap = run_snapshot(run_dir, titles)
    errors, warnings = problems_as_baseline(snap)
    if errors:
        raise BaselineError("This run can't be the baseline: " + "; ".join(errors) + ".")
    doc = {**snap, "set_at": utc_now(), "note": note, "warnings": warnings}
    path = baseline_path(project_folder, snap["profile_file"], snapshot_kind(snap))
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, doc)
    return doc


def clear_baseline(project_folder: Path, profile_file: str = "profile.yaml", kind: str = "run") -> bool:
    path = baseline_path(project_folder, profile_file, kind)
    if path.is_file():
        path.unlink()
        return True
    return False
