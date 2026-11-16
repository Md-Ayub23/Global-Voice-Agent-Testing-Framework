"""L4 connectors: text_python and text_http (text mode); websocket_audio, twilio_media_stream and phone_call
(a real phone number, voice mode)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping


VOICE_TYPES = ("websocket_audio", "twilio_media_stream", "phone_call")


def call_metadata(ctx: Any) -> dict[str, Any]:
    """What a call tells the agent about itself, beyond the time and test id: its direction and, for an
    outbound call, what the agent's dialler would be told (scenario `outbound`, with $facts filled in);
    the caller's number when the test gives one (`caller_id`, so an agent can recognise a returning
    caller). Empty for an ordinary inbound call, so agents that know nothing of it see no change."""
    if ctx is None:
        return {}
    sc = getattr(ctx, "scenario", None) or {}
    facts = (sc.get("caller") or {}).get("facts") or {}

    def fill(v: Any) -> Any:
        if isinstance(v, str) and v.startswith("$") and v[1:] in facts:
            return facts[v[1:]]
        if isinstance(v, dict):
            return {k: fill(x) for k, x in v.items()}
        if isinstance(v, list):
            return [fill(x) for x in v]
        return v

    out: dict[str, Any] = {}
    if sc.get("direction") == "outbound":
        out["direction"] = "outbound"
        out["outbound"] = fill(dict(sc.get("outbound") or {}))
    if sc.get("caller_id"):
        out["from"] = str(fill(sc["caller_id"]))
    return out


class ConnectError(Exception):
    """Could not reach or start the agent. A harness error, never blamed on the agent."""


def is_voice(profile: Mapping[str, Any]) -> bool:
    """True when the profile's connection carries the call as audio."""
    return profile.get("connection", {}).get("type") in VOICE_TYPES


def build_connector(profile: Mapping[str, Any], root: Path, tools: Any):
    kind = profile["connection"]["type"]
    if kind == "text_python":
        from .text_python import TextPythonConnector

        return TextPythonConnector(profile, root, tools)
    if kind == "text_http":
        from .text_http import TextHttpConnector

        return TextHttpConnector(profile, root, tools)
    if kind == "websocket_audio":
        from .websocket_audio import WebSocketAudioConnector

        return WebSocketAudioConnector(profile, root, tools)
    if kind == "twilio_media_stream":
        from .twilio_stream import TwilioStreamConnector

        return TwilioStreamConnector(profile, root, tools)
    if kind == "phone_call":
        from .phone_call import PhoneCallConnector

        return PhoneCallConnector(profile, root, tools)
    raise ConnectError(f"connection type {kind!r} is not supported yet "
                       "(text_python, text_http, websocket_audio, twilio_media_stream and phone_call are)")
