"""Evidence store (contracts/evidence.py): one locked timeline per call, in local folders.

Layout: runs/<run_id>/calls/<test_id>/{call.json, events.jsonl, verdict.json, llm/}
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from . import __version__
from .contracts.models import AudioClip, CallContext, CallTrace, TraceEvent
from .util import mask_token, read_json, sha256_file, write_json

TEXT_FIELDS = {"caller_turn": "text", "agent_turn": "heard_text"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class EvidenceLocked(Exception):
    pass


class LocalCallWriter:
    """Append-only while the call runs. lock() masks PII and seals the file with a hash."""

    def __init__(self, folder: Path, record: dict[str, Any], profile: Mapping[str, Any]):
        self.folder = folder
        self.record = record
        self._profile = profile
        self._events: list[TraceEvent] = []
        self._start = time.monotonic()
        self._locked = False
        self._file = folder / record["events_file"]
        self._file.write_text("", encoding="utf-8")
        write_json(folder / "call.json", record)

    @property
    def llm_dir(self) -> Path:
        return self.folder / self.record["llm_log_dir"]

    @property
    def events(self) -> tuple[TraceEvent, ...]:
        return tuple(self._events)

    def now_ms(self) -> int:
        return int((time.monotonic() - self._start) * 1000)

    def emit(self, source: str, type: str, data: Mapping[str, Any],
             turn: int | None = None, t_ms: int | None = None) -> TraceEvent:
        if self._locked:
            raise EvidenceLocked(f"call {self.record['test_id']} is locked")
        t = self.now_ms() if t_ms is None else t_ms
        if self._events:
            t = max(t, self._events[-1].t_ms)  # one clock, never goes backwards
        event = TraceEvent(len(self._events) + 1, t, source, type, dict(data), turn)
        self._events.append(event)
        with self._file.open("a", encoding="utf-8") as f:
            f.write(_event_line(event) + "\n")
        return event

    def save_audio(self, name: str, clip: AudioClip) -> str:
        raise NotImplementedError("audio arrives in Phase 2 (voice mode)")

    def lock(self, status: str, cost_usd: float = 0.0) -> Mapping[str, Any]:
        masked = [_mask_event(e, self._profile, self.record["test_id"]) for e in self._events]
        self._file.write_text("".join(_event_line(e) + "\n" for e in masked), encoding="utf-8")
        self._events = masked
        self._locked = True
        self.record.update({
            "ended_at": utc_now(),
            "status": status,
            "event_count": len(masked),
            "events_sha256": sha256_file(self._file),
            "locked": True,
            "pii_masked": True,
            "cost_usd": round(cost_usd, 6),
        })
        write_json(self.folder / "call.json", self.record)
        return self.record


def _event_line(e: TraceEvent) -> str:
    d = {"seq": e.seq, "t_ms": e.t_ms, "source": e.source, "type": e.type}
    if e.turn is not None:
        d["turn"] = e.turn
    d["data"] = e.data
    return json.dumps(d, ensure_ascii=False)


def pii_keys(profile: Mapping[str, Any]) -> set[str]:
    return {k for t in profile.get("tools", []) for k in t.get("pii_args", [])}


def _mask_keys(node: Any, keys: set[str], salt: str) -> Any:
    if isinstance(node, dict):
        return {k: (mask_token(v, salt) if k in keys and v is not None and not isinstance(v, (dict, list))
                    else _mask_keys(v, keys, salt)) for k, v in node.items()}
    if isinstance(node, list):
        return [_mask_keys(v, keys, salt) for v in node]
    return node


def _mask_event(e: TraceEvent, profile: Mapping[str, Any], salt: str) -> TraceEvent:
    data = dict(e.data)
    field = TEXT_FIELDS.get(e.type)
    if field and isinstance(data.get(field), str):
        text = data[field]
        for p in profile.get("pii", {}).get("mask_patterns", []):
            text = re.sub(p["regex"], f"[{p['name']}]", text)
        data[field] = text
    if e.type in ("tool_request", "tool_response", "probe_result"):
        data = _mask_keys(data, pii_keys(profile), salt)
    return TraceEvent(e.seq, e.t_ms, e.source, e.type, data, e.turn)


class LocalEvidenceStore:
    def __init__(self, runs_dir: Path):
        self.runs_dir = Path(runs_dir)

    def call_dir(self, run_id: str, test_id: str) -> Path:
        return self.runs_dir / run_id / "calls" / test_id

    def open_call(self, ctx: CallContext, *, persona_id: str | None = None,
                  profile_sha256: str = "", scenario_sha256: str = "") -> LocalCallWriter:
        folder = self.call_dir(ctx.run_id, ctx.test_id)
        folder.mkdir(parents=True, exist_ok=False)
        record = {
            "schema_version": "0.1",
            "kind": "call_record",
            "test_id": ctx.test_id,
            "run_id": ctx.run_id,
            "profile_id": ctx.profile["id"],
            "profile_sha256": profile_sha256,
            "scenario_id": ctx.scenario["id"],
            "scenario_sha256": scenario_sha256,
            "repeat": ctx.repeat,
            "attempt": ctx.attempt,
            "mode": ctx.mode,
            "framework_version": __version__,
            "started_at": utc_now(),
            "status": "running",
            "events_file": "events.jsonl",
            "llm_log_dir": "llm/",
        }
        if persona_id:
            record["persona_id"] = persona_id
        return LocalCallWriter(folder, record, ctx.profile)

    def read_call(self, run_id: str, test_id: str) -> CallTrace:
        folder = self.call_dir(run_id, test_id)
        record = read_json(folder / "call.json")
        lines = (folder / record["events_file"]).read_text(encoding="utf-8").splitlines()
        events = []
        for line in lines:
            d = json.loads(line)
            events.append(TraceEvent(d["seq"], d["t_ms"], d["source"], d["type"], d["data"], d.get("turn")))
        return CallTrace(record, tuple(events))

    def write_verdict(self, run_id: str, test_id: str, verdict: Mapping[str, Any]) -> None:
        write_json(self.call_dir(run_id, test_id) / "verdict.json", verdict)

    def has_verdict(self, run_id: str, test_id: str) -> bool:
        return (self.call_dir(run_id, test_id) / "verdict.json").exists()

    def verify_lock(self, run_id: str, test_id: str) -> bool:
        """True if events.jsonl still matches the hash written when the call was locked."""
        folder = self.call_dir(run_id, test_id)
        record = read_json(folder / "call.json")
        return record.get("events_sha256") == sha256_file(folder / record["events_file"])

