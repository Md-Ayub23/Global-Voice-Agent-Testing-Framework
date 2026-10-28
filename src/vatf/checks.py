"""L7 hard checks: deterministic checks on the locked timeline.

Every PASS or FAIL cites the events it rests on. When the evidence is missing
the result is NOT_VERIFIED, never PASS.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from .contracts.models import CheckResult, EvidenceLevel, EvidenceRef, Result, TraceEvent
from .latency import percentile, turn_latencies
from .util import MASK_PREFIX, resolve, same_value

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
        # In voice an agent turn may have been heard twice (caller's ears, careful second listen).
        # "said" passes if either hearing has it; "not_said" fails only if every hearing has it,
        # so one mishearing never decides the check.
        pattern = re.compile(check["pattern"])
        second = {e.data["agent_turn"]: e.data.get("text", "") for e in all_events if e.type == "stt_text"}

        def hearings(e):
            return [e.data.get("heard_text", "")] + ([second[e.seq]] if e.seq in second else [])

        if kind == "said":
            hits = [(e, t) for e in agent_turns for t in hearings(e) if pattern.search(t)]
            if hits:
                quote = pattern.search(hits[0][1]).group(0)
                hits = [hits[0][0]]
                return _result(check, check_id, Result.PASS, f'The agent said "{quote}" (matches the expected pattern).',
                               _refs(hits[:1]), EvidenceLevel.SPEECH)
            return _result(check, check_id, Result.FAIL, f"The agent never said anything matching {check['pattern']}.",
                           _refs(agent_turns), EvidenceLevel.SPEECH)
        hits = [e for e in agent_turns if all(pattern.search(t) for t in hearings(e))]
        if hits:
            quote = pattern.search(hits[0].data.get("heard_text", "")).group(0)
            return _result(check, check_id, Result.FAIL, f'The agent said "{quote}", which it must not say.',
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
        all_rows = turn_latencies(events)
        rows = [r for r in all_rows if r["counted"]]
        if not rows:
            return _result(check, check_id, Result.NOT_VERIFIED, "No agent answer could be timed.", (),
                           EvidenceLevel.NONE)
        need = check.get("min_answers", 1)
        if len(rows) < need:
            skipped = "; ".join(f"turn {r['turn']}: {r['note']}" for r in all_rows if not r["counted"])
            return _result(check, check_id, Result.NOT_VERIFIED,
                           f"Only {len(rows)} answer(s) could be timed, {need} needed"
                           + (f" (not counted: {skipped})." if skipped else "."),
                           tuple(EvidenceRef(r["seq"], r["note"] or f"{r['ms']} ms") for r in all_rows),
                           EvidenceLevel.SPEECH)
        p95 = percentile([r["ms"] for r in rows], 95)
        limit = check["p95_ms"]
        cited = tuple(EvidenceRef(r["seq"], f"{r['ms']} ms") for r in rows)
        return _result(check, check_id, Result.PASS if p95 <= limit else Result.FAIL,
                       f"Slowest answers (p95) took {p95} ms over {len(rows)} timed turn(s); the limit is {limit} ms.",
                       cited, EvidenceLevel.SPEECH)

    if kind == "readback":
        return _readback(check, check_id, events, all_events)

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


DIGIT_WORDS = {"zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
               "seven": "7", "eight": "8", "nine": "9"}
# Words speech recognition often writes for a spoken digit; trusted only between two other digits.
SOUND_ALIKES = {"oh": "0", "o": "0", "won": "1", "to": "2", "too": "2", "for": "4", "ate": "8"}


def spoken_numbers(text: str) -> list[str]:
    """Digit runs as heard: "4 8 2, one nine three" and "482-193" both give "482193"."""
    tokens = re.findall(r"[a-z]+|\d+", text.lower())
    digits = [t if t.isdigit() else DIGIT_WORDS.get(t) for t in tokens]
    for i, t in enumerate(tokens):
        if digits[i] is None and t in SOUND_ALIKES and 0 < i < len(tokens) - 1 \
                and digits[i - 1] is not None and (digits[i + 1] is not None or tokens[i + 1] in DIGIT_WORDS):
            digits[i] = SOUND_ALIKES[t]
    runs, cur = [], ""
    for d in digits:
        if d is None:
            if cur:
                runs.append(cur)
            cur = ""
        else:
            cur += d
    return runs + ([cur] if cur else [])


def _readback(check, check_id, events, all_events) -> CheckResult:
    """Did the agent say the value its tool returned (e.g. a booking reference), exactly?

    The value comes from the tool's response as the proxy saw it; what the agent said comes
    from every agent turn after that response, in both listens (heard_text and, in voice
    mode, the careful second transcription in stt_text events). It passes if any of them
    holds the exact value, so one misheard transcript does not fail an agent that spoke correctly.
    """
    tool, field = check["tool"], check["field"]
    responses = [e for e in _of(all_events, "tool_response", tool)
                 if e.data.get("ok") and isinstance(e.data.get("body"), dict) and field in e.data["body"]]
    if not responses:
        return _result(check, check_id, Result.NOT_APPLICABLE,
                       f"{tool} never returned a {field}, so there was nothing to read back.", (),
                       EvidenceLevel.NONE)
    resp = responses[-1]
    want = str(resp.data["body"][field])
    if want.startswith(MASK_PREFIX):
        return _result(check, check_id, Result.NOT_VERIFIED, f"{field} is masked, so it cannot be compared.",
                       _refs([resp]), EvidenceLevel.NONE)
    want_key = re.sub(r"[^0-9a-z]", "", want.lower())
    turns = [e for e in _of(events, "agent_turn") if e.seq > resp.seq]
    second = {e.data.get("agent_turn"): e for e in _of(all_events, "stt_text")}
    heard: list[tuple[TraceEvent, str]] = []
    for t in turns:
        heard.append((t, t.data.get("heard_text", "")))
        if t.seq in second:
            heard.append((second[t.seq], second[t.seq].data.get("text", "")))

    problems, cited = [], [resp]
    if check.get("probe"):
        probes = [e for e in _of(all_events, "probe_result") if e.data.get("probe") == check["probe"] and e.data.get("ok")]
        column = check.get("column", field)
        stored = [str(r[column]) for e in probes[-1:] for r in rows_of(e) if column in r]
        if probes and stored and want not in stored:
            problems.append(f"the backend stored {', '.join(stored)} but {tool} told the agent {want}")
            cited.append(probes[-1])

    def found(text: str) -> bool:
        if want_key.isdigit():
            return any(want_key in run for run in spoken_numbers(text))
        return want_key in re.sub(r"[^0-9a-z]", "", text.lower())

    hits = [(e, t) for e, t in heard if found(t)]
    if hits and not problems:
        return _result(check, check_id, Result.PASS,
                       f'The agent read back {field} {want} correctly ("{_short(hits[0][1])}").',
                       _refs([resp, hits[0][0]]), EvidenceLevel.SPEECH)
    if not hits:
        if not turns:
            problems.append(f"the agent said nothing after {tool} returned {field} {want}")
        else:
            near = [run for _, t in heard for run in spoken_numbers(t) if abs(len(run) - len(want_key)) <= 1]
            problems.append(f"the agent never said {field} {want}" +
                            (f"; it was heard saying {', '.join(dict.fromkeys(near))}" if near else ""))
        cited += [e for e, _ in heard]
    message = "; ".join(problems)
    return _result(check, check_id, Result.FAIL, message[:1].upper() + message[1:] + ".",
                   _refs(sorted({e.seq: e for e in cited}.values(), key=lambda e: e.seq)), EvidenceLevel.SPEECH)


def _short(text: str, width: int = 90) -> str:
    return text if len(text) <= width else text[:width - 3] + "..."


def barge_in_checks(scenario: Mapping[str, Any], events: Events) -> list[CheckResult]:
    """One check per scenario audio.barge_in: did the agent stop talking when the caller cut in?"""
    plan = scenario.get("audio", {}).get("barge_in", [])
    agent_turns = _of(events, "agent_turn")
    out = []
    for i, b in enumerate(plan, start=1):
        check = {"severity": b.get("severity", "major"), "section": "experience", "type": "barge_in"}
        cid = "agent-stops-when-interrupted" + (f"-{i}" if len(plan) > 1 else "")
        limit = b.get("stop_within_ms", 1000)
        n = b["agent_turn"]
        if len(agent_turns) < n:
            out.append(_result(check, cid, Result.NOT_VERIFIED,
                               f"The call ended before the agent's turn {n}, so the caller never cut in.", (),
                               EvidenceLevel.NONE))
            continue
        turn = agent_turns[n - 1]
        if not turn.data.get("interrupted"):
            out.append(_result(check, cid, Result.NOT_VERIFIED,
                               f"The agent had finished turn {n} before the caller was due to cut in "
                               f"({b['offset_ms']} ms in), so barge-in was not tested.",
                               _refs([turn]), EvidenceLevel.NONE))
            continue
        took = turn.data.get("stopped_after_ms", 0)
        ok = took <= limit
        out.append(_result(check, cid, Result.PASS if ok else Result.FAIL,
                           f"The caller cut in {b['offset_ms']} ms into the agent's turn {n}; the agent "
                           f"{'stopped' if ok else 'kept talking and only stopped'} after {took} ms "
                           f"(limit {limit} ms).", _refs([turn]), EvidenceLevel.SPEECH))
    return out


def scenario_checks(scenario: Mapping[str, Any], events: Events, facts: Mapping[str, Any],
                    salt: str, mode: str) -> list[CheckResult]:
    """All hard checks of a scenario: call-level checks, then scripted turn checks (turn<n>.<i>)."""
    results = [evaluate_check(c, c["id"], events, events, facts, salt, mode) for c in scenario.get("checks", [])]
    for n, turn in enumerate(scenario.get("turns", []), start=1):
        # Scripted turn n is conversation turn n + 1 (turn 1 is the agent's greeting).
        scope = [e for e in events if e.turn == n + 1]
        for i, check in enumerate(turn.get("expect", []), start=1):
            results.append(evaluate_check(check, check.get("id", f"turn{n}.{i}"), scope, events, facts, salt, mode))
    if mode == "voice":
        results += barge_in_checks(scenario, events)
    crashes = [e for e in events if e.type == "harness_event" and e.data.get("code") == "agent_exception"]
    if crashes:
        results.append(CheckResult(
            check_id="agent-did-not-crash", origin="hard_check", result=Result.FAIL, severity="blocker",
            section="accuracy", message="The agent raised an error in the middle of the call.",
            evidence=_refs(crashes), evidence_level=EvidenceLevel.PLATFORM))
    return results
