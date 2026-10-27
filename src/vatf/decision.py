"""Decision engine (contracts/evaluation.py DecisionEngine): GO / NO_GO / NO_GO_INSUFFICIENT_EVIDENCE.

Rules, in order (schemas/run_decision.schema.yaml):
  1. NO_GO  - a blocking FAIL in a verified call.
  2. NO_GO  - a critical scenario failed a repeat (pass^k not met); latency only in voice mode.
  3. NO_GO_INSUFFICIENT_EVIDENCE - coverage too low, too few verified repeats, or open NEEDS_REVIEW.
  4. GO.
`verdicts` are the final attempt of each planned call. Rerun ERROR attempts are not passed in.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Mapping, Sequence

from .evidence import utc_now

EXIT_CODES = {"GO": 0, "NO_GO": 1, "NO_GO_INSUFFICIENT_EVIDENCE": 2}
VERIFIED = {"PASS", "FAIL"}


def _section_scores(verdicts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    tally: dict[str, dict[str, int]] = defaultdict(lambda: {"passed": 0, "failed": 0, "total": 0})
    for v in verdicts:
        if v["status"] == "ERROR":
            continue
        for c in v["checks"]:
            if c["result"] == "NOT_APPLICABLE":
                continue
            t = tally[c["section"]]
            t["total"] += 1
            t["passed"] += c["result"] == "PASS"
            t["failed"] += c["result"] == "FAIL"
    out = {}
    for section in ("accuracy", "experience", "security"):
        t = tally.get(section, {"passed": 0, "failed": 0, "total": 0})
        verified = t["passed"] + t["failed"]
        if section == "security" and not t["total"]:
            continue
        out[section] = {
            "score": round(t["passed"] / verified, 3) if verified else None,
            "passed": t["passed"], "failed": t["failed"], "total": t["total"],
            "coverage": round(verified / t["total"], 3) if t["total"] else 0,
        }
    return out


class LocalDecisionEngine:
    def decide(self, run_id: str, verdicts: Sequence[Mapping[str, Any]], scenarios: Sequence[Mapping[str, Any]],
               profile: Mapping[str, Any]) -> dict[str, Any]:
        th = profile.get("thresholds", {})
        repeats = th.get("repeats", 3)
        min_cov = th.get("min_coverage", 0.9)
        reasons_bad: list[dict] = []
        reasons_evidence: list[dict] = []

        # Rule 1: blocking failures in verified calls.
        blockers: dict[str, list[str]] = defaultdict(list)
        for v in verdicts:
            if v["status"] not in VERIFIED:
                continue
            for c in v["checks"]:
                if c["result"] == "FAIL" and c["severity"] == "blocker" and c.get("can_block", True):
                    blockers[c["message"]].append(f"{v['test_id']}/{c['check_id']}")
        for message, refs in blockers.items():
            reasons_bad.append({"rule": "blocker", "message": f"{message} ({len(refs)} occurrence(s))",
                                "refs": refs})

        # Rule 2 and 3: reliability per scenario (pass^k).
        by_scenario: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for v in verdicts:
            by_scenario[v["scenario_id"]].append(v)
        reliability = []
        for sc in scenarios:
            vs = by_scenario.get(sc["id"], [])
            k = len(vs) or sc.get("repeats", repeats)
            need = min(th.get("pass_k_required", k), k)
            passed = sum(v["status"] == "PASS" for v in vs)
            failed = sum(v["status"] == "FAIL" for v in vs)
            unverified = len(vs) - passed - failed
            if failed:
                pass_hat_k = False
            elif passed >= need and passed == k:
                pass_hat_k = True
            else:
                pass_hat_k = None
            reliability.append({
                "scenario_id": sc["id"], "criticality": sc["criticality"], "k": k,
                "passed": passed, "failed": failed, "unverified": unverified,
                "pass_at_k": True if passed else (False if failed == k else None),
                "pass_hat_k": pass_hat_k,
            })
            if sc["criticality"] != "critical":
                continue
            refs = [v["test_id"] for v in vs if v["status"] == "FAIL"]
            if failed:
                reasons_bad.append({"rule": "reliability",
                                    "message": f"Critical scenario {sc['id']} passed {passed} of {k} repeats "
                                               f"({need} required).", "refs": refs})
            elif passed < need:
                reasons_evidence.append({"rule": "coverage",
                                         "message": f"Critical scenario {sc['id']} has only {passed} verified "
                                                    f"passing repeat(s) of {need} required.",
                                         "refs": [v["test_id"] for v in vs if v["status"] not in VERIFIED]})

        # Rule 3: coverage and open reviews.
        planned = len(verdicts)
        verified = sum(v["status"] in VERIFIED for v in verdicts)
        errors = sum(v["status"] == "ERROR" for v in verdicts)
        not_verified = sum(v["status"] == "NOT_VERIFIED" for v in verdicts)
        needs_review = sum(v["status"] == "NEEDS_REVIEW" for v in verdicts)
        ratio = round(verified / planned, 3) if planned else 0.0
        if ratio < min_cov:
            reasons_evidence.append({"rule": "coverage",
                                     "message": f"Only {verified} of {planned} calls ended with a verified result "
                                                f"({ratio:.0%}, {min_cov:.0%} required)."})
        if needs_review:
            reasons_evidence.append({"rule": "needs_review",
                                     "message": f"{needs_review} call(s) need a human review."})

        if reasons_bad:
            decision, reasons = "NO_GO", reasons_bad + reasons_evidence
        elif reasons_evidence:
            decision, reasons = "NO_GO_INSUFFICIENT_EVIDENCE", reasons_evidence
        else:
            decision = "GO"
            reasons = [{"rule": "all_targets_met",
                                      "message": f"All {planned} calls verified; every critical scenario passed "
                                                 f"all its repeats; no blocking failures."}]

        return {
            "schema_version": "0.1",
            "kind": "run_decision",
            "run_id": run_id,
            "profile_id": profile["id"],
            "created_at": utc_now(),
            "decision": decision,
            "exit_code": EXIT_CODES[decision],
            "reasons": reasons,
            "scorecard": _section_scores(verdicts),
            "reliability": reliability,
            "latency": {
                # Text mode: latency is never measured for the gate (Phase 0 review decision 3).
                "turns_measured": 0, "p50_ms": None, "p95_ms": None,
                "p50_limit_ms": th.get("latency_p50_ms", 800), "p95_limit_ms": th.get("latency_p95_ms", 1500),
                "within_limits": None,
            },
            "coverage": {
                "planned_calls": planned, "verified_calls": verified, "error_calls": errors,
                "not_verified_calls": not_verified, "needs_review_calls": needs_review,
                "ratio": ratio, "min_required": min_cov,
            },
            "calls": [{"test_id": v["test_id"], "scenario_id": v["scenario_id"], "repeat": v["repeat"],
                       "status": v["status"]} for v in verdicts],
        }
