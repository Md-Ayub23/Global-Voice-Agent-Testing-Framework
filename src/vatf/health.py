"""Health checks (Phase 6): a short run of a few tests, on a schedule, to catch an agent that broke
between releases (a provider outage, a changed prompt, a slow backend).

Profile `health_checks`:
    enabled: true
    every_minutes: 60        # the dashboard starts one this often while it is open
    scenarios: [ids]         # empty = the agent's critical tests (all tests when none is critical)
    repeats: 1
    conditions: as_written   # voice caller conditions
    judges: false            # AI judges cost money; health checks skip them unless switched on
    start_agent: false       # the dashboard starts the agent for each check (agent_process); off for a live agent

Runs the same path as any run (baseline comparison, alerts), marked trigger=health. Started by the
dashboard's scheduler (web/schedule.py) or by `python -m vatf health <project>` from cron, Windows
Task Scheduler or a scheduled CI job.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

DEFAULTS: dict[str, Any] = {"enabled": False, "every_minutes": 60, "scenarios": [], "repeats": 1,
                            "conditions": "as_written", "judges": False, "start_agent": False}
MIN_MINUTES = 5


def settings(profile: Mapping[str, Any]) -> dict[str, Any]:
    out = {**DEFAULTS, **(profile.get("health_checks") or {})}
    out["every_minutes"] = max(MIN_MINUTES, int(out["every_minutes"]))
    return out


def scenario_ids(profile: Mapping[str, Any], scenarios: Sequence[Mapping[str, Any]]) -> list[str]:
    """The tests a health check runs: the chosen ones that exist, else the critical ones plus fixed bugs kept as
    regression tests (tag `regression`), else all. `scenarios` are the tests that run in this profile's mode."""
    known = [sc["id"] for sc in scenarios]
    chosen = [s for s in settings(profile)["scenarios"] if s in known]
    if chosen:
        return chosen
    critical = [sc["id"] for sc in scenarios
                if sc.get("criticality") == "critical" or "regression" in (sc.get("tags") or [])]
    return critical or known
