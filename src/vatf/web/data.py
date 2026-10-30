"""What the dashboard reads: test projects, past and running runs, calls, recordings and the system.

Everything here reads the same files the command line writes (runs/<run_id>/...), through the same
helpers as the terminal output and report.html (story.py), so the three always agree.
"""

from __future__ import annotations

import importlib
import json
import os
import platform
import re
import shutil
import sys
import tempfile
import wave
from pathlib import Path
from typing import Any, Mapping

import yaml

from .. import __version__, attacks
from ..contracts.models import TraceEvent
from ..evidence import TEXT_FIELDS, mask_events
from ..llm import DEFAULT_MODEL
from ..loader import ProjectError, load_project
from ..story import database_rows, judgement, story_rows
from ..util import read_json

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "runs", ".models", "__pycache__", ".pytest_cache", "src"}
RUN_ID = re.compile(r"^r-\d{8}-\d{6}-[0-9a-f]{4}$")
TEST_ID = re.compile(r"^t-\d{8}-\d{4}-[0-9a-f]{4}$")
FILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*\.ya?ml$")


class NotFound(Exception):
    pass


def _rel(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None


# ---- projects ---------------------------------------------------------------------------

def find_projects(root: Path) -> list[Path]:
    """Folders holding a client profile and a scenarios/ folder (examples/clinic, examples/latency-ref, ...)."""
    found = []

    def walk(folder: Path, depth: int) -> None:
        if (folder / "scenarios").is_dir() and any(
                isinstance(d := _yaml(p), dict) and d.get("kind") == "client_profile" for p in folder.glob("*.yaml")):
            found.append(folder)
            return
        if depth >= 3:
            return
        for child in sorted(folder.iterdir()):
            if child.is_dir() and child.name not in SKIP_DIRS and not child.name.startswith("."):
                walk(child, depth + 1)

    walk(root, 0)
    return found


_bug_cache: dict[str, Any] = {}


def bug_switches(root: Path, connection: Mapping[str, Any]) -> list[dict[str, str]] | None:
    """The agent's bug switches, when its module lists them (BUG_HELP, or BUGS): zoo agents do."""
    target = connection.get("launch") or connection.get("target") or ""
    module = target.partition(":")[0]
    if not module or "://" in target:
        return None
    if module not in _bug_cache:
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        try:
            mod = importlib.import_module(module)
            helps = getattr(mod, "BUG_HELP", None) or {b: "" for b in sorted(getattr(mod, "BUGS", []) or [])}
            _bug_cache[module] = [{"name": k, "help": v} for k, v in helps.items()] or None
        except Exception:  # noqa: BLE001 - an agent that can't be imported here simply shows no switches
            _bug_cache[module] = None
    return _bug_cache[module]


def profile_info(root: Path, folder: Path, file: str) -> dict[str, Any]:
    info: dict[str, Any] = {"file": file, "ok": True, "problems": []}
    try:
        project = load_project(folder, file)
    except ProjectError as e:
        doc = _yaml(folder / file) or {}
        info.update(ok=False, problems=str(e).splitlines()[1:] or [str(e)], id=doc.get("id"),
                    name=doc.get("name", file), mode="voice" if doc.get("connection", {}).get("type")
                    == "websocket_audio" else "text", scenarios=[], personas=[])
        return info
    p = project.profile
    conn = p["connection"]
    mode = "voice" if conn["type"] == "websocket_audio" else "text"
    files = {}
    for path in sorted((folder / "scenarios").glob("*.yaml")):
        doc = _yaml(path)
        if isinstance(doc, dict) and doc.get("id"):
            files[doc["id"]] = f"scenarios/{path.name}"
    th = p.get("thresholds", {})
    info.update({
        "id": p["id"], "name": p.get("name", p["id"]), "description": p.get("description", ""),
        "domain": p.get("domain", ""), "mode": mode, "extends": (_yaml(folder / file) or {}).get("extends"),
        "connection": {"type": conn["type"], "target": conn.get("target", ""), "launch": conn.get("launch", "")},
        "options": {k: "" if v is None else str(v) for k, v in conn.get("options", {}).items()},
        "offline_options": {k: str(v) for k, v in conn.get("offline_options", {}).items()},
        "bugs": bug_switches(root, conn),
        "thresholds": th, "limits": p.get("limits", {}),
        "judges": (p.get("judges") or {}).get("enabled", True) is not False,
        "safety": _safety_info(p, project),
        "tools": [{"name": t["name"], "effect": t.get("effect", "")} for t in p.get("tools", [])],
        "backend": p.get("backend", {}).get("access", "none"),
        "logs": p.get("logs", {}).get("source", "none"),
        "scenarios": [{
            "id": s["id"], "title": s.get("title", s["id"]), "criticality": s.get("criticality", "normal"),
            "style": s["style"], "modes": s.get("modes", ["text", "voice"]),
            "in_mode": mode in s.get("modes", ["text", "voice"]), "tags": s.get("tags", []),
            "persona": s.get("caller", {}).get("persona"), "repeats": s.get("repeats") or th.get("repeats", 3),
            "file": files.get(s["id"]), "expected": s.get("outcome", {}).get("expected"),
            "checks": len(s.get("checks", [])) + sum(len(t.get("expect", [])) for t in s.get("turns", []))
            + (1 if s.get("outcome") else 0),
            "faults": [f"{f.get('tool')}: {f.get('type')}" for f in s.get("setup", {}).get("faults", [])],
            "audio": s.get("audio", {}),
        } for s in project.scenarios if not attacks.is_safety(s)],
        "personas": [{"id": k, "description": v.get("description", ""),
                      "voice": v.get("speech", {}).get("tts_voice")} for k, v in project.personas.items()],
    })
    return info


def _safety_info(profile: dict[str, Any], project) -> dict[str, Any]:
    """The built-in safety tests in short (the Safety tests card has the full list)."""
    c = attacks.catalog(profile, list(project.personas))
    return {"enabled": c["settings"]["enabled"], "intensity": c["settings"]["intensity"],
            "attacks": sum(a["on"] for a in c["attacks"]), "calls": c["calls"],
            "packs": sum(p["count"] > 0 for p in c["packs"])}


def project_keys(root: Path, folders: list[Path] | None = None) -> dict[str, str]:
    """A short, stable key per project for the dashboard's links: the folder's name, or its whole
    path with dashes when two projects share a name."""
    folders = find_projects(root) if folders is None else folders
    paths = [_rel(root, f) for f in folders]
    names = [p.rsplit("/", 1)[-1] for p in paths]
    return {p: (n if names.count(n) == 1 else p.replace("/", "-")) for p, n in zip(paths, names)}


def project_info(root: Path, folder: Path, key: str | None = None) -> dict[str, Any]:
    profiles = [p.name for p in sorted(folder.glob("*.yaml"))
                if isinstance(d := _yaml(p), dict) and d.get("kind") == "client_profile"]
    infos = [profile_info(root, folder, f) for f in profiles]
    main = next((i for i in infos if i["file"] == "profile.yaml"), infos[0] if infos else {})
    path = _rel(root, folder)
    return {"path": path, "key": key or project_keys(root).get(path, folder.name),
            "name": main.get("name", folder.name), "description": main.get("description", ""),
            "domain": main.get("domain") or (_yaml(folder / main["file"]) or {}).get("domain", "") if main else "",
            "example": path.startswith("examples/"), "channels": sorted({i["mode"] for i in infos}),
            "ready": bool(infos) and all(i["ok"] for i in infos), "profiles": infos}


def projects(root: Path) -> list[dict[str, Any]]:
    folders = find_projects(root)
    keys = project_keys(root, folders)
    return [project_info(root, f, keys[_rel(root, f)]) for f in folders]


def project_by_path(root: Path, path: str) -> dict[str, Any]:
    for folder in find_projects(root):
        if _rel(root, folder) == path:
            return project_info(root, folder)
    raise NotFound(f"no test project at {path!r}")


def project_by_key(root: Path, key: str) -> dict[str, Any]:
    folders = find_projects(root)
    keys = project_keys(root, folders)
    for folder in folders:
        if keys[_rel(root, folder)] == key:
            return project_info(root, folder, key)
    raise NotFound(f"no project {key!r}")


# ---- project files (the editor) -----------------------------------------------------------

def _project_folder(root: Path, path: str) -> Path:
    for folder in find_projects(root):
        if _rel(root, folder) == path:
            return folder
    raise NotFound(f"no test project at {path!r}")


def _file_path(folder: Path, rel: str) -> Path:
    parts = rel.split("/")
    ok = (len(parts) == 1 and FILE_NAME.match(parts[0])) or (
        len(parts) == 2 and parts[0] in ("scenarios", "personas") and FILE_NAME.match(parts[1]))
    if not ok:
        raise NotFound(f"{rel!r} is not a profile, scenario or persona file")
    return folder / Path(*parts)


def project_files(root: Path, path: str) -> list[dict[str, Any]]:
    folder = _project_folder(root, path)
    out = []
    for p in sorted(folder.glob("*.yaml")) + sorted((folder / "scenarios").glob("*.yaml")) + sorted(
            (folder / "personas").glob("*.yaml")):
        doc = _yaml(p)
        kind = doc.get("kind") if isinstance(doc, dict) else None
        if p.parent == folder and kind != "client_profile":
            continue
        out.append({"file": p.relative_to(folder).as_posix(), "kind": kind or "unreadable",
                    "id": doc.get("id") if isinstance(doc, dict) else None,
                    "title": (doc.get("title") or doc.get("name") or doc.get("description", "")) if isinstance(doc, dict) else "",
                    "size": p.stat().st_size})
    return out


def read_project_file(root: Path, path: str, rel: str) -> str:
    target = _file_path(_project_folder(root, path), rel)
    if not target.is_file():
        raise NotFound(f"{rel} not found")
    return target.read_text(encoding="utf-8")


def check_project_text(root: Path, path: str, rel: str, text: str) -> dict[str, Any]:
    """Validates the project as it would be with `text` in `rel`, without saving anything:
    YAML syntax, then every profile in the folder through the loader (schemas and references)."""
    folder = _project_folder(root, path)
    _file_path(folder, rel)
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        return {"ok": False, "syntax": False, "problems": [f"YAML syntax: {e}"]}
    if not isinstance(doc, dict):
        return {"ok": False, "syntax": False, "problems": ["the file must be a YAML mapping (key: value lines)"]}
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "project"
        shutil.copytree(folder, copy, ignore=shutil.ignore_patterns("runs", "*.db", "__pycache__"))
        (copy / Path(*rel.split("/"))).parent.mkdir(parents=True, exist_ok=True)
        (copy / Path(*rel.split("/"))).write_text(text, encoding="utf-8")
        profiles = [p.name for p in sorted(copy.glob("*.yaml"))
                    if isinstance(d := _yaml(p), dict) and d.get("kind") == "client_profile"]
        problems = []
        for f in profiles:
            try:
                load_project(copy, f)
            except ProjectError as e:
                problems += [f"with {f}: {line.strip()}" for line in str(e).splitlines()[1:] or [str(e)]]
    return {"ok": not problems, "syntax": True, "problems": problems, "kind": doc.get("kind")}


def write_project_file(root: Path, path: str, rel: str, text: str, create: bool = False) -> dict[str, Any]:
    folder = _project_folder(root, path)
    target = _file_path(folder, rel)
    if create and target.exists():
        raise ValueError(f"{rel} already exists")
    if not create and not target.exists():
        raise NotFound(f"{rel} not found")
    result = check_project_text(root, path, rel, text)
    if not result["syntax"]:
        return {**result, "saved": False}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")
    return {**result, "saved": True}


# ---- runs -------------------------------------------------------------------------------

def _run_dir(runs: Path, run_id: str) -> Path:
    if not RUN_ID.match(run_id) or not (runs / run_id).is_dir():
        raise NotFound(f"no run {run_id!r}")
    return runs / run_id


def _call_dir(runs: Path, run_id: str, test_id: str) -> Path:
    folder = _run_dir(runs, run_id) / "calls" / test_id
    if not TEST_ID.match(test_id) or not folder.is_dir():
        raise NotFound(f"no call {test_id!r} in {run_id}")
    return folder


def _safe_json(path: Path) -> Any:
    try:
        return read_json(path)
    except (OSError, ValueError):
        return None


def _started_from_id(run_id: str) -> str:
    d, t = run_id[2:10], run_id[11:17]
    return f"{d[:4]}-{d[4:6]}-{d[6:]}T{t[:2]}:{t[2:4]}:{t[4:]}Z"


def run_summary(runs: Path, run_id: str, active_runs: set[str] = frozenset()) -> dict[str, Any]:
    folder = runs / run_id
    info = _safe_json(folder / "run.json") or {}
    decision = _safe_json(folder / "decision.json")
    calls = sorted((folder / "calls").glob("t-*")) if (folder / "calls").is_dir() else []
    records = [r for r in (_safe_json(c / "call.json") for c in calls) if r]
    status = info.get("status") or ("finished" if decision else "incomplete")
    if status == "running" and run_id not in active_runs:
        status = "stopped"   # its process is gone (closed terminal, crash or a stop)
    if decision and status != "running":
        status = "finished"
    mode = info.get("mode") or (records[0].get("mode") if records else None)
    counts: dict[str, int] = {}
    for c in (decision or {}).get("calls", []):
        counts[c["status"]] = counts.get(c["status"], 0) + 1
    lat = (decision or {}).get("latency", {})
    return {
        "run_id": run_id, "started_at": info.get("started_at") or _started_from_id(run_id),
        "finished_at": info.get("finished_at") or (decision or {}).get("created_at"),
        "status": status, "decision": (decision or {}).get("decision"),
        "profile_id": info.get("profile_id") or (decision or {}).get("profile_id")
        or (records[0].get("profile_id") if records else None),
        "profile_name": info.get("profile_name"), "project": info.get("project"),
        "profile_file": info.get("profile_file"), "mode": mode, "offline": info.get("offline"),
        "agent_options": info.get("agent_options"), "pii_masked": info.get("pii_masked"),
        "planned": len(info["plan"]) if info.get("plan") else (decision or {}).get("coverage", {}).get("planned_calls"),
        "calls_started": len(records), "counts": counts,
        "p50_ms": lat.get("p50_ms"), "p95_ms": lat.get("p95_ms"),
        "cost_usd": round(sum(r.get("cost_usd") or 0 for r in records) + (info.get("judge_cost_usd") or 0), 4),
        "needs_review": (decision or {}).get("coverage", {}).get("needs_review_calls", 0),
        "judge_fails": sum(j.get("failed", 0) for j in (decision or {}).get("judges", [])),
        "safety": ({k: (decision or {})["safety"].get(k, 0) for k in ("calls", "passed", "failed", "to_review")}
                   if (decision or {}).get("safety") else None),
        "has_report": (folder / "report.html").exists(),
    }


def list_runs(runs: Path, active_runs: set[str] = frozenset()) -> list[dict[str, Any]]:
    if not runs.is_dir():
        return []
    ids = sorted((p.name for p in runs.iterdir() if p.is_dir() and RUN_ID.match(p.name)), reverse=True)
    return [run_summary(runs, i, active_runs) for i in ids]


def _call_brief(folder: Path) -> dict[str, Any] | None:
    record = _safe_json(folder / "call.json")
    if not record:
        return None
    verdict = _safe_json(folder / "verdict.json")
    out = {"test_id": record["test_id"], "scenario_id": record["scenario_id"], "repeat": record.get("repeat"),
           "attempt": record.get("attempt", 1), "persona_id": record.get("persona_id"), "mode": record.get("mode"),
           "state": record.get("status"), "started_at": record.get("started_at"), "ended_at": record.get("ended_at"),
           "cost_usd": record.get("cost_usd"), "status": None}
    if verdict:
        checks = verdict.get("checks", [])
        out.update({
            "status": verdict["status"],
            "headline": next((c["message"] for c in checks if c["check_id"] == "outcome"), ""),
            "outcome_class": (verdict.get("outcome") or {}).get("class"),
            "check_counts": {r: sum(c["result"] == r for c in checks) for r in {c["result"] for c in checks}},
            # Failures that count for the call; advisory judge findings (not calibrated yet) are listed apart,
            # so a PASS call doesn't show red chips that didn't change its result.
            "failed_checks": [c["check_id"] for c in checks if c["result"] in ("FAIL", "ERROR")
                              and c.get("can_block", True)],
            "judge_flags": [c["check_id"] for c in checks if c["result"] == "FAIL" and not c.get("can_block", True)],
            "needs_you": [c["check_id"] for c in checks if c["result"] == "NEEDS_REVIEW" and c.get("origin") == "judge"],
            "metrics": verdict.get("metrics", {}),
        })
    return out


def run_detail(runs: Path, run_id: str, active_runs: set[str] = frozenset()) -> dict[str, Any]:
    folder = _run_dir(runs, run_id)
    decision = _safe_json(folder / "decision.json")
    final_ids = {c["test_id"] for c in (decision or {}).get("calls", [])}
    calls = []
    for c in sorted((folder / "calls").glob("t-*")) if (folder / "calls").is_dir() else []:
        brief = _call_brief(c)
        if brief:
            brief["final"] = brief["test_id"] in final_ids if decision else None
            calls.append(brief)
    return {"summary": run_summary(runs, run_id, active_runs), "info": _safe_json(folder / "run.json"),
            "decision": decision, "calls": calls,
            "files": {name: (folder / name).exists() for name in ("report.html", "transcript.txt", "decision.json")}}


# Live masking: while a call runs, its events are on disk exactly as spoken. They are masked once the
# call is sealed (evidence.py lock). The dashboard shows a running call masked the same way.
_project_cache: dict[tuple[str, str, float], Any] = {}


def _project_for(root: Path, info: Mapping[str, Any]):
    folder = root / info["project"]
    key = (info["project"], info.get("profile_file", "profile.yaml"),
           max((p.stat().st_mtime for p in folder.rglob("*.yaml")), default=0))
    if key not in _project_cache:
        _project_cache.clear()
        _project_cache[key] = load_project(folder, key[1])
    return _project_cache[key]


def _masked_live(root: Path, info: Mapping[str, Any] | None, record: Mapping[str, Any],
                 events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    trace = [TraceEvent(e["seq"], e["t_ms"], e["source"], e["type"], e["data"], e.get("turn")) for e in events]
    try:
        project = _project_for(root, info or {})
        scenario = project.scenario(record["scenario_id"])
        masked = mask_events(trace, project.profile, scenario.get("caller", {}).get("facts", {}), record["test_id"])
        return [_event_dict(e) for e in masked]
    except Exception:  # noqa: BLE001 - can't mask safely: hide the free text until the call is sealed
        out = []
        for e in events:
            data = dict(e["data"])
            for f in TEXT_FIELDS.get(e["type"], ()):
                if isinstance(data.get(f), str):
                    data[f] = "(shown when the call is sealed)"
            if e["type"] in ("tool_request", "tool_response", "probe_result"):
                data = {k: v for k, v in data.items() if k not in ("args", "body", "rows")}
            out.append({**e, "data": data})
        return out


def _event_dict(e: TraceEvent) -> dict[str, Any]:
    d = {"seq": e.seq, "t_ms": e.t_ms, "source": e.source, "type": e.type, "data": e.data}
    if e.turn is not None:
        d["turn"] = e.turn
    return d


def read_events(path: Path) -> list[dict[str, Any]]:
    """events.jsonl, tolerating a half-written last line while the call is running."""
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError:
                break
    return out


def call_detail(root: Path, runs: Path, run_id: str, test_id: str) -> dict[str, Any]:
    folder = _call_dir(runs, run_id, test_id)
    record = _safe_json(folder / "call.json") or {}
    info = _safe_json(folder.parents[1] / "run.json")
    events = read_events(folder / record.get("events_file", "events.jsonl"))
    live = not record.get("locked")
    masked = record.get("pii_masked", (info or {}).get("pii_masked", True))
    if live and (info or {}).get("pii_masked", True):
        events = _masked_live(root, info, record, events)
        masked = True
    verdict = _safe_json(folder / "verdict.json")
    recording = next((e["data"]["recording"] for e in events
                      if e["type"] == "call_ended" and "recording" in e["data"]), None)
    return {
        "record": record, "live": live, "pii_masked": masked, "events": events, "verdict": verdict,
        "story": [{"seq": n, "who": who, "text": text} for n, who, text in story_rows(events)],
        "judgement": [{"label": a, "text": b} for a, b in judgement(verdict, events)] if verdict else [],
        "database": [{"probe": p, "rows": rows} for p, rows in database_rows(events)],
        "recording": ({**recording, "url": f"/runs/{run_id}/calls/{test_id}/{recording['file']}"}
                      if recording and (folder / recording["file"]).exists() else None),
        "files": [p.relative_to(folder).as_posix() for p in sorted(folder.rglob("*"))
                  if p.is_file() and p.parent.name != "llm"],
        "llm_logs": len(list((folder / "llm").glob("*.json"))) if (folder / "llm").is_dir() else 0,
    }


def call_signature(folder: Path) -> tuple:
    """Changes whenever anything the dashboard shows about a call changes on disk."""
    sig = []
    for name in ("call.json", "events.jsonl", "verdict.json"):
        try:
            st = (folder / name).stat()
            sig.append((st.st_size, st.st_mtime_ns))
        except OSError:
            sig.append(None)
    return tuple(sig)


def waveform(runs: Path, run_id: str, test_id: str, file: str = "audio/call.wav", points: int = 1200) -> dict[str, Any]:
    """Peaks per channel for drawing the recording (caller left, agent right)."""
    import numpy as np

    folder = _call_dir(runs, run_id, test_id)
    path = folder / file
    if file != "audio/call.wav" or not path.is_file():
        raise NotFound("no recording for this call")
    with wave.open(str(path), "rb") as w:
        n_ch, rate, n = w.getnchannels(), w.getframerate(), w.getnframes()
        x = np.frombuffer(w.readframes(n), dtype="<i2").astype(np.float32) / 32768.0
    x = x.reshape(-1, n_ch) if n_ch > 1 else x.reshape(-1, 1)
    points = max(50, min(points, 4000))
    size = max(1, len(x) // points)
    usable = (len(x) // size) * size
    chans = []
    for c in range(x.shape[1]):
        blocks = np.abs(x[:usable, c]).reshape(-1, size).max(axis=1) if usable else np.zeros(0)
        chans.append([round(float(v), 3) for v in blocks])
    names = ["caller", "agent"] if n_ch == 2 else [f"channel {i + 1}" for i in range(n_ch)]
    return {"duration_ms": int(len(x) * 1000 / rate), "sample_rate_hz": rate, "bucket_ms": size * 1000 / rate,
            "channels": dict(zip(names, chans))}


def exit_checks(runs: Path) -> list[dict[str, Any]]:
    folder = runs / ".ui" / "exit-checks"
    out = []
    for p in sorted(folder.glob("j-*.json"), reverse=True) if folder.is_dir() else []:
        doc = _safe_json(p)
        if doc:
            if not doc.get("project"):   # older summaries: the project of their first run
                first = next((r["run_id"] for r in doc.get("rows", []) if r.get("run_id")), None)
                doc["project"] = (_safe_json(runs / first / "run.json") or {}).get("project") if first else None
            out.append({"job_id": p.stem, "started_at": _started_from_job(p.stem), **doc})
    return out


def _started_from_job(job_id: str) -> str | None:
    m = re.match(r"^j-(\d{8})-(\d{6})", job_id)
    if not m:
        return None
    d, t = m.groups()
    return f"{d[:4]}-{d[4:6]}-{d[6:]}T{t[:2]}:{t[2:4]}:{t[4:]}"


# ---- the dashboard: every project at a glance, and one project's numbers -------------------

DECISIONS = ("GO", "NO_GO", "NO_GO_INSUFFICIENT_EVIDENCE")


def with_projects(rows: list[dict[str, Any]], projs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Adds the project's key and name to each run (None for a project that no longer exists)."""
    by_path = {p["path"]: p for p in projs}
    for r in rows:
        p = by_path.get(r.get("project") or "")
        r["project_key"] = p["key"] if p else None
        r["project_name"] = p["name"] if p else None
    return rows


def _gated(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Decided runs that judge the agent as it is: runs with bug switches on are meant to get NO-GO
    (they prove Vocto catches bugs), so they don't count against the GO rate."""
    return [r for r in runs if r.get("decision") and not (r.get("agent_options") or {}).get("bugs")]


def _go_rate(decided: list[dict[str, Any]], last: int = 20) -> float | None:
    recent = _gated(decided)[:last]
    return round(sum(r["decision"] == "GO" for r in recent) / len(recent), 3) if recent else None


def project_stats(project: Mapping[str, Any], runs: list[dict[str, Any]]) -> dict[str, Any]:
    """The numbers on a project's card and overview, from its runs (newest first)."""
    mine = [r for r in runs if r.get("project") == project["path"]]
    decided = [r for r in mine if r.get("decision")]
    main = next((p for p in project["profiles"] if p["file"] == "profile.yaml"), project["profiles"][0]
                if project["profiles"] else {})
    voice = next((p for p in project["profiles"] if p.get("mode") == "voice"), None)
    last_voice = next((r for r in decided if r.get("mode") == "voice" and r.get("p50_ms") is not None), None)
    th = (voice or main or {}).get("thresholds") or {}
    return {
        "runs": len(mine), "decided": len(decided),
        "go": sum(r["decision"] == "GO" for r in decided),
        "no_go": sum(r["decision"] != "GO" for r in decided),
        "go_rate": _go_rate(decided), "calls": sum(r.get("calls_started") or 0 for r in mine),
        "cost_usd": round(sum(r.get("cost_usd") or 0 for r in mine), 4),
        "last_run": mine[0] if mine else None,
        "last_decided": (_gated(decided) or decided or [None])[0],   # the latest real release decision
        "history": [r["decision"] for r in reversed(_gated(decided)[:12])],
        "bug_runs": len(decided) - len(_gated(decided)),
        "latency": {"p50_ms": last_voice["p50_ms"], "p95_ms": last_voice["p95_ms"], "at": last_voice["started_at"]}
        if last_voice else None,
        "latency_limits": {"p50_ms": th.get("latency_p50_ms"), "p95_ms": th.get("latency_p95_ms")} if voice else None,
        "scenarios": len({s["id"] for p in project["profiles"] for s in p.get("scenarios", [])}),
        "critical": len({s["id"] for p in project["profiles"] for s in p.get("scenarios", [])
                         if s.get("criticality") == "critical"}),
        "personas": len({x["id"] for p in project["profiles"] for x in p.get("personas", [])}),
    }


def _day(ts: str | None) -> str | None:
    return ts[:10] if ts else None


def activity(runs: list[dict[str, Any]], days: int = 14, today: str | None = None) -> list[dict[str, Any]]:
    """Runs per day for the last `days` days, split by decision (oldest day first)."""
    import datetime as dt

    end = dt.date.fromisoformat(today) if today else dt.datetime.now(dt.timezone.utc).date()
    out = []
    for i in range(days - 1, -1, -1):
        day = (end - dt.timedelta(days=i)).isoformat()
        rows = [r for r in runs if _day(r.get("started_at")) == day]
        out.append({"day": day, **{d: sum(r.get("decision") == d for r in rows) for d in DECISIONS},
                    "none": sum(not r.get("decision") for r in rows)})
    return out


def dashboard(root: Path, runs_dir: Path, active_runs: set[str] = frozenset(),
              today: str | None = None) -> dict[str, Any]:
    """Everything the home page shows: totals, agent types, activity, each project's health."""
    import datetime as dt

    projs = projects(root)
    runs = with_projects(list_runs(runs_dir, active_runs), projs)
    decided = [r for r in runs if r.get("decision")]
    end = dt.date.fromisoformat(today) if today else dt.datetime.now(dt.timezone.utc).date()
    week = (end - dt.timedelta(days=6)).isoformat()
    domains: dict[str, int] = {}
    for p in projs:
        d = (p.get("domain") or "general").strip().lower() or "general"
        domains[d] = domains.get(d, 0) + 1
    rows = []
    for p in projs:
        rows.append({k: p[k] for k in ("key", "name", "description", "domain", "example", "channels", "ready")}
                    | {"stats": project_stats(p, runs)})
    return {
        "totals": {
            "projects": len(projs), "client_projects": sum(not p["example"] for p in projs),
            "examples": sum(p["example"] for p in projs), "runs": len(runs),
            "runs_7d": sum((_day(r.get("started_at")) or "") >= week for r in runs),
            "decided": len(decided), "go": sum(r["decision"] == "GO" for r in decided),
            "no_go": sum(r["decision"] == "NO_GO" for r in decided),
            "insufficient": sum(r["decision"] == "NO_GO_INSUFFICIENT_EVIDENCE" for r in decided),
            "go_rate": _go_rate(decided), "bug_runs": len(decided) - len(_gated(decided)),
            "calls": sum(r.get("calls_started") or 0 for r in runs),
            "cost_usd": round(sum(r.get("cost_usd") or 0 for r in runs), 4),
            "scenarios": sum(x["stats"]["scenarios"] for x in rows),
        },
        "agent_types": {
            "domains": sorted(({"domain": k, "projects": v} for k, v in domains.items()),
                              key=lambda x: (-x["projects"], x["domain"])),
            "channels": {c: sum(c in p["channels"] for p in projs) for c in ("voice", "text")},
        },
        "activity": activity(runs, 14, end.isoformat()),
        "projects": rows,
        "recent_runs": runs[:8],
    }


def project_overview(root: Path, runs_dir: Path, key: str, active_runs: set[str] = frozenset()) -> dict[str, Any]:
    project = project_by_key(root, key)
    runs = [r for r in with_projects(list_runs(runs_dir, active_runs), [project]) if r["project_key"] == key]
    return {"project": project, "stats": project_stats(project, runs), "activity": activity(runs, 14),
            "recent_runs": runs[:8]}


# ---- secrets: the ${NAME} values projects refer to, kept in .env ---------------------------

SECRET_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
SECRET_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,99}$")


def _dotenv(root: Path) -> dict[str, str]:
    path = root / ".env"
    out: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            key, sep, value = line.strip().removeprefix("export ").partition("=")
            if sep and key.strip() and not key.strip().startswith("#"):
                out[key.strip()] = value.strip().strip("'\"")
    return out


def secrets(root: Path) -> list[dict[str, Any]]:
    """Each ${NAME} a project uses, which projects use it, and whether it has a value."""
    used: dict[str, set[str]] = {}
    for folder in find_projects(root):
        main = _yaml(folder / "profile.yaml") or {}
        p = {"name": main.get("name") or folder.name}
        for f in [*folder.glob("*.yaml"), *(folder / "scenarios").glob("*.yaml")]:
            try:
                text = f.read_text(encoding="utf-8")
            except OSError:
                continue
            for line in text.splitlines():
                if line.lstrip().startswith("#"):
                    continue
                for name in SECRET_REF.findall(line):
                    used.setdefault(name, set()).add(p["name"])
    env = _dotenv(root)
    out = []
    for name in sorted(used):
        value = os.environ.get(name) or env.get(name)
        out.append({"name": name, "projects": sorted(used[name]), "set": bool(value),
                    "hint": f"…{value[-4:]}" if value and len(value) > 8 else None})
    return out


def _save_env(root: Path, name: str, value: str) -> None:
    path = root / ".env"
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.is_file() else []
    out, done = [], False
    for line in lines:
        if line.strip().removeprefix("export ").partition("=")[0].strip() == name:
            if not done:
                out.append(f"{name}={value}")
                done = True
            continue
        out.append(line)
    if not done:
        out.append(f"{name}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.environ[name] = value   # runs and connection tests started from now on use it


def save_secret(root: Path, name: str, value: str) -> None:
    value = value.strip()
    if not SECRET_NAME.match(name or ""):
        raise ValueError("a secret's name uses letters, digits and _ only, like ACME_API_TOKEN")
    if not value or "\n" in value or "\r" in value or len(value) > 4000:
        raise ValueError("give the secret a value (one line)")
    _save_env(root, name, value)


# ---- system -------------------------------------------------------------------------------

def _dotenv_key(root: Path) -> str | None:
    path = root / ".env"
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        key, _, value = line.strip().removeprefix("export ").partition("=")
        if key.strip() == "ANTHROPIC_API_KEY" and value.strip().strip("'\""):
            return value.strip().strip("'\"")
    return None


def _size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) if path.is_dir() else (
        path.stat().st_size if path.exists() else 0)


def system(root: Path, runs: Path) -> dict[str, Any]:
    key = os.environ.get("ANTHROPIC_API_KEY") or _dotenv_key(root)
    models = Path(os.environ.get("VATF_MODELS_DIR") or root / ".models")
    piper = sorted(p.stem for p in (models / "piper").glob("*.onnx")) if (models / "piper").is_dir() else []
    whisper = []
    if (models / "whisper").is_dir():
        for d in sorted((models / "whisper").iterdir()):
            m = re.match(r"models--Systran--faster-whisper-(.+)", d.name)
            if m and d.is_dir():
                whisper.append({"name": m.group(1), "bytes": _size(d)})
    return {
        "version": __version__, "python": platform.python_version(), "platform": platform.platform(terse=True),
        "root": str(root), "runs_dir": str(runs), "default_model": DEFAULT_MODEL,
        "api_key": {"set": bool(key), "hint": f"…{key[-4:]}" if key and len(key) > 8 else None,
                    "source": "shell" if os.environ.get("ANTHROPIC_API_KEY") and not _dotenv_key(root) else
                    (".env" if key else None)},
        "models": {"folder": str(models), "piper_voices": piper, "whisper": whisper,
                   "bytes": _size(models) if models.is_dir() else 0},
    }


def save_api_key(root: Path, key: str) -> None:
    key = key.strip()
    if not key or any(c.isspace() for c in key) or len(key) > 400:
        raise ValueError("that doesn't look like an API key")
    _save_env(root, "ANTHROPIC_API_KEY", key)
