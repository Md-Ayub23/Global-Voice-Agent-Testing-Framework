"""The outcome check: what the agent SAID vs what it DID (tool traffic) vs what was STORED (backend).

This is the check that catches an agent saying "you're booked" when nothing was booked.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from .checks import args_match, compare_rows, rows_of
from .contracts.models import CheckResult, EvidenceLevel, EvidenceRef, OutcomeClass, Result, TraceEvent

PASSING_CLASS = {
    "success": OutcomeClass.TRUE_SUCCESS,
    "honest_failure": OutcomeClass.HONEST_FAILURE,
    "no_action": OutcomeClass.NO_ACTION,
}

MESSAGES = {
    OutcomeClass.TRUE_SUCCESS: "The agent said the action succeeded, the write call succeeded and the record is correct.",
    OutcomeClass.HONEST_FAILURE: "The action failed and the agent told the caller so.",
    OutcomeClass.NO_ACTION: "Nothing was written and nothing was claimed.",
    OutcomeClass.FALSE_SUCCESS: "False success: the agent said the action succeeded, but no correct record exists.",
    OutcomeClass.FALSE_FAILURE: "False failure: the agent said the action failed, but the record exists.",
    OutcomeClass.SILENT_DATA_BUG: "Silent data bug: the write call succeeded, but the record is missing or wrong.",
    OutcomeClass.DUPLICATE_ACTION: "Duplicate action: the same write was made more than once.",
    OutcomeClass.UNEXPECTED_ACTION: "Unexpected action: something was written when nothing should have been.",
    OutcomeClass.NOT_VERIFIED: "The outcome could not be verified.",
}


NEGATION = re.compile(r"\b(?:not|never|n't|no longer)\s+(?:\w+\s+){0,2}$", re.I)


def _negated(text: str, start: int) -> bool:
    """True when a success word is negated just before it: "you are not booked yet", "isn't confirmed"."""
    return bool(NEGATION.search(text[max(0, start - 40):start].replace("n't", " n't")))


def detect_claim(agent_turns: list[TraceEvent], claim: Mapping[str, Any], after_seq: int | None,
                 second_listen: Mapping[int, str] | None = None) -> tuple[str, TraceEvent | None, str]:
    """success | failure | unclear | nothing, plus the agent turn it came from and the words that decided.

    Looks at agent turns after the last write call (or all of them if there was
    none), so a retry is judged on how it ended. The first turn matching either
    list decides; matching both is unclear.

    In voice, a turn may have been heard twice: by the caller's ears (heard_text) and by
    the careful second listen (stt_text events, `second_listen` = {agent_turn seq: text}).
    A plain mishearing ("your book with..." for "you're booked with...") must not make a
    clear answer unclear, so a turn counts as a clear claim when its hearings agree or only
    one of them is clear. If one hearing says success and the other failure, it is unclear.
    """
    success = [re.compile(p, re.I) for p in claim.get("success_patterns", [])]
    failure = [re.compile(p, re.I) for p in claim.get("failure_patterns", [])]

    def says_success(text: str) -> bool:
        return any(not _negated(text, m.start()) for p in success for m in p.finditer(text))

    second_listen = second_listen or {}
    turns = [e for e in agent_turns if after_seq is None or e.seq > after_seq]
    for e in turns:
        hearings = [e.data.get("heard_text", "")]
        if e.seq in second_listen:
            hearings.append(second_listen[e.seq])
        found: dict[str, str] = {}
        both = None
        for text in hearings:
            s, f = says_success(text), any(p.search(text) for p in failure)
            if s and f:
                both = both or text
            elif s:
                found.setdefault("success", text)
            elif f:
                found.setdefault("failure", text)
        if len(found) == 1:
            said, text = next(iter(found.items()))
            return said, e, text
        if found or both is not None:
            return "unclear", e, e.data.get("heard_text", "")
    if after_seq is not None:
        return "unclear", (turns[-1] if turns else None), (turns[-1].data.get("heard_text", "") if turns else "")
    return "nothing", None, ""


def evaluate_outcome(scenario: Mapping[str, Any], profile: Mapping[str, Any], events: list[TraceEvent],
                     facts: Mapping[str, Any], salt: str) -> tuple[dict[str, Any], CheckResult] | None:
    spec = scenario.get("outcome")
    if not spec:
        return None
    expected = spec["expected"]
    tool = spec["action"]["tool"]
    tool_def = next((t for t in profile.get("tools", []) if t["name"] == tool), {})

    requests = [e for e in events if e.type == "tool_request" and e.data.get("tool") == tool]
    responses = {e.data["call_id"]: e for e in events if e.type == "tool_response" and e.data.get("tool") == tool}
    pairs = [(r, responses.get(r.data["call_id"])) for r in requests]
    ok_pairs = [(r, s) for r, s in pairs if s is not None and s.data.get("ok")]
    did = {
        "write_calls": len(requests),
        "ok_calls": len(ok_pairs),
        "reached_backend": sum(1 for _, s in pairs if s is not None and s.data.get("reached_backend")),
    }

    # Duplicate by idempotency key: two successful writes with the same key values.
    duplicate_key = False
    key = tool_def.get("idempotency_key")
    if key:
        seen = set()
        for r, _ in ok_pairs:
            k = tuple(str(r.data.get("args", {}).get(name)) for name in key)
            duplicate_key |= k in seen
            seen.add(k)

    # What the agent said.
    agent_turns = [e for e in events if e.type == "agent_turn"]
    second = {e.data["agent_turn"]: e.data.get("text", "") for e in events if e.type == "stt_text"}
    said, claim_event, said_text = detect_claim(agent_turns, spec.get("claim", {}),
                                                requests[-1].seq if requests else None, second)

    # What was stored.
    stored, probe_event = "unknown", None
    record = spec.get("record")
    backend_access = profile.get("backend", {}).get("access", "none")
    if record and backend_access != "none":
        found = [e for e in events if e.type == "probe_result" and e.data.get("probe") == record["probe"]]
        if found and found[-1].data.get("ok"):
            probe_event = found[-1]
            rows = rows_of(probe_event)
            want_count = record.get("expect_count", 1 if expected == "success" else 0)
            if not rows:
                stored = "missing"
            elif want_count >= 1 and len(rows) > want_count:
                stored = "duplicate"
            else:
                fields_ok, _ = compare_rows(rows, None, record.get("expect_fields", {}), facts, salt)
                stored = "match" if fields_ok else "wrong_values"
    if stored == "match" and spec["action"].get("args_match"):
        if any(not args_match(r, spec["action"]["args_match"], facts, salt) for r, _ in ok_pairs):
            stored = "wrong_values"

    cls = _classify(expected, said, stored, did, duplicate_key)
    if cls == OutcomeClass.NOT_VERIFIED:
        result = Result.NOT_VERIFIED
    elif expected == "honest_failure" and did["write_calls"] == 0 and cls in (
            OutcomeClass.NO_ACTION, OutcomeClass.HONEST_FAILURE):
        # The fault was never reached, so saying "sorry, I couldn't" proves nothing.
        cls, result = OutcomeClass.NOT_VERIFIED, Result.NOT_VERIFIED
    else:
        result = Result.PASS if cls == PASSING_CLASS[expected] else Result.FAIL

    cited: list[tuple[TraceEvent, str]] = []
    for r, s in pairs:
        if s is not None:
            cited.append((s, f"{tool} returned {s.data.get('status')}" + ("" if s.data.get("ok") else " (failed)")))
    if claim_event is not None:
        cited.append((claim_event, f"agent claim: {said}"))
    if probe_event is not None:
        cited.append((probe_event, f"backend: {len(rows_of(probe_event))} record(s)"))
    evidence = tuple(EvidenceRef(e.seq, note) for e, note in sorted(cited, key=lambda x: x[0].seq))

    message = MESSAGES[cls]
    if cls == OutcomeClass.NOT_VERIFIED:
        if stored == "unknown":
            message = "The outcome could not be verified: no backend evidence (probe missing or failed)."
        elif said == "unclear":
            message = "The outcome could not be verified: the agent's final words were unclear about the result."
        elif did["write_calls"] == 0:
            message = "The outcome could not be verified: the agent never attempted the action under test."
    if result == Result.FAIL:
        message = f"Expected {PASSING_CLASS[expected].value}, got {cls.value}. {message}"
    level = EvidenceLevel.BACKEND if probe_event is not None else EvidenceLevel.TOOL_TRAFFIC
    if result in (Result.PASS, Result.FAIL) and not evidence:
        result, level = Result.NOT_VERIFIED, EvidenceLevel.NONE

    block = {
        "class": cls.value, "expected": expected, "said": said, "did": did, "stored": stored,
        "said_text": said_text,
        "said_heard_by": ("second listen" if claim_event is not None
                          and said_text != claim_event.data.get("heard_text", "") else "caller"),
        "result": result.value, "evidence": [{"event": r.event, "note": r.note} for r in evidence],
    }
    check = CheckResult(
        check_id="outcome", origin="outcome", result=result, severity="blocker", section="accuracy",
        message=message, evidence=evidence, evidence_level=level)
    return block, check


def _classify(expected: str, said: str, stored: str, did: Mapping[str, int], duplicate_key: bool) -> OutcomeClass:
    if stored == "unknown":
        return OutcomeClass.NOT_VERIFIED
    if duplicate_key or stored == "duplicate":
        return OutcomeClass.DUPLICATE_ACTION
    wrote = did["ok_calls"] > 0 or stored != "missing"
    if expected == "no_action":
        return OutcomeClass.UNEXPECTED_ACTION if wrote else OutcomeClass.NO_ACTION
    if said == "success":
        if stored == "match":
            return OutcomeClass.TRUE_SUCCESS
        if did["ok_calls"] > 0:
            return OutcomeClass.SILENT_DATA_BUG
        return OutcomeClass.FALSE_SUCCESS
    if said == "failure":
        return OutcomeClass.FALSE_FAILURE if stored == "match" else OutcomeClass.HONEST_FAILURE
    if said == "nothing":
        if did["ok_calls"] > 0 and stored in ("missing", "wrong_values"):
            return OutcomeClass.SILENT_DATA_BUG
        if not wrote:
            return OutcomeClass.NO_ACTION
    return OutcomeClass.NOT_VERIFIED  # unclear claim, or a write the agent never mentioned
