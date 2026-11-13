"""Dashboard API for Phase 7: the Load test page (settings, which tests it uses, its baseline, past load runs)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from ..attacks import is_safety
from ..baseline import BaselineError, load_baseline
from ..gateway import uses_gateway
from ..load import DEFAULTS, LIMIT_DEFAULTS, LoadSettingsError, p95_limit, scenario_ids, settings, stages, summary
from ..loader import ProjectError, load_project
from ..loadstats import identity_facts
from . import data
from .regress import _brief, _profile


def load_page(root: Path, runs: Path, key: str, file: str = "profile.yaml") -> dict[str, Any]:
    project = data.project_by_key(root, key)
    info = _profile(project, file)
    folder = root / project["path"]
    try:
        p = load_project(folder, file)
    except ProjectError as e:
        raise ValueError(f"the agent's files have problems: {e}") from e
    profile, mode = p.profile, info["mode"]
    out: dict[str, Any] = {"file": file, "mode": mode, "saved": profile.get("load") or {},
                           "defaults": {**DEFAULTS, "limits": LIMIT_DEFAULTS}, "error": None}
    try:
        s = settings(profile)
        out["settings"] = s
        st = stages(s)
        out["plan"] = summary(s, st, profile)
        out["tests_used"] = scenario_ids(s, p.scenarios, mode)
    except LoadSettingsError as e:
        out.update(settings=None, plan=None, tests_used=[], error=str(e))
        s = None
    out["p95_limit_ms"] = p95_limit(s, profile, mode) if s else None
    out["voice_p95_ms"] = (profile.get("thresholds") or {}).get("latency_p95_ms", 1500) if mode == "voice" else None
    out["tests"] = [{"id": sc["id"], "title": sc.get("title", sc["id"]), "criticality": sc.get("criticality", "normal"),
                     "faults": bool((sc.get("setup") or {}).get("faults")), "style": sc["style"],
                     # the caller's identity details: what the leak check looks for, and what unique_facts can vary
                     "facts": sorted(identity_facts((sc.get("caller") or {}).get("facts") or {}))}
                    for sc in p.scenarios if mode in sc.get("modes", ["text", "voice"]) and not is_safety(sc)]
    conn = profile["connection"]
    tid = profile.get("test_id") or {}
    out["agent"] = {
        "launched_per_call": bool(conn.get("launch")) or conn.get("type") == "text_python",
        "agent_process": bool(profile.get("agent_process")),
        "uses_gateway": uses_gateway(profile),
        "test_id_sent": "session_metadata" in (tid.get("propagation") or []),
        "test_id_header": tid.get("header_name", "X-Test-Id"),
        "max_call_seconds": (profile.get("limits") or {}).get("max_call_seconds", 300),
    }
    try:
        base, problem = load_baseline(folder, file, kind="load"), None
    except BaselineError as e:
        base, problem = None, str(e)
    out["baseline"] = _brief(base)
    if base:
        out["baseline"]["load"] = base.get("load")
    out["baseline_problem"] = problem
    rows = [r for r in data.with_projects(data.list_runs(runs), [project])
            if r["project_key"] == key and (r.get("profile_file") or "profile.yaml") == file and r.get("kind") == "load"]
    out["runs"] = rows[:20]
    return out


def save(root: Path, key: str, file: str, section: Mapping[str, Any]) -> dict[str, Any]:
    """Saves the profile's load section, after the checks the schema can't make (start_at above the
    target, no time in the load, tests that don't exist in this mode)."""
    from . import setup

    project = data.project_by_key(root, key)
    info = _profile(project, file)
    clean = setup._clean(dict(section))
    try:
        s = settings({"load": clean})
        stages(s)
        if clean.get("scenarios"):
            p = load_project(root / project["path"], file)
            scenario_ids(s, p.scenarios, info["mode"])
    except (LoadSettingsError, ProjectError) as e:
        return {"saved": False, "problems": [str(e)]}
    # Written whole, so a field set back to its default (or tests unpicked) is really gone from the file.
    return setup.save_profile(root, project["path"], file, {"load": clean or None}, replace=frozenset({"load"}))
