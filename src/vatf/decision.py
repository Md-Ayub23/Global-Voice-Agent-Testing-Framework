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
from . import stats
from .latency import percentile
from .costs import prices, summary as cost_summary
from .drills import by_failure
from .metrics import by_condition, run_quality

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
    for rule, layer in (("reliability", "reliability"), ("latency", "latency"), ("metric", "call_quality"),
                        ("coverage", "evidence"), ("latency_sample", "evidence"),
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


def _shown(q: Mapping[str, Any]) -> str:
    if q["value"] is None:
        return (q.get("detail") or {}).get("note", "no value")
    unit = {"ms": " ms", "%": "%", "wpm": " words/min", "turns": " turns", "s": " s"}[q["unit"]]
    v = q["value"]
    return f"{v:,.0f}{unit}" if q["unit"] in ("ms", "wpm") else f"{v:g}{unit}"


def _limit(q: Mapping[str, Any]) -> str:
    word = "at least" if q["better"] == "higher" else "at most"
    warn = q["warn"] if q["warn"] is not None else q["good"]
    return f"good is {word} {q['good']:g}, warn {word} {warn:g}"


def _metric_refs(verdicts: Sequence[Mapping[str, Any]], mid: str) -> list[str]:
    """The calls whose own value of the metric is bad (or warn when none is bad)."""
    rows = [(v["test_id"], ((v.get("metrics") or {}).get("quality") or {}).get(mid, {}).get("band")) for v in verdicts
            if v.get("status") != "ERROR"]
    bad = [t for t, b in rows if b == "bad"]
    return (bad or [t for t, b in rows if b == "warn"])[:20]


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
            presets = {(v.get("conditions") or {}).get("preset") for v in vs}
            if failed and len(presets) > 1:     # a Speech mode sweep: say which conditions it failed under
                under = sorted({(v.get("conditions") or {}).get("title") or (v.get("conditions") or {}).get("preset", "?")
                                for v in vs if v["status"] == "FAIL"})
                reasons_bad.append({"rule": "reliability",
                                    "message": f"Critical scenario {sc['id']} passed {passed} of {k} calls across "
                                               f"{len(presets)} caller conditions; it failed under: {', '.join(under)}.",
                                    "refs": refs})
            elif failed:
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
        p95_enough = stats.enough(95, len(timed))
        over = sum(ms > p95_limit for ms in timed)
        if voice and timed:
            slow = [v["test_id"] for v in verdicts if v["status"] != "ERROR"
                    and any(ms > p95_limit for ms in v.get("metrics", {}).get("latency_ms", []))]
            p50_ok, p95_ok = p50 <= p50_limit, p95 <= p95_limit
            # Too few answers for a real p95 (it would just be one of the slowest few): the slowest-5% rule
            # is read as "how many answers were over the limit". Clearly more than 5% over is too slow;
            # a few over can't be told from bad luck, so it is not enough evidence either way.
            clearly_slow = not p95_ok and (p95_enough or stats.wilson(over, len(timed))[0] > 0.05)
            if not p50_ok or clearly_slow:
                within = False
                reasons_bad.append({"rule": "latency",
                                    "message": f"Agent too slow to answer: p50 {p50} ms (limit {p50_limit}), "
                                               f"p95 {p95} ms (limit {p95_limit}) over {len(timed)} timed turns"
                                               + ("" if p95_enough or not clearly_slow else f"; {over} of {len(timed)} answers were over "
                                                  f"{p95_limit} ms, clearly more than the 5% allowed") + ".",
                                    "refs": slow})
            elif not p95_ok:
                reasons_evidence.append({"rule": "latency_sample",
                                         "message": f"Slowest answers: {over} of {len(timed)} timed answers took longer "
                                                    f"than {p95_limit} ms. With fewer than {stats.need_for(95)} answers "
                                                    "Vocto can't tell whether more than 5% of answers are that slow: "
                                                    "run more calls (more repeats or test cases).",
                                         "refs": slow})
            else:
                within = True
        elif voice:
            reasons_evidence.append({"rule": "coverage", "message": "No agent answer could be timed."})

        # Call quality (metrics.py): a metric the owner set to block, in its bad band, is a NO-GO.
        quality = run_quality(verdicts, profile)
        for q in quality:
            if q["blocks"] and q["band"] == "bad":
                reasons_bad.append({"rule": "metric", "message": f"{q['title']}: {_shown(q)} is in the bad band "
                                                                 f"({_limit(q)}).", "refs": _metric_refs(verdicts, q["id"])})
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
            weak = [q for q in quality if q["band"] in ("warn", "bad")]
            if weak:
                reasons.append({"rule": "metric_warnings",
                                "message": "Call quality: " + "; ".join(f"{q['title']} {_shown(q)} ({q['band']})"
                                                                        for q in weak)
                                           + ". These don't block (switch Blocks on in Pass rules to make them).",
                                "refs": sorted({r for q in weak for r in _metric_refs(verdicts, q["id"])})[:20]})

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
                "p95_enough": p95_enough if voice else None, "p95_needs": stats.need_for(95),
                "over_p95_limit": over if voice else 0,
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
            **({"quality": quality} if quality else {}),
            **({"by_condition": conds} if (conds := by_condition(verdicts)) else {}),
            **({"by_failure": fails} if (fails := by_failure(verdicts)) else {}),
            **({"costs": cs} if (cs := cost_summary(verdicts, prices(profile))) else {}),
            "calls": [{"test_id": v["test_id"], "scenario_id": v["scenario_id"], "repeat": v["repeat"],
                       "status": v["status"]} for v in verdicts],
        }


def _change_refs(decision: Mapping[str, Any], change: Mapping[str, Any]) -> list[str]:
    """The calls behind a test regression (its failed repeats in this run)."""
    if change["kind"] != "test":
        return []
    return [c["test_id"] for c in decision.get("calls", []) if c["scenario_id"] == change["id"] and c["status"] == "FAIL"]


def apply_regression(decision: dict[str, Any], comparison: Mapping[str, Any] | None) -> dict[str, Any]:
    """Phase 6: folds the comparison with the baseline (compare.py) into the decision.

    A change set to block (profile `regression`) turns GO or not-enough-evidence into NO_GO with rule
    `regression`; the other changes for the worse are listed as `regression_warnings`. The decision
    keeps a short `baseline` block; the full comparison is in comparison.json next to it.
    """
    if not comparison:
        return decision
    changes = comparison["changes"]
    blocking = [c for c in changes if c["blocks"]]
    warn = [c for c in changes if not c["blocks"] and c["direction"] in ("regression", "worse")]
    reasons = list(decision["reasons"])
    if blocking:
        new = [{"rule": "regression", "message": f"Worse than the baseline: {c['message']}",
                **({"refs": refs} if (refs := _change_refs(decision, c)) else {})} for c in blocking]
        if decision["decision"] == "NO_GO":
            later = ("coverage", "needs_review", "load_evidence", "latency_sample")
            reasons = [r for r in reasons if r["rule"] not in later] + new + [r for r in reasons if r["rule"] in later]
        else:
            reasons = new + [r for r in reasons if r["rule"] not in ("all_targets_met", "safety_warnings",
                                                                       "metric_warnings", "load_ok")]
        decision["decision"] = "NO_GO"
        decision["exit_code"] = EXIT_CODES["NO_GO"]
        layers = [x for x in decision["basis"]["decided_by"] if x != "all_checks"]
        decision["basis"]["decided_by"] = layers + ["regression"]
    if warn:
        why = ("the runs were made differently (not like-for-like), so this doesn't block"
               if not comparison["like_for_like"] else "not set to block in Pass rules > Regression gate")
        refs = sorted({r for c in warn for r in _change_refs(decision, c)})[:20]
        reasons.append({"rule": "regression_warnings",
                        "message": f"Worse than the baseline, {why}: " + " ".join(c["message"] for c in warn[:6])
                                   + (f" And {len(warn) - 6} more." if len(warn) > 6 else ""),
                        **({"refs": refs} if refs else {})})
    decision["reasons"] = reasons
    counts = comparison["counts"]
    decision["baseline"] = {
        "run_id": comparison["baseline"]["run_id"], "decision": comparison["baseline"]["decision"],
        "finished_at": comparison["baseline"]["finished_at"], "like_for_like": comparison["like_for_like"],
        "regressions": counts["regression"], "worse": counts["worse"], "better": counts["better"],
        "new": counts["new"], "removed": counts["removed"], "blocking": len(blocking),
    }
    return decision


def apply_load(decision: dict[str, Any], load: Mapping[str, Any]) -> dict[str, Any]:
    """Phase 7: a load run is decided by its load rules (loadstats.summarise), not by pass^k.

    The decision keeps every summary block of a normal run (scorecard, latency, call quality, calls),
    so Results, CI and alerts read it as usual; its decision and reasons come from the load:

      NO_GO         a trusted stage broke a limit (failed calls, answer time p95, a blocking metric), a
                    blocker check failed under load, or a top-vs-low rule set to block (slowdown, pass rate, drift)
      not enough    a stage could not be trusted (too few calls, Vocto's computer couldn't keep up), or
                    too many of Vocto's own errors; with unmatched tool calls, blocker checks can't be trusted
      GO            every stage met the limits; the warnings (slowdown, pass rate, drift, call quality) are listed
    """
    from .loadstats import HARNESS_SHARE

    bad: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    stages = load["stages"]
    for st in stages:
        where = f"{st['name']} ({st['level']} call{'s' if st['level'] != 1 else ''} at once)"
        if st["trusted"]:
            for p in st["problems"]:
                rule = {"failed_calls": "load_failed_calls", "latency": "load_latency"}.get(p["rule"], "load_metric")
                bad.append({"rule": rule, "message": f"{where}: {p['message']}",
                            **({"refs": st["failed_refs"]} if p["rule"] == "failed_calls" and st["failed_refs"] else {})})
        elif st["calls"] or st["seconds"]:
            evidence.append({"rule": "load_evidence", "message": f"{where} can't be judged: {st['untrusted_why']}."})
    blockers: dict[str, list[str]] = {}
    for st in stages:
        for check, refs in st["blockers"].items():
            blockers.setdefault(check, []).extend(refs)
    for check, refs in blockers.items():
        msg = (f"Under load the agent did something it must never do: blocker check {check} failed in "
               f"{len(refs)} call(s).")
        if load.get("unattributed_tool_calls"):
            evidence.append({"rule": "load_evidence", "message": msg + " It can't be trusted in this run: some tool "
                                                                         "calls couldn't be matched to their call.",
                             "refs": refs[:20]})
        else:
            bad.append({"rule": "load_blocker", "message": msg, "refs": refs[:20]})
    for b in load.get("blocking", []):
        bad.append({"rule": f"load_{b['rule']}", "message": b["message"]})
    if load.get("harness_share", 0) > HARNESS_SHARE:
        evidence.append({"rule": "load_evidence", "message":
                         f"{load['harness_errors']} of {load['calls']} calls ended in an error on Vocto's side "
                         f"(more than {HARNESS_SHARE:.0%}); see the calls marked ERROR."})
    if not load["calls"]:
        evidence.append({"rule": "load_evidence", "message": "No load call was made."})
    if bad:
        verdict, reasons = "NO_GO", bad + evidence
    elif evidence:
        verdict, reasons = "NO_GO_INSUFFICIENT_EVIDENCE", evidence
    else:
        verdict = "GO"
        p95 = max((st["latency"]["p95_ms"] or 0 for st in stages), default=0)
        limit = load.get("p95_limit_ms")
        reasons = [{"rule": "load_ok", "message":
                    f"Handled up to {load['capacity']} call{'s' if load['capacity'] != 1 else ''} at once: "
                    f"{load['calls']} calls, failed calls at most {load['limits']['max_failed_pct']:g}% in every stage"
                    + (f", answer time p95 at most {p95:,} ms (limit {limit:,.0f} ms)" if limit else
                       (f", answer time p95 at most {p95:,} ms" if p95 else "")) + "."}]
    if load.get("warnings") and verdict != "NO_GO":
        reasons.append({"rule": "load_warnings", "message": " ".join(w["message"] for w in load["warnings"][:6])
                        + " These don't block (switch Blocks on in the load test's limits to make them)."})
    elif load.get("warnings"):
        reasons.append({"rule": "load_warnings", "message": " ".join(w["message"] for w in load["warnings"][:6])})
    decision["decision"] = verdict
    decision["exit_code"] = EXIT_CODES[verdict]
    decision["reasons"] = reasons
    layers = ["load"] + (["evidence"] if evidence else [])
    decision["basis"] = {**decision.get("basis", {}), "decided_by": layers}
    decision["load"] = {k: load[k] for k in ("shape", "calls_at_once", "capacity", "breaking", "calls", "failed",
                                             "harness_errors", "p95_limit_ms", "unattributed_tool_calls")}
    decision["load"]["stages"] = [{k: st[k] for k in ("index", "name", "kind", "level", "calls", "finished", "failed",
                                                      "failed_pct", "pass_rate", "trusted", "meets_limits")}
                                  | {"p95_ms": st["latency"]["p95_ms"], "p50_ms": st["latency"]["p50_ms"]}
                                  for st in stages]
    return decision
