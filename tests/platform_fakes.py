"""Stand-ins for the hosted platforms in tests: no account, no network.

Each fake runs the zoo clinic agent (rules brain, test-tone "codec" speech) the way the platform would: the
agent's tools are run by the platform (ToolShim), not by Vocto, so Vocto only learns about them from the
platform's own record of the call, exactly as with a real Vapi, Retell or ElevenLabs agent.

  FakeVapi        REST (POST /call, GET /call/{id}, GET /assistant/{id}) + the call WebSocket (raw PCM16)
  FakeElevenLabs  REST (signed URL, GET conversation, GET agent) + the Agents WebSocket (JSON, base64 audio);
                  tools listed in client_tools are sent to Vocto as client tool calls
  FakeRoom        a RoomLink for Retell / LiveKit sessions (no WebRTC): bridges to the same zoo agent
"""

from __future__ import annotations

import asyncio
import base64
import importlib
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parent.parent
CLINIC_PROFILE = ROOT / "examples" / "clinic" / "profile.yaml"
AGENT_OPTIONS = {"brain": "rules", "speech": "codec", "bugs": ""}


class Loop:
    """An event loop in a thread, for the fake servers (the test's own loop runs Vocto)."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def run(self, coro: Any, timeout: float = 30) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)


class ToolShim:
    """The platform runs the agent's tools: the clinic's own Python handlers, with the test id the platform was
    given (so Vocto's database probes find the rows). Every call is recorded with its time on the call clock."""

    def __init__(self, test_id: str = "", client: Callable[[str, dict], dict] | None = None,
                 client_tools: tuple[str, ...] = ()):
        prof = yaml.safe_load(CLINIC_PROFILE.read_text(encoding="utf-8"))
        self.upstreams = {t["name"]: t["upstream"] for t in prof["tools"]}
        self.test_id = test_id
        self.records: list[dict[str, Any]] = []
        self.started = time.monotonic()
        self.client, self.client_tools = client, client_tools

    def call(self, tool: str, **args: Any) -> dict[str, Any]:
        return self.call_with(tool, args)

    def call_with(self, tool: str, args: dict) -> dict[str, Any]:
        rid = "call_" + uuid.uuid4().hex[:10]
        at = time.monotonic() - self.started
        if tool in self.client_tools and self.client is not None:
            out = self.client(tool, {**args, "_id": rid})
            ok, body = out.get("ok", True), out.get("body")
            self.records.append({"id": rid, "name": tool, "args": dict(args), "result": body, "ok": ok, "at": at,
                                 "client": True})
            return {"ok": ok, "status": 200 if ok else 500, "body": body}
        module, _, func = self.upstreams[tool][len("python:"):].partition(":")
        status, body = getattr(importlib.import_module(module), func)(dict(args), {"X-Test-Id": self.test_id})
        ok = 200 <= status < 300
        self.records.append({"id": rid, "name": tool, "args": dict(args), "result": body, "ok": ok, "at": at})
        return {"ok": ok, "status": status, "body": body}


async def launch_agent(shim: ToolShim) -> Any:
    import zoo.clinic.voice as voice

    return await voice.launch(tools=shim, options=dict(AGENT_OPTIONS))


class _Http(ThreadingHTTPServer):
    daemon_threads = True


def http_server(route: Callable[[str, str, dict, Any], tuple[int, Any]]) -> tuple[_Http, str]:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a: Any) -> None:
            pass

        def _go(self, method: str) -> None:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n)) if n else None
            status, out = route(method, self.path, {k.lower(): v for k, v in self.headers.items()}, body)
            raw = json.dumps(out).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            self._go("GET")

        def do_POST(self) -> None:
            self._go("POST")

    srv = _Http(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


class Call:
    def __init__(self, cid: str, variables: dict):
        self.id, self.variables = cid, variables
        self.shim = ToolShim(variables.get("vocto_test_id", ""))
        self.messages: list[dict[str, Any]] = []
        self.status, self.ended_reason = "queued", None
        self.agent_url = ""
        self.launched: Any = None
        self.reads = 0

    def t(self) -> float:
        return time.monotonic() - self.shim.started


# ---- Vapi --------------------------------------------------------------------------------------

class FakeVapi:
    KEY = "vapi-test-key-123456"

    def __init__(self, ready_after_reads: int = 1, cost: float = 0.0731):
        self.loop = Loop()
        self.calls: dict[str, Call] = {}
        self.ready_after_reads = ready_after_reads
        self.cost = cost
        self.requests: list[tuple[str, str, dict]] = []
        self.ws_server = self.loop.run(self._serve())
        self.ws_port = self.ws_server.sockets[0].getsockname()[1]
        self.http, self.base = http_server(self.route)

    async def _serve(self) -> Any:
        import websockets

        return await websockets.serve(self._handle, "127.0.0.1", 0, max_size=None, ping_interval=None)

    def close(self) -> None:
        self.http.shutdown()
        for c in self.calls.values():
            if c.launched is not None:
                try:
                    self.loop.run(c.launched.close(), 5)
                except Exception:
                    pass
        self.ws_server.close()
        self.loop.stop()

    def route(self, method: str, path: str, headers: dict, body: Any) -> tuple[int, Any]:
        self.requests.append((method, path, body or {}))
        if headers.get("authorization") != f"Bearer {self.KEY}":
            return 401, {"message": "Invalid Key. Hot tip, you may be using the private key instead of the public key, or vice versa."}
        if method == "POST" and path == "/call":
            if body.get("transport", {}).get("provider") != "vapi.websocket":
                return 400, {"message": "transport.provider must be vapi.websocket"}
            cid = "call-" + uuid.uuid4().hex[:8]
            call = Call(cid, (body.get("assistantOverrides") or {}).get("variableValues") or {})
            call.launched = self.loop.run(launch_agent(call.shim))
            call.agent_url = call.launched.url
            self.calls[cid] = call
            return 201, {"id": cid, "type": "vapi.websocketCall", "status": "queued", "assistantId": body["assistantId"],
                         "transport": {"provider": "vapi.websocket",
                                       "websocketCallUrl": f"ws://127.0.0.1:{self.ws_port}/{cid}/transport"}}
        if method == "GET" and path.startswith("/call/"):
            call = self.calls.get(path.split("/")[2])
            if call is None:
                return 404, {"message": "call not found"}
            call.reads += 1
            done = call.status == "ended" and call.reads >= self.ready_after_reads
            return 200, self.record(call, done)
        if method == "GET" and path.startswith("/assistant/"):
            return 200, {"id": path.split("/")[2], "name": "Riverside test assistant",
                         "model": {"tools": [{"type": "function", "function": {"name": "book_appointment"}}]}}
        return 404, {"message": "no route"}

    def record(self, call: Call, done: bool) -> dict[str, Any]:
        msgs = sorted(call.messages, key=lambda m: m["secondsFromStart"])
        for r in call.shim.records:
            msgs.append({"role": "tool_calls", "secondsFromStart": r["at"],
                         "toolCalls": [{"id": r["id"], "type": "function",
                                        "function": {"name": r["name"], "arguments": json.dumps(r["args"])}}]})
            msgs.append({"role": "tool_call_result", "secondsFromStart": r["at"] + 0.05, "toolCallId": r["id"],
                         "name": r["name"], "result": json.dumps(r["result"])})
        msgs.sort(key=lambda m: m["secondsFromStart"])
        doc: dict[str, Any] = {"id": call.id, "status": "ended" if done else "in-progress",
                               "endedReason": call.ended_reason if done else None}
        if done:
            doc.update(cost=self.cost, artifact={"messages": msgs, "performanceMetrics": {"turnLatencyAverage": 812}},
                       analysis={"summary": "The caller booked a check-up.", "successEvaluation": "true"})
        return doc

    async def _handle(self, ws: Any) -> None:
        import websockets

        cid = ws.request.path.strip("/").split("/")[0]
        call = self.calls[cid]
        call.status = "in-progress"
        call.shim.started = time.monotonic()
        agent = await websockets.connect(call.agent_url, max_size=None)
        await agent.send(json.dumps({"type": "start", "sample_rate": 16000, "encoding": "pcm16",
                                     "metadata": {"test_id": call.variables.get("vocto_test_id")}}))

        async def up() -> None:
            async for m in ws:
                if isinstance(m, (bytes, bytearray)):
                    await agent.send(m)
                elif json.loads(m).get("type") in ("end-call", "hangup"):
                    call.ended_reason = call.ended_reason or "customer-ended-call"
                    await agent.send(json.dumps({"type": "stop"}))
                    return
            call.ended_reason = call.ended_reason or "customer-ended-call"

        async def down() -> None:
            async for m in agent:
                if isinstance(m, (bytes, bytearray)):
                    await ws.send(m)
                    continue
                d = json.loads(m)
                if d.get("type") == "transcript":
                    call.messages.append({"role": "bot", "message": d["text"], "secondsFromStart": call.t()})
                    await ws.send(json.dumps({"type": "transcript", "role": "assistant", "transcriptType": "final",
                                              "transcript": d["text"]}))
                elif d.get("type") == "heard":
                    call.messages.append({"role": "user", "message": d["text"], "secondsFromStart": call.t()})
                elif d.get("type") == "clear":
                    await ws.send(json.dumps({"type": "user-interrupted"}))
                elif d.get("type") == "end":
                    call.ended_reason = "assistant-ended-call"
                    return

        tasks = [asyncio.ensure_future(up()), asyncio.ensure_future(down())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except Exception:
            pass
        finally:
            for t in tasks:
                t.cancel()
            await agent.close()
            await ws.close()
            call.status = "ended"


# ---- ElevenLabs ----------------------------------------------------------------------------------

class FakeElevenLabs:
    KEY = "xi-test-key-abcdef"

    def __init__(self, client_tools: tuple[str, ...] = ()):
        self.loop = Loop()
        self.calls: dict[str, Call] = {}
        self.client_tools = client_tools
        self.ws_server = self.loop.run(self._serve())
        self.ws_port = self.ws_server.sockets[0].getsockname()[1]
        self.http, self.base = http_server(self.route)
        self.client_results: list[dict] = []
        self.inits: list[dict] = []

    async def _serve(self) -> Any:
        import websockets

        return await websockets.serve(self._handle, "127.0.0.1", 0, max_size=None, ping_interval=None)

    def close(self) -> None:
        self.http.shutdown()
        for c in self.calls.values():
            if c.launched is not None:
                try:
                    self.loop.run(c.launched.close(), 5)
                except Exception:
                    pass
        self.ws_server.close()
        self.loop.stop()

    def route(self, method: str, path: str, headers: dict, body: Any) -> tuple[int, Any]:
        if headers.get("xi-api-key") != self.KEY:
            return 401, {"detail": {"status": "invalid_api_key", "message": "Invalid API key"}}
        if path.startswith("/v1/convai/conversation/get-signed-url"):
            return 200, {"signed_url": f"ws://127.0.0.1:{self.ws_port}/v1/convai/conversation?agent_id=agent_1"
                                       f"&conversation_signature=sig-SECRET-42"}
        if path.startswith("/v1/convai/conversations/"):
            call = self.calls.get(path.rsplit("/", 1)[1])
            if call is None:
                return 404, {"detail": "not found"}
            return 200, self.record(call)
        if path.startswith("/v1/convai/agents/"):
            return 200, {"agent_id": path.rsplit("/", 1)[1], "name": "Riverside voice"}
        return 404, {"detail": "no route"}

    def record(self, call: Call) -> dict[str, Any]:
        rows = [dict(m) for m in sorted(call.messages, key=lambda m: m["time_in_call_secs"])]
        for r in call.shim.records:
            rows.append({"role": "agent", "message": None, "time_in_call_secs": int(r["at"]),
                         "tool_calls": [{"request_id": r["id"], "tool_name": r["name"],
                                         "params_as_json": json.dumps(r["args"]), "tool_has_been_called": True,
                                         "type": "client" if r.get("client") else "webhook"}],
                         "tool_results": [{"request_id": r["id"], "tool_name": r["name"],
                                           "result_value": json.dumps(r["result"]), "is_error": not r["ok"],
                                           "tool_has_been_called": True}]})
        rows.sort(key=lambda m: m["time_in_call_secs"])
        done = call.status == "ended"
        return {"agent_id": "agent_1", "conversation_id": call.id, "status": "done" if done else "processing",
                "transcript": rows, "has_audio": True,
                "metadata": {"start_time_unix_secs": 1, "call_duration_secs": int(call.t()), "cost": 412,
                             "termination_reason": call.ended_reason or ""},
                "analysis": {"call_successful": "success", "transcript_summary": "Booked."}}

    async def _handle(self, ws: Any) -> None:
        import websockets

        init = json.loads(await ws.recv())
        self.inits.append(init)
        variables = init.get("dynamic_variables") or {}
        cid = "conv_" + uuid.uuid4().hex[:8]
        call = Call(cid, variables)
        self.calls[cid] = call
        loop = asyncio.get_running_loop()
        pending: dict[str, asyncio.Future] = {}
        send_lock = asyncio.Lock()
        event_id = [0]

        async def send(msg: dict) -> None:
            async with send_lock:
                await ws.send(json.dumps(msg))

        def client(tool: str, args: dict) -> dict:
            rid = args.pop("_id")
            fut = loop.create_future()
            pending[rid] = fut
            asyncio.run_coroutine_threadsafe(send({"type": "client_tool_call", "client_tool_call": {
                "tool_name": tool, "tool_call_id": rid, "parameters": args, "expects_response": True}}), loop)
            res = asyncio.run_coroutine_threadsafe(asyncio.wait_for(fut, 20), loop).result(25)
            self.client_results.append(res)
            body = json.loads(res["result"]) if isinstance(res.get("result"), str) else res.get("result")
            return {"ok": not res.get("is_error"), "body": body}

        call.shim.client, call.shim.client_tools = client, self.client_tools
        call.launched = await launch_agent(call.shim)
        agent = await websockets.connect(call.launched.url, max_size=None)
        await agent.send(json.dumps({"type": "start", "sample_rate": 16000, "encoding": "pcm16",
                                     "metadata": {"test_id": variables.get("vocto_test_id")}}))
        call.shim.started = time.monotonic()
        call.status = "in-progress"
        await send({"type": "conversation_initiation_metadata", "conversation_initiation_metadata_event": {
            "conversation_id": cid, "agent_output_audio_format": "pcm_16000", "user_input_audio_format": "pcm_16000"}})
        await send({"type": "ping", "ping_event": {"event_id": 1, "ping_ms": 0}})

        async def up() -> None:
            async for m in ws:
                d = json.loads(m)
                if "user_audio_chunk" in d:
                    await agent.send(base64.b64decode(d["user_audio_chunk"]))
                elif d.get("type") == "client_tool_result":
                    fut = pending.pop(d["tool_call_id"], None)
                    if fut is not None and not fut.done():
                        fut.set_result(d)
            call.ended_reason = call.ended_reason or "Client disconnected"
            await agent.send(json.dumps({"type": "stop"}))

        async def down() -> None:
            async for m in agent:
                if isinstance(m, (bytes, bytearray)):
                    event_id[0] += 1
                    await send({"type": "audio", "audio_event": {"event_id": event_id[0],
                                                                 "audio_base_64": base64.b64encode(m).decode()}})
                    continue
                d = json.loads(m)
                if d.get("type") == "transcript":
                    call.messages.append({"role": "agent", "message": d["text"], "time_in_call_secs": int(call.t())})
                    await send({"type": "agent_response", "agent_response_event": {"agent_response": d["text"]}})
                elif d.get("type") == "heard":
                    call.messages.append({"role": "user", "message": d["text"], "time_in_call_secs": int(call.t())})
                    await send({"type": "user_transcript", "user_transcription_event": {"user_transcript": d["text"]}})
                elif d.get("type") == "clear":
                    await send({"type": "interruption", "interruption_event": {"event_id": event_id[0]}})
                elif d.get("type") == "end":
                    call.ended_reason = "end_call tool was called."
                    return

        tasks = [asyncio.ensure_future(up()), asyncio.ensure_future(down())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            await agent.close()
            await ws.close()
            call.status = "ended"


# ---- Retell / LiveKit rooms -------------------------------------------------------------------

class FakeRoom:
    """A RoomLink without WebRTC: the zoo agent behind it, audio and events as a room would deliver them.
    mode "retell": the agent's track is "agent_audio" and Retell's server sends transcript updates as data;
    mode "livekit": an agent participant, transcriptions as lk.transcription text streams, tool reports on
    vocto.tool when report_tools is set."""

    instances: list["FakeRoom"] = []

    def __init__(self, mode: str = "retell", report_tools: bool = False, shim_for: Callable[[str], ToolShim] | None = None):
        self.mode, self.report_tools = mode, report_tools
        self.identity = "vocto-caller-x"
        self.on_audio = lambda x, who: None
        self.on_data = lambda data, topic, who: None
        self.on_text = lambda text, who, attrs: None
        self.on_left = lambda who, agent: None
        self.on_closed = lambda reason: None
        self.pick_track = lambda name, who, agent: True
        self.url = self.token = ""
        self.shim: ToolShim | None = None
        self.shim_for = shim_for
        self._agent: Any = None
        self._launched: Any = None
        self._tasks: list[asyncio.Task] = []
        self.transcript: list[dict] = []
        self.closed = False
        FakeRoom.instances.append(self)

    async def connect(self, url: str, token: str) -> None:
        import websockets

        self.url, self.token = url, token
        self.shim = self.shim_for(token) if self.shim_for else ToolShim()
        if self.report_tools:
            real_call = self.shim.call_with

            def reporting(tool: str, args: dict) -> dict:
                out = real_call(tool, args)
                rec = self.shim.records[-1]
                payload = json.dumps({"id": rec["id"], "name": tool, "args": args, "result": out["body"], "ok": out["ok"]}).encode()
                loop.call_soon_threadsafe(self.on_data, payload, "vocto.tool", "agent-1")
                return out

            self.shim.call_with = reporting          # type: ignore[method-assign]
        loop = asyncio.get_running_loop()
        self._launched = await launch_agent(self.shim)
        self._agent = await websockets.connect(self._launched.url, max_size=None)
        await self._agent.send(json.dumps({"type": "start", "sample_rate": 16000, "encoding": "pcm16", "metadata": {}}))
        self.shim.started = time.monotonic()
        name = "agent_audio" if self.mode == "retell" else "microphone"
        self.pick_track(name, "agent-1", self.mode == "livekit")
        self._tasks.append(asyncio.ensure_future(self._down()))

    async def _down(self) -> None:
        try:
            async for m in self._agent:
                if isinstance(m, (bytes, bytearray)):
                    x = np.frombuffer(m, dtype="<i2").astype(np.float32) / 32768.0
                    for i in range(0, len(x), 160):        # 10 ms frames, like a WebRTC audio stream
                        self.on_audio(x[i:i + 160], "agent-1")
                    continue
                d = json.loads(m)
                if d.get("type") in ("transcript", "heard"):
                    role = "agent" if d["type"] == "transcript" else "user"
                    self.transcript.append({"role": role, "content": d["text"]})
                    if self.mode == "retell":
                        self.on_data(json.dumps({"event_type": "update", "transcript": self.transcript}).encode(),
                                     None, "server")
                    else:
                        who = "agent-1" if role == "agent" else self.identity
                        self.on_text(d["text"], who, {"lk.segment_id": f"seg{len(self.transcript)}",
                                                      "lk.transcribed_track_id": "TR_x"})
                elif d.get("type") == "end":
                    if self.mode == "retell":
                        self.on_data(json.dumps({"type": "status", "state": "ended"}).encode(), None, "server")
                    else:
                        self.on_left("agent-1", True)
                    return
        except Exception:
            pass

    async def push(self, x: np.ndarray) -> None:
        from vatf.audio.pcm import to_pcm16

        if self._agent is not None and not self.closed:
            try:
                await self._agent.send(to_pcm16(x))
            except Exception:
                pass

    async def close(self) -> None:
        self.closed = True
        for t in self._tasks:
            t.cancel()
        if self._agent is not None:
            try:
                await self._agent.send(json.dumps({"type": "stop"}))
                await self._agent.close()
            except Exception:
                pass
        if self._launched is not None:
            await self._launched.close()


class FakeRetellAPI:
    KEY = "retell-test-key-777777"

    def __init__(self) -> None:
        self.http, self.base = http_server(self.route)
        self.calls: dict[str, dict] = {}
        self.rooms: dict[str, FakeRoom] = {}

    def close(self) -> None:
        self.http.shutdown()

    def shim_for(self, token: str) -> ToolShim:
        call = next(c for c in self.calls.values() if c["access_token"] == token)
        shim = ToolShim((call.get("metadata") or {}).get("vocto_test_id", ""))
        call["shim"] = shim
        return shim

    def route(self, method: str, path: str, headers: dict, body: Any) -> tuple[int, Any]:
        if headers.get("authorization") != f"Bearer {self.KEY}":
            return 401, {"error_message": "Unauthorized"}
        if method == "POST" and path == "/v2/create-web-call":
            cid = "call_" + uuid.uuid4().hex[:10]
            self.calls[cid] = {"call_id": cid, "access_token": "eyJ-room-token-" + cid, "metadata": body.get("metadata"),
                               "vars": body.get("retell_llm_dynamic_variables")}
            return 201, {"call_id": cid, "access_token": self.calls[cid]["access_token"], "call_type": "web_call",
                         "agent_id": body["agent_id"], "call_status": "registered"}
        if method == "GET" and path.startswith("/v2/get-call/"):
            call = self.calls[path.rsplit("/", 1)[1]]
            shim: ToolShim | None = call.get("shim")
            room = next((r for r in FakeRoom.instances if r.shim is shim), None)
            rows: list[dict] = []
            t = 0.0
            for u in (room.transcript if room else []):
                rows.append({"role": u["role"], "content": u["content"],
                             "words": [{"word": "x", "start": t, "end": t + 0.5}]})
                t += 1.0
            for r in (shim.records if shim else []):
                rows.append({"role": "tool_call_invocation", "tool_call_id": r["id"], "name": r["name"],
                             "arguments": json.dumps(r["args"])})
                rows.append({"role": "tool_call_result", "tool_call_id": r["id"], "content": json.dumps(r["result"]),
                             "successful": r["ok"]})
            ended = room is not None and room.closed
            return 200, {"call_id": call["call_id"], "call_status": "ended" if ended else "ongoing",
                         "transcript_with_tool_calls": rows if ended else None,
                         "disconnection_reason": "user_hangup", "call_cost": {"combined_cost": 9.5},
                         "latency": {"e2e": {"p50": 900, "p90": 1400}},
                         "call_analysis": {"call_successful": True, "call_summary": "Booked."}}
        if method == "GET" and path.startswith("/get-agent/"):
            return 200, {"agent_id": path.rsplit("/", 1)[1], "agent_name": "Riverside Retell"}
        return 404, {"error_message": "no route"}


def platform_profile(platform: str, target: str, **options: Any) -> dict[str, Any]:
    """The clinic's profile, reached through a hosted platform."""
    from vatf.loader import load_project

    proj = load_project(ROOT / "examples" / "clinic", "profile.yaml")
    prof = json.loads(json.dumps(proj.profile))
    prof["connection"] = {"type": "platform", "platform": platform, "target": target, "options": options}
    prof.setdefault("thresholds", {}).update({"latency_p50_ms": 6000, "latency_p95_ms": 9000})
    return prof
