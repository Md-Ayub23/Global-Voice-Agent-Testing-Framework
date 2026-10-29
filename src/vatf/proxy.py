"""Tool proxy (contracts/observers.py ToolProxy): sits between the agent and its tool backends.

The agent calls proxy.call(tool, **args) instead of the real service. The
proxy records a tool_request and a tool_response event for every call, adds
the test id header, injects the scenario's faults, and forwards the request
to the tool's upstream:

  python:package.module:function   in-process handler, returns (status, body)
  http://... or https://...        JSON POST (or GET with the arguments as the query string when the
                                   tool says method: GET), plus the tool's own headers
                                   (secrets as ${NAME}, read from the .env file)

Agents that run in-process call proxy.call() directly. Agents that run somewhere else send their
tool calls to the tool gateway (gateway.py), which calls proxy.call() for them.

In text mode the delays of "slow" and "timeout" faults are recorded, not
waited out, because text-mode timing never gates the release anyway. In voice
mode (real_delays=True) they are really waited out, so the caller hears the
agent go quiet exactly as it would on a slow day.
"""

from __future__ import annotations

import importlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Mapping, Sequence

from .contracts.models import CallContext
from .contracts.observers import EventSink
from .util import expand_env

DEFAULT_TIMEOUT_MS = 10_000


class InProcessToolProxy:
    def __init__(self, profile: Mapping[str, Any], faults: Sequence[Mapping[str, Any]] = (),
                 real_delays: bool = False):
        self._real_delays = real_delays
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
        return self.call_with(tool, args)

    def call_with(self, tool: str, args: Mapping[str, Any]) -> dict[str, Any]:
        """call() with the arguments as one mapping (any names, even "tool"); the gateway uses it."""
        args = dict(args)
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
            self._wait(fault.get("delay_ms", DEFAULT_TIMEOUT_MS) - ms)
            return self._respond(call_id, tool, 504, {"error": "timeout"},
                                 fault.get("delay_ms", DEFAULT_TIMEOUT_MS), reached)
        if kind == "http_error":
            code = fault.get("status", 500)
            return self._respond(call_id, tool, code, {"error": f"upstream returned {code}"}, ms, reached)
        if kind == "slow":
            self._wait(fault.get("delay_ms", 0))
            return self._respond(call_id, tool, status, body, ms + fault.get("delay_ms", 0), reached)
        # malformed_response: the call happened, but the agent gets garbage back.
        return self._respond(call_id, tool, 200, '{"appointment_id": 4', ms, reached, ok=False)

    def _wait(self, ms: float) -> None:
        # Runs in the agent's own thread (tools are called synchronously), so sleeping is right here.
        if self._real_delays and ms > 0:
            time.sleep(ms / 1000)

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
                spec = self._tools[tool]
                headers = {**{str(k): str(v) for k, v in expand_env(spec.get("headers") or {}).items()},
                           **self._headers()}
                status, body = _http_call(spec.get("method", "POST"), upstream, args, headers)
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


def _http_call(method: str, url: str, args: Mapping[str, Any], headers: Mapping[str, str]) -> tuple[int, Any]:
    if method == "GET":
        query = urllib.parse.urlencode({k: v if isinstance(v, str) else json.dumps(v) for k, v in args.items()})
        req = urllib.request.Request(f"{url}{'&' if '?' in url else '?'}{query}" if query else url,
                                     method="GET", headers=dict(headers))
    else:
        req = urllib.request.Request(url, data=json.dumps(args).encode("utf-8"), method=method,
                                     headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(raw) if raw else None
            except ValueError:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw
    except (urllib.error.URLError, OSError) as e:  # the API is down or unreachable: a 502, as a real gateway says
        return 502, {"error": f"could not reach {url}: {getattr(e, 'reason', e)}"}
