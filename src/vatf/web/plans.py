"""The test plan writer on the dashboard (Set up > Test plan): the agent's instructions, the latest draft
plan, writing a new one with Claude, adding the ticked drafts as tests, and the persona library."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Mapping

from .. import personas, planner
from ..llm import Budget, ClaudeClient, LLMError
from ..loader import ProjectError, load_profile, load_project
from . import data


def _folder(root: Path, key: str) -> Path:
    return root / data.project_by_key(root, key)["path"]


def plan_page(root: Path, key: str, file: str = "profile.yaml") -> dict[str, Any]:
    folder = _folder(root, key)
    profile, _ = load_profile(folder, file)
    have = {p.stem for p in (folder / "personas").glob("*.yaml")}
    plan = planner.latest_plan(folder)
    if plan:
        exists = {t["id"] for t in plan["tests"] if (folder / t["file"]).exists()}
        for t in plan["tests"]:
            t["accepted"] = t["id"] in exists
    return {
        "agent_prompt": profile.get("agent_prompt") or "",
        "has_rules": bool((profile.get("judges") or {}).get("policy")),
        "plan": plan,
        "mix": planner.MIX, "categories": planner.CATEGORY_TITLES, "model": planner.PLAN_MODEL,
        "api_key": bool(os.environ.get("ANTHROPIC_API_KEY") or data._dotenv_key(root)),
        "library": [{**p, "added": p["id"] in have} for p in personas.catalog()],
    }


def generate(root: Path, key: str, body: Mapping[str, Any]) -> dict[str, Any]:
    folder = _folder(root, key)
    file = body.get("file", "profile.yaml")
    api_key = os.environ.get("ANTHROPIC_API_KEY") or data._dotenv_key(root)
    if not api_key:
        raise ValueError("add your Claude API key in Settings first: the test plan is written by Claude")
    try:
        project = load_project(folder, file)
    except ProjectError as e:
        raise ValueError(f"fix the project first: {e}") from e
    llm = ClaudeClient(Budget(project.profile.get("limits", {}).get("budget_usd", 5)), effort="medium", api_key=api_key)
    try:
        plan = asyncio.run(planner.write_plan(project, llm, count=int(body.get("count") or 20),
                                              focus=str(body.get("focus") or ""), model=str(body.get("model") or planner.PLAN_MODEL),
                                              log_dir=folder / "plans" / "llm"))
    except (planner.PlanError, LLMError) as e:
        raise ValueError(str(e)) from e
    planner.save_plan(folder, plan)
    return plan_page(root, key, file)


def add_tests(root: Path, key: str, body: Mapping[str, Any]) -> dict[str, Any]:
    folder = _folder(root, key)
    plan = planner.latest_plan(folder)
    if not plan:
        raise data.NotFound("no test plan yet")
    out = planner.accept(folder, plan, list(body.get("ids") or []), body.get("criticality") or {},
                         body.get("file", "profile.yaml"))
    return {**out, "page": plan_page(root, key, body.get("file", "profile.yaml"))}


def add_personas(root: Path, key: str, body: Mapping[str, Any]) -> dict[str, Any]:
    added = planner.add_personas(_folder(root, key), list(body.get("ids") or []))
    return {"added": added}
