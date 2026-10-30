"""Loads a test project folder (profile, personas, scenarios) and checks it against the schemas."""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from . import attacks
from .rubrics import library, unknown_items
from .util import sha256_file

# The schemas live at the repository root, next to src/.
SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schemas"


class ProjectError(Exception):
    """The project files are invalid. Nothing has been run."""


_validators: dict[str, tuple[float, Draft202012Validator]] = {}


def schema_validator(name: str) -> Draft202012Validator:
    """The schema's validator, read once and again only when the file changes (a project with safety
    tests checks dozens of generated scenarios, and the dashboard loads projects often)."""
    path = SCHEMA_DIR / f"{name}.schema.yaml"
    mtime = path.stat().st_mtime
    cached = _validators.get(name)
    if cached and cached[0] == mtime:
        return cached[1]
    validator = Draft202012Validator(yaml.safe_load(path.read_text(encoding="utf-8")))
    _validators[name] = (mtime, validator)
    return validator


def schema_errors(name: str, doc: Any) -> list[str]:
    errors = sorted(schema_validator(name).iter_errors(doc), key=lambda e: list(e.path))
    return [f"{'/'.join(map(str, e.path)) or '(top)'}: {e.message}" for e in errors]


@dataclass
class Project:
    folder: Path
    profile: dict[str, Any]
    profile_sha256: str
    personas: dict[str, dict[str, Any]]
    scenarios: list[dict[str, Any]]
    scenario_sha256: dict[str, str] = field(default_factory=dict)
    profile_file: str = "profile.yaml"

    def scenario(self, scenario_id: str) -> dict[str, Any]:
        return next(s for s in self.scenarios if s["id"] == scenario_id)

    def with_agent_options(self, options: dict[str, Any]) -> "Project":
        """A copy whose connection.options are updated (e.g. bug switches from the command line)."""
        clone = copy.deepcopy(self)
        conn = clone.profile.setdefault("connection", {})
        conn["options"] = {**conn.get("options", {}), **options}
        return clone


def _load_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def merge(base: Any, over: Any) -> Any:
    """Mappings merge key by key; anything else in `over` replaces the base value."""
    if isinstance(base, dict) and isinstance(over, dict):
        return {**base, **{k: merge(base.get(k), v) if k in base else v for k, v in over.items()}}
    return copy.deepcopy(over)


def load_profile(folder: Path, name: str) -> tuple[dict[str, Any], str]:
    """The profile with its `extends` chain applied, and a hash over every file involved."""
    chain, path = [], folder / name
    while True:
        if not path.exists():
            raise ProjectError(f"{path} not found")
        if path in [p for p, _ in chain]:
            raise ProjectError(f"{path}: extends loops back on itself")
        doc = _load_yaml(path)
        if not isinstance(doc, dict):
            raise ProjectError(f"{path}: not a YAML mapping")
        chain.append((path, doc))
        if not doc.get("extends"):
            break
        path = folder / doc["extends"]
    profile: dict[str, Any] = {}
    for _, doc in reversed(chain):
        profile = merge(profile, {k: v for k, v in doc.items() if k != "extends"})
    digest = hashlib.sha256("".join(sha256_file(p) for p, _ in chain).encode()).hexdigest() \
        if len(chain) > 1 else sha256_file(chain[0][0])
    return profile, digest


def load_project(folder: str | Path, profile_file: str = "profile.yaml") -> Project:
    folder = Path(folder)
    problems: list[str] = []

    profile, profile_sha256 = load_profile(folder, profile_file)
    problems += [f"{profile_file}: {e}" for e in schema_errors("client_profile", profile)]
    problems += [f"{profile_file}: judges.items: unknown rubric item {i!r}" for i in unknown_items(profile)]

    personas = {}
    for path in sorted((folder / "personas").glob("*.yaml")):
        doc = _load_yaml(path)
        problems += [f"personas/{path.name}: {e}" for e in schema_errors("persona", doc)]
        personas[doc.get("id", path.stem)] = doc

    scenarios, hashes = [], {}
    tools = {t["name"]: t for t in profile.get("tools", [])}
    probes = {p["id"] for p in profile.get("backend", {}).get("probes", [])}
    for path in sorted((folder / "scenarios").glob("*.yaml")):
        doc = _load_yaml(path)
        where = f"scenarios/{path.name}"
        problems += [f"{where}: {e}" for e in schema_errors("scenario", doc)]
        problems += [f"{where}: {e}" for e in _reference_problems(doc, profile, personas, tools, probes)]
        scenarios.append(doc)
        hashes[doc.get("id", path.stem)] = sha256_file(path)

    has_files = bool(scenarios)
    # The built-in safety tests (attacks.py), written from the profile. A file with the same id wins.
    for doc in attacks.generate(profile, list(personas)):
        if doc["id"] in hashes:
            continue
        problems += [f"safety test {doc['id']}: {e}" for e in schema_errors("scenario", doc)]
        problems += [f"safety test {doc['id']}: {e}" for e in _reference_problems(doc, profile, personas, tools, probes)]
        scenarios.append(doc)
        hashes[doc["id"]] = attacks.digest(doc)

    if problems:
        raise ProjectError("the project files have problems:\n  " + "\n  ".join(problems))
    if not has_files:
        raise ProjectError(f"no scenarios found in {folder / 'scenarios'}")
    return Project(folder, profile, profile_sha256, personas, scenarios, hashes, profile_file)


def _reference_problems(sc, profile, personas, tools, probes) -> list[str]:
    out = []
    if sc.get("profile") != profile.get("id"):
        out.append(f"written for profile {sc.get('profile')!r}, not {profile.get('id')!r}")
    if "caller" in sc and "persona" in sc["caller"] and sc["caller"]["persona"] not in personas:
        out.append(f"unknown persona {sc['caller'].get('persona')!r}")
    facts = sc.get("caller", {}).get("facts", {})
    for fault in sc.get("setup", {}).get("faults", []):
        if fault.get("tool") not in tools:
            out.append(f"fault on unknown tool {fault.get('tool')!r}")
    checks = list(sc.get("checks", [])) + [c for t in sc.get("turns", []) for c in t.get("expect", [])]
    known_items = set(library()) | {c["id"] for c in (profile.get("judges") or {}).get("custom", [])}
    for c in checks:
        for key in ("tool", "first", "then"):
            if key in c and c[key] not in tools:
                out.append(f"check uses unknown tool {c[key]!r}")
        if "probe" in c and c["probe"] not in probes:
            out.append(f"check uses unknown probe {c['probe']!r}")
        for name in c.get("entities", []):
            if name not in facts:
                out.append(f"entity {name!r} is not a caller fact")
        if c.get("type") == "judge" and c.get("rubric") and not c.get("intent") and c["rubric"] not in known_items:
            out.append(f"judge check uses unknown rubric item {c['rubric']!r} (give intent: for a one-off question)")
    out_block = sc.get("outcome")
    if out_block:
        action = out_block["action"]["tool"]
        if tools.get(action, {}).get("effect") != "write":
            out.append(f"outcome action {action!r} is not a write tool")
        if "record" in out_block and out_block["record"]["probe"] not in probes:
            out.append(f"outcome uses unknown probe {out_block['record']['probe']!r}")
    return out
