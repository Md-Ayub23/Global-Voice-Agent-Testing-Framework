"""Caller variations: the same test case, asked by callers who say it differently.

A case that passes with one wording can fail with another ("book me in Friday" vs "is there anything
at the end of the week?"). This asks Claude to rewrite a simulated case's caller a few ways: other
words, another order of details, another kind of caller, one small realistic complication. What must
NOT change is kept by Vocto, not by the model: the facts (so the probes and checks still hold), the
expected result, the outcome, the checks and the set-up are copied from the original case. Each draft
is checked like a hand-written case, and nothing is saved until a person ticks it.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Mapping

from . import personas as library
from .loader import Project, _reference_problems, load_project, schema_errors
from .planner import PLAN_MODEL, PlanError, _ask, _dump, _slug

SYSTEM = """You are a senior QA engineer for voice agents. You get one test case: a simulated caller with a
persona, a goal and a fact sheet. Rewrite the CALLER a few different ways so the test checks that the agent
copes with how real people ask, not one lucky wording.

Each variation:
- keeps the same task and the same facts. Never change a fact value (names, dates, numbers); the caller may
  give them in another order, spread over the call, or only when asked.
- changes how the caller asks: other words, indirect or vague asks, a different kind of caller, chatty or
  terse, one small realistic complication (a side question first, a correction of its own words, details
  given all at once). The result the test expects must still be right for the new caller.
- is something a real caller would do. No attacks, no rule breaking, no new requests.
Give each a short label (3 to 6 words), the new goal in plain words, a persona from the list, extra rules
for how the caller speaks (one line each), and why it is a useful variation. Answer with JSON only."""


def _schema(persona_ids: list[str], count: int) -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False, "required": ["variations"], "properties": {
        "variations": {"type": "array", "maxItems": count, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["label", "goal", "persona", "caller_rules", "why"],
            "properties": {"label": {"type": "string"}, "goal": {"type": "string"},
                           "persona": {"type": "string", "enum": persona_ids},
                           "caller_rules": {"type": "array", "items": {"type": "string"}},
                           "why": {"type": "string"}}}}}}


def _case_text(s: Mapping[str, Any]) -> str:
    c = s.get("caller") or {}
    o = s.get("outcome") or {}
    lines = [f"Test case: {s.get('title', s['id'])}", f"Persona: {c.get('persona')}", f"Goal: {c.get('goal')}",
             "Facts: " + ", ".join(f"{k} = {v}" for k, v in (c.get("facts") or {}).items())]
    if c.get("rules"):
        lines.append("Caller rules: " + "; ".join(c["rules"]))
    if o:
        lines.append(f"Expected result: {o.get('expected')} (the agent's tool {o['action']['tool']})")
    return "\n".join(lines)


async def draft(project: Project, llm, scenario_id: str, count: int = 3, model: str = PLAN_MODEL,
                log_dir: Path | None = None) -> list[dict[str, Any]]:
    s = next((x for x in project.scenarios if x["id"] == scenario_id), None)
    if s is None:
        raise PlanError(f"no test case {scenario_id!r}")
    if s.get("style") != "simulated":
        raise PlanError("only cases with a simulated caller can be varied (scripted lines are fixed on purpose)")
    if str(s["id"]).startswith("safety."):
        raise PlanError("safety tests have their own attack packs: vary those on the Safety page")
    count = max(1, min(int(count), 6))
    persona_ids = sorted(set(project.personas) | set(library.LIBRARY))
    who = [f"- {pid}: {library.title(pid)}. {library.LIBRARY[pid]['description']}" for pid in sorted(library.LIBRARY)]
    who += [f"- {pid}: {doc.get('description', '')}" for pid, doc in project.personas.items() if pid not in library.LIBRARY]
    text = "\n".join([_case_text(s), "", "Personas you can use:", *who, "", f"Write {count} variations."])
    answer = await _ask(llm, "variations", model, SYSTEM, text, _schema(persona_ids, count), 6000, log_dir)
    used = {x["id"] for x in project.scenarios}
    tools = {t["name"]: t for t in project.profile.get("tools", [])}
    probes = {p["id"] for p in (project.profile.get("backend") or {}).get("probes", [])}
    out = []
    for n, v in enumerate(answer["variations"], 1):
        doc = copy.deepcopy(dict(s))
        doc.pop("_file", None)
        base = f"{s['id']}.v-{_slug(v['label'])}"
        sid, i = base, 2
        while sid in used:
            sid, i = f"{base}-{i}", i + 1
        used.add(sid)
        doc["id"] = sid
        doc["title"] = f"{s.get('title', s['id'])} ({v['label'].strip().rstrip('.')})"
        caller = dict(doc["caller"])
        caller["goal"] = v["goal"].strip()
        caller["persona"] = v["persona"]
        caller["rules"] = list(caller.get("rules") or []) + [r.strip() for r in v["caller_rules"] if r.strip()]
        doc["caller"] = caller                      # facts kept exactly as they were
        doc["tags"] = list(dict.fromkeys(list(doc.get("tags") or []) + ["variation"]))
        doc["description"] = f"A variation of {s['id']}: {v['why'].strip()}"
        if doc.get("criticality") == "critical":
            doc["criticality"] = "normal"           # a variation informs; the original case gates the release
        problems = list(schema_errors("scenario", doc))
        problems += _reference_problems(doc, project.profile, {pid: {} for pid in persona_ids}, tools, probes)
        out.append({"n": n, "id": sid, "file": f"scenarios/{sid.replace('.', '-')}.yaml", "label": v["label"],
                    "why": v["why"], "goal": caller["goal"], "persona": caller["persona"],
                    "persona_new": caller["persona"] not in project.personas, "rules": v["caller_rules"],
                    "problems": problems, "scenario": doc, "of": s["id"]})
    return out


def add(folder: Path, drafts: list[Mapping[str, Any]], profile_file: str = "profile.yaml") -> dict[str, Any]:
    """Writes the ticked variations (and any library persona they need), then proves the project loads."""
    written: list[Path] = []
    added, skipped = [], []
    try:
        for d in drafts:
            doc = dict(d["scenario"])
            if schema_errors("scenario", doc):
                skipped.append({"id": doc.get("id"), "reason": "does not match the test case format"})
                continue
            path = Path(folder) / f"scenarios/{doc['id'].replace('.', '-')}.yaml"
            if path.exists():
                skipped.append({"id": doc["id"], "reason": f"{path.name} already exists"})
                continue
            persona = doc["caller"]["persona"]
            ppath = Path(folder) / "personas" / f"{persona}.yaml"
            if persona in library.LIBRARY and not ppath.exists():
                ppath.parent.mkdir(parents=True, exist_ok=True)
                ppath.write_text(_dump(library.persona_doc(persona), f"From Vocto's persona library: {library.title(persona)}."),
                                 encoding="utf-8", newline="\n")
                written.append(ppath)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_dump(doc, f"A caller variation written by AI and added from the dashboard. Same facts, "
                                       f"outcome and checks as {d.get('of') or 'the original case'}; only the caller differs."),
                            encoding="utf-8", newline="\n")
            written.append(path)
            added.append(doc["id"])
        load_project(Path(folder), profile_file)
    except Exception:
        for p in written:
            p.unlink(missing_ok=True)
        raise
    return {"added": added, "skipped": skipped}
