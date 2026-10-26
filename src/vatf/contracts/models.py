"""Shared value types. These mirror the YAML/JSON schemas in /schemas."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping

# Loaded YAML documents. Phase 1 may replace these with typed models;
# until then they are validated against the schemas and passed as mappings.
Profile = Mapping[str, Any]
Scenario = Mapping[str, Any]
Persona = Mapping[str, Any]


class Result(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    ERROR = "ERROR"                    # harness problem, never blamed on the agent
    NOT_VERIFIED = "NOT_VERIFIED"      # evidence missing, never counted as a pass
    NOT_APPLICABLE = "NOT_APPLICABLE"
    NEEDS_REVIEW = "NEEDS_REVIEW"      # judges disagree on a blocker; a human must label it


class OutcomeClass(StrEnum):
    TRUE_SUCCESS = "TRUE_SUCCESS"
    HONEST_FAILURE = "HONEST_FAILURE"
    NO_ACTION = "NO_ACTION"
    FALSE_SUCCESS = "FALSE_SUCCESS"
    FALSE_FAILURE = "FALSE_FAILURE"
    SILENT_DATA_BUG = "SILENT_DATA_BUG"
    DUPLICATE_ACTION = "DUPLICATE_ACTION"
    UNEXPECTED_ACTION = "UNEXPECTED_ACTION"
    NOT_VERIFIED = "NOT_VERIFIED"


class EvidenceLevel(StrEnum):
    """Strongest first. The evidence ranking from the project rules."""

    BACKEND = "backend"
    TOOL_TRAFFIC = "tool_traffic"
    PLATFORM = "platform"
    SPEECH = "speech"
    AI_OPINION = "ai_opinion"
    NONE = "none"


class Decision(StrEnum):
    GO = "GO"
    NO_GO = "NO_GO"
    NO_GO_INSUFFICIENT_EVIDENCE = "NO_GO_INSUFFICIENT_EVIDENCE"


@dataclass(frozen=True)
class CallContext:
    """Everything a component needs to know about the call it is serving."""

    test_id: str
    run_id: str
    profile: Profile
    scenario: Scenario
    repeat: int
    attempt: int = 1
    mode: str = "text"                 # "text" or "voice"


@dataclass(frozen=True)
class TraceEvent:
    """One line of events.jsonl (schemas/trace_event.schema.yaml)."""

    seq: int
    t_ms: int
    source: str
    type: str
    data: Mapping[str, Any]
    turn: int | None = None


@dataclass(frozen=True)
class CallTrace:
    """A locked call: its record (call.json) plus its timeline."""

    record: Mapping[str, Any]
    events: tuple[TraceEvent, ...]


@dataclass(frozen=True)
class EvidenceRef:
    event: int
    note: str | None = None


@dataclass(frozen=True)
class CheckResult:
    """One entry of a verdict's checks list (schemas/call_verdict.schema.yaml)."""

    check_id: str
    origin: str                        # hard_check | outcome | metric | judge | validator
    result: Result
    severity: str                      # blocker | major | minor
    section: str                       # accuracy | experience | security
    message: str
    evidence: tuple[EvidenceRef, ...]
    evidence_level: EvidenceLevel
    can_block: bool = True
    type: str | None = None
    judge: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class AudioClip:
    pcm16: bytes
    sample_rate_hz: int
    start_ms: int | None = None        # on the call clock, when known


@dataclass(frozen=True)
class LLMMessage:
    role: str                          # "user" or "assistant"
    content: str


@dataclass(frozen=True)
class LLMRequest:
    purpose: str                       # caller | validator | judge_primary | judge_secondary
    model: str                         # from config, never hard-coded
    system: str
    messages: tuple[LLMMessage, ...]
    max_tokens: int
    temperature: float = 0.0
    metadata: Mapping[str, str] = field(default_factory=dict)  # e.g. test_id, for the LLM log


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    from_cache: bool = False
