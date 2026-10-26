"""L7 Evaluators and L8 decision and report."""

from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence

from .models import CallTrace, CheckResult, Profile, Scenario


class Evaluator(Protocol):
    """Turns locked evidence into check results.

    Evaluators run in this order: hard checks, outcome check, metrics,
    validator, then judges. Each one receives the results so far in `prior`.
    Every PASS or FAIL must cite at least one event.
    """

    name: str

    def evaluate(
        self,
        trace: CallTrace,
        scenario: Scenario,
        profile: Profile,
        prior: Sequence[CheckResult],
    ) -> Sequence[CheckResult]: ...


class VerdictBuilder(Protocol):
    """Combines all check results into one verdict.json and rolls up the call status.

    Enforces "AI can raise but never clear": a judge result can add a FAIL or
    NEEDS_REVIEW, but can never change a non-judge FAIL, ERROR or NOT_VERIFIED.
    """

    def build(self, trace: CallTrace, results: Sequence[CheckResult]) -> Mapping[str, Any]: ...


class DecisionEngine(Protocol):
    """Applies the rules in schemas/run_decision.schema.yaml to a run's verdicts."""

    def decide(
        self,
        run_id: str,
        verdicts: Sequence[Mapping[str, Any]],
        scenarios: Sequence[Scenario],
        profile: Profile,
    ) -> Mapping[str, Any]: ...


class ReportWriter(Protocol):
    """Static HTML report. Every number links back to the evidence behind it."""

    def write(
        self,
        decision: Mapping[str, Any],
        verdicts: Sequence[Mapping[str, Any]],
        out_dir: str,
    ) -> str: ...
