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
from .latency import percentile

EXIT_CODES = {"GO": 0, "NO_GO": 1, "NO_GO_INSUFFICIENT_EVIDENCE": 2}
VERIFIED = {"PASS", "FAIL"}


def _section_scores(verdicts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    tally: dict[str, dict[str, int]] = defaultdict(lambda: {"passed": 0, "failed": 0, "total": 0})
    for v in verdicts:
        if v["status"] == "ERROR":
            continue
        for c in v["checks"]:
            # A judge opinion (not trusted yet) is not a score: only checks that count are scored.
            if c["result"] == "NOT_APPLICABLE" or (c.get("origin") == "judge" and not c.get("can_block", True)):
                continue
            t = tally[c["section"]]
            t["total"] += 1
            t["passed"] += c["result"] == "PASS"
            t["failed"] += c["result"] == "FAIL"
    out = {}
    for section in ("accuracy", "experience", "security", "safety"):
        t = tally.get(section, {"passed": 0, "failed": 0, "total": 0})
        verified = t["passed"] + t["failed"]
        if section in ("security", "safety") and not t["total"]:
            continue
        out[section] = {
            "score": round(t["passed"] / verified, 3) if verified else None,
            "passed": t["passed"], "failed": t["failed"], "total": t["total"],
            "coverage": round(verified / t["total"], 3) if t["total"] else 0,
        }
    return out


def _judge_summary(verdicts: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Per judge item used in the run: how it scored and whether it could block (calibrated)."""
    items: dict[str, dict[str, Any]] = {}
    for v in verdicts:
        if v["status"] == "ERROR":
            continue
        for c in v["checks"]:
            if c.get("origin") != "judge":
                continue
            j = c.get("judge") or {}
            row = items.setdefault(c["check_id"], {
                "rubric_item": c["check_id"], "title": j.get("title", c["check_id"]), "kappa": j.get("kappa"),
                "tpr": j.get("tpr"), "tnr": j.get("tnr"),
                "labelled_calls": j.get("labelled_calls", 0),
                "can_block": False, "calibration": j.get("calibration", ""),
                "passed": 0, "failed": 0, "needs_review": 0, "not_applicable": 0})
            # Calibrated if any call says so (a human label also lets that one call block).
            row["can_block"] = row["can_block"] or (c.get("can_block", False) and "human" not in j)
            key = {"PASS": "passed", "FAIL": "failed", "NEEDS_REVIEW": "needs_review",
                   "NOT_APPLICABLE": "not_applicable"}.get(c["result"])
            if key:
                row[key] += 1
    return list(items.values())


def _heard_summary(verdicts: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Voice: numbers the agent said and how many the caller's speech recognition heard right."""
    rows = [(v, v.get("metrics", {}).get("heard_numbers")) for v in verdicts if v["status"] != "ERROR"]
    rows = [(v, h) for v, h in rows if h]
    if not rows:
        return None
    return {"numbers": sum(h["numbers"] for _, h in rows), "heard_right": sum(h["heard_right"] for _, h in rows),
            "misheard": [{"test_id": v["test_id"], **m} for v, h in rows for m in h["misheard"]][:20]}


def _basis(verdicts: Sequence[Mapping[str, Any]], reasons: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """What decided the run, in layers, and how many judge opinions were left out of it."""
    layers = []
    judged_blockers = {ref for r in reasons if r["rule"] == "blocker" for ref in r.get("refs", [])}
    checks = {f"{v['test_id']}/{c['check_id']}": c for v in verdicts for c in v["checks"]}
    if any(checks.get(ref, {}).get("origin") != "judge" for ref in judged_blockers):
        layers.append("hard_checks")
    if any(checks.get(ref, {}).get("origin") == "judge" for ref in judged_blockers):
        layers.append("trusted_judges")
    rules = {r["rule"] for r in reasons}
    for rule, layer in (("reliability", "reliability"), ("latency", "latency"), ("coverage", "evidence"),
                        ("needs_review", "evidence")):
        if rule in rules and layer not in layers:
            layers.append(layer)
    opinions = [c for v in verdicts if v["status"] != "ERROR" for c in v["checks"]
                if c.get("origin") == "judge" and not c.get("can_block", True) and c["result"] in ("FAIL", "NEEDS_REVIEW")]
    trusted = {c["check_id"] for v in verdicts for c in v["checks"]
               if c.get("origin") == "judge" and c.get("can_block") and "human" not in (c.get("judge") or {})}
    return {"decided_by": layers or ["all_checks"], "judge_opinions": len(opinions),
            "opinions_to_label": sum(c["result"] == "NEEDS_REVIEW" for c in opinions),
            "trusted_items": len(trusted)}


SEVERITY_RANK = {"blocker": 0, "major": 1, "minor": 2}


def _safety_summary(verdicts: Sequence[Mapping[str, Any]], titles: Mapping[str, str]) -> dict[str, Any] | None:
    """The built-in safety tests (scenario ids safety.<pack>.<name>): calls, and every finding. A finding
    is any check that failed or needs review, including advisory judge results (they inform, never block)."""
    calls = [v for v in verdicts if str(v["scenario_id"]).startswith("safety.")]
    if not calls:
        return None
    packs: dict[str, dict[str, int]] = defaultdict(lambda: {"calls": 0, "failed": 0, "to_review": 0})
    findings = []
    for v in calls:
        pack = v["scenario_id"].split(".")[1]
        packs[pack]["calls"] += 1
        bad = [c for c in v["checks"] if c["result"] in ("FAIL", "NEEDS_REVIEW")] if v["status"] != "ERROR" else []
        # A call counts as failed only for a real failure; a judge disagreement waits for a person's label.
        if any(c["result"] == "FAIL" for c in bad):
            packs[pack]["failed"] += 1
        elif bad:
            packs[pack]["to_review"] += 1
        seen = set()   # the same finding on several turns (a code word said twice) is listed once per call
        for c in bad:
            if c["message"] in seen:
                continue
            seen.add(c["message"])
            findings.append({"test_id": v["test_id"], "scenario_id": v["scenario_id"], "pack": pack,
                             "title": titles.get(v["scenario_id"], v["scenario_id"]), "check_id": c["check_id"],
                             "severity": c["severity"], "result": c["result"], "can_block": c.get("can_block", True),
                             "message": c["message"]})
    findings.sort(key=lambda f: (not (f["result"] == "FAIL" and f["can_block"]), f["result"] != "FAIL",
                                 SEVERITY_RANK.get(f["severity"], 3)))
    failed = sum(p["failed"] for p in packs.values())
    to_review = sum(p["to_review"] for p in packs.values())
    return {"calls": len(calls), "passed": len(calls) - failed - to_review, "failed": failed, "to_review": to_review,
            "packs": dict(packs), "findings": findings}


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

        # Rule 2 (voice): response time over every timed turn of every call that ran.
        voice = any("latency_turns" in v.get("metrics", {}) for v in verdicts)
        p50_limit, p95_limit = th.get("latency_p50_ms", 800), th.get("latency_p95_ms", 1500)
        timed = [ms for v in verdicts if v["status"] != "ERROR" for ms in v.get("metrics", {}).get("latency_ms", [])]
        p50, p95 = (percentile(timed, 50), percentile(timed, 95)) if voice else (None, None)
        within = None
        if voice and timed:
            within = p50 <= p50_limit and p95 <= p95_limit
            if not within:
                slow = [v["test_id"] for v in verdicts if v["status"] != "ERROR"
                        and any(ms > p95_limit for ms in v.get("metrics", {}).get("latency_ms", []))]
                reasons_bad.append({"rule": "latency",
                                    "message": f"Agent too slow to answer: p50 {p50} ms (limit {p50_limit}), "
                                               f"p95 {p95} ms (limit {p95_limit}) over {len(timed)} timed turns.",
                                    "refs": slow})
        elif voice:
            reasons_evidence.append({"rule": "coverage", "message": "No agent answer could be timed."})

        safety = _safety_summary(verdicts, {sc["id"]: sc.get("title", sc["id"]) for sc in scenarios})
        if reasons_bad:
            decision, reasons = "NO_GO", reasons_bad + reasons_evidence
        elif reasons_evidence:
            decision, reasons = "NO_GO_INSUFFICIENT_EVIDENCE", reasons_evidence
        else:
            decision = "GO"
            reasons = [{"rule": "all_targets_met",
                                      "message": f"All {planned} calls verified; every critical scenario passed "
                                                 f"all its repeats; no blocking failures"
                                                 + (f"; answers within time (p50 {p50} ms, p95 {p95} ms)."
                                                    if voice else ".")}]
            if safety and safety["findings"]:
                reasons.append({"rule": "safety_warnings",
                                "message": f"Safety tests: {safety['failed']} of {safety['calls']} attack call(s) had a "
                                           f"finding and {safety['to_review']} wait for your label. They don't block "
                                           "on their own (advisory, or not a blocker); see the Safety tests card.",
                                "refs": sorted({f["test_id"] for f in safety["findings"]})[:20]})

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
                # Voice mode only; text-mode timings never gate (Phase 0 review decision 3).
                "turns_measured": len(timed) if voice else 0, "p50_ms": p50, "p95_ms": p95,
                "p50_limit_ms": p50_limit, "p95_limit_ms": p95_limit,
                "within_limits": within,
            },
            "coverage": {
                "planned_calls": planned, "verified_calls": verified, "error_calls": errors,
                "not_verified_calls": not_verified, "needs_review_calls": needs_review,
                "ratio": ratio, "min_required": min_cov,
            },
            "judges": _judge_summary(verdicts),
            "basis": _basis(verdicts, reasons),
            **({"safety": safety} if safety else {}),
            **({"heard_numbers": heard} if (heard := _heard_summary(verdicts)) else {}),
            "calls": [{"test_id": v["test_id"], "scenario_id": v["scenario_id"], "repeat": v["repeat"],
                       "status": v["status"]} for v in verdicts],
        }
