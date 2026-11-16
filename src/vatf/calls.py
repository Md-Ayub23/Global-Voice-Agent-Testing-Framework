"""Outbound calls, voicemail and returning callers (scenario `direction`, `answered_by`, `voicemail`,
`returning`, `remember`).

Outbound: the agent calls the customer. Vocto tells the agent it is an outbound call (call metadata, see
connectors.call_metadata) and plays whoever picks up:
  - a person, who says "Hello?" (scenario `pickup`) and then talks like any test caller, or
  - a voicemail: a greeting, a beep, then silence while it records. A good agent waits for the beep,
    leaves a short message (who it is, why it called, how to call back, nothing private) and hangs up.

Returning callers: after the first call the same caller rings again, once per `returning` entry, with the
agent's systems as the earlier calls left them. Details the agent gave on an earlier call (a booking
reference) are carried into the next ones with `remember` patterns, so call 2 can say "I'm calling about
booking RC-1234".
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

import numpy as np

DEFAULT_PICKUP = "Hello?"
DEFAULT_GREETING = ("Hi, you've reached my voicemail. I can't take your call right now. "
                    "Please leave your name, number and a short message after the tone.")
DEFAULT_GAP_S = 2.0


def is_outbound(scenario: Mapping[str, Any]) -> bool:
    return scenario.get("direction") == "outbound"


def is_voicemail(scenario: Mapping[str, Any]) -> bool:
    return is_outbound(scenario) and scenario.get("answered_by") == "voicemail"


def voicemail_settings(scenario: Mapping[str, Any]) -> dict[str, Any]:
    v = scenario.get("voicemail") or {}
    return {"greeting": (v.get("greeting") or DEFAULT_GREETING).strip(), "beep": v.get("beep", True),
            "max_message_s": int(v.get("max_message_s") or 60)}


def pickup(scenario: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """(the words said when the call is answered, what the caller_turn event records about it)."""
    if is_voicemail(scenario):
        s = voicemail_settings(scenario)
        # Text runs: the beep is written out, as a transcript of a voicemail would show it.
        return s["greeting"] + (" [beep]" if s["beep"] else ""), {"voicemail": True, "pickup": True}
    return (scenario.get("pickup") or DEFAULT_PICKUP).strip(), {"pickup": True}


def beep(sr: int, seconds: float = 0.45, hz: float = 1000.0) -> np.ndarray:
    """The voicemail tone: a clean sine, faded in and out so it doesn't click."""
    n = int(sr * seconds)
    t = np.arange(n, dtype=np.float32) / sr
    x = 0.35 * np.sin(2 * np.pi * hz * t).astype(np.float32)
    ramp = min(n // 2, int(sr * 0.01))
    if ramp:
        x[:ramp] *= np.linspace(0, 1, ramp, dtype=np.float32)
        x[-ramp:] *= np.linspace(1, 0, ramp, dtype=np.float32)
    return x


# ---- returning callers ----------------------------------------------------------------------------

def legs(scenario: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The calls of one test: the scenario itself, then one per `returning` entry (each as a scenario of
    its own: same caller, persona and facts; a goal for an AI caller or scripted lines)."""
    first = dict(scenario)
    out = [first]
    caller = dict(scenario.get("caller") or {})
    for n, part in enumerate(scenario.get("returning") or [], 2):
        leg: dict[str, Any] = {k: v for k, v in scenario.items()
                               if k not in ("returning", "turns", "follow_up", "direction", "outbound", "answered_by",
                                            "pickup", "voicemail", "audio")}
        leg["title"] = part.get("title") or f"{scenario.get('title', scenario.get('id'))} (call {n})"
        leg["call_number"] = n
        c = dict(caller)
        if part.get("turns"):
            leg["style"] = "scripted"
            leg["turns"] = [{"caller": t["caller"], **({"expect": t["expect"]} if t.get("expect") else {})}
                            for t in part["turns"]]
            leg["follow_up"] = False
        else:
            leg["style"] = "simulated"
            c["goal"] = part.get("goal") or c.get("goal") or "follow up on your earlier call"
            for key in ("rules", "end_when", "max_turns"):
                if part.get(key) is not None:
                    c[key] = part[key]
                else:
                    c.pop(key, None) if key != "rules" else None
            rules = list(c.get("rules") or [])
            rules.append("You called this agent before, earlier today. This is a new call about that.")
            c["rules"] = rules
        leg["caller"] = c
        out.append(leg)
    return out


def gap_s(scenario: Mapping[str, Any], call_number: int) -> float:
    parts = scenario.get("returning") or []
    if 2 <= call_number <= len(parts) + 1:
        g = parts[call_number - 2].get("gap_s")
        if g is not None:
            return float(g)
    return DEFAULT_GAP_S


def _texts(events: Sequence[Any]) -> list[str]:
    import json

    out = []
    for e in events:
        t, d = (e.type, e.data) if hasattr(e, "type") else (e["type"], e["data"])
        if t == "agent_turn":
            out += [str(d.get("platform_transcript") or ""), str(d.get("heard_text") or "")]
        elif t == "tool_response":
            out.append(json.dumps(d.get("body"), ensure_ascii=False, default=str))
    return [x for x in out if x]


def remember(scenario: Mapping[str, Any], events: Sequence[Any]) -> dict[str, str]:
    """{name: value} for each `remember` pattern found in the calls so far (the latest match wins)."""
    found: dict[str, str] = {}
    texts = _texts(events)
    for name, pattern in (scenario.get("remember") or {}).items():
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error:
            continue
        for text in texts:
            for m in rx.finditer(text):
                found[name] = m.group(1) if rx.groups else m.group(0)
    return found


def _fill(text: str, values: Mapping[str, Any]) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), text)


def with_remembered(leg: Mapping[str, Any], remembered: Mapping[str, str]) -> dict[str, Any]:
    """The call with remembered details added to the caller's facts and filled into its lines and goal."""
    leg = dict(leg)
    caller = dict(leg.get("caller") or {})
    facts = {**(caller.get("facts") or {}), **remembered}
    caller["facts"] = facts
    if caller.get("goal"):
        caller["goal"] = _fill(re.sub(r"\$(\w+)", lambda m: "{" + m.group(1) + "}", caller["goal"]), facts)
    leg["caller"] = caller
    if leg.get("turns"):
        leg["turns"] = [{**t, "caller": _fill(t["caller"], facts)} for t in leg["turns"]]
    return leg


def call_scope(events: Sequence[Any], call: int) -> list[Any]:
    """The events of one call of a returning caller's chain (1 = the first call)."""
    starts = [0]
    for i, e in enumerate(events):
        t, d = (e.type, e.data) if hasattr(e, "type") else (e["type"], e["data"])
        if t == "harness_event" and d.get("code") == "next_call":
            starts.append(i)
    if call < 1 or call > len(starts):
        return []
    a = starts[call - 1]
    b = starts[call] if call < len(starts) else len(events)
    return list(events[a:b])


def first_turns(events: Sequence[Any]) -> dict[int, int]:
    """{call number: the turn number its first line has} for a returning caller's chain."""
    out = {1: 1}
    for e in events:
        t, d = (e.type, e.data) if hasattr(e, "type") else (e["type"], e["data"])
        if t == "harness_event" and d.get("code") == "next_call":
            out[int(d.get("call", 0))] = int(d.get("first_turn", 0))
    return out
