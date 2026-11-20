"""LiveKit Agents: a test call in a room on the team's own LiveKit server (LiveKit Cloud or self-hosted).

Vocto makes a new room for every call and joins it as the caller, with a token it signs itself from the
project's API key and secret. With an agent name, the token asks LiveKit to send that agent into the room
(explicit dispatch, https://docs.livekit.io/agents/worker/agent-dispatch/); without one, agents that join
every new room (automatic dispatch) come by themselves. The room is deleted when the call ends.

What Vocto sees: the agent's audio, LiveKit's transcriptions (text streams "lk.transcription"), the agent
leaving. LiveKit keeps no record of tool calls; an agent reports them in a data message on topic
"vocto.tool" (docs/platforms-guide.md), or its tools go through Vocto's tool gateway.

Profile: connection {type: platform, platform: livekit, target: <agent name, or empty>,
                     options: {url: "wss://my-project.livekit.cloud", api_key: "${LIVEKIT_API_KEY}",
                               api_secret: "${LIVEKIT_API_SECRET}", room_prefix: "vocto"}}
"""

from __future__ import annotations

import asyncio
import json
import secrets as pysecrets
from datetime import timedelta
from typing import Any, Mapping

from ... import netguard
from ...audio.recorder import Recorder
from ...contracts.models import CallContext
from .base import SAMPLE_RATE, PlatformError, PlatformReport, Secrets, call_variables, option, secret_option
from .livekit_room import RoomSession
from . import livekit_room


def server_url(profile: Mapping[str, Any]) -> str:
    url = str(option(profile, "url") or "").strip()
    if not url.startswith(("wss://", "ws://", "https://", "http://")):
        raise PlatformError("the LiveKit server address is not set (Connect › LiveKit URL, e.g. wss://my-project.livekit.cloud)")
    return url


def http_url(url: str) -> str:
    return "https://" + url[6:] if url.startswith("wss://") else ("http://" + url[5:] if url.startswith("ws://") else url)


def ws_url(url: str) -> str:
    return "wss://" + url[8:] if url.startswith("https://") else ("ws://" + url[7:] if url.startswith("http://") else url)


def credentials(profile: Mapping[str, Any], secrets: Secrets) -> tuple[str, str]:
    key = secret_option(profile, "api_key", secrets, label="LiveKit API key")
    secret = secret_option(profile, "api_secret", secrets, label="LiveKit API secret")
    return str(key), str(secret)


def caller_token(key: str, secret: str, room: str, identity: str, agent_name: str, metadata: Mapping[str, Any]) -> str:
    from livekit import api

    token = (api.AccessToken(key, secret).with_identity(identity).with_name("Vocto caller")
             .with_ttl(timedelta(hours=1))
             .with_metadata(json.dumps(dict(metadata)))
             .with_grants(api.VideoGrants(room_join=True, room=room, can_publish=True, can_subscribe=True,
                                          can_publish_data=True)))
    if agent_name:
        token = token.with_room_config(api.RoomConfiguration(agents=[
            api.RoomAgentDispatch(agent_name=agent_name, metadata=json.dumps(dict(metadata)))]))
    return token.to_jwt()


class LiveKitSession(RoomSession):
    platform = "livekit"

    def __init__(self, link: Any, recorder: Recorder, secrets: Secrets, room: str, profile: Mapping[str, Any]):
        super().__init__(link, recorder, secrets)
        self.room, self._profile = room, profile
        self.call_id = room
        self.call_meta = {"platform": "livekit", "call_id": room}

    async def close(self) -> None:
        await super().close()
        await delete_room(self._profile, self.room, self.secrets)

    async def fetch_report(self) -> PlatformReport:
        summary = {"platform": "livekit", "call_id": self.room,
                   "ended_reason": self.close_reason or None, "reported_tools": len(self.reported_tools)}
        call = {"source": "livekit", "items": list(self.reported_tools), "timed": False, "transferred": False,
                "ended_reason": None, "duration_ms": None, "clock": "session"}
        return PlatformReport("livekit", self.room, call=call, summary={k: v for k, v in summary.items() if v is not None})


async def delete_room(profile: Mapping[str, Any], room: str, secrets: Secrets) -> None:
    """Best effort: an empty room closes by itself after a while anyway."""
    try:
        from livekit import api

        key, secret = credentials(profile, secrets)
        lk = api.LiveKitAPI(http_url(server_url(profile)), key, secret)
        try:
            await asyncio.wait_for(lk.room.delete_room(api.DeleteRoomRequest(room=room)), 10)
        finally:
            await lk.aclose()
    except Exception:  # noqa: BLE001
        pass


class LiveKitConnector:
    mode = "voice"
    sample_rate = SAMPLE_RATE

    def __init__(self, profile: Mapping[str, Any], root: Any = None, tools: Any = None):
        self._profile = profile

    async def open(self, ctx: CallContext, recorder: Recorder) -> LiveKitSession:
        secrets = Secrets()
        url = ws_url(server_url(self._profile))
        key, secret = credentials(self._profile, secrets)
        agent_name = str(self._profile.get("connection", {}).get("target") or "").strip()
        prefix = str(option(self._profile, "room_prefix") or "vocto").strip() or "vocto"
        test_id = ctx.test_id if ctx is not None else "test"
        room = f"{prefix}-{test_id}-{pysecrets.token_hex(3)}"[:120]
        metadata: dict[str, Any] = dict(call_variables(ctx, self._profile))
        try:
            await asyncio.to_thread(netguard.check_url, url, netguard.WS_SCHEMES)
            token = caller_token(key, secret, room, f"vocto-caller-{pysecrets.token_hex(3)}", agent_name, metadata)
        except Exception as e:  # noqa: BLE001
            raise PlatformError(secrets.scrub(f"could not prepare the LiveKit call: {type(e).__name__}: {e}")) from e
        secrets.add(token)
        link = livekit_room.ROOM_LINK()
        session = LiveKitSession(link, recorder, secrets, room, self._profile)
        try:
            await link.connect(url, token)
            await session.wait_for_agent(float(option(self._profile, "agent_wait_s", livekit_room.AGENT_WAIT_S)))
        except Exception as e:  # noqa: BLE001
            await session.close()
            if isinstance(e, PlatformError):
                raise
            raise PlatformError(secrets.scrub(f"could not join the LiveKit room: {type(e).__name__}: {e}")) from e
        return session


async def check(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Are the address, key and secret right? Lists rooms (an authenticated call); places no call."""
    from livekit import api

    secrets = Secrets()
    url = server_url(profile)
    key, secret = credentials(profile, secrets)
    await asyncio.to_thread(netguard.check_url, http_url(url), netguard.HTTP_SCHEMES)
    lk = api.LiveKitAPI(http_url(url), key, secret)
    try:
        rooms = await asyncio.wait_for(lk.room.list_rooms(api.ListRoomsRequest()), 15)
    except Exception as e:  # noqa: BLE001
        text = secrets.scrub(f"{type(e).__name__}: {e}")
        if "401" in text or "unauthorized" in text.lower():
            raise PlatformError("LiveKit refused the API key or secret (HTTP 401)") from None
        raise PlatformError(f"could not reach LiveKit at {url}: {text}") from None
    finally:
        await lk.aclose()
    agent = str(profile.get("connection", {}).get("target") or "").strip()
    return {"ok": True, "message": "LiveKit accepted the key and secret.",
            "details": [f"Server: {url}", f"Rooms open now: {len(rooms.rooms)}",
                        f"Agent: {agent} (sent into each test room)" if agent
                        else "Agent: none named, so an agent that joins every new room must be running"]}
