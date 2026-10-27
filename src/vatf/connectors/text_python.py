"""text_python connector: imports a Python agent and talks to it in plain text.

The profile's connection.target is "module:callable". The callable is called as

    build_agent(tools=<tool proxy>, session={...}, options=connection.options)

and must return an object with:

    start() -> str        the agent's first words (greeting)
    reply(text) -> str    the agent's answer to one caller turn
    ended: bool           (optional) True once the agent has hung up

session holds "now" (the scenario's fixed time) and, when the profile lists
session_metadata, "test_id". Tools are called as tools.call(name, **args) and
return {"ok": bool, "status": int, "body": ...}.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Mapping

from ..contracts.connector import AgentOutput
from ..contracts.models import AudioClip, CallContext
from . import ConnectError


class TextPythonSession:
    def __init__(self, agent: Any, clock: Callable[[], int]):
        self._agent = agent
        self._clock = clock
        self._queue: asyncio.Queue[AgentOutput] = asyncio.Queue()

    async def start(self) -> None:
        greeting = await asyncio.to_thread(self._agent.start)
        await self._queue.put(AgentOutput("speech", self._clock(), text=greeting or ""))

    async def send_text(self, text: str) -> None:
        # An exception here is the agent crashing mid-call; the orchestrator records it.
        answer = await asyncio.to_thread(self._agent.reply, text)
        await self._queue.put(AgentOutput("speech", self._clock(), text=answer or ""))
        if getattr(self._agent, "ended", False):
            await self._queue.put(AgentOutput("closed", self._clock()))

    async def send_audio(self, clip: AudioClip) -> None:
        raise NotImplementedError("text_python is a text-only connector")

    async def outputs(self) -> AsyncIterator[AgentOutput]:
        while True:
            yield await self._queue.get()

    async def receive(self) -> AgentOutput:
        """The next output (text mode is turn-based, so it is always ready after send_text)."""
        return await self._queue.get()

    def has_output(self) -> bool:
        return not self._queue.empty()

    async def close(self) -> None:
        close = getattr(self._agent, "close", None)
        if callable(close):
            await asyncio.to_thread(close)


class TextPythonConnector:
    mode = "text"

    def __init__(self, profile: Mapping[str, Any], root: Path, tools: Any):
        self._profile = profile
        self._root = root
        self._tools = tools

    async def open(self, ctx: CallContext, clock: Callable[[], int] = lambda: 0) -> TextPythonSession:
        conn = self._profile["connection"]
        target = conn.get("target", "")
        module_name, _, func_name = target.partition(":")
        if not module_name or not func_name:
            raise ConnectError(f"connection.target must look like module:function, got {target!r}")
        if str(self._root) not in sys.path:
            sys.path.insert(0, str(self._root))
        session_meta = {"now": ctx.scenario.get("setup", {}).get("now")}
        if "session_metadata" in self._profile.get("test_id", {}).get("propagation", []):
            session_meta["test_id"] = ctx.test_id
        try:
            build = getattr(importlib.import_module(module_name), func_name)
            agent = await asyncio.to_thread(
                lambda: build(tools=self._tools, session=session_meta, options=dict(conn.get("options", {}))))
            session = TextPythonSession(agent, clock)
            await session.start()
        except Exception as e:
            raise ConnectError(f"could not start agent {target}: {type(e).__name__}: {e}") from e
        return session
