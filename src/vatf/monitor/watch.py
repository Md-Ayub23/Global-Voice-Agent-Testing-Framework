"""Alert rules over real calls (Phase 8), checked after every import and every few minutes while the
dashboard is open.

Rules (profile monitor.alerts), over the calls of the last window_hours:
  safety          any safety judge item failed                       (any number of calls)
  false_success   the agent may have claimed a failed action worked  (any number of calls)
  pass_rate       pass rate below pass_rate_below %                  (needs min_calls)
  flagged         flagged share above flagged_above %                (needs min_calls)
  metric:<id>     a call-quality metric in its bad band over the window (needs min_calls)
An alert is sent once when it is raised (event `monitor`), and once when every alert has cleared
(event `monitor_recovered`), through the Phase 6 alert targets. state.json remembers what is raised.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence

from .. import alerts as alerting
from ..evidence import utc_now
from . import trends
from .store import MonitorStore


def window_rows(rows: Sequence[Mapping[str, Any]], hours: float, now: datetime) -> list[Mapping[str, Any]]:
    return trends.between(rows, now - timedelta(hours=hours), now + timedelta(seconds=1))


def evaluate(rows: Sequence[Mapping[str, Any]], rules: Mapping[str, Any], profile: Mapping[str, Any],
             now: datetime | None = None) -> dict[str, dict[str, Any]]:
    """{alert id: {title, message, calls}} for every rule broken now."""
    now = now or datetime.now(timezone.utc)
    hours = float(rules.get("window_hours") or 24)
    win = window_rows(rows, hours, now)
    span = f"the last {int(hours) if hours == int(hours) else hours} hours"
    out: dict[str, dict[str, Any]] = {}
    if rules.get("safety", True):
        bad = [r for r in win if any(f["kind"] == "judge" and f.get("section") == "safety" for f in r.get("flags") or [])]
        if bad:
            out["safety"] = {"title": "Safety failure on a real call",
                             "message": f"{len(bad)} real call(s) in {span} failed a safety judge item.",
                             "calls": [r["call_id"] for r in bad][:10]}
    if rules.get("false_success", True):
        bad = [r for r in win if any(f["id"] == "real.false_success" for f in r.get("flags") or [])]
        if bad:
            out["false_success"] = {"title": "Possible false success",
                                    "message": f"{len(bad)} real call(s) in {span}: a tool failed and the agent "
                                               "then said it was done. A person should check.",
                                    "calls": [r["call_id"] for r in bad][:10]}
    if len(win) < int(rules.get("min_calls") or 1):
        return out
    s = trends.summary(win)
    below = rules.get("pass_rate_below")
    if below is not None and s["pass_rate"] is not None and s["pass_rate"] < float(below):
        out["pass_rate"] = {"title": "Pass rate dropped",
                            "message": f"Pass rate is {s['pass_rate']}% over {s['passed'] + s['failed']} checked "
                                       f"real calls in {span} (alert below {below}%).", "calls": []}
    above = rules.get("flagged_above")
    if above is not None and s["flagged_pct"] is not None and s["flagged_pct"] > float(above):
        out["flagged"] = {"title": "Many calls flagged",
                          "message": f"{s['flagged_pct']}% of {s['calls']} real calls in {span} were flagged "
                                     f"(alert above {above}%).",
                          "calls": [r["call_id"] for r in win if r.get("flagged")][:10]}
    if rules.get("metrics_bad", True):
        for q in trends.quality(win, profile):
            if q["band"] == "bad":
                out[f"metric:{q['id']}"] = {"title": f"{q['title']} in the bad band",
                                            "message": f"{q['title']} is {q['value']} {q['unit']} over {q['calls']} "
                                                       f"real calls in {span} (good is {q['good']}, warn {q['warn']}).",
                                            "calls": []}
    return out


def check(store: MonitorStore, profile: Mapping[str, Any], rules: Mapping[str, Any], agent: str,
          now: datetime | None = None, post: Callable | None = None, send: bool = True) -> dict[str, Any]:
    """Evaluates the rules, sends what changed, saves the state. Returns {active, raised, cleared, sent}."""
    active = evaluate(store.rows(), rules, profile, now)
    state = store.state()
    before = state.get("active") or {}
    raised = {k: v for k, v in active.items() if k not in before}
    cleared = [k for k in before if k not in active]
    sent: list[dict[str, Any]] = []
    kw = {"post": post} if post else {}
    if send and raised:
        lines = [f"Vocto: real calls of {agent} need attention"] + [f"- {v['title']}: {v['message']}" for v in raised.values()]
        sent += alerting.send_monitor(profile, "monitor", "\n".join(lines),
                                      {"agent": agent, "alerts": [{"id": k, **v} for k, v in raised.items()]}, **kw)
    if send and before and not active:
        sent += alerting.send_monitor(profile, "monitor_recovered",
                                      f"Vocto: every real-call alert for {agent} has cleared.",
                                      {"agent": agent, "cleared": cleared}, **kw)
    since = {k: (before[k].get("since") if k in before else utc_now()) for k in active}
    history = list(state.get("history") or [])
    for k, v in raised.items():
        history.append({"at": utc_now(), "id": k, "event": "raised", "message": v["message"]})
    for k in cleared:
        history.append({"at": utc_now(), "id": k, "event": "cleared", "message": before[k].get("message", "")})
    store.save_state({"active": {k: {**v, "since": since[k]} for k, v in active.items()}, "history": history[-50:],
                      "last_sent": sent or state.get("last_sent") or []})
    return {"active": active, "raised": list(raised), "cleared": cleared, "sent": sent}
