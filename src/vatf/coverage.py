"""What the Functional test cases cover: the agent's jobs, its rules and its write tools, and the gaps.

A run can be GO and still have tested only the happy path. This lists, with no AI:
  jobs   (intents)   from the test plan writer's reading of the agent's instructions, each with its
                     test cases split into normal calls, edge cases and misuse;
  rules              each rule with the test cases linked to it;
  write tools        each tool that changes data, with the cases where it must work and the cases
                     where it must not be used (or must fail honestly).
A case is linked to a job or rule when the plan writer wrote it for that job or rule and it was added
(plans/*.json). Cases written by hand still count for the write tools, from their outcome and checks.
The gaps become a focus line for "Write tests for these", which asks the plan writer for just those.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import modes

GROUPS = ("happy", "edge", "adversarial")
GROUP_TITLES = {"happy": "Normal", "edge": "Edge cases", "adversarial": "Misuse"}


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text).lower()).strip()


def _plans(folder: Path) -> list[dict[str, Any]]:
    """Every saved plan, oldest first (the latest of each type is a copy, so skip the latest-*.json)."""
    d = Path(folder) / "plans"
    out = []
    for p in sorted(d.glob("plan-*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    if not out:
        for name in ("latest-functional.json", "latest.json"):
            p = d / name
            if p.is_file():
                try:
                    out.append(json.loads(p.read_text(encoding="utf-8")))
                    break
                except (OSError, ValueError):
                    pass
    return out


def intent_of(folder: Path) -> dict[str, str]:
    """{test case id: the job (intent) the plan writer wrote it for}, from every saved Functional plan."""
    out: dict[str, str] = {}
    for p in _plans(folder):
        if (p.get("test_type") or "functional") != "functional":
            continue
        for t in p.get("tests", []):
            if t.get("intent") and t.get("id"):
                out[t["id"]] = t["intent"]
    return out


def _group_of(s: Mapping[str, Any], planned: Mapping[str, Any] | None) -> str:
    if planned and planned.get("category") in GROUPS:
        return planned["category"]
    expected = (s.get("outcome") or {}).get("expected")
    tags = set(s.get("tags") or [])
    if tags & {"misuse", "adversarial", "abuse"}:
        return "adversarial"
    if expected in (None, "success"):
        return "happy"
    return "edge"


def functional_coverage(folder: Path, profile: Mapping[str, Any], scenarios: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    cases = [s for s in scenarios if not str(s.get("id", "")).startswith("safety.") and modes.type_of(s) == "functional"]
    by_id = {s["id"]: s for s in cases}
    plans = [p for p in _plans(folder) if (p.get("test_type") or "functional") == "functional"]
    latest = plans[-1] if plans else None
    analysis = (latest or {}).get("analysis") or {}

    # Which job and rules each case was written for (any plan; rules matched by their text).
    planned: dict[str, dict[str, Any]] = {}
    for p in plans:
        for t in p.get("tests", []):
            if t.get("id") in by_id:
                planned[t["id"]] = t
    rule_key = {r["id"]: _norm(r["text"]) for r in analysis.get("rules", [])}
    key_rule = {v: k for k, v in rule_key.items()}

    intents = []
    for it in analysis.get("intents", []):
        linked = [sid for sid, t in planned.items() if t.get("intent") == it["id"]]
        counts = {g: sum(1 for sid in linked if _group_of(by_id[sid], planned.get(sid)) == g) for g in GROUPS}
        intents.append({"id": it["id"], "title": it["id"].replace("_", " ").capitalize(),
                        "description": it.get("description", ""), "counts": counts,
                        "cases": [{"id": sid, "title": by_id[sid].get("title", sid)} for sid in linked]})
    rules = []
    for r in analysis.get("rules", []):
        linked = [sid for sid, t in planned.items()
                  if any(key_rule.get(_norm(x.get("text", ""))) == r["id"] for x in t.get("rules", []))]
        rules.append({"id": r["id"], "text": r["text"], "kind": r["kind"],
                      "cases": [{"id": sid, "title": by_id[sid].get("title", sid)} for sid in linked]})

    tools = []
    for t in profile.get("tools", []):
        if t.get("effect") != "write":
            continue
        works, refuses = [], []
        for s in cases:
            outs = ([s["outcome"]] if s.get("outcome") else []) + list(s.get("more_outcomes") or [])
            checks = list(s.get("checks", [])) + [c for turn in s.get("turns", []) for c in turn.get("expect", [])]
            for o in outs:
                if o["action"]["tool"] == t["name"]:
                    (works if o["expected"] in ("success", "recover_or_honest") else refuses).append(s)
            if any(c.get("type") == "tool_not_called" and c.get("tool") == t["name"] for c in checks):
                refuses.append(s)
        tools.append({"name": t["name"], "description": t.get("description", ""),
                      "works": [{"id": s["id"], "title": s.get("title", s["id"])} for s in {s["id"]: s for s in works}.values()],
                      "refuses": [{"id": s["id"], "title": s.get("title", s["id"])} for s in {s["id"]: s for s in refuses}.values()]})

    gaps: list[dict[str, str]] = []
    for it in intents:
        if not any(it["counts"].values()):
            gaps.append({"kind": "intent", "id": it["id"], "text": f"the job {it['id']} has no test case"})
        else:
            missing = [GROUP_TITLES[g].lower() for g in ("edge", "adversarial") if not it["counts"][g]]
            if missing:
                gaps.append({"kind": "intent", "id": it["id"],
                             "text": f"the job {it['id']} has no {' or '.join(missing)}"})
    for r in rules:
        if not r["cases"] and r["kind"] != "fact":
            gaps.append({"kind": "rule", "id": r["id"], "text": f"rule {r['id']} ({_short(r['text'])}) has no test case"})
    for t in tools:
        if not t["works"]:
            gaps.append({"kind": "tool", "id": t["name"], "text": f"no case where {t['name']} must work"})
        if not t["refuses"]:
            gaps.append({"kind": "tool", "id": t["name"],
                         "text": f"no case where {t['name']} must not be used or must fail honestly"})
    focus = ("Write test cases for these gaps only: " + "; ".join(g["text"] for g in gaps[:12]) + ".") if gaps else ""
    return {"has_plan": bool(analysis), "plan_date": (latest or {}).get("created_at"),
            "cases": len(cases), "intents": intents, "rules": rules, "tools": tools, "gaps": gaps, "focus": focus}


def _short(text: str, n: int = 70) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"
