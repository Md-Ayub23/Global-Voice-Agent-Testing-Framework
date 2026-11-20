"""What the agent costs to run, per call and per completed task, from the prices the owner enters
(profile `costs`). Vocto can't see the agent's bills, so it never guesses a price: with no prices set,
no cost is shown.

  per_call       a fixed amount for every call (a phone number's share, a platform fee)
  per_minute     every minute of a call (phone line, speech to text, text to speech, AI, all in)
  per_reply      every answer the agent gives (an AI request per answer, mostly for chat agents)
  per_tool_call  every request the agent makes to your systems

A hosted platform (Vapi, Retell, ElevenLabs) reports what each call really cost: that amount is used for
the call instead of the owner's prices (metrics.platform_cost), when it is in the same currency. With no
prices set, the platform's amounts alone make the summary ("source": "platform").

Cost per completed task: what all the calls that should have done a task (book, order) cost, divided
by how many really got it done. A call that failed still costs money, so an agent that needs two calls
to get one booking done costs twice as much per booking. Calls Vocto couldn't make (setup errors) are
left out: they never reached the agent.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

FIELDS = ("per_call", "per_minute", "per_reply", "per_tool_call")
TASK_EXPECTED = ("success", "recover_or_honest", "alternative_or_honest")


def prices(profile: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The owner's prices, or None when none are set."""
    raw = (profile or {}).get("costs") or {}
    p = {k: float(raw.get(k) or 0) for k in FIELDS}
    if not any(p.values()):
        return None
    return {"currency": str(raw.get("currency") or "USD").upper(), **p}


def _platform_cost(verdict: Mapping[str, Any], currency: str) -> float | None:
    m = verdict.get("metrics") or {}
    if isinstance(m.get("platform_cost"), (int, float)) and str(m.get("platform_currency") or "USD").upper() == currency:
        return float(m["platform_cost"])
    return None


def call_cost(verdict: Mapping[str, Any], p: Mapping[str, Any]) -> float:
    m = verdict.get("metrics") or {}
    billed = _platform_cost(verdict, p["currency"])
    if billed is not None:
        return billed
    minutes = (m.get("duration_ms") or 0) / 60000
    return (p["per_call"] + p["per_minute"] * minutes + p["per_reply"] * (m.get("agent_turns") or 0)
            + p["per_tool_call"] * (m.get("tool_calls") or 0))


def _r(x: float | None) -> float | None:
    return None if x is None else round(x, 4)


def summary(verdicts: Sequence[Mapping[str, Any]], p: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """decision.costs: totals for the run, or None without prices (or platform-billed calls) or calls."""
    made = [v for v in verdicts if v.get("status") != "ERROR"]
    if not made:
        return None
    source = "prices"
    if not p:
        billed = [v for v in made if isinstance((v.get("metrics") or {}).get("platform_cost"), (int, float))]
        currencies = {str((v.get("metrics") or {}).get("platform_currency") or "USD").upper() for v in billed}
        if len(billed) != len(made) or len(currencies) != 1:
            return None              # some calls have no bill: a total would be too low
        p = {"currency": currencies.pop(), **{k: 0.0 for k in FIELDS}}
        source = "platform"
    elif any(_platform_cost(v, p["currency"]) is not None for v in made):
        source = "platform+prices"
    costs = [call_cost(v, p) for v in made]
    task = [(v, c) for v, c in zip(made, costs) if (v.get("outcome") or {}).get("expected") in TASK_EXPECTED]
    done = sum((v.get("outcome") or {}).get("class") == "TRUE_SUCCESS" for v, _ in task)
    task_cost = sum(c for _, c in task)
    minutes = sum((v.get("metrics") or {}).get("duration_ms") or 0 for v in made) / 60000
    return {"currency": p["currency"], "prices": {k: p[k] for k in FIELDS}, "source": source,
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
