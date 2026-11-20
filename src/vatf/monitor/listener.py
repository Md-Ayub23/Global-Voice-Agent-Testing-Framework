"""Receives real calls from an agent platform's webhook (Phase 8).

POST /ingest/<agent key> with the call as the body (Vapi end-of-call report, Retell call webhook, Vocto
JSON, or a plain transcript). The dashboard runs one listener for every agent that switches it on,
while it is open; `python -m vatf monitor listen <project>` runs one for a single agent (on a server),
where /ingest also works.

Every request must carry the agent's token (monitor.intake.token, kept in the .env file as ${NAME}):
  Authorization: Bearer <token>   or   X-Vocto-Token: <token>   or   X-Vapi-Secret: <token>   or   ?token=<token>
It listens on 127.0.0.1 by default. To reach it from a platform in the cloud, put a tunnel in front of
it (for example cloudflared or ngrok) rather than opening the port to the internet.

The call is answered at once (202) and processed in the background: redacted, saved, scored, judged.
Messages that are not a finished call (Vapi status updates, Retell call_started) get 200 and are ignored.
"""

from __future__ import annotations

import hmac
import json
import queue
import sys
import threading
from http.server import BaseHTTPRequestHandler
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from ..ratelimit import RateLimiter
from ..util import MissingSecret, expand_env
from ..web.httpbase import BoundedHTTPServer
from .intake import IntakeError, parse

MAX_BODY = 10 * 1024 * 1024


def _env_int(name: str, default: int) -> int:
    import os

    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


def token_of(monitor: Any) -> str | None:
    """The agent's intake token, or None when it has none (then its webhook is refused)."""
    raw = monitor.settings["intake"].get("token")
    if not raw:
        return None
    try:
        value = expand_env(raw)
    except MissingSecret:
        return None
    return value or None


def offered_tokens(headers: Any, query: str) -> list[str]:
    """Every token the request offers, from each place a platform may put it.

    A platform can send its own Authorization header (Basic auth, or a bearer token of its own) while the
    Vocto token sits in the URL as ?token=, so no single place wins: the call is let in when any of them
    matches."""
    found: list[str] = []
    auth = (headers.get("Authorization") or "").strip()
    if auth:
        scheme, _, rest = auth.partition(" ")
        found.append(rest.strip() if scheme.lower() == "bearer" and rest.strip() else auth)
    for name in ("X-Vocto-Token", "X-Vapi-Secret"):
        value = (headers.get(name) or "").strip()
        if value:
            found.append(value)
    found += [t for t in parse_qs(query).get("token", []) if t]
    return found


def token_matches(want: str, headers: Any, query: str) -> bool:
    """True when any offered token equals the agent's token (each compared in constant time)."""
    ok = False
    for given in offered_tokens(headers, query):
        ok |= hmac.compare_digest(given.encode(), want.encode())
    return ok


class Listener:
    """resolve(key) -> a Monitor for that agent key, or None. Calls are processed by one worker thread."""

    def __init__(self, resolve: Callable[[str], Any], host: str = "127.0.0.1", port: int = 8791,
                 judge: bool = True, log: Callable[[str], None] | None = None):
        self.resolve, self.host, self.port, self.judge = resolve, host, port, judge
        self.log = log or (lambda m: print(m, file=sys.stderr))
        # Calls wait here to be processed; when the line is full the platform is told to retry (503).
        self.jobs: queue.Queue = queue.Queue(maxsize=_env_int("VOCTO_INTAKE_QUEUE", 1000))
        self.limit = RateLimiter(_env_int("VOCTO_INTAKE_PER_MINUTE", 600), burst=_env_int("VOCTO_INTAKE_BURST", 120))
        self.received = 0
        self.last_error: str | None = None
        self.httpd: BoundedHTTPServer | None = None

    def start(self) -> "Listener":
        self.httpd = BoundedHTTPServer((self.host, self.port), self._handler())
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.3}, daemon=True).start()
        threading.Thread(target=self._work, daemon=True).start()
        return self

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        try:
            self.jobs.put_nowait(None)
        except queue.Full:
            pass

    def _work(self) -> None:
        while True:
            job = self.jobs.get()
            if job is None:
                return
            monitor, body, name = job
            try:
                out = monitor.ingest(body, name, via="webhook")
                self.log(f"real call: {len(out['added'])} added, {len(out['duplicates'])} already had"
                         + (f", problems: {'; '.join(out['problems'])}" if out["problems"] else ""))
                if self.judge and any(True for _ in out["added"]):
                    monitor.judge_pending()
                    monitor.check_alerts()
            except Exception as e:  # noqa: BLE001 - keep receiving
                self.last_error = f"{type(e).__name__}: {e}"
                self.log(f"real call not processed: {self.last_error}")

    def _handler(self):
        listener = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "vocto-intake"
            sys_version = ""
            protocol_version = "HTTP/1.1"
            timeout = 60

            def log_message(self, fmt, *args):
                pass

            def reply(self, status: int, obj: dict[str, Any], headers: dict[str, str] | None = None) -> None:
                raw = json.dumps(obj).encode()
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                url = urlparse(self.path)
                if url.path in ("/", "/health"):
                    return self.reply(200, {"ok": True, "service": "vocto real-call intake"})
                return self.reply(404, {"error": "POST calls to /ingest/<agent>"})

            def do_POST(self):
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    n = -1
                if n < 0 or n > MAX_BODY or self.headers.get("Transfer-Encoding"):
                    self.close_connection = True
                    return self.reply(413 if n > MAX_BODY else 400,
                                      {"error": "call too large (10 MB at most)" if n > MAX_BODY else "bad request length"})
                wait = listener.limit.check(self.client_address[0] if self.client_address else "?")
                if wait:
                    self.rfile.read(n)
                    return self.reply(429, {"error": "too many requests"}, {"Retry-After": str(max(1, int(wait + 0.999)))})
                body = self.rfile.read(n) if n > 0 else b""
                url = urlparse(self.path)
                parts = [p for p in url.path.split("/") if p]
                if not parts or parts[0] != "ingest" or len(parts) > 2:
                    return self.reply(404, {"error": "POST calls to /ingest/<agent>"})
                monitor = listener.resolve(parts[1] if len(parts) == 2 else "")
                if monitor is None:
                    return self.reply(404, {"error": "no agent with real-call intake switched on at this address"})
                want = token_of(monitor)
                if not want:
                    return self.reply(403, {"error": "this agent has no intake token set up yet"})
                if not token_matches(want, self.headers, url.query):
                    return self.reply(401, {"error": "wrong or missing token"})
                try:
                    calls = parse(body)
                except IntakeError as e:
                    return self.reply(400, {"error": str(e)})
                if not calls:
                    return self.reply(200, {"ignored": True, "why": "not a finished call"})
                try:
                    listener.jobs.put_nowait((monitor, body, "webhook"))
                except queue.Full:
                    return self.reply(503, {"error": "busy: too many calls waiting to be processed; retry shortly"},
                                      {"Retry-After": "30"})
                listener.received += len(calls)
                return self.reply(202, {"accepted": len(calls)})

        return Handler
