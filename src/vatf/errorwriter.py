"""AI error writer: how this agent's own tools would say no, for the business refusal drills.

A refusal drill (drills.py: already taken, payment declined, out of stock) makes a booking or order tool
answer with a business "no" instead of crashing. The agent should read that answer, say so and offer
another option. A plain {"error": "conflict"} tests that only half way: real APIs answer in their own
shape and words. This asks Claude to write, per tool and kind, the answer that tool would most likely
give, from its name, description and the agent's industry. Drafts only: the Resilience page shows them,
the person edits or ticks them and presses Save (profile resilience.refusals). Nothing here is measured
or decided by AI: the drill's results still come from the tool traffic and the database.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from . import drills
from .loader import Project
from .planner import PLAN_MODEL, PlanError, _ask

KINDS = [k for k, v in drills.FAILURES.items() if v.get("refusal")]

SYSTEM = """You write realistic API error answers for testing AI voice agents. Given one tool of an agent (its
name, what it does, the agent's industry) and a kind of business refusal, write the HTTP status and the JSON body
that a typical real API for that tool would send back. Keep the shape such APIs use (an error code, a message a
developer would write, and the fields such an API usually adds, e.g. the slot or item, or a decline reason). Never
include personal data, secrets or real card numbers. Never make the answer look like a success. Answer with JSON only."""


def _schema(n: int) -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False, "required": ["answers"], "properties": {
        "answers": {"type": "array", "maxItems": n, "items": {
            "type": "object", "additionalProperties": False, "required": ["tool", "kind", "status", "body_json"],
            "properties": {"tool": {"type": "string"}, "kind": {"type": "string", "enum": KINDS},
                           "status": {"type": "integer"}, "body_json": {"type": "string"}}}}}}


def action_tools(profile: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [t for t in profile.get("tools", []) if t.get("effect") == "write"]


async def draft(project: Project, llm, kinds: list[str] | None = None, tools: list[str] | None = None,
                model: str = PLAN_MODEL, log_dir: Path | None = None) -> list[dict[str, Any]]:
    """Drafts (not saved): one refusal answer per write tool and kind."""
    profile = project.profile
    kinds = [k for k in (kinds or KINDS) if k in KINDS]
    if not kinds:
        raise PlanError(f"pick a kind of refusal ({', '.join(KINDS)})")
    pick = [t for t in action_tools(profile) if not tools or t["name"] in tools]
    if not pick:
        raise PlanError("this agent has no tool that books, orders or changes data (effect: write)")
    lines = [f"Agent: {profile.get('name', 'the agent')} ({profile.get('domain', 'no industry given')})",
             "Tools that change data:"]
    lines += [f"- {t['name']}: {t.get('description', '(no description)')}" for t in pick]
    lines += ["", "Kinds of refusal:"]
    lines += [f"- {k}: {drills.FAILURES[k]['about']}" for k in kinds]
    lines += ["", "Write one answer for each tool and kind that makes sense for it (skip a kind that never happens "
                  "for a tool, e.g. out of stock for a booking). body_json is the JSON body as a string."]
    n = len(pick) * len(kinds)
    answer = await _ask(llm, "error-writer", model, SYSTEM, "\n".join(lines), _schema(n), 3000, log_dir)
    return check_drafts(profile, answer.get("answers", [])[:n])


def check_drafts(profile: Mapping[str, Any], raw: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Each draft as a profile resilience.refusals entry, with what is wrong with it."""
    names = {t["name"] for t in action_tools(profile)}
    out = []
    for a in raw:
        problems = []
        try:
            body: Any = json.loads(str(a.get("body_json") or ""))
        except ValueError:
            body, problems = str(a.get("body_json") or ""), ["the answer isn't valid JSON (it is kept as text)"]
        status = int(a.get("status") or 0)
        if not 400 <= status <= 599:
            problems.append(f"status {status} isn't an error")
            status = drills.FAILURES.get(str(a.get("kind")), {}).get("fault", {}).get("status", 409) if a.get("kind") in KINDS else 409
        if a.get("tool") not in names:
            problems.append(f"{a.get('tool')!r} isn't one of this agent's tools that change data")
        text = json.dumps(body).lower()
        if any(w in text for w in ('"success": true', '"ok": true', '"status": "booked"', '"confirmed"')):
            problems.append("it reads like a success")
        out.append({"refusal": {"tool": a.get("tool"), "kind": a.get("kind"), "status": status, "body": body,
                                "from": "ai-writer"}, "problems": problems})
    return out
