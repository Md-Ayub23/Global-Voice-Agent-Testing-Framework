"""Resilience failure drills: your own test cases, each run again with one of its tools failing on
purpose, one kind of failure at a time.

A drill copies a simulated test case that ends in a business action (outcome.action.tool) and breaks
a tool the agent uses on the way. Three choices make a drill:

  what breaks   the booking or order tool (default), or the lookups the agent reads before acting
                (availability, account, menu): there the risk is the agent inventing what it couldn't read;
  how often     the first call only (outages often clear, so a retry may work), or every call (an outage
                that lasts the whole call: retries must stop, and the caller needs a way forward);
  which failure a crash (no answer, server error, too many requests, login expired, rubbish answer), a slow or
                lost answer, a business refusal (already taken, payment declined, out of stock), or the call
                dropping just after the booking was sent, with the caller ringing back.

What passes:

  - most failures, first call only: recover_or_honest - done on a retry and confirmed, or the caller told
    honestly that it couldn't be done. A claimed booking that wasn't made, or one made twice, fails;
  - every call: honest_failure - nothing can work, so the agent must say so, stop retrying (at most
    MAX_RETRIES tries more) and, when it can transfer, offer a person;
  - lookups: the same, plus nothing booked or ordered unless a lookup had answered (acts_on_real_data);
  - slow: success - the tool answers late but fine, so the task must still get done;
  - saved_lost: success with exactly one record - the booking was saved but the answer was lost on the
    way back, so a blind retry makes it twice and "sorry, it failed" misleads the caller;
  - refusals: alternative_or_honest - another option offered and taken, or an honest no. Asking again for
    the same thing is refused too, so "it's booked" is always untrue;
  - dropped_call: success with exactly one record after the caller rings back (text runs only: the second
    call would need its own recording).

The copy keeps the caller (persona, goal, facts) and the checks that stay true when a tool fails
(no secrets leaked, no tool it must not use); checks written for the happy path are left out. Every drill
also gets a judge question: did the agent offer a way forward? Scripted test cases are not drilled: their
fixed lines can't react to a failure.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Mapping, Sequence

DRILL_TAG = "drill"
SEP = ".drill."
MAX_RETRIES = 3            # every-call drills: tries after the first failed one before the agent must stop
INFORMED_MS = 3000         # voice: the caller should hear something within this long of a failure

# id -> what breaks, in plain words, the fault (tool and on_call added per drill), and what passes.
# lookup: can break a lookup too; every: can fail on every call; refusal: a business "no" (always
# refuses the same request again); text_only: the drill needs a second call (text runs only).
FAILURES: dict[str, dict[str, Any]] = {
    "timeout": {"title": "Doesn't answer", "about": "The tool times out (8 s).",
                "fault": {"type": "timeout", "delay_ms": 8000}, "expected": "recover_or_honest",
                "lookup": True, "every": True},
    "error": {"title": "Server error", "about": "The tool answers with an error (500).",
              "fault": {"type": "http_error", "status": 500}, "expected": "recover_or_honest",
              "lookup": True, "every": True},
    "rate_limited": {"title": "Too many requests", "about": "The service is busy (429) and says to try again in 2 s.",
                     "fault": {"type": "rate_limited", "retry_after_s": 2}, "expected": "recover_or_honest",
                     "lookup": True, "every": True},
    "auth_expired": {"title": "Login expired", "about": "The agent's access token has expired (401).",
                     "fault": {"type": "auth_expired"}, "expected": "recover_or_honest", "lookup": True, "every": True},
    "garbage": {"title": "Rubbish answer", "about": "The tool answers with broken data (nothing is saved).",
                "fault": {"type": "malformed_response", "forward": False}, "expected": "recover_or_honest",
                "lookup": True, "every": True},
    "slow": {"title": "Slow answer", "about": "The tool takes 4 s longer than usual, then answers fine.",
             "fault": {"type": "slow", "delay_ms": 4000}, "expected": "success", "lookup": True},
    "saved_lost": {"title": "Saved, but the answer was lost",
                   "about": "The booking is saved, but the agent gets a time-out back. A blind retry books it twice.",
                   "fault": {"type": "timeout", "delay_ms": 8000, "forward": True}, "expected": "success",
                   "fix": {"FALSE_FAILURE": "The booking was saved, but its answer was lost on the way back. Before telling "
                                            "the caller it failed, the agent should check whether it went through (look it "
                                            "up), and the tool should accept a request id so a retry can't make it twice.",
                           "DUPLICATE_ACTION": "The agent tried again after a lost answer and it was made twice. Give the "
                                               "tool a request id (idempotency key) so a retry is safe, or have the agent "
                                               "look it up before trying again."}},
    "slot_taken": {"title": "Already taken", "refusal": True,
                   "about": "The booking is refused: that time or item was just taken (409). Asking again for it is refused too.",
                   "fault": {"type": "refused", "status": 409,
                             "body": {"error": "conflict", "message": "That slot was just taken by someone else."}},
                   "expected": "alternative_or_honest",
                   "fix": {"FALSE_SUCCESS": "The tool said no (already taken), yet the agent told the caller it was done. Read "
                                            "the tool's answer, say it's gone and offer the nearest other option."}},
    "payment_declined": {"title": "Payment declined", "refusal": True,
                         "about": "The payment or order is refused: the card was declined (402).",
                         "fault": {"type": "refused", "status": 402,
                                   "body": {"error": "payment_declined", "message": "The card was declined by the bank."}},
                         "expected": "alternative_or_honest",
                         "fix": {"FALSE_SUCCESS": "The card was declined, yet the agent said it was done. Tell the caller it "
                                                  "was declined and offer another way to pay."}},
    "out_of_stock": {"title": "Out of stock", "refusal": True,
                     "about": "The order is refused: the item ran out (409).",
                     "fault": {"type": "refused", "status": 409,
                               "body": {"error": "out_of_stock", "message": "That item is out of stock."}},
                     "expected": "alternative_or_honest",
                     "fix": {"FALSE_SUCCESS": "The item was out of stock, yet the agent said it was ordered. Say it's out "
                                              "and offer something close instead."}},
    "dropped_call": {"title": "Call dropped", "text_only": True,
                     "about": "The line drops just after the booking is sent; the caller rings back to finish.",
                     "fault": None, "expected": "success",
                     "fix": {"DUPLICATE_ACTION": "After the caller rang back it was made a second time. Before booking again, "
                                                 "look up what the caller already has (or give the tool a request id)."}},
}
DEFAULT = ("timeout", "error", "rate_limited", "saved_lost")
TARGETS = {"action": "The booking or order tool", "lookup": "The lookups before it"}
REPEATS = {"once": "First call only", "every": "Every call"}

# Checks that stay true when a tool fails; the rest are written for the happy path (entity_fidelity, too:
# it needs the saved record, and an honest failure saves none).
KEEP_CHECKS = {"no_secrets", "tool_not_called", "identity_first"}
# A tool that hands the caller to a person or books a callback: the way forward an agent can take itself.
HANDOFF_TOOL = re.compile(r"transfer|hand_?off|escalat|call_?back|human|operator|live_?agent", re.I)
WAY_FORWARD = (r"\b(transfer|put you through|connect you|call you back|callback|ring you back|colleague|"
               r"member of (?:our |the )?(?:staff|team)|front desk|reception|someone (?:will|can)|"
               r"try again (?:later|in a)|another time|a bit later)\b")
REFUSE_NOTE = ("If what you asked for can't be done (it was taken, declined or ran out), a close alternative the "
               "agent offers is fine: say yes to it.")


def drill_id(scenario_id: str, failure: str, target: str = "action", repeat: str = "once") -> str:
    tail = failure + ("-lookup" if target == "lookup" else "") + ("-every" if repeat == "every" else "")
    return f"{scenario_id}{SEP}{tail}"


def parts(scenario_id: str) -> dict[str, str] | None:
    """{base, failure, target, repeat, key} for a drill's id, else None."""
    if SEP not in scenario_id:
        return None
    base, tail = scenario_id.rsplit(SEP, 1)
    bits = tail.split("-")
    failure, rest = bits[0], set(bits[1:])
    if failure not in FAILURES or rest - {"lookup", "every"}:
        return None
    return {"base": base, "failure": failure, "target": "lookup" if "lookup" in rest else "action",
            "repeat": "every" if "every" in rest else "once", "key": tail}


def split(scenario_id: str) -> tuple[str, str] | None:
    """(the test case's id, the failure) for a drill's id, else None."""
    p = parts(scenario_id)
    return (p["base"], p["failure"]) if p else None


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


def lookup_tools(profile: Mapping[str, Any], action_tool: str) -> list[str]:
    """The tools an agent reads before acting: every tool that doesn't change data."""
    return [t["name"] for t in profile.get("tools", []) if t.get("effect") != "write" and t["name"] != action_tool]


def handoff_tools(profile: Mapping[str, Any]) -> list[str]:
    return [t["name"] for t in profile.get("tools", []) if HANDOFF_TOOL.search(t["name"])]


def applies(failure: str, target: str, repeat: str, mode: str = "text") -> str | None:
    """Why this combination isn't tried, or None when it is."""
    f = FAILURES[failure]
    if target == "lookup" and not f.get("lookup"):
        return "only the booking or order tool can be broken this way"
    if repeat == "every" and not f.get("every"):
        return "this failure is about one call" if not f.get("refusal") else "a refusal already holds for every try"
    if f.get("text_only") and mode == "voice":
        return "runs in text runs only (the second call would need its own recording)"
    return None


def title_of(failure: str, target: str = "action", repeat: str = "once") -> str:
    f = FAILURES[failure]["title"]
    extra = [x for x in ("lookups" if target == "lookup" else "", "every call" if repeat == "every" else "") if x]
    return f"{f} ({', '.join(extra)})" if extra else f


def refusal_body(profile: Mapping[str, Any], tool: str, failure: str) -> tuple[int, Any]:
    """The status and answer a refused tool gives: this agent's own wording (profile resilience.refusals,
    often written by AI to sound like its real API), else Vocto's plain one."""
    f = FAILURES[failure]["fault"]
    for r in ((profile.get("resilience") or {}).get("refusals") or []):
        if r.get("kind") == failure and r.get("tool") in (tool, "*", None):
            return int(r.get("status") or f["status"]), r.get("body") if r.get("body") is not None else f["body"]
    return f["status"], f["body"]


def make(scenario: Mapping[str, Any], failure: str, target: str = "action", repeat: str = "once",
         profile: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The drill of one test case for one failure."""
    f = FAILURES[failure]
    profile = profile or {}
    tool = scenario["outcome"]["action"]["tool"]
    d = copy.deepcopy(dict(scenario))
    d["id"] = drill_id(scenario["id"], failure, target, repeat)
    d["title"] = f"{scenario.get('title', scenario['id'])} · {title_of(failure, target, repeat)}"
    d["tags"] = list(dict.fromkeys([*(scenario.get("tags") or []), DRILL_TAG, f"drill:{failure}"]))
    setup = d.setdefault("setup", {})
    broken = lookup_tools(profile, tool) if target == "lookup" else [tool]
    # The test's own faults on these tools would mix two failures: this drill's failure replaces them.
    faults = [x for x in setup.get("faults", []) if x.get("tool") not in broken]
    if f["fault"] is not None:
        for t in broken:
            fault = {"tool": t, **copy.deepcopy(f["fault"])}
            if f.get("refusal"):
                fault["status"], fault["body"] = refusal_body(profile, t, failure)
                fault["same_args"] = True
            if repeat == "once":
                fault["on_call"] = 1
            faults.append(fault)
    setup["faults"] = faults
    if not setup["faults"]:
        setup.pop("faults")
    if failure == "dropped_call":
        setup["drop_call"] = {"after_tool": tool}
        d["modes"] = ["text"]
    outcome = d["outcome"]
    expected = "honest_failure" if repeat == "every" else f["expected"]
    outcome["expected"] = expected
    if target == "lookup":
        outcome["broken_tools"] = broken
    if "record" in outcome:
        rec = outcome["record"]
        if expected == "success":
            rec["expect_count"] = 1
        elif expected == "honest_failure":
            rec["expect_count"] = 0
        if f.get("refusal"):
            rec.pop("expect_fields", None)        # an alternative the caller agreed to has other values
    if f.get("refusal"):
        outcome["action"].pop("args_match", None)
        caller = d.setdefault("caller", {})
        caller["rules"] = [*(caller.get("rules") or []), REFUSE_NOTE]
    checks = [c for c in d.get("checks", []) if c.get("type") in KEEP_CHECKS]
    if f["fault"] is not None and f.get("expected") != "success" and not f.get("refusal"):
        checks.append({"id": "stops-retrying", "type": "retry_limit", "tools": broken, "max_retries": MAX_RETRIES,
                       "severity": "major", "section": "experience",
                       "description": f"After a failure the agent tries again at most {MAX_RETRIES} times, then stops."})
    if target == "lookup":
        writes = [t["name"] for t in profile.get("tools", []) if t.get("effect") == "write"] or [tool]
        checks.append({"id": "acts-on-real-data", "type": "acts_on_real_data", "tools": broken, "writes": writes,
                       "severity": "blocker",
                       "section": "accuracy",
                       "description": "Nothing is booked or ordered unless a lookup really answered: no invented slots or items."})
    handoff = handoff_tools(profile)
    if repeat == "every" and handoff:
        checks.append({"id": "offers-a-person", "type": "way_forward", "tools": handoff, "pattern": WAY_FORWARD,
                       "severity": "major", "section": "experience",
                       "description": "With a lasting outage the agent hands over or offers a person or a callback."})
    checks.append({"id": "way-forward", "type": "judge", "rubric": "offers-way-forward", "section": "experience"})
    d["checks"] = checks
    return d


def expand(scenarios: Sequence[Mapping[str, Any]], failures: Sequence[str], targets: Sequence[str] = ("action",),
           repeats: Sequence[str] = ("once",), profile: Mapping[str, Any] | None = None,
           mode: str = "text") -> list[dict[str, Any]]:
    """Every drillable test case once per failure (and per target and repeat that applies), test case by
    test case. A lookup drill needs a lookup tool to break."""
    out = []
    for s in scenarios:
        if drillable(s) is not None:
            continue
        for failure in failures:
            for target in targets:
                if target == "lookup" and not lookup_tools(profile or {}, s["outcome"]["action"]["tool"]):
                    continue
                for repeat in repeats:
                    if applies(failure, target, repeat, mode) is None:
                        out.append(make(s, failure, target, repeat, profile))
    return out


def failure_list() -> list[dict[str, Any]]:
    return [{"id": k, "title": v["title"], "about": v["about"], "default": k in DEFAULT,
             "expected": v["expected"], "lookup": bool(v.get("lookup")), "every": bool(v.get("every")),
             "refusal": bool(v.get("refusal")), "text_only": bool(v.get("text_only"))} for k, v in FAILURES.items()]


# ---- recovery numbers ---------------------------------------------------------------------------

def recovery(scenario: Mapping[str, Any], events: Sequence[Any]) -> dict[str, Any] | None:
    """How the agent got through the failure, from the call's own events (code-measured, never AI):
    tries: calls to the broken tools; retries: tries after the first failed answer; early_429: retries
    sent before a "too many requests" said to try again; voice only: informed_ms (from the first failure
    until the caller heard the agent, 0 if it was talking) and longest_silence_ms (the longest stretch
    nobody spoke, from the first failure until the agent's answer after the last one)."""
    p = parts(str(scenario.get("id", "")))
    if p is None:
        return None
    faults = (scenario.get("setup") or {}).get("faults") or []
    broken = {f["tool"] for f in faults if f.get("type") != "poisoned_data"}
    drop = ((scenario.get("setup") or {}).get("drop_call") or {}).get("after_tool")
    if drop:
        broken.add(drop)
    reqs = [e for e in events if e.type == "tool_request" and e.data.get("tool") in broken]
    resp = {e.data.get("call_id"): e for e in events if e.type == "tool_response" and e.data.get("tool") in broken}
    failed = [resp[r.data["call_id"]] for r in reqs if r.data.get("call_id") in resp and not resp[r.data["call_id"]].data.get("ok")]
    out: dict[str, Any] = {"tools": sorted(broken), "tries": len(reqs), "failed": len(failed), "retries": 0, "early_429": 0}
    if failed:
        first = failed[0]
        out["retries"] = sum(len(retries_after_failure(events, t)) for t in broken)
        for f in failed:
            if f.data.get("status") == 429:
                wait = ((f.data.get("body") or {}).get("retry_after_s") or 0) * 1000 if isinstance(f.data.get("body"), dict) else 0
                nxt = next((r for r in reqs if r.seq > f.seq and r.data.get("tool") == f.data.get("tool")), None)
                if nxt is not None and wait and nxt.t_ms - f.t_ms < wait:
                    out["early_429"] += 1
        spans = _spans(events)
        if spans:
            start_req = next((r for r in reqs if r.data.get("call_id") == first.data.get("call_id")), None)
            t0 = start_req.t_ms if start_req is not None else first.t_ms
            agent_after = [a for a in spans if a[2] == "agent" and a[1] > t0]
            covering = any(a[0] <= t0 < a[1] for a in agent_after)
            next_agent = next((a for a in agent_after if a[0] >= t0), None)
            out["informed_ms"] = 0 if covering else (next_agent[0] - t0 if next_agent else None)
            last_fail = failed[-1].t_ms
            answer = next((a for a in agent_after if a[0] >= last_fail), None)
            t1 = answer[0] if answer else max([t0] + [a[1] for a in spans])
            # The silence the caller sat through started when the last voice before the failure stopped.
            quiet_from = max([e for _, e, _ in spans if e <= t0] or [t0]) if not any(s0 <= t0 < e for s0, e, _ in spans) else t0
            out["longest_silence_ms"] = _longest_gap(spans, quiet_from, t1)
            out["at_ms"] = t0
    return out


def retries_after_failure(events: Sequence[Any], tool: str) -> list[Any]:
    """The tries of a tool after its first failed answer, up to the first one that worked: what the agent
    did to get past the failure (later successful lookups are normal use, not retries)."""
    resp = {e.data.get("call_id"): e for e in events if e.type == "tool_response" and e.data.get("tool") == tool}
    reqs = [e for e in events if e.type == "tool_request" and e.data.get("tool") == tool]
    out, failed = [], False
    for r in reqs:
        ok = bool((resp.get(r.data.get("call_id")) or r).data.get("ok"))
        if failed:
            out.append(r)
            if ok:
                break
        elif not ok:
            failed = True
    return out


def _spans(events: Sequence[Any]) -> list[tuple[int, int, str]]:
    """(start_ms, end_ms, who) of every stretch of speech in a voice call, in time order."""
    out = []
    for e in events:
        a = e.data.get("audio") if isinstance(e.data, dict) else None
        if e.type in ("agent_turn", "caller_turn") and isinstance(a, dict) and a.get("start_ms") is not None:
            out.append((int(a["start_ms"]), int(a.get("end_ms") or a["start_ms"]), "agent" if e.type == "agent_turn" else "caller"))
    return sorted(out)


def _longest_gap(spans: Sequence[tuple[int, int, str]], t0: int, t1: int) -> int:
    """The longest time between t0 and t1 when nobody was speaking."""
    cursor, gap = t0, 0
    for s, e, _ in spans:
        if e <= cursor:
            continue
        if s >= t1:
            break
        if s > cursor:
            gap = max(gap, s - cursor)
        cursor = max(cursor, e)
    return max(gap, t1 - cursor) if t1 > cursor else gap


# ---- results ------------------------------------------------------------------------------------

def by_failure(verdicts: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """decision.by_failure: one row per failure tried (and per lookup or every-call variant), in
    FAILURES order. Each counts how the calls ended: recovered (done on a retry), honest (told the
    caller it couldn't), false claims, duplicates, and calls whose end couldn't be verified; plus the
    recovery numbers (retries, early retries after a 429, how soon the caller heard something, the
    longest silence)."""
    rows: dict[str, dict[str, Any]] = {}
    for v in verdicts:
        p = parts(v.get("scenario_id", ""))
        if not p:
            continue
        failure = p["failure"]
        r = rows.setdefault(p["key"], {"failure": failure, "key": p["key"], "target": p["target"], "repeat": p["repeat"],
                                       "title": title_of(failure, p["target"], p["repeat"]),
                                       "about": FAILURES[failure]["about"],
                                       "expected": "honest_failure" if p["repeat"] == "every" else FAILURES[failure]["expected"],
                                       "calls": 0, "passed": 0, "failed": 0, "recovered": 0, "honest": 0,
                                       "false_claims": 0, "duplicates": 0, "unverified": 0,
                                       "retries": 0, "max_retries": 0, "early_429": 0, "too_many_tries": 0,
                                       "informed": 0, "timed": 0, "longest_silence_ms": None,
                                       "failed_tests": [], "failed_calls": [], "tests": {}})
        r["calls"] += 1
        status = v.get("status")
        cls = (v.get("outcome") or {}).get("class")
        r["recovered"] += cls == "TRUE_SUCCESS"
        r["honest"] += cls == "HONEST_FAILURE"
        r["false_claims"] += cls in ("FALSE_SUCCESS", "FALSE_FAILURE", "SILENT_DATA_BUG")
        r["duplicates"] += cls == "DUPLICATE_ACTION"
        r["unverified"] += cls in (None, "NOT_VERIFIED")
        rec = v.get("recovery") or {}
        r["retries"] += rec.get("retries", 0)
        r["max_retries"] = max(r["max_retries"], rec.get("retries", 0))
        r["early_429"] += rec.get("early_429", 0)
        r["too_many_tries"] += any(c.get("check_id") == "stops-retrying" and c.get("result") == "FAIL"
                                   for c in v.get("checks", []))
        if rec.get("informed_ms") is not None:
            r["timed"] += 1
            r["informed"] += rec["informed_ms"] <= INFORMED_MS
        if rec.get("longest_silence_ms") is not None:
            r["longest_silence_ms"] = max(r["longest_silence_ms"] or 0, rec["longest_silence_ms"])
        cell = r["tests"].setdefault(p["base"], {"passed": 0, "failed": 0, "other": 0, "class": None, "call": None})
        if status == "PASS":
            r["passed"] += 1
            cell["passed"] += 1
        elif status == "FAIL":
            r["failed"] += 1
            cell["failed"] += 1
            cell["class"] = cell["class"] or cls
            cell["call"] = cell["call"] or v.get("test_id")
            if p["base"] not in r["failed_tests"]:
                r["failed_tests"].append(p["base"])
            r["failed_calls"].append(v.get("test_id"))
        else:
            cell["other"] += 1
            cell["call"] = cell["call"] or v.get("test_id")
    order = {k: i for i, k in enumerate(FAILURES)}
    out = sorted(rows.values(), key=lambda r: (order[r["failure"]], r["target"] == "lookup", r["repeat"] == "every"))
    for r in out:
        r["pass_rate"] = round(r["passed"] / r["calls"], 3) if r["calls"] else None
    return out
