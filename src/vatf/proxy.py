"""Tool proxy (contracts/observers.py ToolProxy): sits between the agent and its tool backends.

The agent calls proxy.call(tool, **args) instead of the real service. The
proxy records a tool_request and a tool_response event for every call, adds
the test id header, injects the scenario's faults, and forwards the request
to the tool's upstream:

  python:package.module:function   in-process handler, returns (status, body)
  http://... or https://...        JSON POST

In text mode the delays of "slow" and "timeout" faults are recorded, not
waited out, because text-mode timing never gates the release anyway.
"""

from __future__ import annotations

import importlib
import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping, Sequence

from .contracts.models import CallContext
from .contracts.observers import EventSink

DEFAULT_TIMEOUT_MS = 10_000


class InProcessToolProxy:
    def __init__(self, profile: Mapping[str, Any], faults: Sequence[Mapping[str, Any]] = ()):
        self._tools = {t["name"]: t for t in profile.get("tools", [])}
        test_id_cfg = profile.get("test_id", {})
        self._propagation = test_id_cfg.get("propagation", ["proxy_header"])
        self._header = test_id_cfg.get("header_name", "X-Test-Id")
        self._faults = list(faults)
        self._handlers: dict[str, Callable[[dict, dict], tuple[int, Any]]] = {}
        self._ctx: CallContext | None = None
        self._sink: EventSink | None = None
        self._calls_per_tool: dict[str, int] = {}
        self._n = 0
        self.turn: int | None = None  # set by the orchestrator before each caller turn

    async def attach(self, ctx: CallContext, sink: EventSink) -> None:
        self._ctx, self._sink = ctx, sink

    async def detach(self) -> None:
        self._sink = None

    def endpoint_for(self, tool: str) -> str:
        return self._tools[tool].get("upstream", "")

    # The agent-facing method.
    def call(self, tool: str, **args: Any) -> dict[str, Any]:
        if self._sink is None:
            raise RuntimeError("tool proxy is not attached to a call")
        self._n += 1
        call_id = f"c{self._n}"
        self._sink.emit("tool_proxy", "tool_request", {"call_id": call_id, "tool": tool, "args": args},
                        turn=self.turn)
        n_for_tool = self._calls_per_tool[tool] = self._calls_per_tool.get(tool, 0) + 1

        if tool not in self._tools:
            return self._respond(call_id, tool, 404, {"error": f"unknown tool {tool}"}, 0, False)

        fault = next((f for f in self._faults if f["tool"] == tool
                      and f.get("on_call", n_for_tool) == n_for_tool), None)
        if fault is None:
            status, body, ms = self._forward(tool, args)
            return self._respond(call_id, tool, status, body, ms, True)

        self._sink.emit("tool_proxy", "fault_injected", {"call_id": call_id, "tool": tool, "fault": dict(fault)},
                        turn=self.turn)
        kind, forward = fault["type"], bool(fault.get("forward", False))
        reached = False
        status, body, ms = 0, None, 0
        if forward or kind in ("slow", "malformed_response"):
            status, body, ms = self._forward(tool, args)
            reached = True
        if kind == "timeout":
            return self._respond(call_id, tool, 504, {"error": "timeout"},
                                 fault.get("delay_ms", DEFAULT_TIMEOUT_MS), reached)
        if kind == "http_error":
            code = fault.get("status", 500)
            return self._respond(call_id, tool, code, {"error": f"upstream returned {code}"}, ms, reached)
        if kind == "slow":
            return self._respond(call_id, tool, status, body, ms + fault.get("delay_ms", 0), reached)
        # malformed_response: the call happened, but the agent gets garbage back.
        return self._respond(call_id, tool, 200, '{"appointment_id": 4', ms, reached, ok=False)

    def _respond(self, call_id: str, tool: str, status: int, body: Any, ms: int, reached: bool,
                 ok: bool | None = None) -> dict[str, Any]:
        ok = (200 <= status < 300) if ok is None else ok
        self._sink.emit("tool_proxy", "tool_response", {
            "call_id": call_id, "tool": tool, "ok": ok, "status": status, "body": body,
            "duration_ms": int(ms), "reached_backend": reached,
        }, turn=self.turn)
        return {"ok": ok, "status": status, "body": body}

    def _headers(self) -> dict[str, str]:
        if "proxy_header" in self._propagation and self._ctx:
            return {self._header: self._ctx.test_id}
        return {}

    def _forward(self, tool: str, args: dict) -> tuple[int, Any, int]:
        upstream = self.endpoint_for(tool)
        start = time.monotonic()
        try:
            if upstream.startswith("python:"):
                status, body = self._handler(upstream)(dict(args), self._headers())
            elif upstream.startswith(("http://", "https://")):
                status, body = _http_post(upstream, args, self._headers())
            else:
                status, body = 502, {"error": f"tool {tool} has no usable upstream ({upstream!r})"}
        except Exception as e:  # the upstream crashed: that is a 500 from the backend
            status, body = 500, {"error": f"{type(e).__name__}: {e}"}
        return status, body, int((time.monotonic() - start) * 1000)

    def _handler(self, upstream: str) -> Callable[[dict, dict], tuple[int, Any]]:
        if upstream not in self._handlers:
            module, _, func = upstream[len("python:"):].partition(":")
            self._handlers[upstream] = getattr(importlib.import_module(module), func)
        return self._handlers[upstream]


def _http_post(url: str, args: Mapping[str, Any], headers: Mapping[str, str]) -> tuple[int, Any]:
    req = urllib.request.Request(url, data=json.dumps(args).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw
