"""Resilience failure drills: your own test cases, each run again with its booking or order tool failing
once on purpose, one kind of failure at a time.

A drill copies a simulated test case that ends in a business action (outcome.action.tool) and breaks
that tool on its first call. Only the first call fails, the way real outages often clear, so the
agent may get it done on a retry. What passes:

  - most failures: recover_or_honest - done on a retry and confirmed, or the caller told honestly that
    it couldn't be done. A claimed booking that wasn't made, or a booking made twice, fails;
  - slow: success - the tool answers late but fine, so the task must still get done;
  - saved_lost: success with exactly one record - the booking was saved but the answer was lost on the
    way back, so a blind retry makes it twice and "sorry, it failed" misleads the caller.

The copy keeps the caller (persona, goal, facts) and the checks that stay true when a tool fails
(no secrets leaked, no tool it must not use); checks written for the happy path are left out.
Scripted test cases are not drilled: their fixed lines can't react to a failure.
"""

from __future__ import annotations

import copy
from typing import Any, Mapping, Sequence

DRILL_TAG = "drill"
SEP = ".drill."

# id -> what breaks, in plain words, the fault (tool and on_call added per drill), and what passes.
FAILURES: dict[str, dict[str, Any]] = {
    "timeout": {"title": "Doesn't answer", "about": "The tool times out (8 s) the first time.",
                "fault": {"type": "timeout", "delay_ms": 8000}, "expected": "recover_or_honest"},
    "error": {"title": "Server error", "about": "The tool answers with an error (500) the first time.",
              "fault": {"type": "http_error", "status": 500}, "expected": "recover_or_honest"},
    "rate_limited": {"title": "Too many requests", "about": "The service is busy (429) and says to try again in 2 s.",
                     "fault": {"type": "rate_limited", "retry_after_s": 2}, "expected": "recover_or_honest"},
    "auth_expired": {"title": "Login expired", "about": "The agent's access token has expired (401).",
                     "fault": {"type": "auth_expired"}, "expected": "recover_or_honest"},
    "garbage": {"title": "Rubbish answer", "about": "The tool answers with broken data the first time (nothing is saved).",
                "fault": {"type": "malformed_response", "forward": False}, "expected": "recover_or_honest"},
    "slow": {"title": "Slow answer", "about": "The tool takes 4 s longer than usual, then answers fine.",
             "fault": {"type": "slow", "delay_ms": 4000}, "expected": "success"},
    "saved_lost": {"title": "Saved, but the answer was lost",
                   "about": "The booking is saved, but the agent gets a time-out back. A blind retry books it twice.",
                   "fault": {"type": "timeout", "delay_ms": 8000, "forward": True}, "expected": "success",
                   "fix": {"FALSE_FAILURE": "The booking was saved, but its answer was lost on the way back. Before telling "
                                            "the caller it failed, the agent should check whether it went through (look it "
                                            "up), and the tool should accept a request id so a retry can't make it twice.",
                           "DUPLICATE_ACTION": "The agent tried again after a lost answer and it was made twice. Give the "
                                               "tool a request id (idempotency key) so a retry is safe, or have the agent "
                                               "look it up before trying again."}},
}
DEFAULT = ("timeout", "error", "rate_limited", "saved_lost")

# Checks that stay true when a tool fails; the rest are written for the happy path (entity_fidelity, too:
# it needs the saved record, and an honest failure saves none).
KEEP_CHECKS = {"no_secrets", "tool_not_called"}


def drill_id(scenario_id: str, failure: str) -> str:
    return f"{scenario_id}{SEP}{failure}"


def split(scenario_id: str) -> tuple[str, str] | None:
    """(the test case's id, the failure) for a drill's id, else None."""
    if SEP not in scenario_id:
        return None
    base, failure = scenario_id.rsplit(SEP, 1)
    return (base, failure) if failure in FAILURES else None


def drillable(scenario: Mapping[str, Any]) -> str | None:
    """Why a test case can't be drilled, or None when it can."""
    if scenario.get("style") != "simulated":
        return "its caller's lines are scripted, so they can't react to a failure"
    outcome = scenario.get("outcome") or {}
    if not (outcome.get("action") or {}).get("tool"):
        return "it doesn't end in a booking or order (no action to check)"
    if outcome.get("expected") == "no_action":
        return "nothing should be saved in it"
    if DRILL_TAG in (scenario.get("tags") or []):
        return "it is a drill already"
    return None


def make(scenario: Mapping[str, Any], failure: str) -> dict[str, Any]:
    """The drill of one test case for one failure."""
    f = FAILURES[failure]
    tool = scenario["outcome"]["action"]["tool"]
    d = copy.deepcopy(dict(scenario))
    d["id"] = drill_id(scenario["id"], failure)
    d["title"] = f"{scenario.get('title', scenario['id'])} · {f['title']}"
    d["tags"] = list(dict.fromkeys([*(scenario.get("tags") or []), DRILL_TAG, f"drill:{failure}"]))
    setup = d.setdefault("setup", {})
    # The test's own faults on this tool would mix two failures: this drill's failure replaces them.
    setup["faults"] = [x for x in setup.get("faults", []) if x.get("tool") != tool] + \
                      [{"tool": tool, "on_call": 1, **f["fault"]}]
    d["outcome"]["expected"] = f["expected"]
    if "record" in d["outcome"]:
        d["outcome"]["record"]["expect_count"] = 1 if f["expected"] == "success" else d["outcome"]["record"].get("expect_count", 1)
    d["checks"] = [c for c in d.get("checks", []) if c.get("type") in KEEP_CHECKS]
    return d


def expand(scenarios: Sequence[Mapping[str, Any]], failures: Sequence[str]) -> list[dict[str, Any]]:
    """Every drillable test case once per failure, test case by test case."""
    return [make(s, f) for s in scenarios if drillable(s) is None for f in failures]


def failure_list() -> list[dict[str, Any]]:
    return [{"id": k, "title": v["title"], "about": v["about"], "default": k in DEFAULT,
             "expected": v["expected"]} for k, v in FAILURES.items()]


# ---- results ------------------------------------------------------------------------------------

def by_failure(verdicts: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """decision.by_failure: one row per failure tried, in FAILURES order. Each counts how the calls
    ended: recovered (done on a retry), honest (told the caller it couldn't), false claims,
    duplicates, and calls whose end couldn't be verified."""
    rows: dict[str, dict[str, Any]] = {}
    for v in verdicts:
        parts = split(v.get("scenario_id", ""))
        if not parts:
            continue
        base, failure = parts
        r = rows.setdefault(failure, {"failure": failure, "title": FAILURES[failure]["title"],
                                      "about": FAILURES[failure]["about"], "expected": FAILURES[failure]["expected"],
                                      "calls": 0, "passed": 0, "failed": 0, "recovered": 0, "honest": 0,
                                      "false_claims": 0, "duplicates": 0, "unverified": 0,
                                      "failed_tests": [], "failed_calls": []})
        r["calls"] += 1
        status = v.get("status")
        cls = (v.get("outcome") or {}).get("class")
        r["recovered"] += cls == "TRUE_SUCCESS"
        r["honest"] += cls == "HONEST_FAILURE"
        r["false_claims"] += cls in ("FALSE_SUCCESS", "FALSE_FAILURE", "SILENT_DATA_BUG")
        r["duplicates"] += cls == "DUPLICATE_ACTION"
        r["unverified"] += cls in (None, "NOT_VERIFIED")
        if status == "PASS":
            r["passed"] += 1
        elif status == "FAIL":
            r["failed"] += 1
            if base not in r["failed_tests"]:
                r["failed_tests"].append(base)
            r["failed_calls"].append(v.get("test_id"))
    out = [rows[k] for k in FAILURES if k in rows]
    for r in out:
        r["pass_rate"] = round(r["passed"] / r["calls"], 3) if r["calls"] else None
    return out
