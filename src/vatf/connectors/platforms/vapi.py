"""Vapi: a test call over Vapi's WebSocket call transport (https://docs.vapi.ai/calls/websocket-transport).

  POST {api}/call   {"assistantId", "transport": {"provider": "vapi.websocket",
                     "audioFormat": {"format": "pcm_s16le", "container": "raw", "sampleRate": 16000}},
                     "assistantOverrides": {"variableValues": {...}}}      Authorization: Bearer <private key>
                    -> {"id", "transport": {"websocketCallUrl": "wss://.../transport"}}
  the socket        binary frames: raw PCM16 16 kHz both ways; text frames: JSON control messages
                    (Vocto sends {"type": "end-call"} to hang up; it reads transcripts, tool calls, the call's
                    status and "user-interrupted" when the server sends them)
  GET {api}/call/{id}  after the call: artifact.messages (turns, tool calls with arguments and results),
                    endedReason, cost, artifact.performanceMetrics

Profile: connection {type: platform, platform: vapi, target: <assistant id>,
                     options: {api_key: "${VAPI_API_KEY}", variables: {...}, report_wait_s: 45}}
No phone number is used, so a test call costs only Vapi's own per-minute price.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Mapping

import numpy as np

from ... import netguard
from ...audio.pcm import from_pcm16, to_pcm16
from ...audio.recorder import Recorder
from ...contracts.models import CallContext
from .base import (SAMPLE_RATE, PlatformAPI, PlatformError, PlatformReport, PlatformSession, Secrets, api_base,
                   call_variables, poll, report_wait, secret_option)

DEFAULT_API = "https://api.vapi.ai"
CONNECT_TIMEOUT_S = 15
ENDED = {"ended"}


def make_api(profile: Mapping[str, Any], secrets: Secrets) -> PlatformAPI:
    key = secret_option(profile, "api_key", secrets, label="Vapi private API key")
    return PlatformAPI("Vapi", api_base("vapi", DEFAULT_API), {"Authorization": f"Bearer {key}"}, secrets)


def assistant_id(profile: Mapping[str, Any]) -> str:
    aid = str(profile.get("connection", {}).get("target") or "").strip()
    if not aid:
        raise PlatformError("the Vapi assistant id is not set (Connect › Assistant id)")
    return aid


class VapiSession(PlatformSession):
    platform = "vapi"

    def __init__(self, ws: Any, recorder: Recorder, api: PlatformAPI, call_id: str, profile: Mapping[str, Any],
                 secrets: Secrets):
        super().__init__(recorder, secrets)
        self._ws, self._api, self._profile = ws, api, profile
        self.call_id = call_id
        self.call_meta = {"platform": "vapi", "call_id": call_id}
        self._agent_text: dict[str, int] = {}
        self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        import websockets

        try:
            async for msg in self._ws:
                if isinstance(msg, (bytes, bytearray)):
                    if len(msg) % 2:
                        msg = msg[:-1]
                    self.play(from_pcm16(bytes(msg)))
                    continue
                try:
                    data = json.loads(msg)
                except ValueError:
                    self.error(f"not JSON: {str(msg)[:80]!r}")
                    continue
                if isinstance(data, Mapping):
                    self._control(data)
                if self.closed.is_set():
                    break
        except websockets.ConnectionClosed as e:
            self.ended(f"Vapi closed the call ({e.rcvd.reason if e.rcvd else 'no reason'})" if e.rcvd and e.rcvd.reason
                       else "the agent hung up")
        except Exception as e:  # noqa: BLE001 - any read failure ends the call
            self.ended(f"connection error: {type(e).__name__}: {e}")
        finally:
            self.ended("the agent hung up")

    def _control(self, m: Mapping[str, Any]) -> None:
        kind = str(m.get("type") or "")
        if kind == "transcript" and str(m.get("transcriptType", "final")) == "final":
            text = str(m.get("transcript") or "").strip()
            if text:
                (self.transcripts if m.get("role") == "assistant" else self.heard).append((self.rec.now_sample(), text))
        elif kind in ("tool-calls", "function-call"):
            calls = m.get("toolCallList") or m.get("toolCalls") or ([m["functionCall"]] if m.get("functionCall") else [])
            for tc in calls if isinstance(calls, list) else []:
                if isinstance(tc, Mapping) and tc.get("id"):
                    self.live_tools.setdefault(str(tc["id"]), self.now_ms())
        elif kind in ("user-interrupted", "clear"):
            self.rec.clear()
        elif kind == "status-update" and m.get("status") == "ended":
            reason = str(m.get("endedReason") or "")
            self.ended(f"the call ended ({reason})" if reason else "the agent hung up")
        elif kind == "hang":
            self.error("Vapi reported the assistant hanging (no answer in time)")

    async def send_frame(self, x: np.ndarray) -> None:
        import websockets

        if self.closed.is_set():
            return
        try:
            await self._ws.send(to_pcm16(x))
        except websockets.ConnectionClosed:
            pass

    async def close(self) -> None:
        self._closing = True
        if not self.closed.is_set():
            try:
                await self._ws.send(json.dumps({"type": "end-call"}))
            except Exception:  # noqa: BLE001
                pass
        try:
            await self._ws.close()
        except Exception:  # noqa: BLE001
            pass
        self._reader.cancel()
        try:
            await self._reader
        except (asyncio.CancelledError, Exception):
            pass
        self.closed.set()

    async def fetch_report(self) -> PlatformReport:
        from ...monitor.intake import IntakeError, from_vapi

        doc = await poll(lambda: self._api.request("GET", f"call/{self.call_id}"),
                         lambda d: isinstance(d, Mapping) and d.get("status") in ENDED
                         and bool(((d.get("artifact") or {}).get("messages") or d.get("messages"))),
                         report_wait(self._profile))
        if not isinstance(doc, Mapping):
            return PlatformReport("vapi", self.call_id, error="Vapi returned no call record")
        summary = summarize(doc)
        try:
            call = from_vapi(doc)
        except IntakeError as e:
            return PlatformReport("vapi", self.call_id, summary=summary, error=f"Vapi's record has no turns yet ({e})")
        if doc.get("status") not in ENDED:
            summary["incomplete"] = True
        return PlatformReport("vapi", self.call_id, call=call, summary=summary)


def summarize(doc: Mapping[str, Any]) -> dict[str, Any]:
    art = doc.get("artifact") or {}
    perf = art.get("performanceMetrics") or {}
    out: dict[str, Any] = {"platform": "vapi", "call_id": doc.get("id"), "ended_reason": doc.get("endedReason"),
                           "status": doc.get("status")}
    if isinstance(doc.get("cost"), (int, float)):
        out["cost"], out["currency"] = round(float(doc["cost"]), 4), "USD"
    lat = perf.get("turnLatencyAverage")
    if isinstance(lat, (int, float)):
        out["latency_ms"] = {"turn_avg": int(lat)}
        for k, name in (("modelLatencyAverage", "model_avg"), ("voiceLatencyAverage", "voice_avg"),
                        ("transcriberLatencyAverage", "transcriber_avg")):
            if isinstance(perf.get(k), (int, float)):
                out["latency_ms"][name] = int(perf[k])
    out["recording"] = bool(art.get("recordingUrl") or doc.get("recordingUrl"))
    analysis = doc.get("analysis") or {}
    if isinstance(analysis, Mapping):
        if analysis.get("successEvaluation") not in (None, ""):
            out["platform_success"] = str(analysis["successEvaluation"])[:200]
        if analysis.get("summary"):
            out["platform_summary"] = str(analysis["summary"])[:600]
    if doc.get("id"):
        out["link"] = f"https://dashboard.vapi.ai/calls/{doc['id']}"
    return {k: v for k, v in out.items() if v is not None}


class VapiConnector:
    mode = "voice"
    sample_rate = SAMPLE_RATE

    def __init__(self, profile: Mapping[str, Any], root: Any = None, tools: Any = None):
        self._profile = profile

    async def open(self, ctx: CallContext, recorder: Recorder) -> VapiSession:
        import websockets

        secrets = Secrets()
        api = make_api(self._profile, secrets)
        body: dict[str, Any] = {
            "assistantId": assistant_id(self._profile),
            "transport": {"provider": "vapi.websocket",
                          "audioFormat": {"format": "pcm_s16le", "container": "raw", "sampleRate": SAMPLE_RATE}},
        }
        variables = call_variables(ctx, self._profile)
        if variables:
            body["assistantOverrides"] = {"variableValues": variables}
        call = await api.request("POST", "call", body)
        if not isinstance(call, Mapping):
            raise PlatformError("Vapi did not create the call")
        url = str((call.get("transport") or {}).get("websocketCallUrl") or "")
        call_id = str(call.get("id") or "")
        if not url.startswith("wss://") and not url.startswith("ws://"):
            raise PlatformError("Vapi created the call but sent no WebSocket address for it "
                                "(is WebSocket transport enabled for this assistant?)")
        try:
            await asyncio.to_thread(netguard.check_url, url, netguard.WS_SCHEMES)
            ws = await asyncio.wait_for(websockets.connect(url, max_size=netguard.MAX_RESPONSE, ping_interval=20),
                                        CONNECT_TIMEOUT_S)
        except Exception as e:  # noqa: BLE001
            raise PlatformError(secrets.scrub(f"could not open Vapi's call socket: {type(e).__name__}: {e}")) from e
        return VapiSession(ws, recorder, api, call_id, self._profile, secrets)


async def check(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Connect's "Test connection": is the key accepted, and does the assistant exist? Places no call."""
    secrets = Secrets()
    api = make_api(profile, secrets)
    aid = assistant_id(profile)
    doc = await api.request("GET", f"assistant/{aid}")
    name = (doc or {}).get("name") if isinstance(doc, Mapping) else None
    tools = (((doc or {}).get("model") or {}).get("tools") or []) if isinstance(doc, Mapping) else []
    names = [str((t.get("function") or {}).get("name") or t.get("type")) for t in tools if isinstance(t, Mapping)]
    return {"ok": True, "message": f"Vapi accepted the key and found the assistant{f' {name!r}' if name else ''}.",
            "details": [f"Assistant id: {aid}"] + ([f"Its tools: {', '.join(names)}"] if names else [])}
