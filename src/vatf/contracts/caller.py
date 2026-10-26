"""L3 Caller simulator and its validator."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from .models import CallTrace, Scenario


@dataclass(frozen=True)
class CallerAction:
    """What the caller does next: say something, or hang up."""

    say: str | None
    hang_up: bool = False
    barge_in: bool = False


class CallerSimulator(Protocol):
    """An LLM playing the persona with the scenario's goal and fact sheet.

    It only ever receives heard_text (what our STT heard), never the agent's
    own transcript. Built per call from scenario + persona + LLMClient.
    """

    async def next_turn(self, heard_text: str) -> CallerAction: ...


@dataclass(frozen=True)
class ValidatorResult:
    on_script: bool
    problems: Sequence[str] = ()


class SimulatorValidator(Protocol):
    """Checks the simulated caller kept to its goal and facts.

    If not, the call is a harness ERROR and the orchestrator reruns it.
    """

    async def validate(self, trace: CallTrace, scenario: Scenario) -> ValidatorResult: ...
