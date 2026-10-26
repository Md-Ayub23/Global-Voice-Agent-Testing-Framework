"""L5 Observers: they watch the call from outside and write evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

from .models import CallContext, TraceEvent


class EventSink(Protocol):
    """Where observers write timeline events. Given out by the evidence store."""

    def now_ms(self) -> int:
        """The call clock: milliseconds since call_started."""
        ...

    def emit(
        self,
        source: str,
        type: str,
        data: Mapping[str, Any],
        turn: int | None = None,
        t_ms: int | None = None,
    ) -> TraceEvent:
        """Append one event. t_ms defaults to now_ms(). Returns it with its seq."""
        ...


class Observer(Protocol):
    """Generic observer: audio recorder, platform event listener, ..."""

    async def attach(self, ctx: CallContext, sink: EventSink) -> None: ...

    async def detach(self) -> None: ...


class ToolProxy(Observer, Protocol):
    """Sits between the agent and its tool backends.

    Records every tool_request / tool_response, adds the test id header,
    and injects the scenario's faults (fault_injected events).
    """

    def endpoint_for(self, tool: str) -> str:
        """The local URL the agent should call instead of the real upstream."""
        ...


@dataclass(frozen=True)
class ProbeResult:
    probe: str
    ok: bool                            # False = the probe itself failed (harness problem)
    rows: Sequence[Mapping[str, Any]] | None = None
    value: Any = None
    error: str | None = None


class BackendProbe(Protocol):
    """Reads real backend state after the call. Implementations: sql, rest."""

    def run(self, probe: Mapping[str, Any], ctx: CallContext) -> ProbeResult: ...
