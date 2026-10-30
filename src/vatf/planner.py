"""The test plan writer: reads what the agent is told to do (its own instructions, the business rules,
its tools and backend checks) and writes a draft test plan of ready-to-run scenarios.

Three steps, so every draft can be checked before anyone trusts it:
  1. understand  Claude lists the agent's jobs (intents), the details it needs (slots), its rules and
                 its steps, as structured JSON.
  2. write       Claude writes the tests in a fixed mix of kinds (see MIX), each linked to the rules or
                 jobs it tests, as structured JSON.
  3. check       Vocto (no AI) turns each draft into a scenario file and checks it with the same schema
                 and reference checks as a hand-written one. Drafts that don't trace back to the prompt
                 become exploratory, drafts too close to an existing test are marked duplicate, and a
                 broken draft is shown with its problems instead of being saved.

Nothing is saved as a test until a person ticks it (accept). The plan itself is kept in the project's
plans/ folder, so the dashboard can show it again.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import yaml

from . import personas as library
from .contracts.models import LLMMessage, LLMRequest
from .loader import Project, _reference_problems, load_project, schema_errors

PLAN_MODEL = "claude-opus-5-5"

# The share of each kind of test in a plan (Hamming's published test-set mix).
MIX = {"happy": 0.40, "edge": 0.30, "error": 0.15, "adversarial": 0.10, "audio": 0.05}
CATEGORY_TITLES = {"happy": "Normal calls", "edge": "Edge cases", "error": "Tool failures",
                   "adversarial": "Misuse", "audio": "Noise and interruptions"}
KINDS = {
    "happy_path": "happy", "variation": "happy",
    "invalid_input": "edge", "boundary": "edge", "missing_info": "edge", "ambiguous": "edge",
    "topic_change": "edge", "contradiction": "edge", "repetition": "edge", "out_of_scope": "edge",
    "escalation": "edge", "cancel_midway": "edge",
    "tool_error": "error", "tool_timeout": "error", "tool_slow": "error", "tool_bad_reply": "error",
    "other_persons_data": "adversarial", "skip_verification": "adversarial", "forbidden_action": "adversarial",
    "pressure": "adversarial",
    "noise": "audio", "interruption": "audio", "fast_speech": "audio",
}
# Fault per error kind when the draft names none.
KIND_FAULT = {"tool_error": "http_error", "tool_timeout": "timeout", "tool_slow": "slow", "tool_bad_reply": "malformed_response"}
# Words that make claims clear when no test of the same tool has its own.
SUCCESS_WORDS = [r"\b(done|confirmed|booked|placed|all set|on its way|scheduled|completed|reference|order number)\b"]
FAILURE_WORDS = [r"\b(couldn't|could not|unable|not able|wasn't able|failed|isn't working|not working|problem|issue|"
                 r"trouble|sorry)\b", r"\btry again\b", r"\bcall back\b"]
STOP = set("a an the and or to of for in on at is are be it its this that with as by from i my me you your "
           "caller agent wants want asks ask says call calls".split())


class PlanError(Exception):
    """The plan could not be written (no agent description, or the model's answer was unusable)."""


# ---- step 0: what the writer is told about the agent -----------------------------------------

def agent_context(project: Project) -> str:
    p = project.profile
    lines = [f"Agent: {p.get('name', p['id'])}. {p.get('description', '')}".strip(),
             f"Domain: {p.get('domain', '')}. Languages: {', '.join(p.get('languages', ['en']))}."]
    if p.get("agent_prompt"):
        lines += ["", "The agent's own instructions (its system prompt):", "<<<", p["agent_prompt"].strip(), ">>>"]
    policy = (p.get("judges") or {}).get("policy")
    if policy:
        lines += ["", "Business rules the agent must follow:", policy.strip()]
    lines += ["", "Tools the agent calls (Vocto sees every call and can make one fail on purpose):"]
    for t in p.get("tools", []):
        lines.append(f"- {t['name']} ({t.get('effect', 'read')}): {t.get('description', '')}")
    probes = (p.get("backend") or {}).get("probes", [])
    if probes:
        lines += ["", "Database checks Vocto can run after a call (what was really saved):"]
        for pr in probes:
            cols = ", ".join(probe_columns(pr)) or "?"
            lines.append(f"- {pr['id']}: {pr.get('description', '')} Columns: {cols}.")
    if project.scenarios:
        lines += ["", "Tests that already exist (don't write these again):"]
        lines += [f"- {s['id']}: {s.get('title', '')}" for s in project.scenarios if not s["id"].startswith("safety.")]
    return "\n".join(lines)


def probe_columns(probe: Mapping[str, Any]) -> list[str]:
    """Column names in a probe's SELECT (best effort; aliases win)."""
    m = re.search(r"select\s+(.*?)\s+from\s", str(probe.get("query", "")), re.I | re.S)
    if not m:
        return []
    out = []
    for part in m.group(1).split(","):
        part = part.strip()
        alias = re.search(r"\bas\s+(\w+)$", part, re.I)
        name = alias.group(1) if alias else re.sub(r".*\.", "", part.split()[-1] if part else "")
        if re.fullmatch(r"\w+", name or ""):
            out.append(name)
    return out


# ---- step 1: understand ----------------------------------------------------------------------

ANALYSIS_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["summary", "intents", "slots", "rules", "workflows", "unclear"],
    "properties": {
        "summary": {"type": "string"},
        "intents": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["id", "description", "tools"],
            "properties": {"id": {"type": "string"}, "description": {"type": "string"},
                           "tools": {"type": "array", "items": {"type": "string"}}}}},
        "slots": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["name", "description", "example"],
            "properties": {"name": {"type": "string"}, "description": {"type": "string"}, "example": {"type": "string"}}}},
        "rules": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["id", "text", "kind"],
            "properties": {"id": {"type": "string"}, "text": {"type": "string"},
                           "kind": {"type": "string", "enum": ["must", "must_not", "escalate", "fact"]}}}},
        "workflows": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["intent", "steps"],
            "properties": {"intent": {"type": "string"}, "steps": {"type": "array", "items": {"type": "string"}}}}},
        "unclear": {"type": "array", "items": {"type": "string"}},
    },
}

ANALYSIS_SYSTEM = """You are a senior QA engineer studying a voice agent before testing it.
Read everything you are given about the agent and list, using only what the text supports:
- intents: the jobs callers can ask it to do (short snake_case ids like place_order), with the tools each job uses.
- slots: the details it must collect from the caller, with a made-up example value.
- rules: every instruction or business rule it must follow, one per item, ids R1, R2, ... in order.
  kind: must (it has to do this), must_not (it must never do this), escalate (when to hand over), fact (a fact
  it may state, like a price).
- workflows: the steps of each job in order.
- unclear: things a tester would need to know that the text does not say.
Do not invent rules the text does not contain."""


# ---- step 2: write ---------------------------------------------------------------------------

def drafts_schema(tools: list[str], write_tools: list[str], persona_ids: list[str]) -> dict[str, Any]:
    tool_enum = {"type": "string", "enum": sorted(tools) + ["none"]}
    pair = {"type": "object", "additionalProperties": False, "required": ["name", "value"],
            "properties": {"name": {"type": "string"}, "value": {"type": "string"}}}
    test = {
        "type": "object", "additionalProperties": False,
        "required": ["category", "kind", "title", "why", "intent", "rule_ids", "persona", "goal", "facts",
                     "end_when", "max_turns", "criticality", "fault", "outcome", "must_call", "must_not_call",
                     "questions", "audio"],
        "properties": {
            "category": {"type": "string", "enum": list(MIX)},
            "kind": {"type": "string", "enum": list(KINDS)},
            "title": {"type": "string"},
            "why": {"type": "string"},
            "intent": {"type": "string"},
            "rule_ids": {"type": "array", "items": {"type": "string"}},
            "persona": {"type": "string", "enum": persona_ids},
            "goal": {"type": "string"},
            "facts": {"type": "array", "items": pair},
            "end_when": {"type": "string"},
            "max_turns": {"type": "integer"},
            "criticality": {"type": "string", "enum": ["critical", "normal", "exploratory"]},
            "fault": {"type": "object", "additionalProperties": False, "required": ["tool", "type", "status"],
                      "properties": {"tool": tool_enum,
                                     "type": {"type": "string", "enum": ["none", "http_error", "timeout", "slow", "malformed_response"]},
                                     "status": {"type": "integer"}}},
            "outcome": {"type": "object", "additionalProperties": False,
                        "required": ["expected", "tool", "expect_count", "fields", "success_phrases", "failure_phrases"],
                        "properties": {
                            "expected": {"type": "string", "enum": ["success", "honest_failure", "no_action", "none"]},
                            "tool": {"type": "string", "enum": sorted(write_tools) + ["none"]},
                            "expect_count": {"type": "integer"},
                            "fields": {"type": "array", "items": pair},
                            "success_phrases": {"type": "array", "items": {"type": "string"}},
                            "failure_phrases": {"type": "array", "items": {"type": "string"}}}},
            "must_call": {"type": "array", "items": tool_enum},
            "must_not_call": {"type": "array", "items": tool_enum},
            "questions": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["title", "question", "pass_when", "fail_when"],
                "properties": {"title": {"type": "string"}, "question": {"type": "string"},
                               "pass_when": {"type": "string"}, "fail_when": {"type": "string"}}}},
            "audio": {"type": "object", "additionalProperties": False, "required": ["noise", "interrupt_line"],
                      "properties": {"noise": {"type": "string", "enum": ["none", "office", "street", "cafe", "car"]},
                                     "interrupt_line": {"type": "string"}}},
        },
    }
    return {"type": "object", "additionalProperties": False, "required": ["tests"],
            "properties": {"tests": {"type": "array", "items": test}}}


WRITE_SYSTEM = """You are a senior QA engineer writing phone-call tests for a voice agent. Vocto's AI caller
will phone the agent and act out each test; Vocto then checks the tool calls and the database, not just
the words.

For each test give:
- category and kind. Kinds by category:
  happy: happy_path (the main job done normally), variation (same job, different valid choices).
  edge: invalid_input, boundary, missing_info, ambiguous, topic_change, contradiction, repetition,
        out_of_scope, escalation, cancel_midway.
  error: tool_error, tool_timeout, tool_slow, tool_bad_reply (Vocto breaks a tool the call will use; set fault).
  adversarial: other_persons_data, skip_verification, forbidden_action, pressure (agent-specific misuse only;
        general jailbreaks and secret probing are already covered by Vocto's safety tests).
  audio: noise (set audio.noise), interruption (set audio.interrupt_line: what the caller says when cutting in
        on the agent's second turn), fast_speech.
- intent: the intent id it exercises, or "none".
- rule_ids: the rule ids (R1, R2...) it tests. Every test that isn't a plain happy path must name at least one.
- persona: who calls, from the list you are given. Vary them.
- goal: instructions to the caller, in second person, in plain words ("Order a large pepperoni pizza. When...").
  The caller knows only what a real caller would. Say how the caller reacts at the key moment.
- facts: the caller's details as name/value pairs (made-up test data only, never real people).
- end_when and max_turns (8 to 16).
- criticality: critical only when a broken rule would hurt a customer or the business (false confirmations,
  wrong orders, data leaks); normal otherwise; exploratory when you're unsure what the agent should do.
- fault: tool "none" and type "none" unless the kind is an error kind. status only matters for http_error (use 500 or 503).
- outcome: what must end up in the database. expected success (the write tool runs and the record exists),
  honest_failure (the write can't succeed, so nothing is saved and the agent must say so), no_action (nothing
  may be written), or none (no business action in this call). expect_count is how many records the
  database check should find. fields: only columns listed for the database check, with exact expected values.
  success_phrases / failure_phrases: a few words the agent would use to say it worked / it didn't.
- must_call: read tools the agent must use before answering (e.g. it must look up a time, not guess).
- must_not_call: write tools that must not run in this call.
- questions: zero to two yes/no questions an AI judge answers about this call, each with what a pass and a fail look like.
- audio: noise "none" and interrupt_line "" unless it is an audio test.
Write exactly the number of tests asked for in each category. Don't repeat existing tests or each other."""


def split_counts(total: int, mix: Mapping[str, float] = MIX) -> dict[str, int]:
    """Whole numbers per category that add up to total (largest remainder)."""
    raw = {k: total * v for k, v in mix.items()}
    counts = {k: int(v) for k, v in raw.items()}
    for k in sorted(raw, key=lambda k: raw[k] - counts[k], reverse=True)[: total - sum(counts.values())]:
        counts[k] += 1
    return counts


async def _ask(llm, purpose: str, model: str, system: str, text: str, schema: Mapping[str, Any],
               max_tokens: int, log_dir: Path | None) -> dict[str, Any]:
    meta = {"log_dir": str(log_dir)} if log_dir else {}
    response = await llm.complete(LLMRequest(purpose=purpose, model=model, system=system,
                                             messages=(LLMMessage("user", text),), max_tokens=max_tokens,
                                             metadata=meta, json_schema=schema))
    try:
        doc = json.loads(response.text)
    except json.JSONDecodeError as e:
        raise PlanError(f"the {purpose} answer was not JSON ({e})") from e
    errors = sorted(_validator(schema).iter_errors(doc), key=lambda e: list(e.path))
    if errors:
        raise PlanError(f"the {purpose} answer had the wrong shape: {errors[0].message}")
    return doc


def _validator(schema):
    from jsonschema import Draft202012Validator
    return Draft202012Validator(schema)


async def write_plan(project: Project, llm, count: int = 20, focus: str = "", model: str = PLAN_MODEL,
                     log_dir: Path | None = None) -> dict[str, Any]:
    """Steps 1 to 3. Returns the plan (nothing is saved)."""
    p = project.profile
    if not (p.get("agent_prompt") or (p.get("judges") or {}).get("policy") or p.get("description")):
        raise PlanError("tell Vocto what the agent does first: paste its instructions (system prompt) or business rules")
    count = max(1, min(int(count), 60))
    context = agent_context(project)
    analysis = await _ask(llm, "plan_understand", model, ANALYSIS_SYSTEM, context, ANALYSIS_SCHEMA, 8000, log_dir)

    tools = [t["name"] for t in p.get("tools", [])]
    write_tools = [t["name"] for t in p.get("tools", []) if t.get("effect") == "write"]
    persona_ids = sorted(set(library.LIBRARY) | set(project.personas))
    counts = split_counts(count)
    who = [f"- {pid}: {library.title(pid)}. {library.LIBRARY[pid]['description']}" for pid in sorted(library.LIBRARY)]
    who += [f"- {pid}: {doc.get('description', '')}" for pid, doc in project.personas.items() if pid not in library.LIBRARY]
    ask = "\n".join([
        context, "", "What you worked out about the agent:", json.dumps(analysis, indent=1), "",
        "Callers you can use:", *who, "",
        "Write these many tests: " + ", ".join(f"{k}: {n}" for k, n in counts.items() if n) + ".",
        *([f"Focus from the tester: {focus.strip()}"] if focus.strip() else []),
    ])
    drafts = await _ask(llm, "plan_write", model, WRITE_SYSTEM, ask, drafts_schema(tools, write_tools, persona_ids),
                        32000, log_dir)
    tests = check_drafts(project, analysis, drafts["tests"])
    return {
        "schema_version": "0.1", "kind": "test_plan", "profile": p["id"],
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": model, "asked": {"count": count, "focus": focus, "mix": counts},
        "got": dict(Counter(t["category"] for t in tests)),
        "analysis": analysis, "tests": tests,
        "cost_usd": round(getattr(llm, "spent_usd", 0.0), 4),
    }


# ---- step 3: check (no AI) -------------------------------------------------------------------

def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2 and w not in STOP}


def _similar(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _phrase_pattern(phrases: list[str]) -> list[str]:
    words = [re.escape(x.strip().lower()) for x in phrases if x.strip()]
    return [r"\b(" + "|".join(words) + r")\b"] if words else []


def _common(values: list[Any]) -> Any:
    """The most used value (setups shared by the project's own tests: fixtures, fixed time)."""
    keyed = Counter(json.dumps(v, sort_keys=True) for v in values if v)
    return json.loads(keyed.most_common(1)[0][0]) if keyed else None


def check_drafts(project: Project, analysis: Mapping[str, Any], drafts: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    p = project.profile
    tools = {t["name"]: t for t in p.get("tools", [])}
    probes = {pr["id"]: pr for pr in (p.get("backend") or {}).get("probes", [])}
    rules = {r["id"]: r for r in analysis.get("rules", [])}
    intents = {i["id"] for i in analysis.get("intents", [])}
    own = [s for s in project.scenarios if not s["id"].startswith("safety.")]
    setups = {"fixtures": _common([(s.get("setup") or {}).get("fixtures") for s in own]),
              "now": _common([(s.get("setup") or {}).get("now") for s in own])}
    claims: dict[str, Any] = {}
    tool_probes: dict[str, str] = {}
    for s in own:
        o = s.get("outcome") or {}
        if o.get("claim") and o["action"]["tool"] not in claims:
            claims[o["action"]["tool"]] = o["claim"]
        if (o.get("record") or {}).get("probe"):
            tool_probes.setdefault(o["action"]["tool"], o["record"]["probe"])
    personas_known = set(project.personas) | set(library.LIBRARY)
    existing = [(s["id"], _words(f"{s.get('title', '')} {(s.get('caller') or {}).get('goal', '')} {s.get('description', '')}"))
                for s in own]
    used_ids = {s["id"] for s in project.scenarios}
    out: list[dict[str, Any]] = []
    for n, d in enumerate(drafts, 1):
        notes: list[str] = []
        kind = d["kind"] if d["kind"] in KINDS else "variation"
        category = KINDS[kind]
        traced = [r for r in d.get("rule_ids", []) if r in rules]
        intent = d.get("intent") if d.get("intent") in intents else None
        if not traced and not (intent and category == "happy"):
            notes.append("Not linked to a rule in the agent's instructions, so it runs as exploratory.")
        doc, more = to_scenario(p, d, kind, tools, probes, setups, claims, tool_probes)
        notes += more
        if not traced and not (intent and category == "happy"):
            doc["criticality"] = "exploratory"
        elif doc["criticality"] == "critical" and not any(rules[r]["kind"] in ("must", "must_not", "escalate") for r in traced) \
                and category != "happy":
            doc["criticality"] = "normal"
            notes.append("Lowered to normal: none of its rules is a must / must-not rule.")
        base = f"gen.{kind.replace('_', '-')}.{_slug(d['title'])}"
        sid, i = base, 2
        while sid in used_ids:
            sid, i = f"{base}-{i}", i + 1
        used_ids.add(sid)
        doc["id"] = sid
        words = _words(f"{d['title']} {d['goal']}")
        dup = next((eid for eid, w in existing if _similar(words, w) >= 0.55), None)
        existing.append((sid, words))
        problems = list(schema_errors("scenario", doc))
        problems += _reference_problems(doc, p, {pid: {} for pid in personas_known}, tools, set(probes))
        out.append({
            "n": n, "id": sid, "file": f"scenarios/{sid.replace('.', '-')}.yaml",
            "category": category, "kind": kind, "title": d["title"], "why": d["why"],
            "intent": intent, "rules": [{"id": r, "text": rules[r]["text"], "kind": rules[r]["kind"]} for r in traced],
            "persona": doc["caller"]["persona"], "persona_new": doc["caller"]["persona"] not in project.personas,
            "criticality": doc["criticality"], "duplicate_of": dup, "notes": notes, "problems": problems,
            "selected": not problems and not dup, "scenario": doc,
        })
    return out


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40].strip("-") or "test"


def to_scenario(profile: Mapping[str, Any], d: Mapping[str, Any], kind: str, tools: Mapping[str, Any],
                probes: Mapping[str, Any], setups: Mapping[str, Any], claims: Mapping[str, Any],
                tool_probes: Mapping[str, str] | None = None) -> tuple[dict[str, Any], list[str]]:
    """One draft as a scenario file (simulated style), plus notes on what was changed to make it sound."""
    notes: list[str] = []
    category = KINDS[kind]
    facts = {f["name"].strip().replace(" ", "_"): f["value"] for f in d.get("facts", []) if f.get("name", "").strip()}
    rules_text = [f"Tests rule {r}." for r in d.get("rule_ids", [])]
    doc: dict[str, Any] = {
        "schema_version": "0.1", "kind": "scenario", "id": "gen.draft", "title": d["title"].strip(),
        "description": " ".join([d.get("why", "").strip(), *rules_text]).strip(),
        "profile": profile["id"], "tags": ["generated", category, kind.replace("_", "-")],
        "criticality": d.get("criticality", "normal"), "style": "simulated",
    }
    setup: dict[str, Any] = {}
    if setups.get("now"):
        setup["now"] = setups["now"]
    if setups.get("fixtures"):
        setup["fixtures"] = setups["fixtures"]

    fault = d.get("fault") or {}
    ftype = fault.get("type", "none")
    if category == "error" and ftype == "none":
        ftype = KIND_FAULT.get(kind, "http_error")
    ftool = fault.get("tool", "none")
    if category == "error" and ftool not in tools:
        writes = [t for t, v in tools.items() if v.get("effect") == "write"]
        ftool = (writes or list(tools) or ["none"])[0]
    if ftype != "none" and ftool in tools:
        f: dict[str, Any] = {"tool": ftool, "type": ftype}
        if ftype == "http_error":
            f["status"] = fault.get("status") if 400 <= int(fault.get("status") or 0) <= 599 else 503
        if ftype in ("timeout", "slow"):
            f["delay_ms"] = 8000 if ftype == "timeout" else 4000
        setup["faults"] = [f]
    if setup:
        doc["setup"] = setup

    caller: dict[str, Any] = {"persona": d["persona"], "goal": d["goal"].strip(), "facts": facts}
    if d.get("end_when", "").strip():
        caller["end_when"] = d["end_when"].strip()
    caller["max_turns"] = max(6, min(int(d.get("max_turns") or 12), 20))
    doc["caller"] = caller

    o = d.get("outcome") or {}
    expected, otool = o.get("expected", "none"), o.get("tool", "none")
    breaks_write = bool(setup.get("faults")) and setup["faults"][0]["tool"] == otool and \
        setup["faults"][0]["type"] in ("http_error", "timeout", "malformed_response")
    if expected == "success" and breaks_write:
        expected = "honest_failure"
        notes.append("Expected result changed to an honest failure: Vocto breaks the very tool that saves it.")
    if expected != "none" and tools.get(otool, {}).get("effect") == "write":
        outcome: dict[str, Any] = {"expected": expected, "action": {"tool": otool}}
        probe = (tool_probes or {}).get(otool) or _probe_for(otool, probes)
        if probe:
            count = 1 if expected == "success" else 0
            if expected == "success" and int(o.get("expect_count") or 1) > 1:
                count = int(o["expect_count"])
            record: dict[str, Any] = {"probe": probe, "expect_count": count}
            cols = set(probe_columns(probes[probe]))
            fields = {f["name"]: f["value"] for f in o.get("fields", []) if f.get("name") in cols and str(f.get("value", "")).strip()}
            dropped = [f["name"] for f in o.get("fields", []) if f.get("name") not in cols]
            if dropped:
                notes.append(f"Left out database fields the check doesn't return: {', '.join(dropped)}.")
            if fields and expected == "success":
                record["expect_fields"] = fields
            outcome["record"] = record
        claim = claims.get(otool)
        if claim:
            outcome["claim"] = claim
        else:
            outcome["claim"] = {"method": "patterns",
                                "success_patterns": _phrase_pattern(o.get("success_phrases", [])) or SUCCESS_WORDS,
                                "failure_patterns": _phrase_pattern(o.get("failure_phrases", [])) + FAILURE_WORDS}
        doc["outcome"] = outcome
    elif expected not in ("none",) and otool != "none":
        notes.append(f"Dropped the database outcome: {otool} is not a write tool.")

    checks: list[dict[str, Any]] = []
    for t in dict.fromkeys(d.get("must_call", [])):
        if t in tools:
            checks.append({"id": f"calls-{_slug(t)}", "type": "tool_called", "tool": t,
                           "description": f"The agent uses {t} instead of guessing."})
    for t in dict.fromkeys(d.get("must_not_call", [])):
        if t in tools and t != (doc.get("outcome") or {}).get("action", {}).get("tool"):
            checks.append({"id": f"never-{_slug(t)}", "type": "tool_not_called", "tool": t,
                           "severity": "blocker" if doc["criticality"] == "critical" else "major",
                           "description": f"{t} must not run in this call."})
    for i, q in enumerate(d.get("questions", [])[:2], 1):
        if q.get("question", "").strip():
            checks.append({"id": f"ask-{i}", "type": "judge", "intent": q["question"].strip(),
                           "title": q.get("title", "").strip() or f"Question {i}",
                           **({"pass_when": q["pass_when"].strip()} if q.get("pass_when", "").strip() else {}),
                           **({"fail_when": q["fail_when"].strip()} if q.get("fail_when", "").strip() else {})})
    if checks:
        doc["checks"] = checks

    audio = d.get("audio") or {}
    if category == "audio" or audio.get("noise", "none") != "none" or audio.get("interrupt_line", "").strip():
        block: dict[str, Any] = {}
        if audio.get("noise", "none") != "none":
            block["noise"] = audio["noise"]
            block["snr_db"] = 10
        elif kind == "noise":
            block["noise"] = "cafe"
            block["snr_db"] = 10
        line = audio.get("interrupt_line", "").strip()
        if line or kind == "interruption":
            block["barge_in"] = [{"agent_turn": 2, "offset_ms": 1500, "say": line or "Sorry, can I add something?"}]
        if block:
            doc["audio"] = block
            doc["modes"] = ["voice"]
            notes.append("Voice runs only (noise and interruptions need real audio).")
    return doc, notes


def _probe_for(tool: str, probes: Mapping[str, Any]) -> str | None:
    """The database check for a write tool no existing test uses: the only one, or the closest by name."""
    if len(probes) == 1:
        return next(iter(probes))
    words = _words(tool)
    best = max(probes, key=lambda pid: _similar(words, _words(f"{pid} {probes[pid].get('description', '')}")), default=None)
    return best


# ---- keeping plans and turning ticked drafts into tests ----------------------------------------

def plans_dir(folder: Path) -> Path:
    return folder / "plans"


def save_plan(folder: Path, plan: Mapping[str, Any]) -> Path:
    d = plans_dir(folder)
    d.mkdir(parents=True, exist_ok=True)
    stamp = plan["created_at"].replace(":", "").replace("-", "")[:15]
    path = d / f"plan-{stamp}.json"
    text = json.dumps(plan, indent=1, ensure_ascii=False)
    path.write_text(text, encoding="utf-8")
    (d / "latest.json").write_text(text, encoding="utf-8")
    return path


def latest_plan(folder: Path) -> dict[str, Any] | None:
    path = plans_dir(folder) / "latest.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def accept(folder: Path, plan: dict[str, Any], ids: list[str], critical: Mapping[str, str] | None = None,
           profile_file: str = "profile.yaml") -> dict[str, Any]:
    """Writes the ticked drafts as scenario files (and any library persona they need), then loads the
    project to prove it still validates. Nothing is kept if it doesn't."""
    by_id = {t["id"]: t for t in plan["tests"]}
    unknown = [i for i in ids if i not in by_id]
    if unknown:
        raise ValueError(f"not in this plan: {', '.join(unknown)}")
    written: list[Path] = []
    added_personas: list[str] = []
    skipped: list[dict[str, Any]] = []
    try:
        for i in ids:
            t = by_id[i]
            if t["problems"]:
                skipped.append({"id": i, "reason": "has problems: " + "; ".join(t["problems"])})
                continue
            path = folder / t["file"]
            if path.exists():
                skipped.append({"id": i, "reason": f"{t['file']} already exists"})
                continue
            doc = dict(t["scenario"])
            if critical and i in critical and critical[i] in ("critical", "normal", "exploratory"):
                doc["criticality"] = critical[i]
            persona = doc["caller"]["persona"]
            ppath = folder / "personas" / f"{persona}.yaml"
            if persona in library.LIBRARY and not ppath.exists():
                ppath.parent.mkdir(parents=True, exist_ok=True)
                ppath.write_text(_dump(library.persona_doc(persona), f"From Vocto's persona library: {library.title(persona)}."),
                                 encoding="utf-8", newline="\n")
                written.append(ppath)
                added_personas.append(persona)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_dump(doc, _header(t, plan)), encoding="utf-8", newline="\n")
            written.append(path)
        load_project(folder, profile_file)
    except Exception:
        for path in written:
            path.unlink(missing_ok=True)
        raise
    for t in plan["tests"]:
        if t["id"] in ids and not any(s["id"] == t["id"] for s in skipped):
            t["accepted"] = True
    save_plan(folder, plan)
    return {"added": [t for t in ids if not any(s["id"] == t for s in skipped)], "personas": added_personas,
            "skipped": skipped}


def add_personas(folder: Path, ids: list[str]) -> list[str]:
    added = []
    for pid in ids:
        if pid not in library.LIBRARY:
            raise ValueError(f"no persona {pid!r} in the library")
        path = folder / "personas" / f"{pid}.yaml"
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_dump(library.persona_doc(pid), f"From Vocto's persona library: {library.title(pid)}."),
                        encoding="utf-8", newline="\n")
        added.append(pid)
    return added


def _header(t: Mapping[str, Any], plan: Mapping[str, Any]) -> str:
    lines = [f"Written by Vocto's test plan writer on {plan['created_at'][:10]} ({CATEGORY_TITLES[t['category']]}: "
             f"{t['kind'].replace('_', ' ')}); reviewed and added from the dashboard.", t["why"]]
    lines += [f"Rule {r['id']}: {r['text']}" for r in t["rules"]]
    return "\n".join(lines)


def _dump(doc: Mapping[str, Any], comment: str) -> str:
    head = "".join(f"# {line}\n" for c in comment.splitlines() for line in _wrap(c))
    return head + yaml.safe_dump(dict(doc), sort_keys=False, allow_unicode=True, width=110)


def _wrap(text: str, width: int = 100) -> list[str]:
    words, lines, line = text.split(), [], ""
    for w in words:
        if line and len(line) + 1 + len(w) > width:
            lines.append(line)
            line = w
        else:
            line = f"{line} {w}".strip()
    return lines + ([line] if line else [])
