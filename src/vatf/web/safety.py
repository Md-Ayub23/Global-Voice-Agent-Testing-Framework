"""The Safety tests card: every built-in attack call for one project and how the latest safety run went."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .. import attacks
from ..loader import ProjectError, load_profile, load_project
from . import data


def safety_page(root: Path, runs_dir: Path, key: str, file: str = "profile.yaml",
                active: set[str] = frozenset()) -> dict[str, Any]:
    if not re.fullmatch(r"[\w.\-]+\.ya?ml", file) or file.startswith("."):
        raise ValueError(f"not a profile file: {file!r}")
    project = data.project_by_key(root, key)
    folder = root / project["path"]
    problems: list[str] = []
    try:
        loaded = load_project(folder, file)
        profile, personas = loaded.profile, list(loaded.personas)
    except ProjectError as e:
        problems.append(str(e))
        profile, personas = load_profile(folder, file)[0], []
    out = attacks.catalog(profile, personas)
    out["defaults"] = {**attacks.DEFAULTS, "packs": list(attacks.PACKS)}
    out["live_only"] = not personas
    out["problems"] = problems
    out["last_run"] = None
    for r in data.list_runs(runs_dir, active):
        if r.get("project") != project["path"] or not r.get("safety"):
            continue
        decision = data._safe_json(runs_dir / r["run_id"] / "decision.json") or {}
        out["last_run"] = {"run_id": r["run_id"], "decision": r.get("decision"),
                           "started_at": r.get("started_at"), **decision.get("safety", {})}
        break
    return out
