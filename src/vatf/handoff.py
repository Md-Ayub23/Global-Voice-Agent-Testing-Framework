"""Did the agent hand the caller to a person (or another agent)? One answer for checks, metrics and real calls.

Strongest evidence first:
  platform   the platform says so: a "handoff" event (a multi-agent platform, or a real call's record), or the
             call ended for a transfer (Vapi "assistant-forwarded-call", Retell "call_transfer", ElevenLabs
             "transfer..."), or a platform's built-in transfer tool ran (transferCall, transfer_to_number,
             transfer_to_agent, transfer_call)
  tool       the agent called one of its own tools whose name says transfer / hand off / escalate / a person
             (transfer_to_human, escalate_ticket, live_agent...), and it did not fail
  speech     the agent said it is putting the caller through ("I'll transfer you", "connecting you to a
             colleague") in one of its last two turns: weaker, as saying it is not doing it

A callback booked for later is a way forward (drills.HANDOFF_TOOL counts it) but not a hand-off here.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

# The agent's own tools that hand over: by name.
HANDOFF_TOOL = re.compile(r"transfer|hand_?off|escalat|human|operator|live_?agent|forward_?call|connect_?to_?(?:agent|person|staff)", re.I)
# Built-in transfer tools of the platforms (their names in tool-call records).
PLATFORM_TRANSFER_TOOLS = {"transfercall", "transfer_call", "transfer_to_number", "transfer_to_agent",
                           "transfer_to_human", "warm_transfer", "cold_transfer"}
# Why a call ended, as platforms word a transfer.
TRANSFER_REASON = re.compile(r"transfer|forward(?:ed)?[-_ ]call|handed[-_ ]?off|hand[-_ ]?off", re.I)
SAYS_TRANSFER = re.compile(
    # "you" / "your call" must follow: "I'll transfer $50 to savings" or "I'm connecting to our booking
    # system" is not putting the caller through.
    r"\b(?:i(?:'ll| will| am|'m)? (?:now )?(?:(?:transfer(?:ring)?|connect(?:ing)?) (?:you|your call)\b"
    r"|put(?:ting)? you through|pass(?:ing)? you (?:over|on|to)\b)"
    r"|(?:transferring|connecting) you (?:now |over )?(?:to|with)"
    r"|let me (?:transfer|connect) (?:you|your call)\b"
    r"|putting you through"
    r"|hold (?:on|the line) while i (?:transfer|connect) (?:you|your call)\b)", re.I)


# A tool that moves money or data is not a hand-off, even when it is called transfer (a bank's transfer_funds).
NOT_HANDOFF_TOOL = re.compile(r"fund|money|payment|cash|balance|wire|amount|points|data|file", re.I)


def tool_is_handoff(name: str) -> bool:
    n = (name or "").strip()
    return n.lower() in PLATFORM_TRANSFER_TOOLS or (bool(HANDOFF_TOOL.search(n)) and not NOT_HANDOFF_TOOL.search(n))


def reason_is_transfer(reason: Any) -> bool:
    return bool(reason) and bool(TRANSFER_REASON.search(str(reason)))


def _get(e: Any, key: str) -> Any:
    return e.get(key) if isinstance(e, Mapping) else getattr(e, key, None)


def detect(events: Iterable[Any]) -> dict[str, Any]:
    """{"handed_off": bool, "how": "platform"|"tool"|"speech"|None, "seq": evidence seq, "detail": words}.
    Works on TraceEvents or their dicts."""
    ev = list(events)
    for e in ev:
        t, d = _get(e, "type"), _get(e, "data") or {}
        if t == "handoff":
            return _found("platform", e, f"the platform handed the call over ({d.get('to') or d.get('reason') or 'handoff'})")
    for e in ev:
        t, d = _get(e, "type"), _get(e, "data") or {}
        if t == "call_ended":
            reason = (d.get("platform") or {}).get("ended_reason") if isinstance(d.get("platform"), Mapping) else None
            for r in (reason, d.get("reason")):
                if reason_is_transfer(r):
                    return _found("platform", e, f"the call ended with a transfer ({r})")
    # A transfer tool that failed did not hand over; one with no answer at all did (a transfer often ends
    # the call before the tool can answer).
    failed = {(_get(e, "data") or {}).get("call_id") for e in ev
              if _get(e, "type") == "tool_response" and not (_get(e, "data") or {}).get("ok")}
    for e in ev:
        t, d = _get(e, "type"), _get(e, "data") or {}
        if t == "tool_request" and tool_is_handoff(str(d.get("tool") or "")) and d.get("call_id") not in failed:
            builtin = str(d.get("tool") or "").lower() in PLATFORM_TRANSFER_TOOLS
            return _found("platform" if builtin and d.get("reported_by") else "tool", e, f"it called {d.get('tool')}")
    agent = [e for e in ev if _get(e, "type") == "agent_turn" and (_get(e, "data") or {}).get("heard_text")]
    for e in agent[-2:]:
        text = str((_get(e, "data") or {}).get("heard_text") or "")
        m = SAYS_TRANSFER.search(text)
        if m:
            return _found("speech", e, f'it said "{m.group(0)}"')
    return {"handed_off": False, "how": None, "seq": None, "detail": "no transfer or hand-off"}


def _found(how: str, e: Any, detail: str) -> dict[str, Any]:
    return {"handed_off": True, "how": how, "seq": _get(e, "seq"), "detail": detail}
