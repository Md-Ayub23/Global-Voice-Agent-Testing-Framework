"""L4 connectors: text_python and text_http (text mode), websocket_audio and twilio_media_stream (voice mode)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping


VOICE_TYPES = ("websocket_audio", "twilio_media_stream")


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
    raise ConnectError(f"connection type {kind!r} is not supported yet "
                       "(text_python, text_http, websocket_audio and twilio_media_stream are)")
