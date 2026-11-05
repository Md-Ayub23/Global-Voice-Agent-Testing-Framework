"""Alerts after a run (Phase 6): Slack, Discord or any webhook, on the events the owner picks.

Events:
  no_go         the run's decision is NO_GO
  regression    the run is worse than the baseline (any regression or change for the worse)
  insufficient  NO_GO_INSUFFICIENT_EVIDENCE (the harness, not the agent, needs attention)
  recovered     GO after the agent's previous run (same profile) was not GO
  monitor            Phase 8: a real-call alert rule was broken (monitor/watch.py)
  monitor_recovered  Phase 8: every real-call alert has cleared

Runs with bug switches or planted bugs never alert: they are meant to fail. A failed alert is
recorded in the run's alerts.json and printed, and never changes the run's result or exit code.
URLs are secrets: the profile holds ${NAME} and the value lives in the .env file.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .evidence import utc_now
from .util import MissingSecret, expand_env, read_json, write_json

EVENTS = ("no_go", "regression", "insufficient", "recovered", "monitor", "monitor_recovered")
DEFAULT_ON = ["no_go", "regression", "recovered", "monitor", "monitor_recovered"]
TYPES = ("slack", "discord", "webhook")
TITLE = {"GO": "GO", "NO_GO": "NO-GO", "NO_GO_INSUFFICIENT_EVIDENCE": "NOT ENOUGH EVIDENCE"}


def previous_decision(runs_dir: Path, run_id: str, project: str, profile_file: str, kind: str = "run") -> str | None:
    """The decision of the agent's last finished run before this one (same project, profile and kind of
    run, no bugs): a load test recovers from an earlier load test, not from a normal run."""
    if not runs_dir.is_dir():
        return None
    for folder in sorted((p for p in runs_dir.iterdir() if p.is_dir() and p.name.startswith("r-")), reverse=True):
        if folder.name >= run_id:
            continue
        try:
            info = read_json(folder / "run.json")
        except (OSError, ValueError):
            continue
        if (info.get("project") == project and info.get("profile_file", "profile.yaml") == profile_file
                and info.get("decision") and not (info.get("agent_options") or {}).get("bugs")
                and ("load" if info.get("kind") == "load" else "run") == kind):
            return info["decision"]
    return None


def events_for(decision: Mapping[str, Any], previous: str | None) -> list[str]:
    out = []
    if decision["decision"] == "NO_GO":
        out.append("no_go")
    if decision["decision"] == "NO_GO_INSUFFICIENT_EVIDENCE":
        out.append("insufficient")
    b = decision.get("baseline") or {}
    if b.get("regressions") or b.get("worse"):
        out.append("regression")
    if decision["decision"] == "GO" and previous and previous != "GO":
        out.append("recovered")
    return out


def message(decision: Mapping[str, Any], info: Mapping[str, Any], events: Sequence[str]) -> str:
    name = info.get("profile_name") or decision.get("profile_id")
    lines = [f"Vocto: {name} is {TITLE[decision['decision']]} (run {decision['run_id']}"
             + (", load test" if info.get("kind") == "load" else "")
             + (f", {info['trigger']}" if info.get("trigger") and info["trigger"] != "manual" else "") + ")"]
    if "recovered" in events:
        lines.append("Back to GO after the last run failed.")
    for r in decision["reasons"][:4]:
        lines.append(f"- {r['message']}")
    b = decision.get("baseline")
    if b:
        lines.append(f"Vs baseline {b['run_id']}: {b['regressions']} new failure(s), {b['worse']} worse, "
                     f"{b['better']} better" + ("" if b["like_for_like"] else " (not like-for-like)") + ".")
    return "\n".join(lines)


def payload(kind: str, text: str, decision: Mapping[str, Any], info: Mapping[str, Any],
            events: Sequence[str]) -> dict[str, Any]:
    if kind == "slack":          # also Google Chat and Mattermost incoming webhooks
        return {"text": text}
    if kind == "discord":
        return {"content": text[:1990]}
    return {"text": text, "source": "vocto", "events": list(events), "run_id": decision["run_id"],
            "agent": info.get("profile_name") or decision.get("profile_id"), "profile_id": decision.get("profile_id"),
            "decision": decision["decision"], "exit_code": decision["exit_code"],
            "trigger": info.get("trigger", "manual"),
            "reasons": [{"rule": r["rule"], "message": r["message"]} for r in decision["reasons"]],
            "baseline": decision.get("baseline")}


def _post(url: str, body: Mapping[str, Any], timeout: float = 10) -> int:
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "vocto-alerts"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (the owner's own URL)
        return resp.status


def send_alerts(profile: Mapping[str, Any], decision: Mapping[str, Any], info: Mapping[str, Any],
                runs_dir: Path, post: Callable[[str, Mapping[str, Any]], int] = _post,
                force_events: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """Sends the alerts this run calls for; returns one result per alert target (also in alerts.json)."""
    targets = profile.get("alerts") or []
    if not targets or (info.get("agent_options") or {}).get("bugs"):
        return []
    previous = previous_decision(runs_dir, decision["run_id"], info.get("project", ""),
                                 info.get("profile_file", "profile.yaml"),
                                 "load" if info.get("kind") == "load" else "run")
    events = list(force_events) if force_events is not None else events_for(decision, previous)
    text = message(decision, info, events)
    results = []
    for i, t in enumerate(targets):
        name = t.get("name") or f"alert {i + 1}"
        wanted = [e for e in events if e in (t.get("events") or DEFAULT_ON)]
        if not wanted or t.get("enabled") is False:
            continue
        row = {"name": name, "type": t.get("type", "webhook"), "events": wanted, "at": utc_now()}
        try:
            url = expand_env(t["url"])
            status = post(url, payload(row["type"], text, decision, info, wanted))
            row.update(sent=200 <= status < 300, status=status)
        except MissingSecret as e:
            row.update(sent=False, error=str(e))
        except urllib.error.HTTPError as e:
            row.update(sent=False, status=e.code, error=f"the webhook answered {e.code} {e.reason}")
        except (urllib.error.URLError, OSError, ValueError) as e:
            row.update(sent=False, error=f"could not reach the webhook: {getattr(e, 'reason', e)}")
        results.append(row)
    if results:
        write_json(runs_dir / decision["run_id"] / "alerts.json", {"events": events, "text": text, "sent": results})
    return results


def send_test(target: Mapping[str, Any], agent: str, post: Callable[[str, Mapping[str, Any]], int] = _post) -> dict[str, Any]:
    """The Send test button: one short message to one alert target, to check its URL works."""
    text = f"Vocto: test alert for {agent}. Alerts for this agent will arrive here."
    kind = target.get("type", "webhook")
    body = {"text": text} if kind == "slack" else {"content": text} if kind == "discord" else \
        {"text": text, "source": "vocto", "events": ["test"], "agent": agent}
    try:
        status = post(expand_env(target["url"]), body)
        return {"sent": 200 <= status < 300, "status": status}
    except MissingSecret as e:
        return {"sent": False, "error": str(e)}
    except urllib.error.HTTPError as e:
        return {"sent": False, "status": e.code, "error": f"the webhook answered {e.code} {e.reason}"}
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        return {"sent": False, "error": f"could not reach the webhook: {getattr(e, 'reason', e)}"}


def send_monitor(profile: Mapping[str, Any], event: str, text: str, body: Mapping[str, Any],
                 post: Callable[[str, Mapping[str, Any]], int] = _post) -> list[dict[str, Any]]:
    """Phase 8: a real-call alert (event monitor or monitor_recovered) to every target that wants it."""
    results = []
    for i, t in enumerate(profile.get("alerts") or []):
        if t.get("enabled") is False or event not in (t.get("events") or DEFAULT_ON):
            continue
        kind = t.get("type", "webhook")
        row = {"name": t.get("name") or f"alert {i + 1}", "type": kind, "events": [event], "at": utc_now()}
        msg = {"text": text} if kind == "slack" else {"content": text[:1990]} if kind == "discord" else \
            {"text": text, "source": "vocto", "events": [event], **body}
        try:
            status = post(expand_env(t["url"]), msg)
            row.update(sent=200 <= status < 300, status=status)
        except MissingSecret as e:
            row.update(sent=False, error=str(e))
        except urllib.error.HTTPError as e:
            row.update(sent=False, status=e.code, error=f"the webhook answered {e.code} {e.reason}")
        except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
            row.update(sent=False, error=f"could not reach the webhook: {getattr(e, 'reason', e)}")
        results.append(row)
    return results
