"""L3 caller simulators and the simulator validator (contracts/caller.py).

  LLMCaller      - Claude plays the persona with the scenario's goal and fact sheet
  ScriptedCaller - fixed caller lines (scripted scenarios)
  ScriptThenLLM  - the fixed lines first, then Claude carries on the call (scripted scenarios, live runs)
  RuleCaller     - offline stand-in that answers questions from the fact sheet by
                   keyword. For smoke tests and CI without an API key only.
  LLMValidator   - checks afterwards that the simulated caller kept to its script
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping, Sequence

from .contracts.caller import CallerAction, ValidatorResult
from .contracts.models import CallTrace, LLMMessage, LLMRequest

HANG_UP = "[HANG UP]"


VOICE_NOTE = """
This is a real phone call. You hear the agent through speech recognition, so its words may
contain small recognition mistakes; if something important is unclear, ask the agent to repeat it.
Say numbers and dates the way people say them out loud (for example "the 14th of March, 1985")."""


def caller_system_prompt(scenario: Mapping[str, Any], persona: Mapping[str, Any],
                         agent_name: str, voice: bool = False) -> str:
    c = scenario["caller"]
    traits = ", ".join(f"{k}: {v}" for k, v in persona.get("traits", {}).items())
    facts = "\n".join(f"- {k}: {v}" for k, v in c.get("facts", {}).items())
    rules = "\n".join(f"- {r}" for r in c.get("rules", [])) or "- none"
    now = scenario.get("setup", {}).get("now", "")
    return f"""You are role-playing a person phoning {agent_name}. You are the CALLER, never the agent.

Who you are: {persona.get('description', 'an ordinary caller')}
Personality ({traits}). Stay in character.

Your goal: {c['goal']}

Facts you know. Use exactly these values and never invent other personal details.
Share a fact when it is asked for or clearly needed:
{facts}

Extra rules:
{rules}

The current date and time is {now}.
End the call when: {c.get('end_when', 'your goal is reached or clearly cannot be reached')}
To end the call, say a short goodbye and then write {HANG_UP} at the very end.

Reply with only the words you say out loud: one or two short spoken sentences, no stage
directions, no quotation marks, no descriptions of tone.""" + (VOICE_NOTE if voice else "")


class LLMCaller:
    def __init__(self, scenario: Mapping[str, Any], persona: Mapping[str, Any], llm: Any, *,
                 model: str, agent_name: str, max_tokens: int = 2048, metadata: Mapping[str, str] = (),
                 voice: bool = False):
        self._llm = llm
        self._model = model
        self._max_tokens = max_tokens
        self._system = caller_system_prompt(scenario, persona, agent_name, voice)
        self._metadata = dict(metadata or {})
        self._history: list[LLMMessage] = []

    async def next_turn(self, heard_text: str) -> CallerAction:
        # The caller only ever sees heard_text: what reached its "ears".
        self._history.append(LLMMessage("user", heard_text.strip() or "(silence)"))
        response = await self._llm.complete(LLMRequest(
            purpose="caller", model=self._model, system=self._system,
            messages=tuple(self._history), max_tokens=self._max_tokens, metadata=self._metadata))
        text = response.text.strip()
        hang_up = HANG_UP.lower() in text.lower()
        said = re.sub(re.escape(HANG_UP), "", text, flags=re.I).strip().strip('"').strip()
        self._history.append(LLMMessage("assistant", said or "(hangs up)"))
        return CallerAction(say=said or None, hang_up=hang_up)

    async def redo(self, heard_text: str) -> CallerAction:
        """Voice: the agent went on talking before the caller answered. Answer what it said in all,
        instead of the answer to its first part."""
        heard_before = self._history[-2].content
        del self._history[-2:]
        if heard_before == "(silence)":
            heard_before = ""
        return await self.next_turn(f"{heard_before} {heard_text.strip()}".strip())

    def note_line(self, heard_text: str, said: str) -> None:
        """A fixed line the caller said (ScriptThenLLM), kept in the history like its own answer."""
        self._history.append(LLMMessage("user", heard_text.strip() or "(silence)"))
        self._history.append(LLMMessage("assistant", said))

    def extend_heard(self, heard_text: str) -> None:
        """The agent went on talking before the caller's last line: what it heard grows."""
        before = self._history[-2].content
        before = "" if before == "(silence)" else before
        self._history[-2] = LLMMessage("user", f"{before} {heard_text.strip()}".strip() or "(silence)")

    def last_said(self) -> str:
        return self._history[-1].content

    def note_barge_in(self, heard_before: str, said: str) -> None:
        """Voice: the scenario made the caller cut in with fixed words; keep the history true."""
        self._history.append(LLMMessage("user", (heard_before.strip() or "(silence)") + " ..."))
        self._history.append(LLMMessage("assistant", said))


FINISH_GOAL = ("Your fixed lines are done. Finish the call the way a real caller would: answer the agent's "
               "questions in line with what you already said, confirm only what you asked for, never add new "
               "requests, and hang up once the agent has finished with you (for example after it confirms or "
               "says goodbye).")


def follow_up_scenario(scenario: Mapping[str, Any]) -> dict[str, Any]:
    """The brief for the Claude caller that carries on a scripted call after its fixed lines."""
    c = dict(scenario.get("caller") or {})
    f = scenario.get("follow_up") or {}
    rules = list(c.get("rules", [])) + list(f.get("rules", []))
    rules.append("The first lines of this call were fixed, and you said them. Carry on from there; never repeat them.")
    return {**scenario, "caller": {**c, "goal": f.get("goal") or FINISH_GOAL, "facts": c.get("facts", {}),
                                   "rules": rules, "end_when": f.get("end_when") or c.get("end_when") or
                                   "the agent has finished with you, or you have reached your goal or clearly can't"}}


class ScriptThenLLM:
    """A scripted call's fixed lines, then Claude carries on for up to `turns` more caller turns,
    listening to what the agent really says. The fixed lines keep runs comparable; the rest is
    what a real caller would do next (push on a weak answer, finish the order, hear the read-back)."""

    def __init__(self, lines: Sequence[str], llm_caller: LLMCaller, turns: int):
        self._lines = list(lines)
        self._llm = llm_caller
        self._left = turns
        self.following_up = False

    async def next_turn(self, heard_text: str) -> CallerAction:
        if self._lines:
            line = self._lines.pop(0)
            self._llm.note_line(heard_text, line)      # so Claude knows the whole call when it takes over
            return CallerAction(say=line)
        if self._left <= 0:
            return CallerAction(say=None, hang_up=True)
        self.following_up = True
        self._left -= 1
        return await self._llm.next_turn(heard_text)

    async def redo(self, heard_text: str) -> CallerAction:
        if not self.following_up:                       # a fixed line stays as it is
            self._llm.extend_heard(heard_text)
            return CallerAction(say=self._llm.last_said())
        return await self._llm.redo(heard_text)

    def note_barge_in(self, heard_before: str, said: str) -> None:
        self._llm.note_barge_in(heard_before, said)


class ScriptedCaller:
    def __init__(self, lines: Sequence[str]):
        self._lines = list(lines)

    async def next_turn(self, heard_text: str) -> CallerAction:
        if not self._lines:
            return CallerAction(say=None, hang_up=True)
        return CallerAction(say=self._lines.pop(0))


class RuleCaller:
    """Answers the agent from the fact sheet using keywords. Not a real simulator:
    it exists so the pipeline can run end to end with no API key."""

    def __init__(self, scenario: Mapping[str, Any], spoken: bool = False):
        self._spoken = spoken  # voice: say dates as words ("14 March 1985"), not 1985-03-14
        c = scenario["caller"]
        self._goal = c["goal"]
        self._facts = c.get("facts", {})
        self._turns = 0
        self._confused = 0

    async def next_turn(self, heard_text: str) -> CallerAction:
        self._turns += 1
        heard = heard_text.lower()
        f = self._facts
        if self._turns == 1:
            return CallerAction(say=f"Hi, I'd like to {self._goal[0].lower()}{self._goal[1:]}")
        if "goodbye" in heard:
            return CallerAction(say=None, hang_up=True)
        if "anything else" in heard:
            return CallerAction(say="No, that's all. Thanks, bye.", hang_up=True)
        if re.search(r"\b(shall i|should i|would you like me to|do you want me to)\b", heard):
            return CallerAction(say="Yes please.")
        parts = []
        if "name" in heard and "full_name" in f:
            parts.append(str(f["full_name"]))
        if re.search(r"birth|\bdob\b", heard) and "date_of_birth" in f:
            parts.append(_spoken_date(str(f["date_of_birth"])) if self._spoken else str(f["date_of_birth"]))
        if parts:
            return CallerAction(say=", ".join(parts) + ".")
        if "which doctor" in heard and "doctor" in f:
            return CallerAction(say=f"{f['doctor']}, please.")
        if "which day" in heard or "which other day" in heard:
            return CallerAction(say=f"{f.get('preferred_day', 'next Tuesday')}, please.")
        # "9:30am", or as speech recognition may write it: "9:30 a.m.", "9.30 a m"
        times = re.findall(r"\b(\d{1,2})(?:[:.](\d{2}))?\s?([ap])\.?\s?m\b", heard)
        if times and re.search(r"\b(which|what time|would you like|suit)\b", heard):
            hour, minute, half = times[0]
            return CallerAction(say=f"{hour}{':' + minute if minute else ''}{half}m please.")
        self._confused += 1
        if self._confused >= 3:
            return CallerAction(say="Sorry, I'll call back later. Bye.", hang_up=True)
        return CallerAction(say="Sorry, could you say that again?")


def _spoken_date(iso: str) -> str:
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", iso.strip())
    if not m:
        return iso
    months = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
              "October", "November", "December"]
    return f"{int(m.group(3))} {months[int(m.group(2)) - 1]} {m.group(1)}"


VALIDATOR_SYSTEM = """You review a recorded phone call made by a SIMULATED CALLER (an AI playing a person).
Decide whether the caller stayed on script. The caller is off script only for a CLEAR violation:
- it stated a personal detail that contradicts its fact sheet, or invented one that matters,
- it pursued a different goal than the one it was given,
- it broke character (talked like an AI, or played the agent's role),
- it plainly broke one of its extra rules.
Not violations: answering a question the agent asked (including several details asked for together),
reasonable small talk, rephrasing, asking the agent to try again, or reacting to the agent's mistakes.
The agent's behaviour does not matter here: only judge the caller. If you are unsure, the caller is on script.
Decide first, then answer once with JSON only, no other text:
{"on_script": true or false, "problems": ["short reason", ...]}"""


class LLMValidator:
    def __init__(self, llm: Any, *, model: str, metadata: Mapping[str, str] = ()):
        self._llm = llm
        self._model = model
        self._metadata = dict(metadata or {})

    async def validate(self, trace: CallTrace, scenario: Mapping[str, Any]) -> ValidatorResult:
        c = scenario["caller"]
        lines = []
        for e in trace.events:
            if e.type == "caller_turn":
                lines.append(f"CALLER: {e.data['text']}")
            elif e.type == "agent_turn":
                lines.append(f"AGENT: {e.data['heard_text']}")
        brief = json.dumps({"goal": c["goal"], "facts": c.get("facts", {}), "rules": c.get("rules", [])},
                           ensure_ascii=False, indent=1)
        prompt = f"The caller's brief:\n{brief}\n\nThe call:\n" + "\n".join(lines)
        response = await self._llm.complete(LLMRequest(
            purpose="validator", model=self._model, system=VALIDATOR_SYSTEM,
            messages=(LLMMessage("user", prompt),), max_tokens=4096, metadata=self._metadata))
        data = _last_json_object(response.text)
        try:
            on_script = bool(data["on_script"])
            problems = [str(p) for p in data.get("problems", [])]
        except (ValueError, KeyError, TypeError) as e:
            raise ValueError(f"validator gave an unreadable answer: {response.text[:200]!r}") from e
        return ValidatorResult(on_script=on_script, problems=tuple(problems))


def _last_json_object(text: str) -> dict | None:
    """The model's final answer: it sometimes writes a JSON verdict, corrects itself, then writes another."""
    decoder, found, i = json.JSONDecoder(), None, text.find("{")
    while i != -1:
        try:
            obj, end = decoder.raw_decode(text, i)
        except ValueError:
            i = text.find("{", i + 1)
            continue
        if isinstance(obj, dict):
            found = obj
        i = text.find("{", end)
    return found
