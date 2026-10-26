"""Run orchestrator: plans calls, runs them, and retries harness errors only."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

from .models import Persona, Profile, Scenario


@dataclass(frozen=True)
class PlannedCall:
    scenario_id: str
    persona_id: str | None              # None for scripted scenarios
    repeat: int


class RunOrchestrator(Protocol):
    """Builds the plan (scenario x persona x repeat), gives each call a test id,
    runs calls within the profile limits, reruns a call only when it ended in a
    harness ERROR (up to max_harness_retries), then evaluates and decides.
    """

    def plan(
        self,
        profile: Profile,
        scenarios: Sequence[Scenario],
        personas: Sequence[Persona],
    ) -> Sequence[PlannedCall]: ...

    async def run(
        self,
        profile: Profile,
        scenarios: Sequence[Scenario],
        personas: Sequence[Persona],
    ) -> Mapping[str, Any]:
        """Returns the run decision (decision.json content)."""
        ...
