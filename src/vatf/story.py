"""Plain-English view of a call: who said what, which tools ran, what the database held, and how it was judged.

Used by the terminal output, transcript.txt and report.html, so all three read the same way.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

EXPECTED = {
    "success": "the agent books it and says so (TRUE_SUCCESS)",
    "honest_failure": "the booking fails and the agent admits it (HONEST_FAILURE)",
    "no_action": "nothing is written (NO_ACTION)",
}
STORED = {
    "match": "a record for this call is in the database, with the right values",
    "missing": "no record for this call in the database",
    "wrong_values": "a record is in the database, but with wrong values",
    "duplicate": "more than one record for this call in the database",
    "unknown": "could not check the database",
}
MARK = {"PASS": "PASS", "FAIL": "FAIL", "ERROR": "ERROR", "NOT_VERIFIED": "UNSURE",
        "NEEDS_REVIEW": "REVIEW", "NOT_APPLICABLE": "skip"}


def load_events(call_dir: Path) -> list[dict[str, Any]]:
    path = call_dir / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _value(v: Any) -> str:
    if isinstance(v, list):
        return "[" + ", ".join(_value(x) for x in v) + "]" if v else "[] (empty)"
    if isinstance(v, dict):
        return ", ".join(f"{k}={_value(x)}" for k, x in v.items())
    return str(v)


def _short(text: str, limit: int = 220) -> str:
    return text if len(text) <= limit else text[:limit - 3] + "..."


def story_rows(events: Sequence[Mapping[str, Any]]) -> list[tuple[int, str, str]]:
    """(event number, who, what happened) for every event worth showing."""
    rows: list[tuple[int, str, str]] = []
    for e in events:
        d, t, n = e["data"], e["type"], e["seq"]
        if t == "agent_turn":
            rows.append((n, "AGENT", d.get("heard_text", "")))
        elif t == "caller_turn":
            rows.append((n, "CALLER", d.get("text", "")))
        elif t == "tool_request":
            rows.append((n, "AGENT uses tool", f"{d['tool']}({_value(d.get('args', {}))})"))
        elif t == "fault_injected":
            f = d.get("fault", {})
            extra = " (the request still reaches the backend)" if f.get("forward") else ""
            rows.append((n, "vatf breaks tool", f"{d['tool']}: injected {f.get('type')}{extra}"))
        elif t == "tool_response":
            state = "OK" if d.get("ok") else "FAILED"
            where = "" if d.get("reached_backend") else ", never reached the database"
            rows.append((n, "tool answer", _short(f"{d['tool']} -> {state} {d.get('status')}"
                                                  f"{where}: {_value(d.get('body', {}))}")))
        elif t == "call_ended":
            rows.append((n, "call ended", f"{d.get('reason', '')} (by {d.get('ended_by')})"))
        elif t == "validator_result":
            text = "the simulated caller stayed on script" if d.get("on_script") else (
                "the simulated caller went off script: " + "; ".join(d.get("problems", [])))
            rows.append((n, "validator", text))
        elif t == "probe_result":
            if not d.get("ok"):
                rows.append((n, "database check", f"{d.get('probe')}: failed ({d.get('error', 'unknown error')})"))
            else:
                found = d.get("rows")
                count = f"{len(found)} row(s) found" if found is not None else _value(d.get("value"))
                rows.append((n, "database check", f"{d.get('probe')}: {count}"))
        elif t == "harness_event":
            rows.append((n, "vatf error", f"{d.get('code')}: {d.get('message')} (not blamed on the agent)"))
    return rows


def database_rows(events: Sequence[Mapping[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    """What each probe found in the backend after the call."""
    return [(e["data"]["probe"], e["data"].get("rows") or [])
            for e in events if e["type"] == "probe_result" and e["data"].get("ok") and "rows" in e["data"]]


def judgement(verdict: Mapping[str, Any], events: Sequence[Mapping[str, Any]]) -> list[tuple[str, str]]:
    """(label, plain-English line) pairs: expected, said, did, stored, outcome."""
    o = verdict.get("outcome")
    if not o:
        return []
    by_seq = {e["seq"]: e for e in events}
    claim = next((r["event"] for r in o.get("evidence", []) if r["note"].startswith("agent claim")), None)
    said = o["said"]
    if claim in by_seq:
        said += f'  ("{_short(by_seq[claim]["data"].get("heard_text", ""), 120)}", event {claim})'
    tools = sorted({m.group(1) for r in o.get("evidence", []) for m in [re.match(r"(\w+) returned", r["note"])] if m})
    did = o["did"]
    action = " / ".join(tools) or "the write tool"
    did_text = (f"{action} called {did.get('write_calls', 0)} time(s), {did.get('ok_calls', 0)} succeeded"
                if did.get("write_calls") else "the agent never called the write tool")
    return [
        ("Expected", EXPECTED.get(o["expected"], o["expected"])),
        ("Said", said),
        ("Did", did_text),
        ("Stored", STORED.get(o["stored"], o["stored"])),
        ("Outcome", f"{o['class']}  -> {MARK.get(o['result'], o['result'])}"),
    ]


def call_story_text(verdict: Mapping[str, Any], events: Sequence[Mapping[str, Any]], note: str = "") -> str:
    out = [f"{'=' * 78}",
           f"CALL {verdict['test_id']}  |  {verdict['scenario_id']}  |  repeat {verdict['repeat']}"
           f"  |  RESULT: {verdict['status']}{note}",
           "=" * 78, "", "CONVERSATION AND ACTIONS"]
    for n, who, text in story_rows(events):
        out.append(f"  #{n:<3} {who:<16} {text}")
    for probe, rows in database_rows(events):
        out += ["", f"DATABASE AFTER THE CALL ({probe}, rows written by this call)"]
        out += [f"  - {_value(r)}" for r in rows] or ["  (no rows)"]
    lines = judgement(verdict, events)
    if lines:
        out += ["", "HOW IT WAS JUDGED (said vs did vs stored)"]
        out += [f"  {label:<9}: {text}" for label, text in lines]
    out += ["", "CHECKS"]
    for c in verdict["checks"]:
        refs = ", ".join(f"#{r['event']}" for r in c["evidence"])
        out.append(f"  [{MARK.get(c['result'], c['result']):<6}] {c['check_id']} ({c['severity']}): {c['message']}"
                   + (f"  [events {refs}]" if refs else ""))
    out += ["", f"CALL RESULT: {verdict['status']}", ""]
    return "\n".join(out)


def run_story_text(decision: Mapping[str, Any], calls: Sequence[tuple[Mapping[str, Any], list]],
                   errors: Sequence[tuple[Mapping[str, Any], list]] = ()) -> str:
    out = [f"vatf run {decision['run_id']}  ({decision['profile_id']}, text mode)",
           "",
           "Who is who: CALLER = the simulated customer (an AI or a script). AGENT = the voice agent under test.",
           "'AGENT uses tool' = the agent called one of its tools; vatf records it on the way through.",
           "'database check' = vatf looks in the backend after the call to see what was really stored.",
           ("Personal data is hidden as [masked:...]; the same value always gets the same mask within a call."
            if decision.get("_pii_masked", True) else
            "WARNING: personal data is NOT masked in this run (--show-pii). Do not share or commit it."),
           ""]
    out += [call_story_text(v, ev) for v, ev in calls]
    if errors:
        out += ["#" * 78, "HARNESS ERRORS (vatf's own failures, rerun automatically, never blamed on the agent)",
                "#" * 78, ""]
        out += [call_story_text(v, ev, note="  (harness error, rerun)") for v, ev in errors]
    out += ["=" * 78, f"DECISION: {decision['decision']}"]
    out += [f"  - {r['message']}" for r in decision["reasons"]]
    out.append("")
    return "\n".join(out)
