"""L6 Evidence store: one locked timeline per call."""

from __future__ import annotations

from typing import Any, Mapping, Protocol

from .models import AudioClip, CallContext, CallTrace
from .observers import EventSink


class CallWriter(EventSink, Protocol):
    """Open while a call is running. Append-only."""

    def save_audio(self, name: str, clip: AudioClip) -> str:
        """Store an audio file and return its path relative to the call folder."""
        ...

    def lock(self, status: str) -> Mapping[str, Any]:
        """Mask PII, write events_sha256 into call.json, and refuse further writes.

        status is "completed" or "harness_error". Returns the final call record.
        """
        ...


class EvidenceStore(Protocol):
    """Local folders + SQLite index now; S3/Postgres later behind the same interface.

    Layout: runs/<run_id>/calls/<test_id>/{call.json, events.jsonl, verdict.json, audio/, llm/}
            runs/<run_id>/decision.json, runs/<run_id>/report.html
    """

    def open_call(self, ctx: CallContext) -> CallWriter: ...

    def read_call(self, run_id: str, test_id: str) -> CallTrace: ...

    def write_verdict(self, run_id: str, test_id: str, verdict: Mapping[str, Any]) -> None: ...

    def has_verdict(self, run_id: str, test_id: str) -> bool:
        """So a call that is already judged is never judged again."""
        ...
