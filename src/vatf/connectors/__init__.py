"""L4 connectors. Phase 1: text_python."""

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
    raise ConnectError(f"connection type {kind!r} is not supported yet (Phase 1 has text_python)")
