"""L4 Connectors: one interface, many ways to reach an agent."""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Mapping, Any, Protocol

from .models import AudioClip, CallContext


@dataclass(frozen=True)
class AgentOutput:
    """Something the agent did, as seen by the connector.

    kind:
      speech     - text (text mode) or audio (voice mode) from the agent
      tool_event - a tool call the platform reports (for agents whose tools
                   do not go through our proxy); stored as platform evidence
      handoff    - a sub-agent took over (multi-agent platforms)
      platform   - any other platform event worth keeping
      closed     - the agent ended the call
    """

    kind: str
    t_ms: int
    text: str | None = None
    audio: AudioClip | None = None
    data: Mapping[str, Any] | None = None


class AgentSession(Protocol):
    """One live call with the agent."""

    async def send_text(self, text: str) -> None: ...

    async def send_audio(self, clip: AudioClip) -> None: ...

    def outputs(self) -> AsyncIterator[AgentOutput]: ...

    async def close(self) -> None: ...


class Connector(Protocol):
    """Implementations: text_python, text_http, websocket_audio, twilio_media_stream; later sip, webrtc, platforms.

    The connector passes ctx.test_id to the agent when the profile says
    session_metadata. A failure to connect is a harness error, not an agent failure.
    """

    mode: str  # "text" or "voice"

    async def open(self, ctx: CallContext) -> AgentSession: ...
