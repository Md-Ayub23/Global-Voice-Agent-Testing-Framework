"""Fixes from the 2026-11-17 audit: a run where Vocto couldn't complete any call says why first and never
reads as the agent failing; hand-off detection doesn't mistake moving money for putting a caller through."""

from vatf import handoff, readiness


def _decision():
    rel = [{"scenario_id": s, "criticality": "critical", "k": 3, "passed": 0, "failed": 0, "unverified": 3,
            "pass_at_k": None, "pass_hat_k": None} for s in ("a.book", "a.cancel")]
    return {"decision": "NO_GO_INSUFFICIENT_EVIDENCE", "reliability": rel,
            "reasons": [{"rule": "coverage", "message": "Critical scenario a.book has only 0 verified passing repeat(s) of 3 required."}],
            "coverage": {"planned_calls": 6, "verified_calls": 0, "error_calls": 6, "ratio": 0.0, "min_required": 0.9}}


def test_scorecard_does_not_call_unjudged_cases_failures():
    rows = {r["area"]: r for r in readiness._scorecard(_decision(), [], [], {}, False, set())}
    assert rows["task"]["status"] == "watch" and "could be judged" in rows["task"]["value"]
    assert "failed" not in rows["task"]["detail"] and not rows["task"].get("range")


def test_some_cases_judged_counts_only_those():
    d = _decision()
    d["reliability"][0].update(passed=3, unverified=0, pass_hat_k=True)
    rows = {r["area"]: r for r in readiness._scorecard(d, [], [], {}, False, set())}
    assert rows["task"]["value"] == "1 of 1 test case passed every repeat"
    assert rows["task"]["status"] == "watch" and "1 not judged" in rows["task"]["detail"]


def test_verdict_leads_with_why_no_call_finished():
    finding = {"kind": "error", "status": "error", "blocks": False, "title": "Some calls could not be completed",
               "messages": ["could not reach http://127.0.0.1:9102: Connection refused"]}
    v = readiness._verdict(_decision(), [finding], None, "functional")
    assert v["level"] == "not_enough_evidence"
    assert v["line"].startswith("Vocto couldn't complete any of the 6 calls")
    assert "could not reach http://127.0.0.1:9102" in v["line"] and "0 verified" not in v["line"]


def test_handoff_speech_needs_the_caller_to_be_put_through():
    said = ["I'll transfer you to a nurse now.", "I'm connecting you with billing.", "Let me connect you to a colleague.",
            "Please hold the line while I transfer your call.", "I'll put you through to reception."]
    not_said = ["I'll transfer $50 to your savings account.", "I'm connecting to our booking system now.",
                "I'll transfer the money tomorrow."]
    assert all(handoff.SAYS_TRANSFER.search(t) for t in said)
    assert not any(handoff.SAYS_TRANSFER.search(t) for t in not_said)


def test_money_transfer_tools_are_not_handoffs():
    assert all(handoff.tool_is_handoff(n) for n in ("transfer_to_human", "transferCall", "escalate_ticket", "live_agent", "transfer"))
    assert not any(handoff.tool_is_handoff(n) for n in ("transfer_funds", "wire_transfer", "transfer_money"))
