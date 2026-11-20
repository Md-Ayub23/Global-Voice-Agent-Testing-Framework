"""ElevenLabs Agents: a test call over the Agents WebSocket
(https://elevenlabs.io/docs/eleven-agents/api-reference/eleven-agents/websocket).

  GET {api}/v1/convai/conversation/get-signed-url?agent_id=...   xi-api-key: <key>   -> {"signed_url"}
      (a public agent needs no key: wss://{api host}/v1/convai/conversation?agent_id=...)
  the socket
    Vocto -> agent  {"type": "conversation_initiation_client_data", "dynamic_variables": {...}}   first
                    {"user_audio_chunk": "<base64 audio in the agent's input format>"}           every 20 ms
                    {"type": "pong", "event_id"}                                                 for each ping
                    {"type": "client_tool_result", "tool_call_id", "result", "is_error"}         see below
    agent -> Vocto  conversation_initiation_metadata (conversation_id, audio formats), audio (base64),
                    agent_response, agent_response_correction, user_transcript, interruption, ping,
                    client_tool_call, agent_tool_response; the socket closes when the agent ends the call
  GET {api}/v1/convai/conversations/{id}   after the call: transcript with tool_calls / tool_results,
                    metadata (call_duration_secs, cost, termination_reason), analysis

Client tools (tools the agent asks the caller's app to run) are answered by Vocto's tool proxy when the
agent's profile lists a tool of that name: Vocto forwards it to the tool's upstream and can inject faults,
exactly as for an agent in this repo. A client tool Vocto doesn't know is answered with an error.

Profile: connection {type: platform, platform: elevenlabs, target: <agent id>,
                     options: {api_key: "${ELEVENLABS_API_KEY}" (optional for a public agent), variables: {...}}}
"""

from __future__ import annotations

import asyncio
import base64
import json
import urllib.parse
from typing import Any, Mapping

import numpy as np

from ... import netguard
from ...audio.pcm import resample
from ...audio.recorder import Recorder
from ...contracts.models import CallContext
from .base import (SAMPLE_RATE, PlatformAPI, PlatformError, PlatformReport, PlatformSession, Secrets, api_base,
                   call_variables, pcm_from_format, pcm_to_format, poll, report_wait, secret_option)

DEFAULT_API = "https://api.elevenlabs.io"
CONNECT_TIMEOUT_S = 15
DONE = {"done", "failed"}


def make_api(profile: Mapping[str, Any], secrets: Secrets, required: bool = False) -> PlatformAPI | None:
    key = secret_option(profile, "api_key", secrets, required=required, label="ElevenLabs API key")
    if not key:
        return None
    return PlatformAPI("ElevenLabs", api_base("elevenlabs", DEFAULT_API), {"xi-api-key": key}, secrets)


def agent_id(profile: Mapping[str, Any]) -> str:
    aid = str(profile.get("connection", {}).get("target") or "").strip()
    if not aid:
        raise PlatformError("the ElevenLabs agent id is not set (Connect › Agent id)")
    return aid


def public_ws_url(aid: str) -> str:
    base = api_base("elevenlabs", DEFAULT_API)
    ws = "wss://" + base[len("https://"):] if base.startswith("https://") else "ws://" + base.split("://", 1)[-1]
    return f"{ws}/v1/convai/conversation?" + urllib.parse.urlencode({"agent_id": aid})


class ElevenLabsSession(PlatformSession):
    platform = "elevenlabs"

    def __init__(self, ws: Any, recorder: Recorder, api: PlatformAPI | None, profile: Mapping[str, Any],
                 secrets: Secrets, tools: Any):
        super().__init__(recorder, secrets)
        self._ws, self._api, self._profile, self._tools = ws, api, profile, tools
        self.out_format, self.in_format = "pcm_16000", "pcm_16000"
        self._last_interrupt = 0
        self._send_lock = asyncio.Lock()
        self._tool_tasks: set[asyncio.Task] = set()
        self.started = asyncio.Event()
        self.call_meta = {"platform": "elevenlabs"}
        self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        import websockets

        try:
            async for msg in self._ws:
                if isinstance(msg, (bytes, bytearray)):
                    self.error("unexpected binary frame")
                    continue
                try:
                    data = json.loads(msg)
                except ValueError:
                    self.error(f"not JSON: {str(msg)[:80]!r}")
                    continue
                if isinstance(data, Mapping):
                    await self._on(data)
        except websockets.ConnectionClosed as e:
            reason = e.rcvd.reason if e.rcvd and e.rcvd.reason else ""
            self.ended(f"the agent ended the conversation ({reason})" if reason else "the agent ended the conversation")
        except Exception as e:  # noqa: BLE001 - any read failure ends the call
            self.ended(f"connection error: {type(e).__name__}: {e}")
        finally:
            self.started.set()
            self.ended("the agent ended the conversation")

    async def _on(self, m: Mapping[str, Any]) -> None:
        kind = m.get("type")
        if kind == "conversation_initiation_metadata":
            ev = m.get("conversation_initiation_metadata_event") or {}
            self.call_id = str(ev.get("conversation_id") or "") or None
            self.out_format = str(ev.get("agent_output_audio_format") or self.out_format)
            self.in_format = str(ev.get("user_input_audio_format") or self.in_format)
            self.call_meta = {"platform": "elevenlabs", "call_id": self.call_id}
            self.started.set()
        elif kind == "audio":
            ev = m.get("audio_event") or {}
            try:
                if int(ev.get("event_id") or 0) <= self._last_interrupt:
                    return                          # audio of an answer the caller already cut off
                x, sr = pcm_from_format(base64.b64decode(ev.get("audio_base_64") or ""), self.out_format)
            except (ValueError, TypeError):
                self.error("audio that is not base64")
                return
            self.play(resample(x, sr, self.rec.sr) if sr != self.rec.sr else x)
        elif kind == "interruption":
            ev = m.get("interruption_event") or {}
            try:
                self._last_interrupt = max(self._last_interrupt, int(ev.get("event_id") or 0))
            except (TypeError, ValueError):
                pass
            self.rec.clear()
        elif kind == "agent_response":
            text = str((m.get("agent_response_event") or {}).get("agent_response") or "").strip()
            if text:
                self.transcripts.append((self.rec.now_sample(), text))
        elif kind == "agent_response_correction":
            ev = m.get("agent_response_correction_event") or {}
            old = str(ev.get("original_agent_response") or "").strip()
            new = str(ev.get("corrected_agent_response") or "").strip()
            for i in range(len(self.transcripts) - 1, -1, -1):     # the agent was cut off: keep what it really said
                if self.transcripts[i][1] == old:
                    self.transcripts[i] = (self.transcripts[i][0], new)
                    break
        elif kind == "user_transcript":
            text = str((m.get("user_transcription_event") or {}).get("user_transcript") or "").strip()
            if text:
                self.heard.append((self.rec.now_sample(), text))
        elif kind == "ping":
            ev = m.get("ping_event") or {}
            await self._send({"type": "pong", "event_id": ev.get("event_id")})
        elif kind == "client_tool_call":
            tc = m.get("client_tool_call") or {}
            task = asyncio.create_task(self._client_tool(tc))
            self._tool_tasks.add(task)
            task.add_done_callback(self._tool_tasks.discard)
        elif kind in ("agent_tool_response", "agent_tool_request", "mcp_tool_call"):
            ev = m.get(kind) or next((v for v in m.values() if isinstance(v, Mapping)), {})
            tid = ev.get("tool_call_id") or ev.get("request_id")
            if tid:
                self.live_tools.setdefault(str(tid), self.now_ms())

    async def _client_tool(self, tc: Mapping[str, Any]) -> None:
        """The agent asks the caller's app to run a tool: Vocto's tool proxy runs it (with any planned fault)."""
        tid, name = str(tc.get("tool_call_id") or ""), str(tc.get("tool_name") or "")
        params = tc.get("parameters") if isinstance(tc.get("parameters"), Mapping) else {}
        self.handled_tools.add(tid)
        self.live_tools.setdefault(tid, self.now_ms())
        known = self._tools is not None and hasattr(self._tools, "knows") and self._tools.knows(name)
        if not known:
            self.error(f"the agent asked Vocto to run client tool {name!r}, which this agent's tools don't list")
            result, is_error = json.dumps({"error": f"no tool named {name}"}), True
        else:
            try:
                out = await asyncio.to_thread(self._tools.call_with, name, dict(params))
                body = out.get("body")
                result = body if isinstance(body, str) else json.dumps(body)
                is_error = not out.get("ok")
            except Exception as e:  # noqa: BLE001 - the tool's failure is the agent's to handle
                result, is_error = json.dumps({"error": f"{type(e).__name__}: {e}"}), True
        if tc.get("expects_response", True):
            await self._send({"type": "client_tool_result", "tool_call_id": tid, "result": result, "is_error": is_error})

    async def _send(self, msg: Mapping[str, Any]) -> None:
        import websockets

        if self.closed.is_set():
            return
        try:
            async with self._send_lock:
                await self._ws.send(json.dumps(msg))
        except websockets.ConnectionClosed:
            pass

    async def send_frame(self, x: np.ndarray) -> None:
        if self.closed.is_set():
            return
        chunk = pcm_to_format(x, self.rec.sr, self.in_format)
        await self._send({"user_audio_chunk": base64.b64encode(chunk).decode("ascii")})

    async def close(self) -> None:
        self._closing = True
        try:
            await self._ws.close()
        except Exception:  # noqa: BLE001
            pass
        for t in list(self._tool_tasks):
            t.cancel()
        self._reader.cancel()
        try:
            await self._reader
        except (asyncio.CancelledError, Exception):
            pass
        self.closed.set()

    async def fetch_report(self) -> PlatformReport:
        from ...monitor.intake import IntakeError, from_elevenlabs

        if not self.call_id:
            return PlatformReport("elevenlabs", None, error="ElevenLabs never said which conversation this was")
        if self._api is None:
            return PlatformReport("elevenlabs", self.call_id,
                                  error="no API key, so the conversation record (tool calls, cost) can't be read")
        doc = await poll(lambda: self._api.request("GET", f"v1/convai/conversations/{self.call_id}"),
                         lambda d: isinstance(d, Mapping) and d.get("status") in DONE,
                         report_wait(self._profile))
        if not isinstance(doc, Mapping):
            return PlatformReport("elevenlabs", self.call_id, error="ElevenLabs returned no conversation record")
        summary = summarize(doc)
        try:
            call = from_elevenlabs(doc)
        except IntakeError as e:
            return PlatformReport("elevenlabs", self.call_id, summary=summary, error=f"the record has no turns yet ({e})")
        if doc.get("status") not in DONE:
            summary["incomplete"] = True
        return PlatformReport("elevenlabs", self.call_id, call=call, summary=summary)


def summarize(doc: Mapping[str, Any]) -> dict[str, Any]:
    meta = doc.get("metadata") or {}
    out: dict[str, Any] = {"platform": "elevenlabs", "call_id": doc.get("conversation_id"), "status": doc.get("status"),
                           "ended_reason": meta.get("termination_reason") or None}
    if isinstance(meta.get("cost_fiat"), (int, float)):
        out["cost"], out["currency"] = round(float(meta["cost_fiat"]), 4), "USD"
    if isinstance(meta.get("cost"), (int, float)):
        out["credits"] = int(meta["cost"])
    if isinstance(meta.get("call_duration_secs"), (int, float)):
        out["duration_s"] = meta["call_duration_secs"]
    out["recording"] = bool(doc.get("has_audio"))
    analysis = doc.get("analysis") or {}
    if isinstance(analysis, Mapping):
        if analysis.get("call_successful") not in (None, ""):
            out["platform_success"] = str(analysis["call_successful"])
        if analysis.get("transcript_summary"):
            out["platform_summary"] = str(analysis["transcript_summary"])[:600]
    if doc.get("conversation_id"):
        out["link"] = f"https://elevenlabs.io/app/agents/history/{doc['conversation_id']}"
    return {k: v for k, v in out.items() if v is not None}


class ElevenLabsConnector:
    mode = "voice"
    sample_rate = SAMPLE_RATE

    def __init__(self, profile: Mapping[str, Any], root: Any = None, tools: Any = None):
        self._profile, self._tools = profile, tools

    async def open(self, ctx: CallContext, recorder: Recorder) -> ElevenLabsSession:
        import websockets

        secrets = Secrets()
        api = make_api(self._profile, secrets)
        aid = agent_id(self._profile)
        if api is not None:
            doc = await api.request("GET", "v1/convai/conversation/get-signed-url", query={"agent_id": aid})
            url = str((doc or {}).get("signed_url") or "") if isinstance(doc, Mapping) else ""
            if not url:
                raise PlatformError("ElevenLabs gave no signed address for the conversation")
            parsed = urllib.parse.urlsplit(url)
            for k, v in urllib.parse.parse_qsl(parsed.query):
                if k != "agent_id":
                    secrets.add(v)                  # the signature in the address is a secret too
        else:
            url = public_ws_url(aid)
        try:
            await asyncio.to_thread(netguard.check_url, url, netguard.WS_SCHEMES)
            ws = await asyncio.wait_for(websockets.connect(url, max_size=netguard.MAX_RESPONSE, ping_interval=20),
                                        CONNECT_TIMEOUT_S)
        except Exception as e:  # noqa: BLE001
            raise PlatformError(secrets.scrub(f"could not open the ElevenLabs conversation: {type(e).__name__}: {e}")) from e
        init: dict[str, Any] = {"type": "conversation_initiation_client_data"}
        variables = call_variables(ctx, self._profile)
        if variables:
            init["dynamic_variables"] = variables
        await ws.send(json.dumps(init))
        session = ElevenLabsSession(ws, recorder, api, self._profile, secrets, self._tools)
        try:
            await asyncio.wait_for(session.started.wait(), CONNECT_TIMEOUT_S)
        except asyncio.TimeoutError:
            await session.close()
            raise PlatformError("ElevenLabs opened the socket but never started the conversation") from None
        if session.closed.is_set() and not session.call_id:
            await session.close()
            raise PlatformError(f"ElevenLabs refused the conversation: {session.close_reason}")
        return session


async def check(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Is the key accepted, and does the agent exist? Places no call."""
    secrets = Secrets()
    aid = agent_id(profile)
    api = make_api(profile, secrets)
    if api is None:
        return {"ok": True, "message": "No API key: Vocto will talk to the agent as a public agent. Without a key "
                "it can't read the conversation record (tool calls, cost, why it ended).",
                "details": [f"Agent id: {aid}"]}
    doc = await api.request("GET", f"v1/convai/agents/{aid}")
    name = doc.get("name") if isinstance(doc, Mapping) else None
    return {"ok": True, "message": f"ElevenLabs accepted the key and found the agent{f' {name!r}' if name else ''}.",
            "details": [f"Agent id: {aid}"]}
