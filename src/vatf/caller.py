"""L3 caller simulators and the simulator validator (contracts/caller.py).

  LLMCaller      - Claude plays the persona with the scenario's goal and fact sheet
  ScriptedCaller - fixed caller lines (scripted scenarios)
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


def caller_system_prompt(scenario: Mapping[str, Any], persona: Mapping[str, Any],
                         agent_name: str) -> str:
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
directions, no quotation marks, no descriptions of tone."""


class LLMCaller:
    def __init__(self, scenario: Mapping[str, Any], persona: Mapping[str, Any], llm: Any, *,
                 model: str, agent_name: str, max_tokens: int = 200, metadata: Mapping[str, str] = ()):
        self._llm = llm
        self._model = model
        self._max_tokens = max_tokens
        self._system = caller_system_prompt(scenario, persona, agent_name)
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

    def __init__(self, scenario: Mapping[str, Any]):
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
            parts.append(str(f["date_of_birth"]))
        if parts:
            return CallerAction(say=", ".join(parts) + ".")
        if "which doctor" in heard and "doctor" in f:
            return CallerAction(say=f"{f['doctor']}, please.")
        if "which day" in heard or "which other day" in heard:
            return CallerAction(say=f"{f.get('preferred_day', 'next Tuesday')}, please.")
        times = re.findall(r"\b\d{1,2}(?::\d{2})?\s?(?:am|pm)\b", heard)
        if times and re.search(r"\b(which|what time|would you like|suit)\b", heard):
            return CallerAction(say=f"{times[0]} please.")
        self._confused += 1
        if self._confused >= 3:
            return CallerAction(say="Sorry, I'll call back later. Bye.", hang_up=True)
        return CallerAction(say="Sorry, could you say that again?")


VALIDATOR_SYSTEM = """You review a recorded phone call made by a SIMULATED CALLER (an AI playing a person).
Decide whether the caller stayed on script. The caller is off script if it:
- invented or changed personal details that contradict its fact sheet,
- pursued a different goal than the one it was given,
- broke character (talked like an AI, or played the agent's role),
- ignored one of its extra rules.
The agent's behaviour does not matter here: only judge the caller.
Answer with JSON only, no other text: {"on_script": true or false, "problems": ["short reason", ...]}"""


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
            messages=(LLMMessage("user", prompt),), max_tokens=400, metadata=self._metadata))
        m = re.search(r"\{.*\}", response.text, re.S)
        try:
            data = json.loads(m.group(0)) if m else None
            on_script = bool(data["on_script"])
            problems = [str(p) for p in data.get("problems", [])]
        except (ValueError, KeyError, TypeError) as e:
            raise ValueError(f"validator gave an unreadable answer: {response.text[:200]!r}") from e
        return ValidatorResult(on_script=on_script, problems=tuple(problems))
