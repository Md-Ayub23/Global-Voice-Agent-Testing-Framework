"""What the agent costs to run, per call and per completed task, from the prices the owner enters
(profile `costs`). Vocto can't see the agent's bills, so it never guesses a price: with no prices set,
no cost is shown.

  per_call       a fixed amount for every call (a phone number's share, a platform fee)
  per_minute     every minute of a call (phone line, speech to text, text to speech, AI, all in)
  per_reply      every answer the agent gives (an AI request per answer, mostly for chat agents)
  per_tool_call  every request the agent makes to your systems

Cost per completed task: what all the calls that should have done a task (book, order) cost, divided
by how many really got it done. A call that failed still costs money, so an agent that needs two calls
to get one booking done costs twice as much per booking. Calls Vocto couldn't make (setup errors) are
left out: they never reached the agent.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

FIELDS = ("per_call", "per_minute", "per_reply", "per_tool_call")
TASK_EXPECTED = ("success", "recover_or_honest")


def prices(profile: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The owner's prices, or None when none are set."""
    raw = (profile or {}).get("costs") or {}
    p = {k: float(raw.get(k) or 0) for k in FIELDS}
    if not any(p.values()):
        return None
    return {"currency": str(raw.get("currency") or "USD").upper(), **p}


def call_cost(verdict: Mapping[str, Any], p: Mapping[str, Any]) -> float:
    m = verdict.get("metrics") or {}
    minutes = (m.get("duration_ms") or 0) / 60000
    return (p["per_call"] + p["per_minute"] * minutes + p["per_reply"] * (m.get("agent_turns") or 0)
            + p["per_tool_call"] * (m.get("tool_calls") or 0))


def _r(x: float | None) -> float | None:
    return None if x is None else round(x, 4)


def summary(verdicts: Sequence[Mapping[str, Any]], p: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """decision.costs: totals for the run, or None without prices or calls."""
    if not p:
        return None
    made = [v for v in verdicts if v.get("status") != "ERROR"]
    if not made:
        return None
    costs = [call_cost(v, p) for v in made]
    task = [(v, c) for v, c in zip(made, costs) if (v.get("outcome") or {}).get("expected") in TASK_EXPECTED]
    done = sum((v.get("outcome") or {}).get("class") == "TRUE_SUCCESS" for v, _ in task)
    task_cost = sum(c for _, c in task)
    minutes = sum((v.get("metrics") or {}).get("duration_ms") or 0 for v in made) / 60000
    return {"currency": p["currency"], "prices": {k: p[k] for k in FIELDS},
            "calls": len(made), "minutes": round(minutes, 2), "total": _r(sum(costs)),
            "per_call": _r(sum(costs) / len(made)),
            "task_calls": len(task), "tasks_done": done,
            "per_task": _r(task_cost / done) if done else None}


def money(x: float | None, currency: str) -> str:
    """$0.42, ₹3.50, 0.42 EUR: plain, with enough places for small amounts."""
    if x is None:
        return "-"
    places = 2 if abs(x) >= 1 else 3 if abs(x) >= 0.01 else 4
    sign = {"USD": "$", "INR": "₹", "EUR": "€", "GBP": "£"}.get(currency)
    return f"{sign}{x:,.{places}f}" if sign else f"{x:,.{places}f} {currency}"
