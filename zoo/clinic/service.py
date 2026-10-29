"""The clinic agent run as an outside service, the way a client's agent runs.

Vocto does not start or import it: it is reached only by its addresses, its tool calls go out over
HTTP to whatever tool URL it is configured with (Vocto's tool gateway during tests), and it writes
its own log file. That is exactly how a client's agent is tested, so this proves the framework
treats agents as black boxes. Example project: examples/clinic-service.

    uv run python -m zoo.clinic.service                 (rules brain: free; --brain llm uses Claude)

It serves, on this computer:
  ws://127.0.0.1:9101          voice (docs/voice-protocol.md), with Piper + faster-whisper
  http://127.0.0.1:9102        text (docs/text-http-protocol.md)
  http://127.0.0.1:9103/tools  the clinic's booking API: POST /tools/<name> (the tools' real upstream),
                               GET /appointments?test_id=... (for a REST backend probe)
  logs/clinic-service.log      its log file, one line per event, with the test id when it was given one

Its tool calls go to --tools (default http://127.0.0.1:8790/tools, Vocto's tool gateway).
Bug switches are set when it starts (--bugs lie_on_failure,double_booking), like a bad release.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import secrets
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from . import backend
from .agent import build_agent, parse_bugs

ROOT = Path(__file__).resolve().parents[2]
log = logging.getLogger("clinic-service")


class HttpTools:
    """The agent's tool client: plain HTTP calls to the tool URL it was configured with."""

    def __init__(self, base: str, test_id: str | None):
        self._base = base.rstrip("/")
        self._test_id = test_id

    def call(self, name: str, **args: Any) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self._test_id:
            headers["X-Test-Id"] = self._test_id
        req = urllib.request.Request(f"{self._base}/{name}", data=json.dumps(args).encode("utf-8"),
                                     method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                status, raw = resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError) as e:
            log.error("call %s tool %s could not reach %s: %s", self._test_id or "-", name, self._base, e)
            return {"ok": False, "status": 503, "body": {"error": "tool service unreachable"}}
        try:
            body: Any = json.loads(raw) if raw else None
        except ValueError:
            body = raw
        ok = 200 <= status < 300 and not isinstance(body, str)
        (log.info if ok else log.warning)("call %s tool %s -> %s", self._test_id or "-", name, status)
        return {"ok": ok, "status": status, "body": body}


def _agent(tools_url: str, options: Mapping[str, Any], session: Mapping[str, Any]):
    test_id = session.get("test_id")
    log.info("call %s started", test_id or "-")
    return build_agent(tools=HttpTools(tools_url, test_id), session=dict(session), options=options)


# ---- the clinic's booking API (the tools' real upstream) -------------------------------------

def api_handler():
    tools = {"find_patient": backend.find_patient, "list_free_slots": backend.list_free_slots,
             "book_appointment": backend.book_appointment, "cancel_appointment": backend.cancel_appointment}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _send(self, status: int, body: Any) -> None:
            raw = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            args = json.loads(self.rfile.read(n) or b"{}")
            name = urlparse(self.path).path.removeprefix("/tools/")
            if name not in tools:
                return self._send(404, {"error": f"no tool {name}"})
            status, body = tools[name](args, {"X-Test-Id": self.headers.get("X-Test-Id")})
            log.info("api %s %s -> %s", self.headers.get("X-Test-Id") or "-", name, status)
            self._send(status, body)

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/appointments":
                test_id = parse_qs(url.query).get("test_id", [""])[0]
                with backend._connect() as conn:
                    rows = conn.execute(
                        "SELECT a.id, p.full_name, d.name AS doctor, a.slot_start, a.status, a.reference"
                        " FROM appointments a JOIN patients p ON p.id = a.patient_id"
                        " JOIN doctors d ON d.id = a.doctor_id WHERE a.test_id = ?", (test_id,)).fetchall()
                return self._send(200, [dict(r) for r in rows])
            self._send(200, {"service": "Riverside Clinic booking API", "ok": True})

    return Handler


# ---- the agent, by text ---------------------------------------------------------------------

def text_handler(tools_url: str, options: Mapping[str, Any]):
    sessions: dict[str, Any] = {}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _send(self, status: int, body: Any) -> None:
            raw = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            self._send(200, {"service": "Riverside Clinic agent (text)", "ok": True})

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            msg = json.loads(self.rfile.read(n) or b"{}")
            sid = str(msg.get("session_id") or secrets.token_hex(4))
            kind = msg.get("type")
            try:
                if kind == "start":
                    agent = _agent(tools_url, options, msg.get("metadata") or {})
                    agent.test_id = (msg.get("metadata") or {}).get("test_id")
                    with lock:
                        sessions[sid] = agent
                    return self._send(200, {"reply": agent.start(), "end": False})
                with lock:
                    agent = sessions.get(sid)
                if agent is None:
                    return self._send(404, {"error": "unknown session"})
                if kind == "stop":
                    with lock:
                        sessions.pop(sid, None)
                    log.info("call %s ended", agent.test_id or "-")
                    return self._send(200, {"ok": True})
                answer = agent.reply(str(msg.get("text", "")))
                return self._send(200, {"reply": answer, "end": bool(agent.ended)})
            except Exception as e:  # noqa: BLE001 - a crash is a 500, as a real service would answer
                log.error("call %s crashed: %s: %s", getattr(locals().get("agent"), "test_id", "-"), type(e).__name__, e)
                return self._send(500, {"error": f"{type(e).__name__}: {e}"})

    return Handler


def _serve(handler, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


async def _voice(tools_url: str, options: Mapping[str, Any], port: int, speech: str) -> None:
    from vatf.speech import SpeechConfig, make_stt, make_tts
    from zoo.voice.gateway import VoiceGateway

    from .voice import speak_text

    cfg = SpeechConfig(engine=speech, ears_model=str(options.get("stt_model", "base.en")))
    tts, stt, voice = make_tts(cfg, ROOT), make_stt(cfg, ROOT), str(options.get("voice", "en_US-amy-medium"))
    print("loading speech models...", flush=True)
    await asyncio.to_thread(lambda: (tts.warm_up(voice), stt.warm_up()))
    bugs = parse_bugs(options)
    gateway = VoiceGateway(lambda session: _agent(tools_url, options, session), tts=tts, stt=stt, voice=voice,
                           speak_text=lambda text: speak_text(text, "misread_reference" in bugs))
    await gateway.launch(port=port)
    print(f"voice agent:  ws://127.0.0.1:{port}", flush=True)
    await asyncio.Event().wait()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="The clinic agent as an outside service (for testing Vocto).")
    p.add_argument("--voice-port", type=int, default=9101)
    p.add_argument("--text-port", type=int, default=9102)
    p.add_argument("--api-port", type=int, default=9103)
    p.add_argument("--no-voice", action="store_true", help="text and API only (no speech models)")
    p.add_argument("--speech", default="local", choices=["local", "codec"], help="codec = test tones (Vocto's own tests)")
    p.add_argument("--tools", default="http://127.0.0.1:8790/tools", help="where the agent sends its tool calls")
    p.add_argument("--brain", default="rules", choices=["rules", "llm"])
    p.add_argument("--model", default="claude-haiku-5-5")
    p.add_argument("--bugs", default="", help="bug switches, e.g. lie_on_failure,double_booking")
    p.add_argument("--log", default=str(ROOT / "logs" / "clinic-service.log"))
    args = p.parse_args(argv)

    Path(args.log).parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(args.log, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    from vatf.cli import load_dotenv

    load_dotenv(ROOT / ".env")   # the llm brain needs ANTHROPIC_API_KEY
    options = {"brain": args.brain, "model": args.model, "bugs": args.bugs, "speech": args.speech}
    parse_bugs(options)

    _serve(api_handler(), args.api_port)
    _serve(text_handler(args.tools, options), args.text_port)
    print(f"booking API:  http://127.0.0.1:{args.api_port}/tools/<name>", flush=True)
    print(f"text agent:   http://127.0.0.1:{args.text_port}", flush=True)
    print(f"tool calls go to {args.tools}; log file {args.log}", flush=True)
    if args.bugs:
        print(f"bug switches ON: {args.bugs}", flush=True)
    try:
        if args.no_voice:
            threading.Event().wait()
        else:
            asyncio.run(_voice(args.tools, options, args.voice_port, args.speech))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
