"""Evidence store (contracts/evidence.py): one locked timeline per call, in local folders.

Layout: runs/<run_id>/calls/<test_id>/{call.json, events.jsonl, verdict.json, llm/}
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import __version__
from .contracts.models import AudioClip, CallContext, CallTrace, TraceEvent
from .util import mask_token, read_json, sha256_file, write_json, write_text_atomic

# Data fields that name another event of the same call by its seq (kept right when events are inserted).
SEQ_REFS = ("agent_turn", "caller_turn", "agent_seq")
# Free-text fields that may hold spoken personal data, per event type.
TEXT_FIELDS = {"caller_turn": ("text",), "agent_turn": ("heard_text", "platform_transcript"),
               "stt_text": ("text",), "caller_heard": ("text",), "platform_event": ("text",),
               "agent_heard": ("text",)}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class EvidenceLocked(Exception):
    pass


class LocalCallWriter:
    """Append-only while the call runs. lock() masks PII and seals the file with a hash."""

    def __init__(self, folder: Path, record: dict[str, Any], profile: Mapping[str, Any],
                 facts: Mapping[str, Any] | None = None, mask: bool = True):
        self.folder = folder
        self.record = record
        self._profile = profile
        self._facts = dict(facts or {})
        self._mask = mask  # False only with --show-pii: personal data is then stored as spoken
        self._events: list[TraceEvent] = []
        self._start = time.monotonic()
        self._locked = False
        self._lock = threading.Lock()  # the agent's tool calls can arrive from another thread
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

    def elapsed(self) -> float:
        """The call clock in seconds (float), for sample-accurate audio timing."""
        return time.monotonic() - self._start

    def emit(self, source: str, type: str, data: Mapping[str, Any],
             turn: int | None = None, t_ms: int | None = None) -> TraceEvent:
        with self._lock:
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

    def insert(self, items: Sequence[tuple[int, str, str, Mapping[str, Any], int | None]]) -> list[TraceEvent]:
        """Puts events that happened earlier into their place in time: (t_ms, source, type, data, turn).

        For evidence learnt after the call (a hosted platform's record of the tool calls its agent made).
        Each goes after every event at or before its time; the events after it move down, and the fields
        that point at another event by its seq (SEQ_REFS) are updated to match. Only before lock(), while no
        seq of this call has been quoted anywhere else. Returns the inserted events as they now stand."""
        with self._lock:
            if self._locked:
                raise EvidenceLocked(f"call {self.record['test_id']} is locked")
            if not items:
                return []
            old = [(e.t_ms, 0, i, e) for i, e in enumerate(self._events)]
            new = [(int(t), 1, k, TraceEvent(0, int(t), source, type_, dict(data), turn))
                   for k, (t, source, type_, data, turn) in enumerate(sorted(items, key=lambda x: int(x[0])))]
            merged = sorted(old + new, key=lambda m: (m[0], m[1], m[2]))     # same time: the older event first
            renum = {e.seq: n for n, (_, kind, _, e) in enumerate(merged, start=1) if kind == 0}
            events, added = [], []
            for n, (_, kind, _, e) in enumerate(merged, start=1):
                data = e.data
                refs = {k: renum[v] for k, v in data.items() if k in SEQ_REFS and isinstance(v, int) and v in renum}
                if refs:
                    data = {**data, **refs}
                ev = TraceEvent(n, e.t_ms, e.source, e.type, data, e.turn)
                events.append(ev)
                if kind == 1:
                    added.append(ev)
            self._events = events
            write_text_atomic(self._file, "".join(_event_line(e) + "\n" for e in events))
            return added

    def save_audio(self, name: str, clip: AudioClip) -> str:
        """Writes a mono WAV into the call folder. Audio is never masked: treat recordings as personal data."""
        from .audio.pcm import from_pcm16, write_wav

        if self._locked:
            raise EvidenceLocked(f"call {self.record['test_id']} is locked")
        write_wav(self.folder / name, [from_pcm16(clip.pcm16)], clip.sample_rate_hz)
        return name

    def lock(self, status: str, cost_usd: float = 0.0) -> Mapping[str, Any]:
        masked = (mask_events(self._events, self._profile, self._facts, self.record["test_id"])
                  if self._mask else list(self._events))
        self._file.write_text("".join(_event_line(e) + "\n" for e in masked), encoding="utf-8")
        self._events = masked
        self._locked = True
        self.record.update({
            "ended_at": utc_now(),
            "status": status,
            "event_count": len(masked),
            "events_sha256": sha256_file(self._file),
            "locked": True,
            "pii_masked": self._mask,
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


MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
          "October", "November", "December"]


def fact_patterns(profile: Mapping[str, Any], facts: Any) -> list[tuple[str, re.Pattern]]:
    """Regexes for the caller's personal facts as they may be spoken, so free text is masked too.

    A fact is personal when its name is a tool pii_arg or a pii.mask_patterns name
    (for the clinic: full_name and date_of_birth). Names are also masked word by word
    ("Thank you, Maria"), and an ISO date also matches its spoken forms ("14th of March, 1985").
    """
    personal = pii_keys(profile) | {p["name"] for p in profile.get("pii", {}).get("mask_patterns", [])}
    out, whole, words_out, seen = [], [], [], set()
    items = facts.items() if isinstance(facts, Mapping) else facts
    for name, value in items:
        if name not in personal or not isinstance(value, str) or not value.strip() or (name, value) in seen:
            continue
        seen.add((name, value))
        m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", value.strip())
        if m:
            y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
            month = f"(?:{MONTHS[mo - 1]}|{MONTHS[mo - 1][:3]})"
            day = rf"0?{d}(?:st|nd|rd|th)?"
            forms = [re.escape(value.strip()),
                     rf"(?:the\s+)?{day}\s+(?:of\s+)?{month},?\s+{y}",
                     rf"{month}\s+(?:the\s+)?{day},?\s+{y}",
                     rf"0?{d}[/.-]0?{mo}[/.-]{y}"]
            out.append((name, re.compile(r"\b(?:" + "|".join(forms) + r")\b", re.I)))
            continue
        whole.append((len(value.strip()), name, re.compile(rf"\b{re.escape(value.strip())}\b", re.I)))
        words = [w for w in re.split(r"\s+", value.strip()) if len(w) >= 3]
        if len(words) > 1:
            words_out.append((name, re.compile(r"\b(?:" + "|".join(map(re.escape, words)) + r")\b", re.I)))
    # Whole values first, longest first, then single words: otherwise a misheard tool value
    # ("Maria") would be masked first and leave "Lopez" of the real name in plain text.
    whole.sort(key=lambda w: -w[0])
    return out + [(n, p) for _, n, p in whole] + words_out


def mask_events(events: Sequence[TraceEvent], profile: Mapping[str, Any], facts: Mapping[str, Any],
                salt: str) -> list[TraceEvent]:
    """The events with personal data masked, exactly as a call is sealed (also used by the live UI view)."""
    # Personal values to find in free text: the caller's fact sheet, plus whatever personal
    # values the agent actually sent to its tools (scripted scenarios have no fact sheet).
    values: list[tuple[str, Any]] = list(facts.items())
    keys = pii_keys(profile)
    for e in events:
        if e.type == "tool_request":
            values += [(k, v) for k, v in e.data.get("args", {}).items() if k in keys]
    patterns = fact_patterns(profile, values)
    return [_mask_event(e, profile, salt, patterns) for e in events]


def _mask_event(e: TraceEvent, profile: Mapping[str, Any], salt: str,
                text_patterns: list[tuple[str, re.Pattern]] = ()) -> TraceEvent:
    data = dict(e.data)

    def masked(text: str) -> str:
        for name, pattern in text_patterns:
            text = pattern.sub(f"[{name}]", text)
        for p in profile.get("pii", {}).get("mask_patterns", []):
            text = re.sub(p["regex"], f"[{p['name']}]", text)
        return text

    for field in TEXT_FIELDS.get(e.type, ()):
        if isinstance(data.get(field), str):
            data[field] = masked(data[field])
    # A hosted platform's own summary of the call is free text about the caller too.
    if e.type == "call_ended" and isinstance(data.get("platform"), dict):
        plat = dict(data["platform"])
        for field in ("platform_summary", "platform_success"):
            if isinstance(plat.get(field), str):
                plat[field] = masked(plat[field])
        data["platform"] = plat
    if e.type in ("tool_request", "tool_response", "probe_result", "state_diff"):
        data = _mask_keys(data, pii_keys(profile), salt)
    return TraceEvent(e.seq, e.t_ms, e.source, e.type, data, e.turn)


class LocalEvidenceStore:
    def __init__(self, runs_dir: Path):
        self.runs_dir = Path(runs_dir)

    def call_dir(self, run_id: str, test_id: str) -> Path:
        return self.runs_dir / run_id / "calls" / test_id

    def open_call(self, ctx: CallContext, *, persona_id: str | None = None,
                  profile_sha256: str = "", scenario_sha256: str = "", mask: bool = True) -> LocalCallWriter:
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
        return LocalCallWriter(folder, record, ctx.profile, ctx.scenario.get("caller", {}).get("facts", {}), mask)

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

