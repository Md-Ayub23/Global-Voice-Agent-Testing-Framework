"""One agent's real-call monitoring, end to end (Phase 8). The CLI, the webhook listener and the dashboard
all go through Monitor, so they agree.

  m = Monitor(root, runs, project_folder)
  m.ingest(file_bytes, name, via="upload")   read -> redact -> save -> score (no AI) -> plan judging
  m.judge_pending(llm)                       the AI judges on the calls waiting for them
  m.check_alerts()                           alert rules, retention
"""

from __future__ import annotations

import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from ..calibration import add_labels, calibration
from ..evidence import utc_now
from ..llm import Budget, ClaudeClient
from ..loader import load_profile
from ..rubrics import project_items
from . import score, trends, watch
from .intake import IntakeError, parse
from .redact import Redactor, caller_hash
from .settings import settings as monitor_settings
from .store import MonitorStore, agent_slug, external_key, parse_time, to_events

MAX_CALLS_PER_IMPORT = 2000
STAND_INS = {"name": "Alex Morgan", "phone": "07700 900123", "email": "alex.morgan@example.com",
             "birth_date": "4 May 1990", "address": "10 High Street", "card": "4111 1111 1111 1111",
             "id_number": "123-45-6789", "bank": "GB82 WEST 1234 5698 7654 32"}
_JUDGE_LOCKS: dict[str, threading.Lock] = {}


def have_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


class Monitor:
    def __init__(self, root: Path, runs: Path, folder: Path, profile_file: str = "profile.yaml"):
        self.root, self.runs, self.folder = Path(root), Path(runs), Path(folder)
        self.profile_file = profile_file
        self.profile, _ = load_profile(self.folder, profile_file)
        self.settings = monitor_settings(self.profile)
        self.slug = agent_slug(self.root, self.folder)
        self.store = MonitorStore(self.runs, self.slug)
        self.agent = self.profile.get("name") or self.profile.get("id") or self.folder.name

    # ---- intake --------------------------------------------------------------------------------
    def ingest(self, content: bytes | str, name: str = "", via: str = "upload",
               check_alerts: bool = True) -> dict[str, Any]:
        """Reads every call in one file or request, keeps only the redacted copy, scores it without AI and
        plans the judging. Returns {added: [ids], duplicates: [ids], problems: [text], alerts}."""
        if not self.settings["enabled"]:
            raise IntakeError("real-call monitoring is switched off for this agent")
        names = [self.profile.get("name") or ""] + [w for w in re.split(r"\W+", self.profile.get("name") or "") if len(w) > 2]
        calls = parse(content, name, agent_names=[n for n in names if n])
        if len(calls) > MAX_CALLS_PER_IMPORT:
            raise IntakeError(f"{len(calls)} calls in one go; import at most {MAX_CALLS_PER_IMPORT} at a time")
        added, dupes, problems = [], [], []
        for n, call in enumerate(calls, start=1):
            try:
                cid, new = self.add(call, via, name)
            except (IntakeError, ValueError, KeyError) as e:
                problems.append(f"call {n}: {e}")
                continue
            (added if new else dupes).append(cid)
        out: dict[str, Any] = {"added": added, "duplicates": dupes, "problems": problems, "calls": len(calls)}
        out["deleted_old"] = self.store.prune(int(self.settings["privacy"]["retention_days"] or 0))
        if check_alerts:
            out["alerts"] = self.check_alerts()
        return out

    def add(self, call: Mapping[str, Any], via: str = "upload", file: str = "") -> tuple[str, bool]:
        """(call id, True) for a new call; (existing id, False) when the platform's id was imported before."""
        existing = self.store.find_external(call["source"], call.get("external_id"))
        if existing:
            return existing, False
        privacy = self.settings["privacy"]
        days = int(privacy.get("retention_days") or 0)
        started = parse_time(call.get("started_at"))
        if days and started and started < datetime.now(timezone.utc) - timedelta(days=days):
            raise IntakeError(f"it happened on {call['started_at'][:10]}, before the {days}-day retention period, "
                              "so it was not kept")
        key = external_key(call["source"], call["external_id"]) if call.get("external_id") else None
        call_id = self.store.new_id(call.get("started_at"), f"{key or ''}|{utc_now()}|{id(call)}|{os.urandom(4).hex()}")
        r = Redactor(self.profile, privacy, call_id)
        items = call["items"]
        r.learn([i.get("text", "") for i in items if i["kind"] == "turn"],
                [i.get("args") or {} for i in items if i["kind"] == "tool"])
        clean = []
        for i in items:
            if i["kind"] == "turn":
                clean.append({**i, "text": r.text(i.get("text", ""))})
            else:
                clean.append({**i, "args": r.value(i.get("args") or {}, request=True), "result": r.value(i.get("result"))})
        safe = {**call, "items": clean, "caller_number": None, "ended_reason": r.text(call.get("ended_reason") or "") or None}
        events = to_events(safe, call_id)
        first_caller = next((i["text"] for i in clean if i["kind"] == "turn" and i["role"] == "caller"), "")
        record = {"schema_version": "0.1", "kind": "real_call", "call_id": call_id, "profile_id": self.profile.get("id"),
                  "source": call["source"], "received_via": via, "file": Path(file).name if file else None,
                  "external_id": call.get("external_id"), "external_key": key,
                  "started_at": call.get("started_at"), "imported_at": utc_now(),
                  "agent_version": call.get("agent_version"),
                  "caller_hash": caller_hash(call.get("caller_number"), self.slug),
                  "mode": "voice" if call["timed"] else "text", "timed": call["timed"],
                  "duration_ms": call.get("duration_ms"), "ended_reason": safe["ended_reason"],
                  "recording_url": call.get("recording_url") if privacy.get("keep_recording_links") else None,
                  "redacted": dict(r.counts), "pii_redacted": True, "summary": first_caller[:140]}
        self.store.save_call(call_id, record, events)
        verdict = score.build_verdict(call_id, record, events, self.profile)
        state, note = score.judge_plan(call_id, verdict, self.store.rows(), self.settings["scoring"], have_key())
        verdict["judges"] = {"state": state, "note": note}
        self.store.write_verdict(call_id, verdict)
        self.store.update_rows([score.index_row(call_id, record, verdict)])
        return call_id, True

    # ---- AI judges -------------------------------------------------------------------------------
    def items(self):
        return project_items(self.profile)

    def judge_pending(self, llm: Any = None, limit: int | None = None,
                      on_call: Callable[[str], None] | None = None) -> dict[str, Any]:
        """Runs the judges on the calls waiting for them, oldest first, within today's budget.
        One judging pass per agent at a time (a second caller returns at once)."""
        lock = _JUDGE_LOCKS.setdefault(str(self.store.dir), threading.Lock())
        if not lock.acquire(blocking=False):
            return {"judged": [], "busy": True}
        try:
            return self._judge_pending(llm, limit, on_call)
        finally:
            lock.release()

    def _judge_pending(self, llm, limit, on_call) -> dict[str, Any]:
        rows = sorted((r for r in self.store.rows() if r.get("judge_state") == "pending"),
                      key=lambda r: r.get("imported_at") or "")[:limit]
        if not rows:
            return {"judged": [], "problems": []}
        scoring = self.settings["scoring"]
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        spent = sum(r.get("judge_cost_usd") or 0 for r in self.store.rows() if (r.get("judged_at") or "").startswith(today))
        left = float(scoring["judge_budget_usd_per_day"]) - spent
        if llm is None:
            if not have_key():
                return {"judged": [], "problems": ["No Claude API key, so the AI judges can't run."]}
            llm = ClaudeClient(Budget(max(left, 0.0)), cache_dir=self.runs / ".llm-cache")
        items = self.items()
        cal = calibration(self.folder, self.profile, items)
        judged, problems = [], []
        for row in rows:
            cid = row["call_id"]
            try:
                record, events, verdict = self.store.read(cid)
            except KeyError:
                continue
            if left <= 0:
                self._skip(cid, record, verdict, f"Today's judging budget of ${scoring['judge_budget_usd_per_day']} was spent.")
                continue
            before = getattr(llm, "spent_usd", 0.0) or 0.0
            try:
                checks, probs = score.run_judges(llm, self.profile, events, record.get("mode", "text"), items, cal,
                                                 metadata={"call_id": cid, "purpose": "monitor"})
            except Exception as e:  # noqa: BLE001 - a failed call stays pending and is reported
                if type(e).__name__ == "BudgetExceeded":
                    self._skip(cid, record, verdict, "Today's judging budget was spent.")
                    left = 0
                    continue
                problems.append(f"{cid}: {type(e).__name__}: {e}")
                continue
            cost = round((getattr(llm, "spent_usd", 0.0) or 0.0) - before, 6)
            left -= cost
            new = score.build_verdict(cid, record, events, self.profile, checks, "done",
                                      "; ".join(probs) if probs else f"Judged by {len(items)} item(s).")
            new["judges"].update(cost_usd=cost, at=utc_now())
            self.store.write_verdict(cid, new)
            self.store.update_rows([score.index_row(cid, record, new)])
            judged.append(cid)
            problems += [f"{cid}: {p}" for p in probs]
            if on_call:
                on_call(cid)
        return {"judged": judged, "problems": problems}

    def _skip(self, cid: str, record, verdict, why: str) -> None:
        verdict = verdict or score.build_verdict(cid, record, self.store.read(cid)[1], self.profile)
        verdict["judges"] = {"state": "skipped", "note": why}
        self.store.write_verdict(cid, verdict)
        self.store.update_rows([score.index_row(cid, record, verdict)])

    def rescore(self, call_ids: Iterable[str] | None = None) -> int:
        """Scores calls again without AI (after the agent's metric bands changed); judge results are kept."""
        n = 0
        for cid in call_ids or [r["call_id"] for r in self.store.rows()]:
            try:
                record, events, old = self.store.read(cid)
            except KeyError:
                continue
            judge = [c for c in (old or {}).get("checks", []) if c["origin"] == "judge"]
            new = score.build_verdict(cid, record, events, self.profile, judge, (old or {}).get("judges", {}).get("state", "skipped"),
                                      (old or {}).get("judges", {}).get("note", ""))
            new["judges"] = {**(old or {}).get("judges", {}), **{k: new["judges"][k] for k in ("state", "note")}}
            labels = {c["check_id"]: c["judge"]["human"] for c in judge if (c.get("judge") or {}).get("human")}
            if labels:
                score.apply_labels(new, labels)
            self.store.write_verdict(cid, new)
            self.store.update_rows([score.index_row(cid, record, new)])
            n += 1
        return n

    # ---- alerts and retention -------------------------------------------------------------------
    def check_alerts(self, now: datetime | None = None, post: Callable | None = None) -> dict[str, Any]:
        return watch.check(self.store, self.profile, self.settings["alerts"], self.agent, now, post)

    def maintain(self) -> dict[str, Any]:
        """Retention and alert rules (the dashboard runs this every few minutes)."""
        deleted = self.store.prune(int(self.settings["privacy"]["retention_days"] or 0))
        return {"deleted_old": deleted, "alerts": self.check_alerts()}

    # ---- labels, deleting, tests ------------------------------------------------------------------
    def label(self, call_id: str, choices: Mapping[str, str | None], by: str = "") -> dict[str, Any]:
        record, events, verdict = self.store.read(call_id)
        if not verdict:
            raise KeyError(f"{call_id} has not been scored")
        judge = [c for c in verdict["checks"] if c["origin"] == "judge"]
        excerpt = " / ".join(e["data"].get("heard_text", "") for e in events if e["type"] == "agent_turn")[:600]
        add_labels(self.folder, "monitor", call_id, judge, choices, by,
                   extra={k: {"excerpt": excerpt} for k in choices})
        labels = {k: v for k, v in choices.items() if v}
        for c in judge:          # a label taken away: back to what the judges said, from the votes
            if c["check_id"] in choices and not choices[c["check_id"]] and (c.get("judge") or {}).get("human"):
                c["judge"].pop("human", None)
        score.apply_labels(verdict, labels)
        self.store.write_verdict(call_id, verdict)
        self.store.update_rows([score.index_row(call_id, record, verdict)])
        return verdict

    def delete(self, call_ids: Iterable[str]) -> int:
        return self.store.delete(call_ids)

    def scenario_from(self, call_id: str) -> tuple[str, dict[str, Any]]:
        """(file, scenario) to replay a real call as a test: the caller's own lines (with stand-ins where personal
        data was removed), then Claude carries on, and a judge check for each problem seen on the real call."""
        record, events, verdict = self.store.read(call_id)
        lines = []
        for e in events:
            if e["type"] == "caller_turn" and e["data"].get("text", "").strip():
                lines.append(_stand_in(e["data"]["text"]))
        if not lines:
            raise ValueError("this call has no caller lines to replay")
        checks: list[dict[str, Any]] = []
        known = {i.id for i in self.items()}
        for f in (verdict or {}).get("flags", []):
            if f["id"] == "real.false_success":
                checks.append({"id": "no-false-success", "type": "judge", "severity": "blocker", "section": "accuracy",
                               "intent": "If a tool failed, did the agent avoid telling the caller the action was done?",
                               "pass_when": "The agent only says it is done when the tool worked, or says honestly that it failed.",
                               "fail_when": "A tool failed and the agent still told the caller it was booked, done or confirmed."})
            elif f["id"] == "real.dropped":
                checks.append({"id": "answers-the-caller", "type": "judge", "severity": "major",
                               "intent": "Did the agent answer everything the caller said, without leaving them hanging?",
                               "pass_when": "Every caller turn gets an answer until the call ends normally.",
                               "fail_when": "The caller says something and gets no answer, or the call drops."})
            elif f["kind"] in ("judge", "review") and f["id"] in known:
                checks.append({"id": f"real-{re.sub(r'[^a-z0-9-]+', '-', f['id'].lower())}", "type": "judge",
                               "rubric": f["id"]})
        checks = list({c["id"]: c for c in checks}.values())
        sid = f"real.{call_id[2:]}"
        when = (record.get("started_at") or record.get("imported_at") or "")[:10]
        doc = {"id": sid, "title": f"Replay of a real call from {when}" + (f": {_stand_in(record['summary'])[:60]}" if record.get("summary") else ""),
               "description": ("Made from a real call (personal details replaced with stand-ins). The caller's lines are "
                               "replayed, then Claude carries on the call. Add fixtures if the call needs data in the backend."),
               "profile": self.profile.get("id"), "tags": ["from-real-call"], "criticality": "normal",
               "style": "scripted", "caller": {"facts": {}}, "turns": [{"caller": t} for t in lines[:8]],
               **({"checks": checks} if checks else {})}
        return f"scenarios/real-{call_id[2:]}.yaml", doc

    # ---- reading --------------------------------------------------------------------------------
    def overview(self, now: datetime | None = None) -> dict[str, Any]:
        rows = self.store.rows()
        now = now or datetime.now(timezone.utc)
        state = self.store.state()
        hours = float(self.settings["alerts"]["window_hours"] or 24)
        return {"agent": self.agent, "settings": self.settings, "have_key": have_key(),
                "totals": trends.summary(rows), "window": trends.summary(watch.window_rows(rows, hours, now)),
                "week": trends.week_compare(rows, now), "daily": trends.daily(rows, 14, now),
                "quality": trends.quality(trends.between(rows, now - timedelta(days=7), now), self.profile),
                "versions": trends.by_version(rows), "top_flags": trends.top_flags(rows[:500]),
                "alerts": {"active": state.get("active") or {}, "history": list(reversed(state.get("history") or []))[:20],
                           "last_sent": state.get("last_sent") or []},
                "pending": sum(r.get("judge_state") == "pending" for r in rows)}


def _stand_in(text: str) -> str:
    text = re.sub(r"\[masked:[0-9a-f]+\]", "the same detail as before", text)
    for label, value in STAND_INS.items():
        text = text.replace(f"[{label}]", value)
    return re.sub(r"\[([a-z_]+)\]", lambda m: m.group(1).replace("_", " "), text)
