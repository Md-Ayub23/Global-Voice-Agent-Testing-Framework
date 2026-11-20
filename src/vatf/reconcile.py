"""A hosted platform's record of a call, put into the call's evidence (connectors/platforms).

After a platform call ends, Vocto reads the platform's own record of it (base.PlatformReport). Its tool calls
become ordinary tool_request / tool_response events, with source "platform" and data.reported_by naming the
platform, placed where they happened (writer.insert) and in the caller turn they belong to. So the checks
(tool_called, tool_order, outcome, read-back...), the AI judges and the reports treat them like the tool calls
Vocto's own proxy sees. A transfer the platform reports becomes a "handoff" event.

Where a tool call happened, best first:
  1. the time the platform announced it during the call (session.live_tools), on Vocto's own clock;
  2. its time in the platform's record, moved onto Vocto's clock: the platform's call starts when Vocto's
     session opened (session.t0_ms), corrected by lining up the agent's first words in the record with the
     first agent speech in Vocto's recording (both clocks hear the same greeting);
  3. no time at all: just before the call ended.

Not added twice: a tool call Vocto answered itself (ElevenLabs client tools through the tool proxy:
session.handled_tools), or one the tool gateway already saw (same tool, same arguments).
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping, Sequence

MAX_ALIGN_SHIFT_MS = 10_000     # a lined-up greeting further than this from t0 is a mismatch, not a clock offset


def _norm(args: Any) -> str:
    try:
        return json.dumps(args or {}, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(args)


def clock_offset(call: Mapping[str, Any], events: Sequence[Any], t0_ms: int) -> int:
    """Platform ms -> Vocto ms: add this."""
    if call.get("clock") == "session":
        return t0_ms
    first_theirs = next((i.get("start_ms") for i in call.get("items") or []
                         if i.get("kind") == "turn" and i.get("role") == "agent" and i.get("start_ms") is not None), None)
    first_ours = next((e.data["audio"]["start_ms"] for e in events if e.type == "agent_turn"
                       and isinstance(e.data.get("audio"), Mapping) and e.data["audio"].get("start_ms", -1) >= t0_ms), None)
    if first_theirs is not None and first_ours is not None:
        offset = int(first_ours) - int(first_theirs)
        if abs(offset - t0_ms) <= MAX_ALIGN_SHIFT_MS:
            return offset
    return t0_ms


def turn_at(events: Sequence[Any], t_ms: int) -> int:
    """The caller turn a moment belongs to: the last caller line that started at or before it (else the first turn)."""
    turns = [e.turn for e in events if e.type == "caller_turn" and e.turn is not None and e.t_ms <= t_ms]
    if turns:
        return max(turns)
    known = [e.turn for e in events if e.turn is not None]
    return min(known) if known else 1


def platform_events(report: Any, session: Any, events: Sequence[Any]) -> list[tuple[int, str, str, dict, int | None]]:
    """The events to insert for one platform report: (t_ms, source, type, data, turn)."""
    call = getattr(report, "call", None) or {}
    items = [i for i in call.get("items") or [] if i.get("kind") == "tool"]
    platform = getattr(report, "platform", "platform")
    t0 = int(getattr(session, "t0_ms", 0) or 0)
    offset = clock_offset(call, events, t0)
    live = dict(getattr(session, "live_tools", {}) or {})
    handled = set(getattr(session, "handled_tools", set()) or set())
    ended = [e.t_ms for e in events if e.type == "call_ended"]
    end_t = (ended[-1] - 1) if ended else (events[-1].t_ms if events else t0)
    # The tool gateway's own record of the same calls, so each is counted once.
    seen = [(e.data.get("tool"), _norm(e.data.get("args"))) for e in events
            if e.type == "tool_request" and e.source != "platform"]
    out: list[tuple[int, str, str, dict, int | None]] = []
    for n, item in enumerate(items, start=1):
        tid = str(item.get("id") or "")
        if tid and tid in handled:
            continue
        key = (item.get("name"), _norm(item.get("args")))
        if key in seen:
            seen.remove(key)
            continue
        if tid and tid in live:
            t = live[tid]
        elif item.get("at_ms") is not None:
            t = int(item["at_ms"]) + offset
        else:
            t = end_t
        t = max(t0, min(int(t), end_t))
        turn = turn_at(events, t)
        cid = f"{platform}-{tid or n}"
        common = {"reported_by": platform}
        if item.get("tool_type"):
            common["tool_type"] = item["tool_type"]
        out.append((t, "platform", "tool_request", {"call_id": cid, "tool": item.get("name") or "tool",
                                                    "args": dict(item.get("args") or {}), **common}, turn))
        result = item.get("result")
        out.append((t, "platform", "tool_response", {
            "call_id": cid, "tool": item.get("name") or "tool", "ok": bool(item.get("ok", True)),
            "body": result if isinstance(result, dict) or result is None else {"result": result},
            "duration_ms": 0, "reached_backend": True, **common}, turn))
    if call.get("transferred"):
        out.append((end_t, "platform", "handoff",
                    {"to": "a person", "reason": call.get("ended_reason") or "transferred", "reported_by": platform},
                    turn_at(events, end_t)))
    return out


def apply(writer: Any, reports: Iterable[tuple[Any, Any]]) -> dict[str, Any]:
    """Inserts every report's events into the call; returns {"added": n, "errors": [...]} for the timeline."""
    added, errors = 0, []
    for report, session in reports:
        if getattr(report, "error", None):
            errors.append(f"{report.platform}: {report.error}")
        if getattr(report, "call", None) is None:
            continue
        items = platform_events(report, session, list(writer.events))
        if items:
            added += len(writer.insert(items))
    return {"added": added, "errors": errors}
