"""Where real calls live (Phase 8): runs/monitor/<agent>/, never inside the project folder.

  runs/monitor/<agent>/
    index.json                one row per call (what the lists, trends and alerts read)
    state.json                the alerts raised now, so each fires once (watch.py)
    calls/<call id>/
      call.json               where it came from, when, how much personal data was removed
      events.jsonl            the redacted timeline, in the same event format as test calls
      verdict.json            how it scored (score.py)

runs/ is git-ignored. <agent> is the project folder's path with / as - ("examples/clinic" ->
"examples-clinic"), so it stays the same whatever the dashboard calls the agent.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..evidence import utc_now
from ..util import read_json, sha256_file, write_json

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
CALL_ID = re.compile(r"^c-\d{8}-\d{6}-[0-9a-f]{6}$")


def agent_slug(root: Path, folder: Path) -> str:
    try:
        rel = Path(folder).resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        rel = Path(folder).resolve().name
    return re.sub(r"[^A-Za-z0-9._-]+", "-", rel).strip("-") or "agent"


def _lock(path: Path) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(str(path), threading.Lock())


def parse_time(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def to_events(call: Mapping[str, Any], call_id: str) -> list[dict[str, Any]]:
    """The call as timeline events, in the format test calls use (schemas/trace_event.schema.yaml), so the
    Phase 5 metrics, the judges and the call view read it unchanged. Turn times become audio spans; an
    agent turn right after a caller turn gets its response time (latency_ms), as the recorder writes it."""
    timed = call["timed"]
    events: list[dict[str, Any]] = []

    def emit(source: str, type_: str, data: dict[str, Any], turn: int | None, t_ms: int) -> None:
        t = max(t_ms, events[-1]["t_ms"]) if events else max(0, t_ms)
        e = {"seq": len(events) + 1, "t_ms": t, "source": source, "type": type_}
        if turn:
            e["turn"] = turn
        e["data"] = data
        events.append(e)

    emit("connector", "call_started", {"test_id": call_id, "mode": "voice" if timed else "text", "real_call": True,
                                       "source": call["source"], **({"agent_version": call["agent_version"]}
                                                                    if call.get("agent_version") else {})}, None, 0)
    turn, prev, spoke_first = 0, None, True
    for n, item in enumerate(call["items"]):
        if item["kind"] == "turn":
            # Real calls keep no audio (file ""): the times come from the platform's transcript.
            span = {"file": "", "start_ms": item["start_ms"], "end_ms": item["end_ms"]} if timed else None
            t = item["start_ms"] if timed else 0
            if item["role"] == "caller":
                turn += 1
                spoke_first = False
                emit("caller", "caller_turn", {"text": item["text"], **({"audio": span} if span else {})}, turn, t)
                prev = item
                continue
            d: dict[str, Any] = {"heard_text": item["text"]}
            if span:
                d["audio"] = span
                if prev is not None and prev["role"] == "caller":
                    d["latency_ms"] = item["start_ms"] - prev["end_ms"]
                elif prev is not None and prev["role"] == "agent":
                    d["went_on"] = True
                if spoke_first and prev is None:
                    d["answered_at_ms"] = 0
            elif prev is not None and prev["role"] == "agent":
                d["went_on"] = True
            emit("recorder", "agent_turn", d, max(turn, 1), t)
            prev = item
        else:
            cid = item.get("id") or f"tool-{n + 1}"
            t = (item.get("at_ms") or 0) if timed else 0
            emit("platform", "tool_request", {"call_id": cid, "tool": item["name"], "args": item.get("args") or {}},
                 max(turn, 1), t)
            emit("platform", "tool_response", {"call_id": cid, "tool": item["name"], "ok": bool(item.get("ok", True)),
                                               "body": _body(item.get("result")), "duration_ms": 0, "reached_backend": True},
                 max(turn, 1), t)
    end_t = call.get("duration_ms") or (events[-1]["t_ms"] if events else 0)
    if call.get("transferred"):
        emit("platform", "handoff", {"to": "a person", "reason": call.get("ended_reason") or "transferred"},
             max(turn, 1), end_t)
    emit("connector", "call_ended", {"reason": call.get("ended_reason") or "call ended",
                                     "ended_by": ended_by(call.get("ended_reason"))}, None, end_t)
    return events


def _body(result: Any) -> Any:
    """Tool answers are objects in the timeline; a plain text or list answer goes under "result"."""
    return result if isinstance(result, dict) or result is None else {"result": result}


def ended_by(reason: str | None) -> str:
    r = (reason or "").lower()
    if re.search(r"customer|caller|user", r):
        return "caller"
    if re.search(r"assistant|agent|bot", r):
        return "agent"
    if re.search(r"silence|timeout|timed[-_ ]?out|max[-_ ]?duration|exceeded", r):
        return "timeout"
    return "unknown"


class MonitorStore:
    def __init__(self, runs: Path, slug: str):
        self.dir = Path(runs) / "monitor" / slug
        self.calls = self.dir / "calls"
        self.index_file = self.dir / "index.json"
        self.state_file = self.dir / "state.json"

    # ---- index ----
    def index(self) -> dict[str, dict[str, Any]]:
        if not self.index_file.is_file():
            return {}
        try:
            data = read_json(self.index_file)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def rows(self) -> list[dict[str, Any]]:
        """Every call's index row, newest first."""
        return sorted(self.index().values(), key=lambda r: (r.get("started_at") or r.get("imported_at") or ""),
                      reverse=True)

    def update_rows(self, rows: Iterable[Mapping[str, Any]] = (), remove: Iterable[str] = ()) -> None:
        with _lock(self.index_file):
            idx = self.index()
            for r in rows:
                idx[r["call_id"]] = {**idx.get(r["call_id"], {}), **dict(r)}
            for cid in remove:
                idx.pop(cid, None)
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.index_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.index_file)

    # ---- calls ----
    def new_id(self, started_at: str | None, key: str) -> str:
        dt = parse_time(started_at) or datetime.now(timezone.utc)
        return f"c-{dt.strftime('%Y%m%d-%H%M%S')}-{hashlib.sha256(key.encode()).hexdigest()[:6]}"

    def find_external(self, source: str, external_id: str | None) -> str | None:
        if not external_id:
            return None
        key = external_key(source, external_id)
        for cid, r in self.index().items():
            if r.get("external_key") == key:
                return cid
        return None

    def call_dir(self, call_id: str) -> Path:
        if not CALL_ID.match(call_id or ""):
            raise KeyError(f"no real call {call_id!r}")
        return self.calls / call_id

    def save_call(self, call_id: str, record: Mapping[str, Any], events: list[dict[str, Any]]) -> Path:
        folder = self.call_dir(call_id)
        folder.mkdir(parents=True, exist_ok=False)
        (folder / "events.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events),
                                             encoding="utf-8")
        write_json(folder / "call.json", {**record, "events_sha256": sha256_file(folder / "events.jsonl"),
                                          "event_count": len(events)})
        return folder

    def read(self, call_id: str) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any] | None]:
        folder = self.call_dir(call_id)
        if not (folder / "call.json").is_file():
            raise KeyError(f"no real call {call_id!r}")
        events = [json.loads(line) for line in (folder / "events.jsonl").read_text(encoding="utf-8").splitlines() if line]
        verdict = read_json(folder / "verdict.json") if (folder / "verdict.json").is_file() else None
        return read_json(folder / "call.json"), events, verdict

    def write_verdict(self, call_id: str, verdict: Mapping[str, Any]) -> None:
        write_json(self.call_dir(call_id) / "verdict.json", verdict)

    def delete(self, call_ids: Iterable[str]) -> int:
        ids = [c for c in call_ids if CALL_ID.match(c or "")]
        for cid in ids:
            shutil.rmtree(self.calls / cid, ignore_errors=True)
        self.update_rows(remove=ids)
        return len(ids)

    def prune(self, retention_days: int, now: datetime | None = None) -> int:
        """Deletes calls older than the retention period (by when they happened, else when imported)."""
        if not retention_days or retention_days <= 0:
            return 0
        now = now or datetime.now(timezone.utc)
        cut = now - timedelta(days=retention_days)
        old = [cid for cid, r in self.index().items()
               if (parse_time(r.get("started_at")) or parse_time(r.get("imported_at")) or now) < cut]
        return self.delete(old) if old else 0

    # ---- alert state ----
    def state(self) -> dict[str, Any]:
        try:
            return read_json(self.state_file) if self.state_file.is_file() else {}
        except (OSError, ValueError):
            return {}

    def save_state(self, state: Mapping[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        write_json(self.state_file, {**state, "updated_at": utc_now()})


def external_key(source: str, external_id: str) -> str:
    return hashlib.sha256(f"{source}|{external_id}".encode()).hexdigest()[:16]
