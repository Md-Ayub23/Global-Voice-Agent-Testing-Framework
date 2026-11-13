"""The Safety tests card: every built-in attack call for one project and how the latest safety run went."""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any, Mapping

from .. import attacks, languages
from ..llm import Budget, ClaudeClient, LLMError
from ..loader import ProjectError, load_profile, load_project
from ..planner import PlanError
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
    out["languages"] = [{"id": k, "name": v["name"]} for k, v in languages.LANGUAGES.items() if k != "en"]
    out["tools"] = [{"name": t["name"], "effect": t.get("effect", "read")} for t in profile.get("tools", [])]
    out["last_run"] = None
    for r in data.list_runs(runs_dir, active):
        if r.get("project") != project["path"] or not r.get("safety"):
            continue
        decision = data._safe_json(runs_dir / r["run_id"] / "decision.json") or {}
        out["last_run"] = {"run_id": r["run_id"], "decision": r.get("decision"),
                           "started_at": r.get("started_at"), **decision.get("safety", {})}
        break
    return out


def write_attacks(root: Path, key: str, body: Mapping[str, Any]) -> dict[str, Any]:
    """The AI attack writer (attackwriter.py): drafts only, for the person to tick and save in the form."""
    from .. import attackwriter

    project = data.project_by_key(root, key)
    folder = root / project["path"]
    file = str(body.get("file") or "profile.yaml")
    api_key = os.environ.get("ANTHROPIC_API_KEY") or data._dotenv_key(root)
    if not api_key:
        raise ValueError("add your Claude API key in Settings first: attacks are written by Claude")
    try:
        loaded = load_project(folder, file)
    except ProjectError as e:
        raise ValueError(f"fix the project first: {e}") from e
    llm = ClaudeClient(Budget(1.0), effort="medium", api_key=api_key)
    try:
        drafts = asyncio.run(attackwriter.draft(
            loaded, llm, str(body.get("kind") or "new"), int(body.get("count") or 4), str(body.get("attack_id") or ""),
            str(body.get("language") or ""), str(body.get("pack") or ""), log_dir=folder / "plans" / "llm"))
    except (PlanError, LLMError) as e:
        raise ValueError(str(e)) from e
    return {"drafts": drafts, "cost_usd": round(getattr(llm, "spent_usd", 0.0), 4)}


def resilience_page(root: Path, key: str, file: str = "profile.yaml") -> dict[str, Any]:
    """The Resilience page's refusal answers: this agent's tools that change data, the saved answers, and
    Vocto's plain ones (drills.FAILURES) used when none is saved."""
    from .. import drills, errorwriter

    project = data.project_by_key(root, key)
    profile = load_profile(root / project["path"], file)[0]
    return {"tools": [t["name"] for t in errorwriter.action_tools(profile)],
            "kinds": [{"id": k, "title": drills.FAILURES[k]["title"], "about": drills.FAILURES[k]["about"],
                       "status": drills.FAILURES[k]["fault"]["status"], "body": drills.FAILURES[k]["fault"]["body"]}
                      for k in errorwriter.KINDS],
            "refusals": list((profile.get("resilience") or {}).get("refusals") or [])}


def write_refusals(root: Path, key: str, body: Mapping[str, Any]) -> dict[str, Any]:
    """The AI error writer (errorwriter.py): drafts only, for the person to check and save."""
    from .. import errorwriter

    project = data.project_by_key(root, key)
    folder = root / project["path"]
    file = str(body.get("file") or "profile.yaml")
    api_key = os.environ.get("ANTHROPIC_API_KEY") or data._dotenv_key(root)
    if not api_key:
        raise ValueError("add your Claude API key in Settings first: the answers are written by Claude")
    try:
        loaded = load_project(folder, file)
    except ProjectError as e:
        raise ValueError(f"fix the project first: {e}") from e
    llm = ClaudeClient(Budget(0.5), effort="low", api_key=api_key)
    try:
        drafts = asyncio.run(errorwriter.draft(loaded, llm, list(body.get("kinds") or []) or None,
                                               list(body.get("tools") or []) or None, log_dir=folder / "plans" / "llm"))
    except (PlanError, LLMError) as e:
        raise ValueError(str(e)) from e
    return {"drafts": drafts, "cost_usd": round(getattr(llm, "spent_usd", 0.0), 4)}
