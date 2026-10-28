"""L4 connectors: text_python (text mode) and websocket_audio (voice mode)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping


class ConnectError(Exception):
    """Could not reach or start the agent. A harness error, never blamed on the agent."""


def build_connector(profile: Mapping[str, Any], root: Path, tools: Any):
    kind = profile["connection"]["type"]
    if kind == "text_python":
        from .text_python import TextPythonConnector

        return TextPythonConnector(profile, root, tools)
    if kind == "websocket_audio":
        from .websocket_audio import WebSocketAudioConnector

        return WebSocketAudioConnector(profile, root, tools)
    raise ConnectError(f"connection type {kind!r} is not supported yet (text_python and websocket_audio are)")
