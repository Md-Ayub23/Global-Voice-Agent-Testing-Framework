"""Tool gateway (L5): the tool proxy's front door for agents that run outside vatf.

An agent in this repo calls the tool proxy directly. A client's agent runs somewhere else, so for
a test its tool calls are pointed at vatf instead of at the client's API:

    http://127.0.0.1:8790/tools/<tool name>      (tool_gateway.host / port in the profile)

The gateway hands each request to the tool proxy of the call in progress, which records it, adds
the test id header, injects the scenario's faults and forwards it to the tool's real upstream URL.
The agent gets the upstream's answer (or the injected fault) back. A JSON body is the tool's
arguments; for GET the query string is. Between calls it answers 503: no test call is running.

Calls run one at a time (limits.parallel_calls 1), so every request belongs to the call in progress.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlparse

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8790


class GatewayError(Exception):
    """The gateway could not start (e.g. the port is in use). A harness problem."""


def uses_gateway(profile: Mapping[str, Any]) -> bool:
    """Agents that vatf does not run itself reach their tools through the gateway."""
    conn = profile.get("connection", {})
    return bool(profile.get("tools")) and conn.get("type") != "text_python" and not conn.get("launch")


def gateway_address(profile: Mapping[str, Any]) -> tuple[str, int]:
    cfg = profile.get("tool_gateway") or {}
    return str(cfg.get("host", DEFAULT_HOST)), int(cfg.get("port", DEFAULT_PORT))


def tool_url(profile: Mapping[str, Any], tool: str) -> str:
    host, port = gateway_address(profile)
    return f"http://{host}:{port}/tools/{tool}"


class ToolGateway:
    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
        self.host, self.port = host, port
        self._proxy: Any = None
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> "ToolGateway":
        try:
            self._server = ThreadingHTTPServer((self.host, self.port), _handler(self))
        except OSError as e:
            raise GatewayError(f"the tool gateway could not listen on {self.host}:{self.port} ({e.strerror or e}). "
                               "Is another run going, or is something else using that port? "
                               "Change tool_gateway.port in the profile.") from e
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]   # port 0 = any free port (tests)
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.2},
                                        name="vatf-tool-gateway", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def attach(self, proxy: Any) -> None:
        with self._lock:
            self._proxy = proxy

    def detach(self) -> None:
        with self._lock:
            self._proxy = None

    @property
    def proxy(self) -> Any:
        with self._lock:
            return self._proxy


def _handler(gw: ToolGateway):
    class Handler(BaseHTTPRequestHandler):
        server_version = "vatf-tool-gateway"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        def _send(self, status: int, body: Any) -> None:
            raw = (body if isinstance(body, str) else json.dumps(body, ensure_ascii=False, default=str)).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8" if isinstance(body, str)
                             else "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _any(self) -> None:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n > 0 else b""
            url = urlparse(self.path)
            if url.path in ("/", "/health"):
                return self._send(200, {"ok": True, "call_in_progress": gw.proxy is not None})
            if not url.path.startswith("/tools/") or len(url.path) <= len("/tools/"):
                return self._send(404, {"error": "use /tools/<tool name>"})
            tool = url.path[len("/tools/"):].strip("/")
            args: dict[str, Any] = dict(parse_qsl(url.query))
            if raw.strip():
                try:
                    body = json.loads(raw.decode("utf-8"))
                except ValueError:
                    return self._send(400, {"error": "the request body must be JSON"})
                if isinstance(body, dict):
                    args.update(body)
                else:
                    args["body"] = body
            proxy = gw.proxy
            if proxy is None:
                return self._send(503, {"error": "no vatf test call is in progress"})
            try:
                result = proxy.call_with(tool, args)
            except Exception as e:  # noqa: BLE001 - the call ended under us (locked evidence) or similar
                return self._send(503, {"error": f"vatf could not take this call: {type(e).__name__}: {e}"})
            body = result.get("body")
            return self._send(int(result.get("status") or 500), body if body is not None else {})

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = lambda self: self._any()  # noqa: E731

    return Handler
