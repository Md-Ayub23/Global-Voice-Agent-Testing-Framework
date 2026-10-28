"""Riverside Clinic booking agent: test agent #1 for the framework.

It is deliberately an ordinary agent, not framework code. The framework
reaches it through the text_python connector (profile: connection.target)
and only sees what it says and which tools it calls.

Two interchangeable brains:
  llm   - Claude with tool use (the realistic one; needs an API key)
  rules - a deterministic state machine (free, offline, used by the tests)

Bug switches (connection.options.bugs, or --agent-opt bugs=a,b):
  lie_on_failure     - a failed booking is reported to the brain as a success
  skip_confirmation  - books as soon as the caller picks a time, without asking
  double_booking     - sends every booking request twice
  misread_reference  - voice only (zoo/clinic/voice.py): the speech layer swaps two digits of
                       the booking reference when reading it out; text mode is unaffected
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

BUGS = {"lie_on_failure", "skip_confirmation", "double_booking", "misread_reference"}
GREETING = "Riverside Clinic, how can I help?"
GOODBYE = "Thanks for calling Riverside Clinic. Goodbye."


class ClinicTools:
    """The agent's own tool layer. Two of the bug switches live here, in code,
    so they behave the same whichever brain is used."""

    def __init__(self, gateway: Any, bugs: set[str]):
        self._gateway = gateway
        self._bugs = bugs

    def call(self, name: str, args: Mapping[str, Any]) -> dict:
        if name != "book_appointment":
            return self._gateway.call(name, **args)
        result = self._gateway.call(name, **args)
        if "double_booking" in self._bugs:
            # "Retry to be safe" bug: the request is always sent a second time.
            self._gateway.call(name, **args)
        if "lie_on_failure" in self._bugs and not result.get("ok"):
            # Swallowed error: the brain is told the booking went through.
            return {"ok": True, "status": 201, "body": {"appointment_id": "pending", "status": "booked"}}
        return result


def say_day(date_iso: str) -> str:
    d = datetime.fromisoformat(date_iso[:10])
    return f"{d:%A} {d.day} {d:%B}"


def say_time(slot_iso: str) -> str:
    t = datetime.fromisoformat(slot_iso)
    hour = t.hour % 12 or 12
    suffix = "am" if t.hour < 12 else "pm"
    return f"{hour}{suffix}" if t.minute == 0 else f"{hour}:{t.minute:02d}{suffix}"


class ClinicAgent:
    def __init__(self, brain: Any):
        self._brain = brain
        self.ended = False

    def start(self) -> str:
        return GREETING

    def reply(self, text: str) -> str:
        answer = self._brain.reply(text)
        if "goodbye" in answer.lower():
            self.ended = True
        return answer


def parse_bugs(options: Mapping[str, Any]) -> set[str]:
    bugs = options.get("bugs") or []
    if isinstance(bugs, str):
        bugs = [b for b in bugs.replace(" ", "").split(",") if b]
    unknown = set(bugs) - BUGS
    if unknown:
        raise ValueError(f"unknown clinic bug switch(es): {', '.join(sorted(unknown))}")
    return set(bugs)


def build_agent(*, tools: Any, session: Mapping[str, Any], options: Mapping[str, Any]) -> ClinicAgent:
    """Entry point named in the profile (connection.target)."""
    bugs = parse_bugs(options)
    clinic_tools = ClinicTools(tools, set(bugs))
    now = datetime.fromisoformat(session.get("now") or datetime.now().isoformat())
    confirm = "skip_confirmation" not in bugs

    brain_name = options.get("brain", "llm")
    if brain_name == "rules":
        from .brain_rules import RulesBrain

        brain = RulesBrain(clinic_tools, now, confirm_before_booking=confirm)
    elif brain_name == "llm":
        from .brain_llm import LLMBrain

        brain = LLMBrain(clinic_tools, now, confirm_before_booking=confirm,
                         model=options.get("model", "claude-haiku-5-5"))
    else:
        raise ValueError(f"unknown clinic brain {brain_name!r} (use llm or rules)")
    return ClinicAgent(brain)
