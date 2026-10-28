"""Response time in voice mode, read off the recorded audio.

For every agent turn that answers the caller: from the last sound of the
caller's words to the first sound of the agent's answer, as heard on the line
(agent_turn.data.latency_ms, written by the recorder). Some turns do not count
toward the release gate, and each says why:

  - the agent started before the caller had finished (it talked over the caller)
  - vatf made a tool slow or time out on purpose in that turn (fault injection)
  - the agent said nothing (no answer is a different problem, judged elsewhere)
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

DELAY_FAULTS = {"timeout", "slow"}


def _get(e: Any, key: str) -> Any:
    return e[key] if isinstance(e, Mapping) else getattr(e, key)


def turn_latencies(events: Iterable[Any]) -> list[dict[str, Any]]:
    """One row per agent turn that followed a caller turn: {seq, turn, ms, counted, note}."""
    events = list(events)
    delayed = {_get(e, "turn") for e in events if _get(e, "type") == "fault_injected"
               and _get(e, "data").get("fault", {}).get("type") in DELAY_FAULTS}
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
        rows.append({"seq": _get(e, "seq"), "turn": turn, "ms": ms, "counted": not note, "note": note})
    return rows


def percentile(values: list[int], p: float) -> int | None:
    """Nearest-rank percentile (p50 of [1,2,3,4] is 2; p95 of 20 values is the 19th)."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, -(-len(ordered) * p // 100))  # ceil
    return int(ordered[int(rank) - 1])
