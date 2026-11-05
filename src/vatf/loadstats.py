"""Load test numbers (Phase 7): what each call showed, each stage's numbers, and the agent's capacity.

    call_facts(events, verdict)              -> verdict["load"] facts for one call (how it ended, timings)
    stage_stats(stage, verdicts, samples)    -> one stage's numbers and which limits it broke
    summarise(...)                           -> load.json: every stage, capacity, slowdown, drift, notes

Everything is read from the calls' own verdicts and events, so it works for any agent. A call counts
in the stage it started in. Three kinds of call:

  ok        the call ran to its end (whether its checks passed is the pass rate, a separate number)
  failed    the agent's side: it couldn't be reached, raised an error, stopped answering, or the call
            ran past max_call_seconds. These count toward "failed calls".
  harness   Vocto's side (speech models, the caller's AI, a crash): never the agent's fault, never in
            "failed calls", and too many of them make the run "not enough evidence".
"""

from __future__ import annotations

import bisect
import re
from typing import Any, Iterable, Mapping, Sequence

from .latency import percentile, turn_latencies
from .load import Stage
from .metrics import run_quality

BAND_RANK = {"good": 0, "warn": 1, "bad": 2}
AGENT_CODES = {"connect_failed": "couldn't connect", "agent_exception": "agent error"}
FAILED_WHY = ("couldn't connect", "agent error", "stopped answering", "ran too long")
HARNESS_SHARE = 0.10     # Vocto errors above this share of calls: not enough evidence
# Outcomes where the agent's words or writes don't match the truth: wrong under any load and any data.
BROKEN_OUTCOMES = {"FALSE_SUCCESS", "FALSE_FAILURE", "SILENT_DATA_BUG", "DUPLICATE_ACTION", "UNEXPECTED_ACTION"}
# Check types that fail only when the agent did something it must never do (said a forbidden thing,
# called a tool it must not, in the wrong order, read back wrong data, leaked a secret).
NEVER_TYPES = {"not_said", "tool_not_called", "tool_order", "readback", "entity_fidelity", "no_secrets",
               "log_not_contains"}
CALLED = re.compile(r"was called (\d+) time\(s\); expected exactly (\d+)")


def must_never(check: Mapping[str, Any], verdict: Mapping[str, Any]) -> bool:
    """A failed blocker that is wrong under any load: the agent lied, wrote twice, wrote wrong data,
    broke an order rule or leaked. Failures that only mean "the expected action didn't happen" (the slot
    was taken by another caller, the test data ran out) don't count here; they lower the pass rate.
    Many repeats of a test share one copy of the test data under load, so those are expected."""
    if check.get("check_id") == "outcome":
        return str((verdict.get("outcome") or {}).get("class", "")).upper() in BROKEN_OUTCOMES
    kind = check.get("type")
    if kind == "tool_called":
        m = CALLED.search(check.get("message", ""))
        return bool(m) and int(m.group(1)) > int(m.group(2))     # called more often than allowed: a double write
    return kind in NEVER_TYPES


def _ev(e: Any) -> dict[str, Any]:
    if isinstance(e, Mapping):
        return dict(e)
    return {"seq": e.seq, "t_ms": e.t_ms, "source": e.source, "type": e.type, "data": e.data, "turn": e.turn}


def call_facts(events: Iterable[Any], verdict: Mapping[str, Any]) -> dict[str, Any]:
    """How one load call ended and what it measured."""
    evs = [_ev(e) for e in events]
    errors = [e["data"].get("code") for e in evs if e["type"] == "harness_event" and e["data"].get("level") == "error"]
    ended = next((e["data"] for e in reversed(evs) if e["type"] == "call_ended"), {})
    status = verdict.get("status")
    outcome, why = "ok", ""
    if status == "ERROR":
        code = errors[0] if errors else "harness_crash"
        if code in AGENT_CODES:
            outcome, why = "failed", AGENT_CODES[code]
        else:
            outcome, why = "harness", code
    elif "agent_exception" in errors:
        outcome, why = "failed", "agent error"
    elif ended.get("reason") == "the agent stopped answering":
        outcome, why = "failed", "stopped answering"
    elif ended.get("ended_by") == "timeout":
        outcome, why = "failed", "ran too long"
    turns = [e for e in evs if e["type"] == "agent_turn"]
    unanswered = sum(1 for e in turns if e["data"].get("silent") and (e.get("turn") or 0) > 1)
    started = next((e for e in evs if e["type"] == "call_started"), None)
    quality = (verdict.get("metrics") or {}).get("quality") or {}
    if "ttfw_ms" in quality:
        first_word = quality["ttfw_ms"].get("value")
    elif turns and started is not None:
        first_word = max(0, turns[0]["t_ms"] - started["t_ms"])   # text: from the call's start to the greeting
    else:
        first_word = None
    # Each timed answer with its time in the call, so a stage counts the answers given while it ran
    # (a call that started in one stage can answer in the next, under the next stage's load).
    timed: list[list[int]] = []
    if any("latency_ms" in e["data"] for e in turns):          # voice: from the recording
        at = {e["seq"]: e["t_ms"] for e in turns}
        timed = [[at.get(r["seq"], 0), r["ms"]] for r in turn_latencies(evs) if r["counted"]]
    else:                                                       # text: caller turn to the agent's answer
        last = None
        for e in evs:
            if e["type"] == "caller_turn":
                last = e
            elif e["type"] == "agent_turn" and last is not None:
                timed.append([e["t_ms"], e["t_ms"] - last["t_ms"]])
                last = None
    faulted = {e["data"].get("call_id") for e in evs if e["type"] == "fault_injected"}
    tools = [e["data"] for e in evs if e["type"] == "tool_response" and e["data"].get("call_id") not in faulted]
    failed_blockers = [c for c in verdict.get("checks", []) if c.get("result") == "FAIL"
                       and c.get("severity") == "blocker" and c.get("can_block", True) and c.get("origin") != "judge"]
    blockers = sorted({(f"outcome ({str(verdict['outcome'].get('class'))})" if c["check_id"] == "outcome"
                        and verdict.get("outcome") else c["check_id"]) for c in failed_blockers if must_never(c, verdict)})
    missed = sorted({c["check_id"] for c in failed_blockers if not must_never(c, verdict)})
    return {
        "outcome": outcome, "why": why,
        "timed": timed,               # [t_ms in the call, answer ms]; made absolute (t_s) by the runner
        "answered_turns": sum(1 for e in turns if (e.get("turn") or 0) > 1 and not e["data"].get("silent")),
        "unanswered_turns": unanswered,
        "first_word_ms": first_word,
        "tool_ms": [int(t.get("duration_ms") or 0) for t in tools if t.get("reached_backend")],
        "tool_errors": sum(1 for t in tools if t.get("reached_backend") and int(t.get("status") or 0) >= 500),
        "tool_calls": len(tools),
        "blockers": blockers,          # must-never failures: these block a load run
        "missed": missed,              # blocker checks that failed because the expected action didn't happen
        "outcome_class": (verdict.get("outcome") or {}).get("class"),
    }


def _pct(n: int, d: int) -> float | None:
    return round(100 * n / d, 1) if d else None


def _p(values: Sequence[float], p: float) -> int | None:
    return percentile([int(v) for v in values if v is not None], p)


def answers_in(stage: Stage, verdicts: Sequence[Mapping[str, Any]], samples: Sequence[Sequence[float]]) -> list[int]:
    """Answer times given while the stage ran, with no more calls in flight than its level: after a
    spike, the recovery counts only once the spike's calls have finished. From calls that ran to the end."""
    out = []
    for v in verdicts:
        f = v.get("load") or {}
        if f.get("outcome") != "ok":
            continue
        for t_s, ms in f.get("answers", []):
            if stage.start_s <= t_s < stage.end_s and _in_flight_at(samples, t_s) <= stage.level:
                out.append(ms)
    return out


def _in_flight_at(samples: Sequence[Sequence[float]], t_s: float) -> int:
    """Calls in flight at t_s: the most seen in the monitor's readings within 0.2 s of it (samples are in time order)."""
    times = [x[0] for x in samples] if not isinstance(samples, _Timed) else samples.times
    lo, hi = bisect.bisect_left(times, t_s - 0.2), bisect.bisect_right(times, t_s + 0.2)
    near = [samples[i][2] for i in range(lo, hi)]
    return int(max(near)) if near else 0


class _Timed(list):
    """Monitor samples with their times kept apart, for fast lookups."""

    def __init__(self, samples: Iterable[Sequence[float]]):
        super().__init__(samples)
        self.times = [x[0] for x in self]


def stage_stats(stage: Stage, verdicts: Sequence[Mapping[str, Any]], samples: Sequence[Sequence[float]],
                limits: Mapping[str, Any], p95_limit: float | None, profile: Mapping[str, Any],
                every: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """One stage's numbers. `verdicts` are the calls that started in it; `every` is all the load's calls
    (their answers count in the stage they were given in). `samples`: the monitor's (t_s, lag_ms, in_flight)."""
    facts = [(v, v.get("load") or {}) for v in verdicts]
    ok = [v for v, f in facts if f.get("outcome") == "ok"]
    failed = [(v, f) for v, f in facts if f.get("outcome") == "failed"]
    harness = [v for v, f in facts if f.get("outcome") == "harness"]
    finished = len(ok) + len(failed)
    lat = answers_in(stage, every or verdicts, samples)
    first = [f["first_word_ms"] for v, f in facts if f.get("outcome") == "ok" and f.get("first_word_ms") is not None]
    tool_ms = [ms for _, f in facts for ms in f.get("tool_ms", [])]
    passed = sum(v["status"] == "PASS" for v in ok)
    failing = sum(v["status"] == "FAIL" for v in ok)
    answered = sum(f.get("answered_turns", 0) for _, f in facts)
    unanswered = sum(f.get("unanswered_turns", 0) for _, f in facts)
    blockers: dict[str, list[str]] = {}
    for v, f in facts:
        for check in f.get("blockers", []):
            blockers.setdefault(check, []).append(v["test_id"])
    window = [s for s in samples if stage.start_s <= s[0] < stage.end_s]
    lags = [s[1] for s in window]
    in_flight = [s[2] for s in window]
    why: dict[str, int] = {}
    for _, f in failed:
        why[f.get("why") or "failed"] = why.get(f.get("why") or "failed", 0) + 1
    out: dict[str, Any] = {
        **stage.public(),
        "calls": len(verdicts), "finished": finished, "ok": len(ok), "failed": len(failed), "harness_errors": len(harness),
        "failed_pct": _pct(len(failed), finished), "failed_why": why,
        "failed_refs": [v["test_id"] for v, _ in failed][:20],
        "unanswered_turns": unanswered, "unanswered_pct": _pct(unanswered, answered + unanswered),
        "latency": {"turns": len(lat), "p50_ms": _p(lat, 50), "p90_ms": _p(lat, 90), "p95_ms": _p(lat, 95),
                    "p99_ms": _p(lat, 99), "max_ms": max(lat) if lat else None},
        "first_word_p95_ms": _p(first, 95),
        "tool_p95_ms": _p(tool_ms, 95), "tool_calls": sum(f.get("tool_calls", 0) for _, f in facts),
        "tool_errors": sum(f.get("tool_errors", 0) for _, f in facts),
        "passed": passed, "failed_tests": failing, "pass_rate": _pct(passed, passed + failing),
        "blockers": blockers,
        "quality": run_quality(ok, profile),
        "lag_p95_ms": _p(lags, 95), "lag_max_ms": int(max(lags)) if lags else None,
        "in_flight_mean": round(sum(in_flight) / len(in_flight), 1) if in_flight else None,
        "in_flight_max": int(max(in_flight)) if in_flight else None,
    }
    trusted, untrusted_why = True, ""
    if finished < int(limits["min_calls"]):
        trusted, untrusted_why = False, f"only {finished} call(s) finished ({limits['min_calls']} needed)"
    elif out["lag_p95_ms"] is not None and out["lag_p95_ms"] > float(limits["max_lag_ms"]):
        trusted, untrusted_why = False, (f"Vocto's own computer couldn't keep up (lag p95 {out['lag_p95_ms']} ms, "
                                         f"limit {limits['max_lag_ms']} ms)")
    problems = []
    if out["failed_pct"] is not None and out["failed_pct"] > float(limits["max_failed_pct"]):
        problems.append({"rule": "failed_calls", "message":
                         f"{len(failed)} of {finished} calls failed ({out['failed_pct']:g}%, limit "
                         f"{limits['max_failed_pct']:g}%): " + ", ".join(f"{n} {w}" for w, n in why.items()) + "."})
    p95 = out["latency"]["p95_ms"]
    if p95_limit is not None and p95 is not None and p95 > p95_limit:
        problems.append({"rule": "latency", "message": f"answer time p95 {p95:,} ms (limit {p95_limit:,.0f} ms) "
                                                       f"over {len(lat)} answers."})
    for q in out["quality"]:
        if q["blocks"] and q["band"] == "bad":
            problems.append({"rule": "metric", "message": f"{q['title']} is in the bad band ({q['value']} {q['unit']})."})
    out["trusted"] = trusted
    out["untrusted_why"] = untrusted_why
    out["problems"] = problems
    out["meets_limits"] = None if not trusted else not problems
    return out


def summarise(settings: Mapping[str, Any], stage_list: Sequence[Stage], verdicts: Sequence[Mapping[str, Any]],
              samples: Sequence[Sequence[float]], p95_limit: float | None, profile: Mapping[str, Any],
              mode: str, notes: Sequence[str] = (), gateway: Mapping[str, int] | None = None,
              timeline: Sequence[Sequence[float]] = ()) -> dict[str, Any]:
    """load.json: the stages' numbers, capacity, top-vs-low comparisons and what to keep in mind."""
    limits = settings["limits"]
    samples = _Timed(samples)
    by_stage: dict[int, list[Mapping[str, Any]]] = {}
    for v in verdicts:
        by_stage.setdefault(int((v.get("load") or {}).get("stage", 0)), []).append(v)
    rows = [stage_stats(st, by_stage.get(st.index, []), samples, limits, p95_limit, profile, verdicts)
            for st in stage_list]

    # Capacity: the highest level up to which every stage met the limits (and could be trusted).
    capacity, breaking = 0, None
    for level in sorted({r["level"] for r in rows}):
        at = [r for r in rows if r["level"] == level]
        if all(r["meets_limits"] for r in at):
            if breaking is None:
                capacity = level
        elif breaking is None:
            bad = next((r for r in at if r["meets_limits"] is False), None)
            if bad is not None:
                breaking = {"level": level, "stage": bad["name"], "why": [p["message"] for p in bad["problems"]]}
            else:   # not trusted: capacity can't be read past this level
                breaking = {"level": level, "stage": at[0]["name"], "why": [at[0]["untrusted_why"]], "untrusted": True}

    warnings: list[dict[str, Any]] = []
    blocking: list[dict[str, Any]] = []
    levels_seen = sorted({r["level"] for r in rows if r["finished"]})
    low = [r for r in rows if levels_seen and r["level"] == levels_seen[0] and r["finished"]]
    top = [r for r in rows if levels_seen and r["level"] == levels_seen[-1] and r["finished"]]
    gw = dict(gateway or {})
    unattributed = int(gw.get("unattributed", 0)) + int(gw.get("unknown_ids", 0))

    def stage_p95(rs):
        return _p([ms for r in rs for ms in answers_in(stage_list[r["index"]], verdicts, samples)], 95)

    def stage_rate(rs):
        p = sum(r["passed"] for r in rs)
        f = sum(r["failed_tests"] for r in rs)
        return _pct(p, p + f)

    comparisons: dict[str, Any] = {}
    if low and top and levels_seen[0] != levels_seen[-1]:
        b, t = stage_p95(low), stage_p95(top)
        comparisons["slowdown"] = {"low_level": levels_seen[0], "top_level": levels_seen[-1], "low_p95_ms": b, "top_p95_ms": t}
        rule = limits["slowdown"]
        if b is not None and t is not None and t - b > max(float(rule["ms"]), b * float(rule["pct"]) / 100):
            item = {"rule": "slowdown", "message":
                    f"Answers got slower under load: p95 {b:,} ms at {levels_seen[0]} call(s) at once, {t:,} ms at "
                    f"{levels_seen[-1]} (+{t - b:,} ms; the limit is {rule['pct']:g}% and {rule['ms']:g} ms)."}
            (blocking if rule["blocks"] else warnings).append(item)
        rb, rt = stage_rate(low), stage_rate(top)
        comparisons["pass_rate"] = {"low": rb, "top": rt}
        rule = limits["pass_drop"]
        if rb is not None and rt is not None and rb - rt >= float(rule["points"]) and not unattributed:
            item = {"rule": "pass_drop", "message":
                    f"Fewer tests passed under load: {rt:g}% at {levels_seen[-1]} call(s) at once, {rb:g}% at "
                    f"{levels_seen[0]} ({rt - rb:+g} points; the limit is {rule['points']:g}). All load calls share one "
                    "copy of the test data, so callers can take each other's slot or use the data up; look at the "
                    "failed checks before reading this as the agent's fault."}
            (blocking if rule["blocks"] else warnings).append(item)
        ql = {q["id"]: q for r in low for q in r["quality"]}
        for r in top:
            for q in r["quality"]:
                b0 = ql.get(q["id"])
                if b0 and b0["band"] in BAND_RANK and q["band"] in BAND_RANK and BAND_RANK[q["band"]] > BAND_RANK[b0["band"]]:
                    warnings.append({"rule": "load_metric", "message":
                                     f"{q['title']} went from {b0['band']} at {levels_seen[0]} call(s) at once to "
                                     f"{q['band']} at {levels_seen[-1]}."})
    windows = [r for r in rows if r["kind"] == "window" and r["finished"]]
    if len(windows) >= 2:
        first, last = windows[0], windows[-1]
        b, t = first["latency"]["p95_ms"], last["latency"]["p95_ms"]
        comparisons["drift"] = {"first_p95_ms": b, "last_p95_ms": t,
                                "first_failed_pct": first["failed_pct"], "last_failed_pct": last["failed_pct"]}
        rule = limits["drift"]
        msgs = []
        if b is not None and t is not None and t - b > b * float(rule["pct"]) / 100 and t - b > 100:
            msgs.append(f"answer time p95 went from {b:,} ms in the first window to {t:,} ms in the last")
        if (last["failed_pct"] or 0) > (first["failed_pct"] or 0) and last["failed"] > first["failed"]:
            msgs.append(f"failed calls went from {first['failed_pct'] or 0:g}% to {last['failed_pct']:g}%")
        if msgs:
            item = {"rule": "drift", "message": "Over the soak, " + " and ".join(msgs)
                                                + ". Something builds up while the agent runs (memory, connections, a queue)."}
            (blocking if rule["blocks"] else warnings).append(item)

    calls = len(verdicts)
    harness = sum(1 for v in verdicts if (v.get("load") or {}).get("outcome") == "harness")
    all_notes = list(notes)
    missed = sum(1 for v in verdicts if (v.get("load") or {}).get("missed"))
    if missed:
        all_notes.append(f"In {missed} call(s) a blocker check failed only because the expected action didn't happen "
                         "(for example nothing was booked). The test data is loaded once for the whole load test, so "
                         "later callers can find it taken or used up. These calls count in the pass rate, not as "
                         "blockers; only things the agent must never do (a false success, a double write, wrong data, "
                         "a broken order rule, a leak) block a load test.")
    if unattributed:
        all_notes.append(f"{unattributed} tool call(s) reached Vocto's tool gateway without a test id while several calls "
                         "were going, so they couldn't be matched to their call. They were passed on untouched, but "
                         "tool checks and the pass rate of this run can't be trusted. Have the agent send the "
                         "call's test id with its tool calls (Connection › test id).")
    return {
        "schema_version": "0.1", "kind": "load_summary", "mode": mode,
        "shape": settings["shape"], "calls_at_once": settings["calls_at_once"], "caller": settings["caller"],
        "max_calls": settings["max_calls"], "p95_limit_ms": p95_limit, "limits": dict(limits),
        "calls": calls, "failed": sum(r["failed"] for r in rows), "harness_errors": harness,
        "harness_share": round(harness / calls, 3) if calls else 0.0,
        "capacity": capacity, "breaking": breaking,
        "stages": rows, "comparisons": comparisons,
        "blocking": blocking, "warnings": warnings,
        "unattributed_tool_calls": unattributed,
        "lag_p95_ms": _p([s[1] for s in samples], 95), "lag_max_ms": int(max(s[1] for s in samples)) if samples else None,
        "timeline": [list(t) for t in timeline],
        "notes": all_notes,
    }
