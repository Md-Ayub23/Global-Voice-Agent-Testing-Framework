"""Verdict builder (contracts/evaluation.py VerdictBuilder): one verdict.json per call."""

from __future__ import annotations

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
    if harness_error:
        return Result.ERROR.value
    counted = [r.result for r in results if r.result != Result.NOT_APPLICABLE
               and not (r.result == Result.FAIL and not r.can_block)]
    for level in ROLLUP:
        if level in counted:
            return level.value
    return Result.PASS.value if counted else Result.NOT_VERIFIED.value


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
