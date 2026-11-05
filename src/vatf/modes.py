"""Test modes: one agent, several kinds of testing, each with its own plan, run and report.

A mode is a recipe on top of the one call engine (orchestrator, load, compare, monitor). It never
switches a check off: every call in every mode still gets all its checks (tools, database, words,
timing, judges), so a good-sounding call can never hide a wrong action. What a mode changes is which
tests run, how the run is shaped, and which parts of the readiness report (readiness.py) lead.

The ids are stable (run.json `test_mode`, the dashboard's addresses, `vatf run --test-mode`).
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .attacks import is_safety

# Report areas (readiness.py), in the order the scorecard shows them.
AREAS: dict[str, str] = {
    "task": "Task success",
    "actions": "Actions and data",
    "conversation": "Conversation quality",
    "speed": "Speed and timing",
    "speech": "Speech understanding",
    "safety": "Safety and security",
    "resilience": "Handling failures",
    "load": "Capacity under load",
    "regression": "Change since baseline",
    "evidence": "Test evidence",
}

MODES: tuple[dict[str, Any], ...] = (
    {
        "id": "functional", "title": "Functional", "icon": "check-circle", "kind": "run", "voice_only": False,
        "question": "Does the agent do the job it was built for?",
        "about": "Runs your test cases as real calls and checks each one from start to finish.",
        "checks": ["The caller's task gets done (booking, order, answer)",
                   "The right tool is used, with the right details",
                   "The right data is saved in your system, and nothing else changes",
                   "The agent tells the truth about what it did"],
        "areas": ["task", "actions", "conversation", "evidence"],
        "safety": False,
    },
    {
        "id": "timing", "title": "Timing & turns", "icon": "clock", "kind": "run", "voice_only": True,
        "question": "Does a call with the agent feel natural?",
        "about": "Voice calls that measure how fast the agent answers and how it handles talking over, pauses and silence.",
        "checks": ["How long callers wait for an answer (typical and slowest)",
                   "Whether the agent stops when the caller cuts in",
                   "Whether it keeps going through an \"mm-hm\"",
                   "Long silences, and checking in when the caller goes quiet"],
        "areas": ["speed", "task", "conversation", "evidence"],
        "safety": False,
    },
    {
        "id": "speech", "title": "Speech & accents", "icon": "mic", "kind": "run", "voice_only": True,
        "question": "Does the agent understand different callers on real phone lines?",
        "about": "The same test cases with harder callers: accents, background noise, a phone line, lost audio.",
        "checks": ["Names, numbers and dates passed to your system as the caller said them",
                   "Details read back correctly",
                   "Results split by caller condition (phone line, noise, accent)",
                   "Which conditions break the agent"],
        "areas": ["speech", "task", "actions", "evidence"],
        "safety": False,
    },
    {
        "id": "safety", "title": "Safety & security", "icon": "shield", "kind": "run", "voice_only": False,
        "question": "Can callers trick the agent into something harmful?",
        "about": "Attack calls written for this agent: off-topic questions, pushing for secrets, prompt tricks, other people's data.",
        "checks": ["No secrets, passwords or internal details leak",
                   "No one else's personal data is shared",
                   "Prompt tricks spoken on the call are ignored",
                   "No action is taken that the caller isn't allowed to take"],
        "areas": ["safety", "actions", "evidence"],
        "safety": True,
    },
    {
        "id": "resilience", "title": "Resilience", "icon": "bolt", "kind": "run", "voice_only": False,
        "question": "What happens when your systems fail during a call?",
        "about": "Your test cases with a tool broken on purpose: it times out, returns an error, answers slowly or answers rubbish.",
        "checks": ["The agent tells the caller the truth when a tool fails",
                   "No booking or order is claimed that wasn't made",
                   "No duplicate bookings or orders after a retry",
                   "The agent offers a way forward (try later, a person)"],
        "areas": ["resilience", "actions", "conversation", "evidence"],
        "safety": False,
    },
    {
        "id": "load", "title": "Load & capacity", "icon": "activity", "kind": "load", "voice_only": False,
        "question": "How many calls at once can the agent handle?",
        "about": "Many calls at the same time, rising step by step, in a spike, or held for a long time.",
        "checks": ["How many calls at once before it gets too slow or fails",
                   "Answer time as calls are added",
                   "Calls that fail or drop under load",
                   "Whether results get worse under load"],
        "areas": ["load", "speed", "evidence"],
        "safety": False,
    },
    {
        "id": "regression", "title": "Regression", "icon": "compare", "kind": "compare", "voice_only": False,
        "question": "Did the latest change make anything worse?",
        "about": "Runs the full test set and compares it with the run you saved as the baseline.",
        "checks": ["Tests that passed before and fail now",
                   "Slower answers than before",
                   "Lower scores and new safety findings",
                   "Side-by-side comparison of any two runs"],
        "areas": ["regression", "task", "actions", "speed", "safety", "evidence"],
        "safety": True,
    },
    {
        "id": "monitor", "title": "Real calls", "icon": "phone", "kind": "monitor", "voice_only": False,
        "question": "How is the agent doing with real callers?",
        "about": "Checks real calls from your platform (personal data removed first) and alerts you to problems.",
        "checks": ["Tools that failed on real calls",
                   "The agent claiming success that didn't happen",
                   "Dropped calls",
                   "Trends over days, and turning a bad call into a test"],
        "areas": [],
        "safety": False,
    },
)

BY_ID: dict[str, dict[str, Any]] = {m["id"]: m for m in MODES}
RUN_MODES = tuple(m["id"] for m in MODES if m["kind"] == "run") + ("regression", "full")
CHOICES = tuple(m["id"] for m in MODES if m["kind"] in ("run", "compare")) + ("full",)

FULL = {"id": "full", "title": "Full check", "icon": "play-circle", "kind": "run", "voice_only": False,
        "question": "Is the agent ready to release?",
        "about": "Your test cases and the safety tests in one run, with one combined report.",
        "checks": [], "areas": ["task", "actions", "conversation", "speed", "safety", "evidence"], "safety": True}


def mode(mode_id: str | None) -> dict[str, Any]:
    if mode_id == "full" or not mode_id:
        return FULL
    return BY_ID[mode_id]


def has_faults(scenario: Mapping[str, Any]) -> bool:
    return bool((scenario.get("setup") or {}).get("faults"))


def select(mode_id: str | None, own: Sequence[Mapping[str, Any]], safety: Sequence[Mapping[str, Any]],
           with_safety: bool) -> list[Mapping[str, Any]]:
    """The scenarios a run in this mode plans: `own` are the agent's own tests in this channel,
    `safety` the built-in attack calls. Picked ids (--scenario) are applied after this."""
    if mode_id == "safety":
        return list(safety)
    if mode_id == "resilience":
        return [s for s in own if has_faults(s)]
    if mode_id in ("functional", "timing", "speech"):
        return list(own) + (list(safety) if with_safety else [])
    return list(own) + (list(safety) if with_safety else [])   # full, regression, none


def why_empty(mode_id: str | None) -> str:
    """Why a mode found nothing to run, and what to do about it (plain words)."""
    if mode_id == "safety":
        return ("no safety tests are switched on for this agent: switch them on in the profile's safety section "
                "(Set up > Safety tests)")
    if mode_id == "resilience":
        return ("none of this agent's test cases breaks a tool on purpose: add a fault to a test case "
                "(setup.faults, or Set up > Test cases > a broken tool) or let \"Write tests for me\" write some")
    return "this agent has no test cases for this channel yet"


def available(mode_id: str, voice: bool, profile: Mapping[str, Any], scenarios: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Whether the mode can run for this agent now, and why not (shown on its card)."""
    m = mode(mode_id)
    if m["voice_only"] and not voice:
        return {"ok": False, "why": "Needs a voice connection: this agent is tested by text."}
    own = [s for s in scenarios if not is_safety(s)]
    if mode_id == "resilience" and not any(has_faults(s) for s in own):
        return {"ok": False, "why": "No test case breaks a tool on purpose yet. Add one in Test cases.", "fixable": True}
    if mode_id == "safety" and not (profile.get("safety") or {}).get("enabled"):
        return {"ok": False, "why": "Safety tests are switched off for this agent. Switch them on in Safety tests.", "fixable": True}
    if m["kind"] == "run" and not own and mode_id != "safety":
        return {"ok": False, "why": "No test cases yet. Write some in Test cases.", "fixable": True}
    return {"ok": True}


def of_run(info: Mapping[str, Any] | None) -> str:
    """The mode a run belongs to. Runs from before modes existed are placed by what they ran."""
    info = info or {}
    if info.get("test_mode"):
        return info["test_mode"]
    if info.get("kind") == "load":
        return "load"
    plan = info.get("plan") or []
    ids = [p.get("scenario_id", "") for p in plan]
    if ids and all(i.startswith("safety.") for i in ids):
        return "safety"
    return "full" if info.get("safety") else "functional"


def catalog() -> list[dict[str, Any]]:
    """Every mode for the dashboard (the Full check first)."""
    keep = ("id", "title", "icon", "kind", "voice_only", "question", "about", "checks", "areas")
    return [{k: m[k] for k in keep} for m in (FULL, *MODES)]
