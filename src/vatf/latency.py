"""Response time in voice mode, read off the recorded audio.

For every agent turn that answers the caller: from the last sound of the
caller's words to the first sound of the agent's answer, as heard on the line
(agent_turn.data.latency_ms, written by the recorder). Some turns do not count
toward the release gate, and each says why:

  - the agent started before the caller had finished (it talked over the caller)
  - vatf made a tool slow or time out on purpose in that turn (fault injection)
  - the agent said nothing (no answer is a different problem, judged elsewhere)

Each row also says whether the answer needed a tool (the agent called one in that turn): a 2.5 s wait
after a database lookup is a different problem from a 2.5 s wait to "hello". For a tool answer,
full_wait_ms is the wait until the real answer: from the end of the caller's words to the agent's first
sound after the tool answered (a "let me check" first is a good habit, but the caller still waits for
the answer).

stage_times reads where an answer's time went (speech recognition, AI, voice) from the agent's own log
lines, when its logs say so (profile logs.timing); logs that don't are skipped.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

DELAY_FAULTS = {"timeout", "slow"}
STAGES = ("stt", "llm", "tts")
STAGE_WORDS = {"stt": "Speech recognition", "llm": "AI model", "tts": "Voice"}


def _get(e: Any, key: str) -> Any:
    return e[key] if isinstance(e, Mapping) else getattr(e, key)


def turn_latencies(events: Iterable[Any]) -> list[dict[str, Any]]:
    """One row per agent turn that followed a caller turn: {seq, turn, ms, counted, note}."""
    events = list(events)
    delayed = {_get(e, "turn") for e in events if _get(e, "type") == "fault_injected"
               and _get(e, "data").get("fault", {}).get("type") in DELAY_FAULTS}
    tools_in = {_get(e, "turn") for e in events if _get(e, "type") == "tool_request"}
    answered = {}                                   # turn -> t_ms of its last tool answer
    for e in events:
        if _get(e, "type") == "tool_response" and _get(e, "turn") is not None:
            answered[_get(e, "turn")] = _get(e, "t_ms")
    starts: dict[int, list[int]] = {}               # turn -> start of each stretch the agent spoke
    for e in events:
        a = _get(e, "data").get("audio") if _get(e, "type") == "agent_turn" else None
        if a and a.get("start_ms") is not None:
            starts.setdefault(_get(e, "turn"), []).append(int(a["start_ms"]))
    rows = []
    for e in events:
        if _get(e, "type") != "agent_turn":
            continue
        d, turn = _get(e, "data"), _get(e, "turn")
        if d.get("silent") and turn and turn > 1:
            rows.append({"seq": _get(e, "seq"), "turn": turn, "ms": None, "counted": False,
                         "note": "the agent said nothing"})
            continue
        if "latency_ms" not in d:
            continue  # the greeting: nobody spoke before it
        ms = d["latency_ms"]
        note = ""
        if ms < 0:
            note = "the agent started before the caller had finished"
        elif turn in delayed:
            note = "vatf slowed a tool on purpose in this turn"
        row = {"seq": _get(e, "seq"), "turn": turn, "ms": ms, "counted": not note, "note": note,
               "tool": turn in tools_in}
        if row["tool"] and ms >= 0 and (a := d.get("audio")) and turn in answered:
            caller_end = a["start_ms"] - ms
            after = [s for s in starts.get(turn, []) if s >= answered[turn]]
            row["full_wait_ms"] = (after[0] - caller_end) if after else None
        rows.append(row)
    return rows


def split_waits(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Counted answer times split into plain answers and answers that needed a tool."""
    plain = [r["ms"] for r in rows if r.get("counted") and not r.get("tool")]
    tool = [r for r in rows if r.get("counted") and r.get("tool")]
    full = [r["full_wait_ms"] for r in tool if r.get("full_wait_ms") is not None]
    t = [r["ms"] for r in tool]
    return {"plain": {"n": len(plain), "p50_ms": percentile(plain, 50), "p95_ms": percentile(plain, 95)},
            "tool": {"n": len(t), "p50_ms": percentile(t, 50), "p95_ms": percentile(t, 95),
                     "full_p50_ms": percentile(full, 50), "full_p95_ms": percentile(full, 95), "full_n": len(full)}}


def stage_times(events: Iterable[Any], profile: Mapping[str, Any]) -> dict[str, list[int]]:
    """Per stage (stt, llm, tts), the milliseconds the agent's own logs report for this call. A pattern
    (profile logs.timing.<stage>) must capture the number in a group named ms, e.g.
    "llm_latency=(?P<ms>\\d+)". Empty when the logs don't report it."""
    pats = {k: re.compile(v) for k, v in (((profile.get("logs") or {}).get("timing")) or {}).items() if k in STAGES and v}
    if not pats:
        return {}
    out: dict[str, list[int]] = {}
    for e in events:
        if _get(e, "type") != "platform_event" or _get(e, "data").get("kind") != "log":
            continue
        text = str(_get(e, "data").get("text", ""))
        for k, p in pats.items():
            for m in p.finditer(text):
                try:
                    out.setdefault(k, []).append(int(float(m.group("ms"))))
                except (IndexError, ValueError):
                    continue
    return out


def percentile(values: list[int], p: float) -> int | None:
    """Nearest-rank percentile (p50 of [1,2,3,4] is 2; p95 of 20 values is the 19th)."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, -(-len(ordered) * p // 100))  # ceil
    return int(ordered[int(rank) - 1])


def run_timing(verdicts: Iterable[Mapping[str, Any]], stop_limit_ms: int = 1000) -> dict[str, Any]:
    """The run's answer-time details beside the p50 / p95 rule (decision.latency): plain and tool answers
    apart, the likely range of the p95, the moments worth listening to, and where the time went (stages)."""
    from . import stats

    rows, waits, stops, stages = [], [], [], {}
    for v in verdicts:
        if v.get("status") == "ERROR":
            continue
        m = v.get("metrics") or {}
        for r in m.get("latency_turns", []):
            rows.append(r)
            if r.get("counted"):
                waits.append({"test_id": v["test_id"], "scenario_id": v.get("scenario_id"), "seq": r["seq"],
                              "ms": r["ms"], "tool": bool(r.get("tool")), "full_wait_ms": r.get("full_wait_ms")})
        q = (m.get("quality") or {}).get("barge_in_stop_ms") or {}
        for seq, ms in zip(q.get("seqs") or [], q.get("each") or []):
            if ms is not None and ms > stop_limit_ms:
                stops.append({"test_id": v["test_id"], "scenario_id": v.get("scenario_id"), "seq": seq, "ms": ms})
        for k, xs in (m.get("stages") or {}).items():
            stages.setdefault(k, []).extend(xs)
    timed = [r["ms"] for r in rows if r.get("counted")]
    low, high = stats.percentile_range(timed, 95)
    return {
        "split": split_waits(rows),
        "p95_range": {"low_ms": low, "high_ms": high},
        "slowest": sorted(waits, key=lambda w: -w["ms"])[:5],
        "slow_stops": sorted(stops, key=lambda s: -s["ms"])[:5],
        "stages": {k: {"title": STAGE_WORDS[k], "n": len(xs), "p50_ms": percentile(xs, 50), "p95_ms": percentile(xs, 95)}
                   for k, xs in stages.items() if xs},
    }
