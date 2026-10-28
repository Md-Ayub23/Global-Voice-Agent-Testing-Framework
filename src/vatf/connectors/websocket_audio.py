"""websocket_audio connector: talks to a voice agent over a WebSocket, audio both ways.

The wire protocol (docs/voice-protocol.md):

  vatf -> agent   text   {"type": "start", "sample_rate": 16000, "encoding": "pcm16",
                          "metadata": {"now": ..., "test_id": ...}}       first message
                  binary 20 ms frames of PCM16 mono little-endian, sent in real time
                         for the whole call (speech, or silence / background noise)
                  text   {"type": "stop"}                                  the caller hung up
  agent -> vatf   binary PCM16 audio, any chunk size, may be sent faster than real time
                  text   {"type": "clear"}       drop audio not yet played (barge-in)
                  text   {"type": "transcript", "text": ...}   optional: the agent's own words
                  text   {"type": "end"}         the agent hung up (or it closes the socket)

connection.target is the ws:// URL. For an agent written in Python that should
be started by vatf for each call, connection.launch names "module:function";
it is called as launch(tools=<tool proxy>, options=connection.options) and
returns an object with .url and async close(). The clinic zoo agent works this way.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import sys
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from ..audio.pcm import from_pcm16, to_pcm16
from ..audio.recorder import Recorder
from ..contracts.models import CallContext
from . import ConnectError

CONNECT_TIMEOUT_S = 15


class WebSocketAudioSession:
    def __init__(self, ws: Any, recorder: Recorder, launched: Any = None):
        self._ws = ws
        self.rec = recorder
        self._launched = launched
        self.closed = asyncio.Event()
        self.closed_by_agent = False
        self.close_reason = ""
        self.transcripts: list[tuple[int, str]] = []    # (sample, text) the agent reported
        self.protocol_errors: list[str] = []
        self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        import websockets

        try:
            async for msg in self._ws:
                if isinstance(msg, (bytes, bytearray)):
                    if len(msg) % 2:
                        self.protocol_errors.append("odd-length audio frame")
                        msg = msg[:-1]
                    self.rec.agent_audio(from_pcm16(bytes(msg)))
                    continue
                try:
                    data = json.loads(msg)
                except ValueError:
                    self.protocol_errors.append(f"not JSON: {msg[:80]!r}")
                    continue
                kind = data.get("type")
                if kind == "clear":
                    self.rec.clear()
                elif kind == "transcript":
                    self.transcripts.append((self.rec.now_sample(), str(data.get("text", ""))))
                elif kind == "end":
                    self.closed_by_agent, self.close_reason = True, "the agent hung up"
                    break
        except websockets.ConnectionClosed as e:
            self.closed_by_agent, self.close_reason = True, f"the agent closed the connection ({e.rcvd})"
        except Exception as e:  # noqa: BLE001 - any read failure ends the call
            self.closed_by_agent, self.close_reason = True, f"connection error: {type(e).__name__}: {e}"
        finally:
            if not self.close_reason:
                self.closed_by_agent, self.close_reason = True, "the agent closed the connection"
            self.closed.set()

    async def send_frame(self, x: np.ndarray) -> None:
        import websockets

        if self.closed.is_set():
            return
        try:
            await self._ws.send(to_pcm16(x))
        except websockets.ConnectionClosed:
            pass  # the agent hung up; the reader records it, the line just goes quiet

    async def send_text(self, text: str) -> None:
        raise NotImplementedError("websocket_audio is a voice connector: the caller speaks")

    async def close(self) -> None:
        if not self.closed.is_set():
            try:
                await self._ws.send(json.dumps({"type": "stop"}))
            except Exception:
                pass
        try:
            await self._ws.close()
        except Exception:
            pass
        self._reader.cancel()
        try:
            await self._reader
        except (asyncio.CancelledError, Exception):
            pass
        if self._launched is not None:
            try:
                await self._launched.close()
            finally:
                self._launched = None


class WebSocketAudioConnector:
    mode = "voice"

    def __init__(self, profile: Mapping[str, Any], root: Path, tools: Any):
        self._profile = profile
        self._root = root
        self._tools = tools

    @property
    def sample_rate(self) -> int:
        return int(self._profile["connection"].get("audio", {}).get("sample_rate_hz", 16000))

    async def open(self, ctx: CallContext, recorder: Recorder) -> WebSocketAudioSession:
        import websockets

        conn = self._profile["connection"]
        if conn.get("audio", {}).get("encoding", "pcm16") != "pcm16":
            raise ConnectError("websocket_audio supports encoding pcm16 only")
        launched = None
        if conn.get("launch"):
            launched = await self._launch(conn)
            url = launched.url
        else:
            url = conn.get("target", "")
            if not url.startswith(("ws://", "wss://")):
                raise ConnectError(f"connection.target must be a ws:// or wss:// URL, got {url!r}")
        meta: dict[str, Any] = {"now": ctx.scenario.get("setup", {}).get("now")}
        if "session_metadata" in self._profile.get("test_id", {}).get("propagation", []):
            meta["test_id"] = ctx.test_id
        try:
            ws = await asyncio.wait_for(websockets.connect(url, max_size=None, ping_interval=None),
                                        CONNECT_TIMEOUT_S)
            await ws.send(json.dumps({"type": "start", "sample_rate": self.sample_rate, "encoding": "pcm16",
                                      "metadata": meta}))
        except Exception as e:
            if launched is not None:
                await launched.close()
            raise ConnectError(f"could not connect to {url}: {type(e).__name__}: {e}") from e
        return WebSocketAudioSession(ws, recorder, launched)

    async def _launch(self, conn: Mapping[str, Any]) -> Any:
        module_name, _, func_name = conn["launch"].partition(":")
        if not module_name or not func_name:
            raise ConnectError(f"connection.launch must look like module:function, got {conn['launch']!r}")
        if str(self._root) not in sys.path:
            sys.path.insert(0, str(self._root))
        try:
            fn = getattr(importlib.import_module(module_name), func_name)
            launched = fn(tools=self._tools, options=dict(conn.get("options", {})))
            if inspect.isawaitable(launched):
                launched = await launched
        except Exception as e:
            raise ConnectError(f"could not launch agent {conn['launch']}: {type(e).__name__}: {e}") from e
        return launched
