"""Dashboard API for Phase 6: the agent's baseline, comparing runs, CI files, alerts and health checks."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .. import alerts, ciconfig, health
from ..attacks import is_safety
from ..baseline import BaselineError, clear_baseline, load_baseline, run_snapshot, set_baseline
from ..compare import DEFAULTS as GATE_DEFAULTS, as_ab, compare, rules as gate_rules
from ..conditions import PRESETS
from ..loader import ProjectError, load_project
from . import data


def _profile(project: Mapping[str, Any], file: str) -> Mapping[str, Any]:
    p = next((p for p in project["profiles"] if p["file"] == file), None)
    if p is None:
        raise data.NotFound(f"{project['path']} has no profile {file!r}")
    return p


def _brief(doc: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not doc:
        return None
    return {k: doc.get(k) for k in ("run_id", "decision", "finished_at", "set_at", "note", "warnings", "how")} | {
        "tests": len(doc.get("tests") or {}), "latency": doc.get("latency"), "scores": doc.get("scores")}


def baseline_page(root: Path, runs: Path, key: str, file: str = "profile.yaml") -> dict[str, Any]:
    project = data.project_by_key(root, key)
    _profile(project, file)
    folder = root / project["path"]
    try:
        doc, problem = load_baseline(folder, file), None
    except BaselineError as e:
        doc, problem = None, str(e)
    # Runs of this profile that could be the baseline: finished, no bug switches, newest first.
    rows = [r for r in data.with_projects(data.list_runs(runs), [project])
            if r["project_key"] == key and (r.get("profile_file") or "profile.yaml") == file and r.get("decision")
            and (r.get("kind") or "run") != "load"]     # a load test has its own baseline (Load test page)
    candidates = [{"run_id": r["run_id"], "decision": r["decision"], "started_at": r["started_at"],
                   "mode": r["mode"], "offline": r.get("offline"), "trigger": r.get("trigger"),
                   "bugs": bool((r.get("agent_options") or {}).get("bugs"))} for r in rows[:40]]
    return {"file": file, "baseline": _brief(doc), "problem": problem, "candidates": candidates,
            "committed_with_project": not project["path"].startswith("projects/")}


def set_from_run(root: Path, runs: Path, run_id: str, note: str = "") -> dict[str, Any]:
    run_dir = data._run_dir(runs, run_id)
    info = data._safe_json(run_dir / "run.json") or {}
    if not info.get("project"):
        raise ValueError("this run doesn't say which agent it tested")
    folder = root / info["project"]
    try:
        project = load_project(folder, info.get("profile_file", "profile.yaml"))
    except ProjectError as e:
        raise ValueError(f"the agent's files have problems: {e}") from e
    titles = {sc["id"]: sc.get("title", sc["id"]) for sc in project.scenarios}
    try:
        doc = set_baseline(folder, run_dir, titles, note)
    except BaselineError as e:
        raise ValueError(str(e)) from e
    return {"baseline": _brief(doc)}


def clear(root: Path, key: str, file: str, kind: str = "run") -> dict[str, Any]:
    project = data.project_by_key(root, key)
    _profile(project, file)
    return {"removed": clear_baseline(root / project["path"], file, "load" if kind == "load" else "run")}


def compare_runs(root: Path, runs: Path, run_a: str, run_b: str) -> dict[str, Any]:
    """Any two runs of the dashboard, the later one judged by its own agent's regression settings."""
    a_dir, b_dir = data._run_dir(runs, run_a), data._run_dir(runs, run_b)
    try:
        a, b = run_snapshot(a_dir), run_snapshot(b_dir)
    except BaselineError as e:
        raise ValueError(str(e)) from e
    info = data._safe_json(b_dir / "run.json") or {}
    try:
        project = load_project(root / info["project"], info.get("profile_file", "profile.yaml"))
        profile = project.profile
        titles = {sc["id"]: sc.get("title", sc["id"]) for sc in project.scenarios}
        for snap in (a, b):
            for sid, t in snap["tests"].items():
                t["title"] = titles.get(sid, t["title"])
    except (ProjectError, KeyError, OSError):
        profile = {}
    a_info = data._safe_json(a_dir / "run.json") or {}
    ab = (a_info.get("ab") or {}), (info.get("ab") or {})
    pair = bool(ab[0].get("group")) and ab[0].get("group") == ab[1].get("group") and ab[0].get("side") == "A"
    if pair:    # an A/B test of two versions: B against A, worded so, and nothing blocks
        out = as_ab(compare(a, b, {"regression": {"enabled": False}}, "ab"))
        out["ab"] = {"labels": {"A": ab[0].get("label", "A"), "B": ab[1].get("label", "B")}}
    else:
        out = compare(a, b, profile, run_a)
    out["runs"] = {"a": data.run_summary(runs, run_a), "b": data.run_summary(runs, run_b)}
    out["same_agent"] = a.get("profile_id") == b.get("profile_id")
    out["snapshots"] = {"a": a, "b": b}
    return out


def run_comparison(runs: Path, run_id: str) -> dict[str, Any] | None:
    folder = data._run_dir(runs, run_id)
    return data._safe_json(folder / "comparison.json")


# ---- CI & alerts page ------------------------------------------------------------------------------

def ops_page(root: Path, runs: Path, key: str, file: str = "profile.yaml") -> dict[str, Any]:
    """Set up > CI & alerts: alert targets, health check settings and when they last ran."""
    project = data.project_by_key(root, key)
    info = _profile(project, file)
    folder = root / project["path"]
    try:
        p = load_project(folder, file)
        profile = p.profile
        mode = info["mode"]
        in_mode = [s for s in p.scenarios if mode in s.get("modes", ["text", "voice"]) and not is_safety(s)]
    except ProjectError:
        profile, mode, in_mode = {}, info.get("mode", "text"), []
    hc = health.settings(profile)
    rows = [r for r in data.with_projects(data.list_runs(runs), [project])
            if r["project_key"] == key and (r.get("profile_file") or "profile.yaml") == file]
    checks = [r for r in rows if r.get("trigger") == "health"][:10]
    env = data._dotenv(root)
    import os
    secret_state = {n: bool(env.get(n) or os.environ.get(n)) for n in ciconfig.secret_names(profile, False)}
    return {
        "file": file, "mode": mode,
        "alerts": profile.get("alerts") or [], "alert_events": list(alerts.EVENTS), "alert_types": list(alerts.TYPES),
        "default_events": alerts.DEFAULT_ON,
        "health": hc, "health_tests": health.scenario_ids(profile, in_mode),
        "tests": [{"id": s["id"], "title": s.get("title", s["id"]), "criticality": s.get("criticality", "normal")}
                  for s in in_mode],
        "health_runs": checks, "conditions": list(PRESETS),
        "regression": gate_rules(profile),
        "regression_defaults": GATE_DEFAULTS,
        "providers": ciconfig.PROVIDERS, "secrets": secret_state,
        "committed_with_project": not project["path"].startswith("projects/"),
        "agent_process": info.get("agent_process", False),
    }


def ci_file(root: Path, key: str, q: Mapping[str, str]) -> dict[str, Any]:
    project = data.project_by_key(root, key)
    file = q.get("file", "profile.yaml")
    info = _profile(project, file)
    try:
        profile = load_project(root / project["path"], file).profile
    except ProjectError as e:
        raise ValueError(f"fix the agent's files first: {e}") from e
    cond = q.get("conditions", "as_written")
    if cond not in PRESETS:
        raise ValueError(f"caller conditions must be one of {', '.join(PRESETS)}")
    yes = lambda k: q.get(k) in ("1", "true", "yes")   # noqa: E731
    out = ciconfig.ci_file(q.get("provider", "github"), profile, project["path"], file, mode=info["mode"],
                           offline=yes("offline"), start_agent=yes("start_agent") and info.get("agent_process", False),
                           conditions=cond, safety=yes("safety"), branch=q.get("branch") or "main",
                           schedule=q.get("schedule", "1") != "0")
    return {**out, "secrets": ciconfig.secret_names(profile, yes("offline"))}


def test_alert(root: Path, key: str, body: Mapping[str, Any]) -> dict[str, Any]:
    """Send test on one alert target, as typed in the form (it need not be saved yet)."""
    project = data.project_by_key(root, key)
    target = body.get("target") or {}
    if not target.get("url"):
        raise ValueError("enter the webhook URL first")
    if target.get("type", "webhook") not in alerts.TYPES:
        raise ValueError("unknown alert type")
    import os
    for name, value in data._dotenv(root).items():   # secrets saved from the dashboard since it started
        os.environ.setdefault(name, value)
    return alerts.send_test(target, project["name"])


def health_status(root: Path, runs: Path, now_ts: float | None = None) -> list[dict[str, Any]]:
    """Every profile with health checks on: when one last ran and whether one is due now."""
    import time
    from datetime import datetime
    now_ts = time.time() if now_ts is None else now_ts
    all_runs = data.list_runs(runs)
    out = []
    for project in data.projects(root):
        for p in project["profiles"]:
            if not p["ok"]:
                continue
            try:
                profile = load_project(root / project["path"], p["file"]).profile
            except ProjectError:
                continue
            hc = health.settings(profile)
            if not hc["enabled"]:
                continue
            last = next((r for r in all_runs if r.get("trigger") == "health" and r.get("project") == project["path"]
                         and (r.get("profile_file") or "profile.yaml") == p["file"]), None)
            last_ts = None
            if last:
                try:
                    last_ts = datetime.fromisoformat(last["started_at"].replace("Z", "+00:00")).timestamp()
                except (ValueError, AttributeError):
                    last_ts = None
            due = last_ts is None or now_ts - last_ts >= hc["every_minutes"] * 60
            out.append({"project": project, "profile": p, "file": p["file"], "every_minutes": hc["every_minutes"],
                        "start_agent": bool(hc["start_agent"]) and p.get("agent_process", False),
                        "last": last, "due": due})
    return out
