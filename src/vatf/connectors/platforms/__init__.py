"""Hosted voice-agent platforms, tested without a phone line: connection type "platform".

  vapi        Vapi's WebSocket call transport                 (vapi.py)
  retell      a Retell web call (a LiveKit room)              (retell.py)
  elevenlabs  the ElevenLabs Agents WebSocket                 (elevenlabs.py)
  livekit     a LiveKit Agents room on the team's server      (livekit_agent.py)

Each one opens a live voice session (base.PlatformSession) and, after the call, reads the platform's own
record of it (base.PlatformReport): the tool calls with their arguments and results, why the call ended,
cost and the platform's latency. reconcile.py puts those into the call's evidence, so every check and AI
judge sees the platform's tool calls like any other. docs/platforms-guide.md is the user's guide.
"""

from __future__ import annotations

from typing import Any, Mapping

from .. import ConnectError

PLATFORMS = {
    "vapi": "Vapi",
    "retell": "Retell",
    "elevenlabs": "ElevenLabs",
    "livekit": "LiveKit",
}


def name_of(profile: Mapping[str, Any]) -> str:
    return str(profile.get("connection", {}).get("platform") or "")


def build(profile: Mapping[str, Any], root: Any, tools: Any):
    platform = name_of(profile)
    if platform == "vapi":
        from .vapi import VapiConnector
        return VapiConnector(profile, root, tools)
    if platform == "retell":
        from .retell import RetellConnector
        return RetellConnector(profile, root, tools)
    if platform == "elevenlabs":
        from .elevenlabs import ElevenLabsConnector
        return ElevenLabsConnector(profile, root, tools)
    if platform == "livekit":
        from .livekit_agent import LiveKitConnector
        return LiveKitConnector(profile, root, tools)
    raise ConnectError(f"platform {platform!r} is not supported (vapi, retell, elevenlabs and livekit are)")


async def check(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Connect's "Test connection" for a platform: the key, and that the agent exists. No call is placed."""
    platform = name_of(profile)
    if platform == "vapi":
        from .vapi import check as run
    elif platform == "retell":
        from .retell import check as run
    elif platform == "elevenlabs":
        from .elevenlabs import check as run
    elif platform == "livekit":
        from .livekit_agent import check as run
    else:
        return {"ok": False, "message": f"platform {platform!r} is not supported"}
    return await run(profile)
