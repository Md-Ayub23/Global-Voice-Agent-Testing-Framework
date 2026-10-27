"""L7 hard checks: deterministic checks on the locked timeline.

Every PASS or FAIL cites the events it rests on. When the evidence is missing
the result is NOT_VERIFIED, never PASS.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from .contracts.models import CheckResult, EvidenceLevel, EvidenceRef, Result, TraceEvent
from .util import resolve, same_value

Events = Sequence[TraceEvent]


def _refs(events: Events, note: str | None = None) -> tuple[EvidenceRef, ...]:
    return tuple(EvidenceRef(e.seq, note) for e in events)


def _of(events: Events, type_: str, tool: str | None = None) -> list[TraceEvent]:
    return [e for e in events if e.type == type_ and (tool is None or e.data.get("tool") == tool)]


def _result(check: Mapping[str, Any], check_id: str, result: Result, message: str,
            evidence: tuple[EvidenceRef, ...], level: EvidenceLevel, origin: str = "hard_check",
            **extra: Any) -> CheckResult:
    if result in (Result.PASS, Result.FAIL) and not evidence:
        result, message, level = Result.NOT_VERIFIED, message + " (no event to cite)", EvidenceLevel.NONE
    return CheckResult(
        check_id=check_id, origin=origin, result=result,
        severity=check.get("severity", "major"), section=check.get("section", "accuracy"),
        message=message, evidence=evidence, evidence_level=level, type=check.get("type"), **extra)


def args_match(req: TraceEvent, expected: Mapping[str, Any], facts: Mapping[str, Any], salt: str) -> bool:
    args = req.data.get("args", {})
    return all(k in args and same_value(args[k], resolve(v, facts), salt) for k, v in expected.items())


def rows_of(probe_event: TraceEvent) -> list[dict]:
    data = probe_event.data
    if data.get("rows") is not None:
        return list(data["rows"])
    if isinstance(data.get("value"), dict):
        return [data["value"]]
    return []


def compare_rows(rows: list[dict], expect_count: int | None, expect_fields: Mapping[str, Any],
                 facts: Mapping[str, Any], salt: str) -> tuple[bool, str]:
    if expect_count is not None and len(rows) != expect_count:
        return False, f"expected {expect_count} record(s), found {len(rows)}"
    for name, want in (expect_fields or {}).items():
        want = resolve(want, facts)
        bad = [r for r in rows if name not in r or not same_value(r[name], want, salt)]
        if bad:
            return False, f"field {name} is not {want!r} in {len(bad)} record(s)"
    return True, f"{len(rows)} record(s) as expected"


def evaluate_check(check: Mapping[str, Any], check_id: str, events: Events, all_events: Events,
                   facts: Mapping[str, Any], salt: str, mode: str) -> CheckResult:
    """events = the scope the check looks at (one scripted turn, or the whole call)."""
    kind = check["type"]
    agent_turns = _of(events, "agent_turn")
    if not events:
        return _result(check, check_id, Result.NOT_VERIFIED,
                       "The call ended before this point, so there is nothing to check.", (), EvidenceLevel.NONE)

    if kind in ("tool_called", "tool_not_called"):
        tool = check["tool"]
        reqs = _of(events, "tool_request", tool)
        if check.get("args_match"):
            reqs = [r for r in reqs if args_match(r, check["args_match"], facts, salt)]
        if kind == "tool_not_called":
            if reqs:
                return _result(check, check_id, Result.FAIL, f"{tool} was called but should not have been.",
                               _refs(reqs), EvidenceLevel.TOOL_TRAFFIC)
            return _result(check, check_id, Result.PASS, f"{tool} was not called.",
                           _refs(agent_turns[-1:], "the agent answered without calling it"),
                           EvidenceLevel.TOOL_TRAFFIC)
        times = check.get("times")
        ok = len(reqs) == times if times is not None else len(reqs) >= 1
        want = f"exactly {times} time(s)" if times is not None else "at least once"
        msg = f"{tool} was called {len(reqs)} time(s); expected {want}."
        return _result(check, check_id, Result.PASS if ok else Result.FAIL, msg,
                       _refs(reqs) or _refs(agent_turns[-1:]), EvidenceLevel.TOOL_TRAFFIC)

    if kind == "tool_order":
        first, then = check["first"], check["then"]
        then_reqs = _of(events, "tool_request", then)
        if not then_reqs:
            return _result(check, check_id, Result.NOT_APPLICABLE,
                           f"{then} was never called, so the order rule did not come up.", (), EvidenceLevel.NONE)
        before = [e for e in _of(events, "tool_request", first) if e.seq < then_reqs[0].seq]
        if not before:
            return _result(check, check_id, Result.FAIL, f"{then} was called before any {first}.",
                           _refs(then_reqs[:1]), EvidenceLevel.TOOL_TRAFFIC)
        return _result(check, check_id, Result.PASS, f"{first} was called before {then}.",
                       _refs([before[0], then_reqs[0]]), EvidenceLevel.TOOL_TRAFFIC)

    if kind == "probe":
        probe_events = [e for e in _of(all_events, "probe_result") if e.data.get("probe") == check["probe"]]
        if not probe_events or not probe_events[-1].data.get("ok"):
            return _result(check, check_id, Result.NOT_VERIFIED,
                           f"The backend probe {check['probe']} did not run or failed.",
                           _refs(probe_events[-1:]), EvidenceLevel.NONE)
        ok, msg = compare_rows(rows_of(probe_events[-1]), check.get("expect_count"),
                               check.get("expect_fields", {}), facts, salt)
        return _result(check, check_id, Result.PASS if ok else Result.FAIL, f"Backend: {msg}.",
                       _refs(probe_events[-1:]), EvidenceLevel.BACKEND)

    if kind in ("said", "not_said"):
        pattern = re.compile(check["pattern"])
        hits = [e for e in agent_turns if pattern.search(e.data.get("heard_text", ""))]
        if kind == "said":
            if hits:
                return _result(check, check_id, Result.PASS, f"The agent said something matching {check['pattern']}.",
                               _refs(hits[:1]), EvidenceLevel.SPEECH)
            return _result(check, check_id, Result.FAIL, f"The agent never said anything matching {check['pattern']}.",
                           _refs(agent_turns), EvidenceLevel.SPEECH)
        if hits:
            return _result(check, check_id, Result.FAIL, f"The agent said something matching {check['pattern']}.",
                           _refs(hits), EvidenceLevel.SPEECH)
        return _result(check, check_id, Result.PASS, f"The agent never said anything matching {check['pattern']}.",
                       _refs(agent_turns), EvidenceLevel.SPEECH)

    if kind == "max_turns":
        turns = len(_of(events, "caller_turn"))
        ended = _of(all_events, "call_ended")
        ok = turns <= check["n"]
        return _result(check, check_id, Result.PASS if ok else Result.FAIL,
                       f"The call took {turns} caller turn(s); the limit is {check['n']}.",
                       _refs(ended or agent_turns[-1:]), EvidenceLevel.SPEECH)

    if kind == "entity_fidelity":
        return _entity_fidelity(check, check_id, events, all_events, facts, salt)

    if kind == "handoff":
        hits = [e for e in _of(events, "handoff") if e.data.get("to") == check["to"]]
        ended = _of(all_events, "call_ended")
        return _result(check, check_id, Result.PASS if hits else Result.FAIL,
                       f"Handoff to {check['to']} {'happened' if hits else 'never happened'}.",
                       _refs(hits or ended), EvidenceLevel.PLATFORM)

    if kind == "latency":
        if mode != "voice":
            return _result(check, check_id, Result.NOT_APPLICABLE,
                           "Latency only gates in voice mode; text-mode timings are for information.",
                           (), EvidenceLevel.NONE)
        return _result(check, check_id, Result.NOT_VERIFIED, "Voice latency arrives in Phase 2.", (),
                       EvidenceLevel.NONE)

    if kind == "judge":
        return _result(check, check_id, Result.NOT_APPLICABLE,
                       "AI judges arrive in Phase 3; this item was skipped.", (), EvidenceLevel.AI_OPINION,
                       origin="judge", can_block=False,
                       judge={"rubric_item": check.get("rubric") or check.get("intent", ""),
                              "applicable": False, "votes": []})

    return _result(check, check_id, Result.NOT_VERIFIED, f"Unknown check type {kind!r}.", (), EvidenceLevel.NONE)


def _entity_fidelity(check, check_id, events, all_events, facts, salt) -> CheckResult:
    """Does each caller fact survive unchanged into the tool arguments and the backend record?

    Text mode compares two legs (tool args, backend). The speech leg (what the
    agent heard through STT) is added in voice mode, Phase 2.
    """
    problems, cited, missing_legs = [], [], []
    rows_events = [e for e in _of(all_events, "probe_result") if e.data.get("ok")]
    for name in check["entities"]:
        want = facts[name]
        arg_hits = [e for e in _of(events, "tool_request") if name in e.data.get("args", {})]
        row_hits = [(e, r) for e in rows_events for r in rows_of(e) if name in r]
        for e in arg_hits:
            if not same_value(e.data["args"][name], want, salt):
                problems.append(f"{name} in {e.data['tool']} arguments differs from what the caller said")
                cited.append(e)
        for e, r in row_hits:
            if not same_value(r[name], want, salt):
                problems.append(f"{name} in the backend record differs from what the caller said")
                cited.append(e)
        if not arg_hits:
            missing_legs.append(f"{name} (tool arguments)")
        if not row_hits:
            missing_legs.append(f"{name} (backend)")
        cited += arg_hits + [e for e, _ in row_hits]
    cited = sorted({e.seq: e for e in cited}.values(), key=lambda e: e.seq)
    level = EvidenceLevel.BACKEND if rows_events else EvidenceLevel.TOOL_TRAFFIC
    if problems:
        return _result(check, check_id, Result.FAIL, "; ".join(dict.fromkeys(problems)) + ".", _refs(cited), level)
    if missing_legs:
        return _result(check, check_id, Result.NOT_VERIFIED,
                       "Could not compare: no value seen for " + ", ".join(missing_legs) + ".",
                       _refs(cited), EvidenceLevel.NONE)
    return _result(check, check_id, Result.PASS,
                   "Every entity matches the caller's words in the tool arguments and the backend.",
                   _refs(cited), level)


def scenario_checks(scenario: Mapping[str, Any], events: Events, facts: Mapping[str, Any],
                    salt: str, mode: str) -> list[CheckResult]:
    """All hard checks of a scenario: call-level checks, then scripted turn checks (turn<n>.<i>)."""
    results = [evaluate_check(c, c["id"], events, events, facts, salt, mode) for c in scenario.get("checks", [])]
    for n, turn in enumerate(scenario.get("turns", []), start=1):
        # Scripted turn n is conversation turn n + 1 (turn 1 is the agent's greeting).
        scope = [e for e in events if e.turn == n + 1]
        for i, check in enumerate(turn.get("expect", []), start=1):
            results.append(evaluate_check(check, check.get("id", f"turn{n}.{i}"), scope, events, facts, salt, mode))
    crashes = [e for e in events if e.type == "harness_event" and e.data.get("code") == "agent_exception"]
    if crashes:
        results.append(CheckResult(
            check_id="agent-did-not-crash", origin="hard_check", result=Result.FAIL, severity="blocker",
            section="accuracy", message="The agent raised an error in the middle of the call.",
            evidence=_refs(crashes), evidence_level=EvidenceLevel.PLATFORM))
    return results
