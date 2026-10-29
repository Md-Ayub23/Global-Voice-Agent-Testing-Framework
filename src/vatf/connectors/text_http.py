"""text_http connector: talks to an agent that runs somewhere else, in plain text over HTTP.

Cheap runs against a real agent: no speech models, no audio, the same checks. The agent (or a
small adapter in front of it) answers JSON POSTs to connection.target (docs/text-http-protocol.md):

  {"type": "start",   "session_id": "...", "metadata": {"now": ..., "test_id": ...}}
  {"type": "message", "session_id": "...", "text": "what the caller said"}
  {"type": "stop",    "session_id": "..."}

and replies {"reply": "the agent's words", "end": false} to start and message ("end": true when the
agent hangs up). connection.options:

  headers      extra request headers, e.g. {Authorization: "Bearer ${ACME_TOKEN}"} (names, not secrets)
  reply_field  where the agent's words are in the answer (default "reply"; dots for nesting, "data.text")
  end_field    where the hang-up flag is (default "end")
  timeout_s    how long to wait for one answer (default 60)
"""

from __future__ import annotations

import asyncio
import json
import secrets
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Mapping

from ..contracts.connector import AgentOutput
from ..contracts.models import AudioClip, CallContext
from ..util import MissingSecret, expand_env
from . import ConnectError


def _pick(doc: Any, path: str) -> Any:
    for part in path.split("."):
        if not isinstance(doc, dict):
            return None
        doc = doc.get(part)
    return doc


def post_json(url: str, body: Mapping[str, Any], headers: Mapping[str, str], timeout_s: float) -> Any:
    """One JSON POST. Raises ConnectError with a plain message for anything but a 2xx JSON answer."""
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:200]
        raise ConnectError(f"{url} answered HTTP {e.code}: {detail}") from e
    except (urllib.error.URLError, OSError) as e:
        raise ConnectError(f"could not reach {url}: {getattr(e, 'reason', e)}") from e
    try:
        return json.loads(raw) if raw.strip() else {}
    except ValueError as e:
        raise ConnectError(f"{url} did not answer JSON: {raw[:120]!r}") from e


class TextHttpSession:
    def __init__(self, url: str, opts: Mapping[str, Any], headers: Mapping[str, str], session_id: str,
                 clock: Callable[[], int]):
        self._url = url
        self._headers = dict(headers)
        self._reply_field = str(opts.get("reply_field", "reply"))
        self._end_field = str(opts.get("end_field", "end"))
        self._timeout = float(opts.get("timeout_s", 60))
        self.session_id = session_id
        self._clock = clock
        self._queue: asyncio.Queue[AgentOutput] = asyncio.Queue()
        self._ended = False

    async def _post(self, body: Mapping[str, Any]) -> Any:
        return await asyncio.to_thread(post_json, self._url, body, self._headers, self._timeout)

    async def _answer(self, doc: Any) -> None:
        reply = _pick(doc, self._reply_field)
        if reply is not None and not isinstance(reply, str):
            raise ConnectError(f"the agent's answer has no text at {self._reply_field!r}: {str(doc)[:120]}")
        await self._queue.put(AgentOutput("speech", self._clock(), text=reply or ""))
        if _pick(doc, self._end_field) is True:
            self._ended = True
            await self._queue.put(AgentOutput("closed", self._clock()))

    async def start(self, metadata: Mapping[str, Any]) -> None:
        doc = await self._post({"type": "start", "session_id": self.session_id, "metadata": dict(metadata)})
        await self._answer(doc)

    async def send_text(self, text: str) -> None:
        # An HTTP error here is the agent failing mid-call; the orchestrator records it.
        doc = await self._post({"type": "message", "session_id": self.session_id, "text": text})
        await self._answer(doc)

    async def send_audio(self, clip: AudioClip) -> None:
        raise NotImplementedError("text_http is a text-only connector")

    async def outputs(self) -> AsyncIterator[AgentOutput]:
        while True:
            yield await self._queue.get()

    async def receive(self) -> AgentOutput:
        return await self._queue.get()

    def has_output(self) -> bool:
        return not self._queue.empty()

    async def close(self) -> None:
        if self._ended:
            return
        self._ended = True
        try:
            await self._post({"type": "stop", "session_id": self.session_id})
        except ConnectError:
            pass  # the call is over either way


class TextHttpConnector:
    mode = "text"

    def __init__(self, profile: Mapping[str, Any], root: Path, tools: Any):
        self._profile = profile

    def _setup(self) -> tuple[str, dict[str, Any], dict[str, str]]:
        conn = self._profile["connection"]
        url = conn.get("target", "")
        if not url.startswith(("http://", "https://")):
            raise ConnectError(f"connection.target must be an http:// or https:// URL, got {url!r}")
        opts = dict(conn.get("options", {}))
        try:
            headers = {str(k): str(v) for k, v in expand_env(opts.get("headers") or {}).items()}
        except MissingSecret as e:
            raise ConnectError(str(e)) from e
        return url, opts, headers

    async def open(self, ctx: CallContext, clock: Callable[[], int] = lambda: 0) -> TextHttpSession:
        url, opts, headers = self._setup()
        meta: dict[str, Any] = {"now": ctx.scenario.get("setup", {}).get("now")}
        if "session_metadata" in self._profile.get("test_id", {}).get("propagation", []):
            meta["test_id"] = ctx.test_id
        session = TextHttpSession(url, opts, headers, f"s-{secrets.token_hex(6)}", clock)
        await session.start(meta)
        return session
