"""Verdict builder (contracts/evaluation.py VerdictBuilder): one verdict.json per call."""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from .contracts.models import CallTrace, CheckResult, Result
from .evidence import utc_now
from .latency import percentile, turn_latencies

ROLLUP = [Result.ERROR, Result.FAIL, Result.NEEDS_REVIEW, Result.NOT_VERIFIED]


def check_dict(r: CheckResult) -> dict[str, Any]:
    d = {
        "check_id": r.check_id,
        "origin": r.origin,
        "result": r.result.value,
        "severity": r.severity,
        "section": r.section,
        "message": r.message,
        "evidence": [{"event": e.event, **({"note": e.note} if e.note else {})} for e in r.evidence],
        "evidence_level": r.evidence_level.value,
        "can_block": r.can_block,
    }
    if r.type:
        d["type"] = r.type
    if r.judge is not None:
        d["judge"] = dict(r.judge)
    return d


def merge_results(base: Sequence[CheckResult], judge_results: Sequence[CheckResult]) -> list[CheckResult]:
    """AI can raise but never clear: judge results are added next to the hard results.
    A judge can never replace or overrule a non-judge result."""
    for j in judge_results:
        if j.origin != "judge":
            raise ValueError("only judge results may be merged here")
    return list(base) + list(judge_results)


def rollup(results: Sequence[CheckResult], harness_error: bool) -> str:
    return status_of([check_dict(r) for r in results], harness_error)


def status_of(checks: Sequence[Mapping[str, Any]], harness_error: bool) -> str:
    """A call's status from its check results (dicts as in verdict.json)."""
    if harness_error:
        return Result.ERROR.value
    # An advisory result (can_block false: an uncalibrated judge item) is shown but never sets the status,
    # unless it passed.
    counted = [c["result"] for c in checks if c["result"] != Result.NOT_APPLICABLE.value
               and (c.get("can_block", True) or c["result"] == Result.PASS.value)]
    for level in ROLLUP:
        if level.value in counted:
            return level.value
    return Result.PASS.value if counted else Result.NOT_VERIFIED.value


NUMBER = re.compile(r"\d(?:[\d -]*\d)?")   # "01632 960 000", "8", "1-800": digits with spaces or dashes


def heard_numbers(events) -> dict[str, Any] | None:
    """Voice calls where the agent reports its own words: were the numbers it said heard correctly by
    the caller's speech recognition? A mishearing is not the agent's mistake, but it is worth knowing:
    a real caller may mishear the same number (say it slower, or in groups). None without own words."""
    said = 0
    misheard = []
    seen = False
    for e in events:
        d = e.data if hasattr(e, "data") else e["data"]
        if (e.type if hasattr(e, "type") else e["type"]) != "agent_turn" or not d.get("platform_transcript"):
            continue
        seen = True
        # Each number must come back whole: "960 000, 8am" heard as "960 0008 am" is two numbers run into one.
        heard = {re.sub(r"\D", "", m.group(0)) for m in NUMBER.finditer(d.get("heard_text", ""))}
        for m in NUMBER.finditer(d["platform_transcript"]):
            said += 1
            if re.sub(r"\D", "", m.group(0)) not in heard:
                misheard.append({"seq": e.seq if hasattr(e, "seq") else e["seq"], "said": m.group(0),
                                 "heard": d.get("heard_text", "")[:160]})
    if not seen:
        return None
    return {"numbers": said, "heard_right": said - len(misheard), "misheard": misheard[:10]}


def call_metrics(trace: CallTrace) -> dict[str, Any]:
    events = trace.events
    caller_turns = [e for e in events if e.type == "caller_turn"]
    ended = [e for e in events if e.type == "call_ended"]
    metrics: dict[str, Any] = {"turns": len(caller_turns), "duration_ms": ended[-1].t_ms if ended else 0}
    if trace.record.get("mode") == "voice":
        # From the recorded audio: end of the caller's words to the agent's first sound.
        rows = turn_latencies(events)
        counted = [r["ms"] for r in rows if r["counted"]]
        metrics["latency_ms"] = counted
        metrics["latency_turns"] = rows
        metrics["latency_p50_ms"] = percentile(counted, 50)
        metrics["latency_p95_ms"] = percentile(counted, 95)
        heard = heard_numbers(events)
        if heard:
            metrics["heard_numbers"] = heard
    else:
        latencies, last_caller = [], None
        for e in events:
            if e.type == "caller_turn":
                last_caller = e
            elif e.type == "agent_turn" and last_caller is not None:
                latencies.append(e.t_ms - last_caller.t_ms)
                last_caller = None
        if latencies:  # text mode: processing time only, informational, never gates
            metrics["latency_ms"] = latencies
            metrics["latency_p50_ms"] = percentile(latencies, 50)
            metrics["latency_p95_ms"] = percentile(latencies, 95)
    metrics["cost_usd"] = trace.record.get("cost_usd", 0.0)
    return metrics


class LocalVerdictBuilder:
    def build(self, trace: CallTrace, results: Sequence[CheckResult],
              outcome: Mapping[str, Any] | None = None) -> dict[str, Any]:
        rec = trace.record
        verdict: dict[str, Any] = {
            "schema_version": "0.1",
            "kind": "call_verdict",
            "test_id": rec["test_id"],
            "run_id": rec["run_id"],
            "scenario_id": rec["scenario_id"],
        }
        if rec.get("persona_id"):
            verdict["persona_id"] = rec["persona_id"]
        verdict.update({
            "repeat": rec["repeat"],
            "evaluated_at": utc_now(),
            "status": rollup(results, rec.get("status") == "harness_error"),
        })
        if outcome is not None:
            verdict["outcome"] = dict(outcome)
        verdict["checks"] = [check_dict(r) for r in results]
        verdict["metrics"] = call_metrics(trace)
        return verdict
