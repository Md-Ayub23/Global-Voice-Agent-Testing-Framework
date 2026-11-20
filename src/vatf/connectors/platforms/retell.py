"""Retell: a test call as a Retell web call (https://docs.retellai.com/api-references/create-web-call).

  POST {api}/v2/create-web-call  {"agent_id", "metadata", "retell_llm_dynamic_variables"}
                                 Authorization: Bearer <key>     -> {"call_id", "access_token"}
  the call                       Retell runs web calls in a LiveKit room; Vocto joins it with the access token,
                                 as Retell's own web SDK does (wss://retell-ai-4ihahnq7.livekit.cloud, its
                                 default; VOCTO_RETELL_ROOM_URL changes it on this machine). The agent speaks on
                                 the track "agent_audio"; Retell's server sends call events as data
                                 (transcript updates; {"type": "status", "state": "ended"} when it hangs up).
  GET {api}/v2/get-call/{id}     after the call: transcript_with_tool_calls (tool calls with arguments and
                                 results), disconnection_reason, call_cost, latency, call_analysis

Profile: connection {type: platform, platform: retell, target: <agent id>,
                     options: {api_key: "${RETELL_API_KEY}", variables: {...}}}
"""

from __future__ import annotations

import json
import os
from typing import Any, Mapping

from ...audio.recorder import Recorder
from ...contracts.models import CallContext
from .base import (SAMPLE_RATE, PlatformAPI, PlatformError, PlatformReport, Secrets, api_base, call_variables,
                   poll, report_wait, secret_option)
from .livekit_room import RoomSession
from . import livekit_room

DEFAULT_API = "https://api.retellai.com"
ROOM_URL = "wss://retell-ai-4ihahnq7.livekit.cloud"
ENDED = {"ended", "error", "not_connected"}


def room_url() -> str:
    return os.environ.get("VOCTO_RETELL_ROOM_URL") or ROOM_URL


def make_api(profile: Mapping[str, Any], secrets: Secrets) -> PlatformAPI:
    key = secret_option(profile, "api_key", secrets, label="Retell API key")
    return PlatformAPI("Retell", api_base("retell", DEFAULT_API), {"Authorization": f"Bearer {key}"}, secrets)


def agent_id(profile: Mapping[str, Any]) -> str:
    aid = str(profile.get("connection", {}).get("target") or "").strip()
    if not aid:
        raise PlatformError("the Retell agent id is not set (Connect › Agent id)")
    return aid


class RetellSession(RoomSession):
    platform = "retell"
    agent_track_name = "agent_audio"

    def __init__(self, link: Any, recorder: Recorder, api: PlatformAPI, call_id: str, profile: Mapping[str, Any],
                 secrets: Secrets):
        super().__init__(link, recorder, secrets)
        self._api, self._profile = api, profile
        self.call_id = call_id
        self.call_meta = {"platform": "retell", "call_id": call_id}
        self._pos: dict[str, list[int | None]] = {"agent": [], "user": []}   # utterance -> its place in transcripts / heard

    def _data(self, data: bytes, topic: str | None, who: str | None) -> None:
        if who != "server":
            return super()._data(data, topic, who)
        try:
            ev = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self.error("a Retell call event that is not JSON")
            return
        if not isinstance(ev, Mapping):
            return
        if ev.get("type") == "status" and ev.get("state") in ("ended", "replaced"):
            self.ended("the agent ended the call")
        elif ev.get("event_type") == "update" and isinstance(ev.get("transcript"), list):
            self._update(ev["transcript"])

    def _update(self, rows: list[Any]) -> None:
        """Retell sends the whole transcript so far; an utterance may still grow, so each one keeps its place."""
        by_role: dict[str, list[str]] = {"agent": [], "user": []}
        for r in rows:
            if isinstance(r, Mapping) and r.get("role") in by_role:
                by_role[r["role"]].append(str(r.get("content") or "").strip())
        now = self.rec.now_sample()
        for role, target in (("agent", self.transcripts), ("user", self.heard)):
            pos = self._pos[role]
            for i, text in enumerate(by_role[role]):
                if not text:
                    continue
                while len(pos) <= i:
                    pos.append(None)
                if pos[i] is None:
                    pos[i] = len(target)
                    target.append((now, text))
                elif target[pos[i]][1] != text:
                    target[pos[i]] = (target[pos[i]][0], text)

    async def fetch_report(self) -> PlatformReport:
        from ...monitor.intake import IntakeError, from_retell

        doc = await poll(lambda: self._api.request("GET", f"v2/get-call/{self.call_id}"),
                         lambda d: isinstance(d, Mapping) and d.get("call_status") in ENDED
                         and d.get("transcript_with_tool_calls") is not None,
                         report_wait(self._profile))
        if not isinstance(doc, Mapping):
            return PlatformReport("retell", self.call_id, error="Retell returned no call record")
        summary = summarize(doc)
        try:
            call = from_retell(doc)
        except IntakeError as e:
            return PlatformReport("retell", self.call_id, summary=summary, error=f"the record has no turns yet ({e})")
        if doc.get("call_status") not in ENDED:
            summary["incomplete"] = True
        return PlatformReport("retell", self.call_id, call=call, summary=summary)


def summarize(doc: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"platform": "retell", "call_id": doc.get("call_id"), "status": doc.get("call_status"),
                           "ended_reason": doc.get("disconnection_reason")}
    cost = (doc.get("call_cost") or {}).get("combined_cost") if isinstance(doc.get("call_cost"), Mapping) else None
    if isinstance(cost, (int, float)):
        out["cost"], out["currency"] = round(float(cost) / 100, 4), "USD"      # Retell gives cents
    e2e = ((doc.get("latency") or {}).get("e2e") or {}) if isinstance(doc.get("latency"), Mapping) else {}
    if isinstance(e2e.get("p50"), (int, float)):
        out["latency_ms"] = {"e2e_p50": int(e2e["p50"])}
        if isinstance(e2e.get("p90"), (int, float)):
            out["latency_ms"]["e2e_p90"] = int(e2e["p90"])
    if doc.get("start_timestamp") and doc.get("end_timestamp"):
        out["duration_s"] = round((int(doc["end_timestamp"]) - int(doc["start_timestamp"])) / 1000, 1)
    out["recording"] = bool(doc.get("recording_url"))
    analysis = doc.get("call_analysis") or {}
    if isinstance(analysis, Mapping):
        if analysis.get("call_successful") is not None:
            out["platform_success"] = str(analysis["call_successful"])
        if analysis.get("call_summary"):
            out["platform_summary"] = str(analysis["call_summary"])[:600]
    return {k: v for k, v in out.items() if v is not None}


class RetellConnector:
    mode = "voice"
    sample_rate = SAMPLE_RATE

    def __init__(self, profile: Mapping[str, Any], root: Any = None, tools: Any = None):
        self._profile = profile

    async def open(self, ctx: CallContext, recorder: Recorder) -> RetellSession:
        from ... import netguard
        import asyncio

        secrets = Secrets()
        api = make_api(self._profile, secrets)
        body: dict[str, Any] = {"agent_id": agent_id(self._profile)}
        variables = call_variables(ctx, self._profile)
        if variables:
            body["retell_llm_dynamic_variables"] = variables
        if ctx is not None:
            body["metadata"] = {"vocto_test_id": ctx.test_id}
        call = await api.request("POST", "v2/create-web-call", body)
        token = str((call or {}).get("access_token") or "") if isinstance(call, Mapping) else ""
        call_id = str((call or {}).get("call_id") or "") if isinstance(call, Mapping) else ""
        if not token:
            raise PlatformError("Retell did not create the web call")
        secrets.add(token)
        url = room_url()
        link = livekit_room.ROOM_LINK()
        session = RetellSession(link, recorder, api, call_id, self._profile, secrets)
        try:
            await asyncio.to_thread(netguard.check_url, url, netguard.WS_SCHEMES)
            await link.connect(url, token)
            await session.wait_for_agent()
        except Exception as e:  # noqa: BLE001
            await session.close()
            if isinstance(e, PlatformError):
                raise
            raise PlatformError(secrets.scrub(f"could not join Retell's web call: {type(e).__name__}: {e}")) from e
        return session


async def check(profile: Mapping[str, Any]) -> dict[str, Any]:
    secrets = Secrets()
    api = make_api(profile, secrets)
    aid = agent_id(profile)
    doc = await api.request("GET", f"get-agent/{aid}")
    name = doc.get("agent_name") if isinstance(doc, Mapping) else None
    return {"ok": True, "message": f"Retell accepted the key and found the agent{f' {name!r}' if name else ''}.",
            "details": [f"Agent id: {aid}"]}


