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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from ..util import MissingSecret, expand_env
from .intake import IntakeError, parse

MAX_BODY = 10 * 1024 * 1024


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


class Listener:
    """resolve(key) -> a Monitor for that agent key, or None. Calls are processed by one worker thread."""

    def __init__(self, resolve: Callable[[str], Any], host: str = "127.0.0.1", port: int = 8791,
                 judge: bool = True, log: Callable[[str], None] | None = None):
        self.resolve, self.host, self.port, self.judge = resolve, host, port, judge
        self.log = log or (lambda m: print(m, file=sys.stderr))
        self.jobs: queue.Queue = queue.Queue()
        self.received = 0
        self.last_error: str | None = None
        self.httpd: ThreadingHTTPServer | None = None

    def start(self) -> "Listener":
        self.httpd = ThreadingHTTPServer((self.host, self.port), self._handler())
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.3}, daemon=True).start()
        threading.Thread(target=self._work, daemon=True).start()
        return self

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        self.jobs.put(None)

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
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                pass

            def reply(self, status: int, obj: dict[str, Any]) -> None:
                raw = json.dumps(obj).encode()
                self.send_response(status)
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
                n = int(self.headers.get("Content-Length") or 0)
                if n > MAX_BODY:
                    self.rfile.read(min(n, MAX_BODY))
                    return self.reply(413, {"error": "call too large (10 MB at most)"})
                body = self.rfile.read(n) if n > 0 else b""
                url = urlparse(self.path)
                parts = [p for p in url.path.split("/") if p]
                if not parts or parts[0] != "ingest" or len(parts) > 2:
                    return self.reply(404, {"error": "POST calls to /ingest/<agent>"})
                monitor = listener.resolve(parts[1] if len(parts) == 2 else "")
                if monitor is None:
                    return self.reply(404, {"error": "no agent with real-call intake switched on at this address"})
                want = token_of(monitor)
                given = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip() \
                    or self.headers.get("X-Vocto-Token") or self.headers.get("X-Vapi-Secret") \
                    or (parse_qs(url.query).get("token") or [""])[-1]
                if not want:
                    return self.reply(403, {"error": "this agent has no intake token set up yet"})
                if not hmac.compare_digest(str(given).encode(), want.encode()):
                    return self.reply(401, {"error": "wrong or missing token"})
                try:
                    calls = parse(body)
                except IntakeError as e:
                    return self.reply(400, {"error": str(e)})
                if not calls:
                    return self.reply(200, {"ignored": True, "why": "not a finished call"})
                listener.received += len(calls)
                listener.jobs.put((monitor, body, "webhook"))
                return self.reply(202, {"accepted": len(calls)})

        return Handler
