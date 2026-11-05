"""Scores a real call with the same yardsticks as a test call (Phase 8).

  1. Phase 5 call-quality metrics (metrics.py), banded with this agent's own good / warn values.
  2. Real-call checks, which need no test script:
       real.tool_failed     a tool the agent used answered with an error
       real.false_success   after a tool failed, the agent told the caller it was done (and no later try
                            of that tool worked). Read off the words, so a person confirms it: NEEDS_REVIEW.
       real.dropped         the call ended on a platform error, or the caller's last words got no answer
  3. The agent's AI judge items (rubrics.project_items), on a sample and within daily caps. Judge trust
     comes from the person's labels, exactly as for test calls: an untrusted item's FAIL is shown and
     flagged, but does not set the call's status.

A call is "flagged" when anything is worth a person's look: a failed or to-review check, any judge FAIL
or split, or a metric in its bad band. Handing the caller to a person is counted, not flagged.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .. import metrics as qm
from ..contracts.models import CheckResult, EvidenceLevel, EvidenceRef, Result
from ..evidence import utc_now
from ..judges import JUDGE_SYSTEM, JudgePanel
from ..latency import percentile, turn_latencies
from ..rubrics import RubricItem
from ..verdict import check_dict, status_of

REAL_SCENARIO = {"id": "real-call", "title": "Real call", "caller": {
    "goal": "Not known in advance: this is a real customer, not a test. Work out what they wanted from the call itself."}}
REAL_SYSTEM = (JUDGE_SYSTEM
               .replace("for a release test", "for quality monitoring of real calls")
               .replace("Judge the agent only. The caller is a simulated test caller.",
                        "Judge the agent only. The caller is a real customer on a real call (not a test).")
               .replace("- Values like [masked:ab12cd] are personal data hidden for privacy. Treat them as the real value.",
                        "- Values like [masked:ab12cd], [name], [phone] or [email] are personal data removed for privacy. "
                        "Treat them as the real value; never fail the agent because a value is hidden."))
SUCCESS = re.compile(r"\b(booked|confirmed|scheduled|rescheduled|all set|you'?re set|placed|cancell?ed|reserved|"
                     r"processed|completed|successfully|has been (?:sent|updated|made|changed)|i'?ve (?:sent|updated|"
                     r"made|changed|booked|added))\b", re.I)
HEDGE = re.compile(r"\b(couldn'?t|could not|can'?t|cannot|unable|not able|sorry|failed|problem|issue|error|"
                   r"unfortunately|wasn'?t|didn'?t|isn'?t|not been|try again)\b", re.I)
PLATFORM_ERROR = re.compile(r"error|fail|exceed|crash|timeout|timed[-_ ]?out|unavailable|pipeline|no[-_ ]?answer", re.I)
GOODBYE = re.compile(r"\b(bye|goodbye|thanks?|thank you|cheers|that'?s (?:all|it)|have a good|ok(?:ay)?\.?$)", re.I)


def _result(check_id: str, result: Result, severity: str, message: str, evidence: Sequence[int],
            level: EvidenceLevel = EvidenceLevel.PLATFORM, section: str = "accuracy") -> CheckResult:
    return CheckResult(check_id=check_id, origin="hard_check", result=result, severity=severity, section=section,
                       message=message, evidence=tuple(EvidenceRef(s) for s in evidence), evidence_level=level,
                       type=check_id)


def real_checks(events: Sequence[Mapping[str, Any]]) -> list[CheckResult]:
    out: list[CheckResult] = []
    responses = [e for e in events if e["type"] == "tool_response"]
    failed = [e for e in responses if not e["data"].get("ok")]
    first = next((e["seq"] for e in events if e["type"] == "call_started"), 1)
    if not responses:
        out.append(_result("real.tool_failed", Result.NOT_APPLICABLE, "major", "The agent used no tools in this call.",
                           [first], EvidenceLevel.TOOL_TRAFFIC))
    elif failed:
        # Flagged, but it doesn't fail the call: a broken backend is not the agent's doing. How the agent
        # handled it is (real.false_success, and the judges).
        names = sorted({e["data"]["tool"] for e in failed})
        r = _result("real.tool_failed", Result.FAIL, "major",
                    f"{len(failed)} tool answer(s) failed: {', '.join(names)}.",
                    [e["seq"] for e in failed], EvidenceLevel.TOOL_TRAFFIC)
        out.append(replace(r, can_block=False))
    else:
        out.append(_result("real.tool_failed", Result.PASS, "major",
                           f"All {len(responses)} tool answer(s) were ok.", [e["seq"] for e in responses],
                           EvidenceLevel.TOOL_TRAFFIC))

    # Possible false success: a failed tool, then "you're booked", and no later try of that tool worked.
    claims = []
    for f in failed:
        later_ok = any(r["seq"] > f["seq"] and r["data"]["tool"] == f["data"]["tool"] and r["data"].get("ok")
                       for r in responses)
        if later_ok:
            continue
        for e in events:
            if e["seq"] > f["seq"] and e["type"] == "agent_turn":
                text = e["data"].get("heard_text", "")
                if SUCCESS.search(text) and not HEDGE.search(text):
                    claims.append((f, e))
                    break
    if not failed:
        out.append(_result("real.false_success", Result.NOT_APPLICABLE, "blocker",
                           "No tool failed, so the agent could not claim a failed action worked.", [first],
                           EvidenceLevel.TOOL_TRAFFIC))
    elif claims:
        f, e = claims[0]
        out.append(_result("real.false_success", Result.NEEDS_REVIEW, "blocker",
                           f"Possible false success: {f['data']['tool']} failed (#{f['seq']}), then the agent said "
                           f"\"{_short(e['data'].get('heard_text', ''))}\" (#{e['seq']}). A person should check.",
                           [f["seq"], e["seq"]], EvidenceLevel.TOOL_TRAFFIC))
    else:
        out.append(_result("real.false_success", Result.PASS, "blocker",
                           "After the failed tool, the agent did not say it was done.", [f["seq"] for f in failed],
                           EvidenceLevel.TOOL_TRAFFIC))

    ended = next((e for e in reversed(events) if e["type"] == "call_ended"), None)
    turns = [e for e in events if e["type"] in ("caller_turn", "agent_turn")]
    reason = (ended or {}).get("data", {}).get("reason", "") if ended else ""
    if ended and PLATFORM_ERROR.search(reason) and not re.search(r"customer|caller|user", reason, re.I):
        out.append(_result("real.dropped", Result.FAIL, "major", f"The call ended on a platform problem: {reason}.",
                           [ended["seq"]]))
    elif turns and turns[-1]["type"] == "caller_turn" and not GOODBYE.search(turns[-1]["data"].get("text", "")) \
            and not any(e["type"] == "handoff" for e in events):
        out.append(_result("real.dropped", Result.FAIL, "minor",
                           f"The caller's last words got no answer: \"{_short(turns[-1]['data'].get('text', ''))}\".",
                           [turns[-1]["seq"]], EvidenceLevel.SPEECH, "experience"))
    elif ended:
        out.append(_result("real.dropped", Result.PASS, "major", "The call ended normally.", [ended["seq"]]))
    return out


def _short(text: str, n: int = 90) -> str:
    return text if len(text) <= n else text[: n - 3] + "..."


def call_metrics(events: Sequence[Mapping[str, Any]], mode: str, profile: Mapping[str, Any]) -> dict[str, Any]:
    caller = [e for e in events if e["type"] == "caller_turn"]
    ended = [e for e in events if e["type"] == "call_ended"]
    out: dict[str, Any] = {"turns": len(caller), "duration_ms": ended[-1]["t_ms"] if ended else 0}
    if mode == "voice":
        counted = [r["ms"] for r in turn_latencies(events) if r["counted"]]
        out.update(latency_ms=counted, latency_p50_ms=percentile(counted, 50), latency_p95_ms=percentile(counted, 95))
    out["quality"] = qm.with_bands(qm.call_quality(events, mode, {}), profile)
    return out


def flags_of(checks: Sequence[Mapping[str, Any]], quality: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for c in checks:
        if c["origin"] == "judge":
            if c["result"] == "FAIL":
                out.append({"kind": "judge", "id": c["check_id"], "section": c.get("section", ""),
                            "trusted": bool(c.get("can_block")),
                            "text": f"{(c.get('judge') or {}).get('title') or c['check_id']}: {c['message']}"})
            elif c["result"] == "NEEDS_REVIEW":
                out.append({"kind": "review", "id": c["check_id"], "section": c.get("section", ""),
                            "text": f"{(c.get('judge') or {}).get('title') or c['check_id']}: {c['message']}"})
        elif c["result"] in ("FAIL", "NEEDS_REVIEW"):
            out.append({"kind": "check", "id": c["check_id"], "section": c.get("section", ""), "text": c["message"],
                        "review": c["result"] == "NEEDS_REVIEW"})
    for mid, q in quality.items():
        if q.get("band") == "bad":
            m = qm.BY_ID[mid]
            v = q.get("value")
            shown = "yes" if v is True else ("no" if v is False else f"{v} {m.unit}".strip())
            out.append({"kind": "metric", "id": mid, "section": "experience", "text": f"{m.title}: {shown}"})
    return out


def build_verdict(call_id: str, record: Mapping[str, Any], events: Sequence[Mapping[str, Any]],
                  profile: Mapping[str, Any], judge_checks: Sequence[Mapping[str, Any]] = (),
                  judge_state: str = "skipped", judge_note: str = "") -> dict[str, Any]:
    mode = record.get("mode", "text")
    checks = [check_dict(r) for r in real_checks(events)] + list(judge_checks)
    m = call_metrics(events, mode, profile)
    flags = flags_of(checks, m["quality"])
    return {"schema_version": "0.1", "kind": "call_verdict", "real_call": True, "test_id": call_id,
            "run_id": "monitor", "scenario_id": REAL_SCENARIO["id"], "repeat": 1, "evaluated_at": utc_now(),
            "status": status_of(checks, False), "checks": checks, "metrics": m, "flags": flags,
            "flagged": bool(flags), "judges": {"state": judge_state, "note": judge_note}}


def index_row(call_id: str, record: Mapping[str, Any], verdict: Mapping[str, Any]) -> dict[str, Any]:
    """The few facts per call that lists, trends and alerts read (index.json)."""
    m = verdict["metrics"]
    judged = [c for c in verdict["checks"] if c["origin"] == "judge" and (c.get("judge") or {}).get("votes")]
    return {"call_id": call_id, "started_at": record.get("started_at"), "imported_at": record.get("imported_at"),
            "source": record.get("source"), "agent_version": record.get("agent_version"),
            "caller_hash": record.get("caller_hash"), "mode": record.get("mode"),
            "external_key": record.get("external_key"), "duration_s": round(m.get("duration_ms", 0) / 1000, 1),
            "turns": m.get("turns", 0), "status": verdict["status"], "flagged": verdict["flagged"],
            "flags": [{k: f[k] for k in ("kind", "id", "section", "trusted", "review") if k in f}
                      for f in verdict["flags"]],
            "flag_text": [f["text"] for f in verdict["flags"]][:4],
            "escalated": bool((m["quality"].get("escalated") or {}).get("value")),
            "latency_ms": m.get("latency_ms", []), "quality": m["quality"],
            "judge_state": verdict["judges"]["state"], "judge_cost_usd": verdict["judges"].get("cost_usd", 0.0),
            "judged_at": verdict["judges"].get("at"), "judge_fails": sum(c["result"] == "FAIL" for c in judged),
            "redacted": sum((record.get("redacted") or {}).values()),
            "summary": record.get("summary", "")}


# ---- judging ------------------------------------------------------------------------------------
def sampled(call_id: str, share_pct: float) -> bool:
    """The same call is always in or out of the sample (a hash of its id), so a re-score agrees."""
    if share_pct >= 100:
        return True
    if share_pct <= 0:
        return False
    return int(hashlib.sha256(call_id.encode()).hexdigest()[:8], 16) % 10000 < share_pct * 100


def judge_plan(call_id: str, verdict: Mapping[str, Any], rows: Sequence[Mapping[str, Any]],
               scoring: Mapping[str, Any], have_key: bool, today: str | None = None) -> tuple[str, str]:
    """('pending' | 'skipped', why) for one new call, from the agent's judging settings and today's use."""
    if not scoring.get("judges", True):
        return "skipped", "AI judges are switched off for real calls."
    if not have_key:
        return "skipped", "No Claude API key, so the AI judges did not run."
    flagged = verdict["flagged"] and scoring.get("always_judge_flagged", True)
    if not flagged and not sampled(call_id, float(scoring.get("judge_share_pct", 100))):
        return "skipped", f"Not in the {scoring.get('judge_share_pct')}% sample the judges score."
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    done = [r for r in rows if (r.get("judged_at") or "").startswith(today)]
    pending = [r for r in rows if r.get("judge_state") == "pending"]
    if len(done) + len(pending) >= int(scoring.get("judge_max_per_day", 50)):
        return "skipped", f"Today's limit of {scoring.get('judge_max_per_day')} judged calls was reached."
    if sum(r.get("judge_cost_usd") or 0 for r in done) >= float(scoring.get("judge_budget_usd_per_day", 5)):
        return "skipped", f"Today's judging budget of ${scoring.get('judge_budget_usd_per_day')} was spent."
    return "pending", "Waiting for the AI judges."


def run_judges(llm: Any, profile: Mapping[str, Any], events: Sequence[Mapping[str, Any]], mode: str,
               items: Sequence[RubricItem], calibration: Mapping[str, Mapping[str, Any]],
               metadata: Mapping[str, str] = ()) -> tuple[list[dict[str, Any]], list[str]]:
    panel = JudgePanel(llm, profile, calibration, metadata=metadata, system=REAL_SYSTEM)
    results, problems = asyncio.run(panel.judge(events, REAL_SCENARIO, items, mode))
    return [check_dict(r) for r in results], problems


def apply_labels(verdict: dict[str, Any], labels: Mapping[str, str]) -> dict[str, Any]:
    """A person's labels on the call's judge items replace the judges' results (and can block)."""
    for c in verdict["checks"]:
        if c["origin"] == "judge" and c["check_id"] in labels and labels[c["check_id"]]:
            c.update(result=labels[c["check_id"]], can_block=True,
                     message=f"Labelled by a person: {labels[c['check_id']].replace('_', ' ').lower()}.")
            (c.setdefault("judge", {}))["human"] = labels[c["check_id"]]
    verdict["status"] = status_of(verdict["checks"], False)
    verdict["flags"] = flags_of(verdict["checks"], verdict["metrics"]["quality"])
    verdict["flagged"] = bool(verdict["flags"])
    return verdict
