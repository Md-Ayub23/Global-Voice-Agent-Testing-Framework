"""phone_call connector: Vocto rings the agent's real phone number and talks to it over the phone network.

This tests what a client actually ships: their number, their telephony provider, their agent, the
real phone line in between. Nothing in the agent changes and it needs no test hooks.

How a call works (docs/phone-guide.md):

  1. Vocto starts its phone line: a small WebSocket server on this computer, reachable from the
     internet through connection.options.public_url or a Cloudflare quick tunnel (tunnel.py).
  2. Vocto asks the phone provider (REST API, the team's own account) to call connection.target
     from options.from_number. The call's instructions say: when answered, stream the call to
     wss://<public address>/stream/<one-time token>.
       Twilio: the instructions (TwiML <Connect><Stream>) go with the API request.
       Plivo:  the provider fetches them from Vocto's phone line (GET /answer/<token>, Plivo XML
               <Stream bidirectional="true">) and reports how the call ended to /hangup/<token>.
  3. The agent answers. The provider opens that WebSocket and sends the agent's voice as 8 kHz
     mu-law ("media" messages); Vocto sends the simulated caller's voice back (Twilio "media",
     Plivo "playAudio"). The call's clock starts when the stream starts, so response times
     include the real phone network.
  4. Vocto hangs up through the REST API, or the agent does (the stream stops).

Secrets (account_sid / Plivo Auth ID, auth_token) are given as ${NAME} and kept in .env, never in the profile.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import weakref
from html import escape
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from ..audio.g711 import pcm16_to_ulaw, ulaw_to_pcm16
from ..audio.pcm import from_pcm16, to_pcm16
from ..audio.recorder import Recorder
from ..contracts.models import CallContext
from ..util import MissingSecret, expand_env
from . import ConnectError
from .tunnel import TunnelError, QuickTunnel, cloudflared_path, wait_reachable, ws_url

SAMPLE_RATE = 8000
JITTER_MS = 60
PHONE = re.compile(r"^\+[1-9]\d{6,14}$")
TWILIO_API = "https://api.twilio.com/2010-04-01"
PLIVO_API = "https://api.plivo.com/v1"
ENDED = {"completed", "busy", "failed", "no-answer", "canceled"}
STATUS_WORDS = {
    "busy": "the number was busy",
    "no-answer": "no one answered",
    "failed": "the call could not be placed",
    "canceled": "the call was cancelled before it was answered",
    "completed": "the call ended before the audio stream started",
}
LINE_PORT = 8766       # where the phone line listens when the team brings its own public address
MIN_GAP_S = 1.1        # Twilio places about one call per second per account by default (kept for Plivo too)


def check_number(n: str, what: str) -> str:
    n = re.sub(r"[\s().-]", "", str(n or ""))
    if not PHONE.match(n):
        raise ConnectError(f"{what} must be a phone number in international format, like +14155550123 (got {n or 'nothing'!r})")
    return n


# ---- Twilio REST ------------------------------------------------------------------------------

class ProviderError(Exception):
    """The phone provider's API refused or could not be reached. Plain words, for the person."""


class TwilioError(ProviderError):
    pass


class PlivoError(ProviderError):
    pass


class TwilioClient:
    """What Vocto needs from Twilio's REST API, with the team's own account."""

    name, label = "twilio", "Twilio"
    settle_s = 0.5          # Twilio fills in a call's price within a second or two of the end

    def __init__(self, account_sid: str, auth_token: str, api: str = TWILIO_API):
        if not account_sid or not auth_token:
            raise ConnectError("the Twilio account SID and auth token are not set (Set up › Connection › Phone number)")
        self.sid, self._token, self.api = account_sid, auth_token, api.rstrip("/")

    def _request(self, method: str, path: str, form: Mapping[str, str] | None = None) -> dict[str, Any]:
        data = urllib.parse.urlencode(form).encode() if form is not None else None
        req = urllib.request.Request(f"{self.api}/Accounts/{self.sid}{path}", data=data, method=method)
        auth = base64.b64encode(f"{self.sid}:{self._token}".encode()).decode()
        req.add_header("Authorization", f"Basic {auth}")
        if data is not None:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read() or b"{}")
            except ValueError:
                body = {}
            msg = body.get("message") or e.reason
            if e.code == 401:
                msg = "Twilio refused the account SID or auth token"
            raise TwilioError(f"{msg} (Twilio {e.code}{', code ' + str(body['code']) if body.get('code') else ''})") from e
        except urllib.error.URLError as e:
            raise TwilioError(f"could not reach Twilio: {e.reason}") from e

    def create_call(self, to: str, from_: str, twiml: str, ring_timeout_s: int) -> dict[str, Any]:
        return self._request("POST", "/Calls.json", {"To": to, "From": from_, "Twiml": twiml,
                                                      "Timeout": str(ring_timeout_s)})

    def get_call(self, call_sid: str) -> dict[str, Any]:
        return self._request("GET", f"/Calls/{call_sid}.json")

    def hang_up(self, call_sid: str) -> dict[str, Any]:
        return self._request("POST", f"/Calls/{call_sid}.json", {"Status": "completed"})

    def account(self) -> dict[str, Any]:
        return self._request("GET", ".json")

    def number_kind(self, number: str) -> str | None:
        """"owned" (a number bought in this account), "verified" (a verified caller id) or None."""
        q = urllib.parse.urlencode({"PhoneNumber": number})
        if self._request("GET", f"/IncomingPhoneNumbers.json?{q}").get("incoming_phone_numbers"):
            return "owned"
        if self._request("GET", f"/OutgoingCallerIds.json?{q}").get("outgoing_caller_ids"):
            return "verified"
        return None

    # -- the provider interface PhoneCallConnector uses (same names on PlivoClient) --

    def account_summary(self) -> tuple[str, str | None]:
        """One line about the account, and a warning that stops testing (a trial account) or None."""
        a = self.account()
        line = f"Twilio account: {a.get('friendly_name') or self.sid} ({a.get('status', '?')}, {str(a.get('type', '?')).lower()})"
        if str(a.get("type", "")).lower() == "trial":
            return line, ("Trial account: Twilio plays a short message at the start of every call and waits for a key press, "
                          "so an AI agent never hears the test caller. Upgrade the account before testing.")
        return line, None

    def place(self, to: str, from_: str, line: "PhoneLine", token: str, ring_timeout_s: int, test_id: str | None) -> str:
        params = f'<Parameter name="test_id" value="{escape(test_id)}"/>' if test_id else ""
        twiml = (f'<Response><Connect><Stream url="{escape(line.public)}/stream/{token}">{params}'
                 f"</Stream></Connect></Response>")
        return str(self.create_call(to, from_, twiml, ring_timeout_s).get("sid") or "")

    def live_status(self, ref: str, line: "PhoneLine", token: str) -> str:
        return str(self.get_call(ref).get("status") or "")

    def end(self, ref: str) -> None:
        self.hang_up(ref)

    def final(self, ref: str) -> dict[str, Any]:
        info = self.get_call(ref)
        return {"status": info.get("status"), "duration_s": _int(info.get("duration")),
                "price": _cost(info.get("price")), "price_unit": info.get("price_unit")}

    @staticmethod
    def stream_ids(start: Mapping[str, Any]) -> tuple[str, str]:
        """(stream id, call id) from the stream's "start" message."""
        inner = start.get("start") or {}
        return str(start.get("streamSid") or inner.get("streamSid") or ""), str(inner.get("callSid") or "")

    @staticmethod
    def media(stream_id: str, payload: str) -> str:
        return json.dumps({"event": "media", "streamSid": stream_id, "media": {"payload": payload}})


# ---- Plivo REST -------------------------------------------------------------------------------

PLIVO_STATUS = {"busy": "busy", "no-answer": "no-answer", "timeout": "no-answer", "cancel": "canceled",
                "failed": "failed", "completed": "completed"}
PLIVO_CAUSES = {"Busy Line": "busy", "No Answer": "no-answer", "Ring Timeout Reached": "no-answer",
                "Rejected": "busy", "Invalid Destination Address": "failed", "Unallocated Number": "failed"}


class PlivoClient:
    """What Vocto needs from Plivo's Voice API, with the team's own account (Auth ID + Auth token).

    Plivo's "make a call" request has no inline instructions: it fetches them from an answer URL,
    which Vocto's phone line serves (GET /answer/<token>), and reports the end of a call that never
    connected to a hangup URL (GET /hangup/<token>). Its response has a request id only; the call's
    own id (CallUUID) comes with the answer request and the stream's "start" message."""

    name, label = "plivo", "Plivo"
    settle_s = 1.5          # Plivo writes the call record (duration, amount) a few seconds after the end

    def __init__(self, auth_id: str, auth_token: str, api: str = PLIVO_API):
        if not auth_id or not auth_token:
            raise ConnectError("the Plivo Auth ID and Auth token are not set (Set up › Connection › Phone number)")
        self.sid, self._token, self.api = auth_id, auth_token, api.rstrip("/")
        self._calls: dict[str, str] = {}       # request id -> call id (CallUUID), once known

    def _request(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{self.api}/Account/{self.sid}{path}", data=data, method=method)
        auth = base64.b64encode(f"{self.sid}:{self._token}".encode()).decode()
        req.add_header("Authorization", f"Basic {auth}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            try:
                body_ = json.loads(e.read() or b"{}")
            except ValueError:
                body_ = {}
            msg = (body_.get("error") if isinstance(body_, dict) else None) or e.reason
            if e.code == 401:
                msg = "Plivo refused the Auth ID or Auth token"
            raise PlivoError(f"{msg} (Plivo {e.code})") from e
        except urllib.error.URLError as e:
            raise PlivoError(f"could not reach Plivo: {e.reason}") from e

    def account(self) -> dict[str, Any]:
        return self._request("GET", "/")

    def number_kind(self, number: str) -> str | None:
        """"owned" (a number rented in this Plivo account) or None (not found)."""
        try:
            self._request("GET", f"/Number/{number.lstrip('+')}/")
            return "owned"
        except PlivoError as e:
            if "(Plivo 404)" in str(e):
                return None
            raise

    def account_summary(self) -> tuple[str, str | None]:
        a = self.account()
        kind = str(a.get("account_type") or "?")
        line = (f"Plivo account: {a.get('name') or self.sid} ({'free trial' if kind == 'developer' else kind}"
                + (f", credit {a['cash_credits']} USD" if a.get("cash_credits") not in (None, "") else "") + ")")
        return line, None

    def place(self, to: str, from_: str, line: "PhoneLine", token: str, ring_timeout_s: int, test_id: str | None) -> str:
        extra = f' extraHeaders="test_id={escape(test_id)}"' if test_id else ""
        line.answers[token] = ('<?xml version="1.0" encoding="UTF-8"?>\n<Response><Stream bidirectional="true" '
                               'keepCallAlive="true" contentType="audio/x-mulaw;rate=8000"'
                               f"{extra}>{escape(line.public)}/stream/{token}</Stream></Response>")
        out = self._request("POST", "/Call/", {
            "from": from_.lstrip("+"), "to": to.lstrip("+"),
            "answer_url": f"{line.http_public}/answer/{token}", "answer_method": "GET",
            "hangup_url": f"{line.http_public}/hangup/{token}", "hangup_method": "GET",
            "ring_timeout": int(ring_timeout_s)})
        ref = out.get("request_uuid")
        if isinstance(ref, list):
            ref = ref[0] if ref else ""
        if not ref:
            raise PlivoError(f"Plivo did not place the call: {out.get('message') or out}")
        return str(ref)

    def live_status(self, ref: str, line: "PhoneLine", token: str) -> str:
        """From what Plivo told the phone line: the hangup report of a call that never connected,
        or "in-progress" once the answer URL was fetched."""
        hung = line.hangups.get(token)
        if hung is not None:
            status = PLIVO_STATUS.get(str(hung.get("CallStatus") or "").lower())
            return status or PLIVO_CAUSES.get(str(hung.get("HangupCauseName") or ""), "completed")
        answered = line.answered.get(token)
        if answered is not None:
            if answered.get("CallUUID"):
                self._calls[ref] = str(answered["CallUUID"])
            return "in-progress"
        return ""

    def call_id(self, ref: str) -> str:
        return self._calls.get(ref, "")

    def remember(self, ref: str, call_id: str) -> None:
        if call_id:
            self._calls[ref] = call_id

    def end(self, ref: str) -> None:
        call = self._calls.get(ref)
        if call:
            self._request("DELETE", f"/Call/{call}/")
        else:                       # still ringing: cancel the request itself
            self._request("DELETE", f"/Request/{ref}/")

    def final(self, ref: str) -> dict[str, Any]:
        call = self._calls.get(ref)
        if not call:
            return {"status": None}
        info = self._request("GET", f"/Call/{call}/")
        cause = str(info.get("hangup_cause_name") or "")
        return {"status": PLIVO_CAUSES.get(cause, "completed"), "duration_s": _int(info.get("call_duration")),
                "price": _cost(info.get("total_amount")), "price_unit": "USD", "hangup_cause": cause or None}

    @staticmethod
    def stream_ids(start: Mapping[str, Any]) -> tuple[str, str]:
        inner = start.get("start") or {}
        return str(inner.get("streamId") or start.get("streamId") or ""), str(inner.get("callId") or "")

    @staticmethod
    def media(stream_id: str, payload: str) -> str:
        return json.dumps({"event": "playAudio", "media": {"contentType": "audio/x-mulaw", "sampleRate": SAMPLE_RATE,
                                                            "payload": payload}})


# ---- the phone line: Vocto's public WebSocket ---------------------------------------------------

class PhoneLine:
    """One local WebSocket server per event loop, reachable from the internet. Each call waits on a
    one-time token in its stream address, so calls running at the same time never mix."""

    _lines: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, PhoneLine]" = weakref.WeakKeyDictionary()

    def __init__(self) -> None:
        self.public = ""        # wss://...
        self.http_public = ""   # https://... (the same address, for the provider's answer/hangup requests)
        self.answers: dict[str, str] = {}               # token -> call instructions (Plivo XML)
        self.answered: dict[str, dict[str, str]] = {}   # token -> what the provider sent when the call was answered
        self.hangups: dict[str, dict[str, str]] = {}    # token -> the provider's hang-up report
        self._server: Any = None
        self._tunnel: QuickTunnel | None = None
        self._waiting: dict[str, asyncio.Future] = {}

    @classmethod
    async def get(cls, root: Path, public_url: str = "", port: int = 0) -> "PhoneLine":
        key = asyncio.get_running_loop()
        line = cls._lines.get(key)
        if line is None or (line._tunnel is not None and not line._tunnel.alive()):
            line = cls()
            await line._start(root, public_url, port)
            cls._lines[key] = line
        return line

    async def _start(self, root: Path, public_url: str, port: int = 0) -> None:
        from websockets.asyncio.server import serve

        def health(connection, request):
            url = urllib.parse.urlsplit(request.path)
            path = url.path.rstrip("/")
            if path in ("", "/health"):
                return connection.respond(200, "Vocto phone line\n")
            kind, _, token = path.lstrip("/").partition("/")
            query = dict(urllib.parse.parse_qsl(url.query))
            if kind == "answer" and token in self.answers:      # Plivo fetches the call's instructions
                self.answered[token] = query
                response = connection.respond(200, self.answers[token])
                del response.headers["Content-Type"]          # (Headers keeps repeats: replace, don't add)
                response.headers["Content-Type"] = "application/xml"
                return response
            if kind == "hangup" and token:                      # Plivo: how the call ended
                if token in self.answers:                       # (calls already under way need nothing)
                    self.hangups[token] = query
                return connection.respond(200, "ok\n")
            if kind == "answer":
                return connection.respond(404, "unknown call\n")
            return None

        if public_url and public_url != "local" and not public_url.startswith(("https://", "wss://")):
            raise ConnectError(f"the public address {public_url!r} must start with https:// "
                               "(Twilio only streams calls to a secure address)")
        try:
            self._server = await serve(self._handler, "127.0.0.1", port, process_request=health,
                                       max_size=None, ping_interval=None)
        except OSError as e:
            raise ConnectError(f"Vocto's phone line could not listen on port {port} of this computer: {e}") from e
        port = self._server.sockets[0].getsockname()[1]
        if public_url == "local":      # Vocto's own tests: a phone network on this computer
            self.public = f"ws://127.0.0.1:{port}"
            self.http_public = f"http://127.0.0.1:{port}"
            return
        if public_url:
            self.public = ws_url(public_url)
            self.http_public = "https://" + self.public[len("wss://"):] if self.public.startswith("wss://") else public_url.rstrip("/")
            return
        try:
            exe = await asyncio.to_thread(cloudflared_path, root)
            self._tunnel = QuickTunnel(exe, port)
            https = await asyncio.to_thread(self._tunnel.wait)
            await asyncio.to_thread(wait_reachable, https)
        except TunnelError as e:
            self._server.close()
            raise ConnectError(f"no public address for the phone line: {e}") from e
        self.public = ws_url(https)
        self.http_public = https.rstrip("/")

    def expect(self, token: str) -> asyncio.Future:
        fut = asyncio.get_running_loop().create_future()
        self._waiting[token] = fut
        return fut

    def forget(self, token: str) -> None:
        for d in (self.answers, self.answered, self.hangups):
            d.pop(token, None)
        fut = self._waiting.pop(token, None)
        if fut is not None and not fut.done():
            fut.cancel()

    async def _handler(self, ws) -> None:
        token = ws.request.path.rstrip("/").rsplit("/", 1)[-1]
        fut = self._waiting.pop(token, None)
        if fut is None or fut.done():
            await ws.close(1008, "unknown stream")
            return
        # Twilio sends "connected" then "start", Plivo "start", with the stream and call ids, before any audio.
        start: dict[str, Any] = {}
        try:
            while not start:
                msg = json.loads(await asyncio.wait_for(ws.recv(), 15))
                if isinstance(msg, dict) and msg.get("event") == "start":
                    start = msg
        except Exception as e:  # noqa: BLE001
            fut.set_exception(ConnectError(f"the phone stream opened but never started: {type(e).__name__}: {e}"))
            return
        done = asyncio.Event()
        fut.set_result((ws, start, done))
        await done.wait()          # the session reads and writes; the connection lives until it is done


# ---- one call ---------------------------------------------------------------------------------

class PhoneCallSession:
    def __init__(self, ws: Any, start: Mapping[str, Any], done: asyncio.Event, recorder: Recorder,
                 client: Any, meta: dict[str, Any], jitter_ms: int = JITTER_MS, ref: str = ""):
        self._ws, self._done, self._client = ws, done, client
        self.rec = recorder
        self.stream_sid, call_id = client.stream_ids(start)
        self._ref = ref or meta["call_sid"]          # what the provider's API knows the call by
        if call_id and hasattr(client, "remember"):
            client.remember(self._ref, call_id)
        if call_id and not meta.get("call_sid"):
            meta["call_sid"] = call_id
        self.call_sid = meta["call_sid"]
        self.call_meta = meta                       # saved with the call (call_ended.phone)
        self._jitter = jitter_ms * recorder.sr // 1000
        self.closed = asyncio.Event()
        self.closed_by_agent = False
        self.close_reason = ""
        self.transcripts: list[tuple[int, str]] = []
        self.protocol_errors: list[str] = []
        self._send_lock = asyncio.Lock()
        self._reader = asyncio.create_task(self._read())
        self._watcher = asyncio.create_task(self._watch_close())

    async def _watch_close(self) -> None:
        """The provider closing the stream (Plivo's way of saying the call ended) counts at once, not
        when the TCP connection finally goes, which can take the close timeout (10 s)."""
        protocol = getattr(self._ws, "protocol", None)
        while not self.closed.is_set():
            if protocol is not None and getattr(protocol, "close_rcvd", None) is not None:
                if not self.close_reason:
                    self.closed_by_agent, self.close_reason = True, "the call ended (the phone stream closed)"
                self.closed.set()
                return
            await asyncio.sleep(0.05)

    async def _read(self) -> None:
        try:
            async for raw in self._ws:
                if isinstance(raw, (bytes, bytearray)):
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                event = msg.get("event") if isinstance(msg, dict) else None
                if event == "media":
                    try:
                        audio = base64.b64decode((msg.get("media") or {}).get("payload") or "", validate=True)
                    except (ValueError, TypeError):
                        self._error("media payload is not base64")
                        continue
                    if audio:
                        self.play(from_pcm16(ulaw_to_pcm16(audio)))
                elif event == "stop":
                    self.closed_by_agent, self.close_reason = True, "the agent hung up"
                    break
        except Exception as e:  # noqa: BLE001 - any read failure ends the call
            self.closed_by_agent, self.close_reason = True, f"the phone stream dropped: {type(e).__name__}: {e}"
        finally:
            if not self.close_reason:
                self.closed_by_agent, self.close_reason = True, "the call ended (the phone stream closed)"
            self.closed.set()

    def play(self, x: np.ndarray) -> None:
        """The agent's voice, joined on without a gap when it arrives just after the audio before it
        (Twilio's 20 ms packets come with network jitter), like a phone's playout buffer."""
        now = self.rec.now_sample()
        cursor = self.rec.cursor
        if self._jitter and self.rec.chunks and cursor < now <= cursor + self._jitter:
            self.rec.agent_audio(x, arrival_s=cursor / self.rec.sr)
        else:
            self.rec.agent_audio(x)

    def _error(self, text: str) -> None:
        if len(self.protocol_errors) < 20:
            self.protocol_errors.append(text)

    async def send_frame(self, x: np.ndarray) -> None:
        if self.closed.is_set():
            return
        payload = base64.b64encode(pcm16_to_ulaw(to_pcm16(x))).decode("ascii")
        try:
            async with self._send_lock:
                await self._ws.send(self._client.media(self.stream_sid, payload))
        except Exception:  # noqa: BLE001 - the reader records the hang-up; the line just goes quiet
            pass

    async def send_text(self, text: str) -> None:
        raise NotImplementedError("phone_call is a voice connector: the caller speaks")

    async def close(self) -> None:
        if not self.closed.is_set():
            try:
                await asyncio.to_thread(self._client.end, self._ref)
            except Exception as e:  # noqa: BLE001
                self._error(f"hang-up: {e}")
        try:
            await self._ws.close()
        except Exception:  # noqa: BLE001
            pass
        self._done.set()
        self._watcher.cancel()
        self._reader.cancel()
        try:
            await self._reader
        except (asyncio.CancelledError, Exception):
            pass
        # How long the call was and what it cost. The provider fills these in a moment after the call
        # ends, so ask a few times; a call still up (the stream closed, the line didn't) is hung up here.
        for attempt in range(8):
            try:
                info = await asyncio.to_thread(self._client.final, self._ref)
            except Exception:  # noqa: BLE001 - e.g. Plivo's call record is not written yet
                info = {}
            self.call_meta.update({k: v for k, v in info.items() if v is not None or k not in self.call_meta})
            status = info.get("status")
            if status in ENDED and info.get("price") is not None:
                break
            if status and status not in ENDED and attempt == 2:
                try:
                    await asyncio.to_thread(self._client.end, self._ref)
                except Exception:  # noqa: BLE001
                    pass
            await asyncio.sleep(self._client.settle_s)


def _cost(v: Any) -> float | None:
    """Twilio reports a call's price as a negative string ("-0.0140"): the cost, as a positive number."""
    try:
        return abs(float(v))
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


class PhoneCallConnector:
    mode = "voice"
    sample_rate = SAMPLE_RATE
    _last_dial = 0.0
    _dial_lock: asyncio.Lock | None = None

    def __init__(self, profile: Mapping[str, Any], root: Path | None = None, tools: Any = None):
        self._profile = profile
        self._root = Path(root or ".")

    def _options(self) -> dict[str, Any]:
        try:
            return dict(expand_env(self._profile["connection"].get("options") or {}))
        except MissingSecret as e:
            import re
            names = re.findall(r"\$\{(\w+)\}", str(e)) or ["a secret"]
            names = list(dict.fromkeys(names))
            where = ("Plivo console › Overview shows both" if self._provider() == "plivo"
                     else "Twilio console › Account info shows both")
            raise ConnectError(f"{' and '.join(names)} {'has' if len(names) == 1 else 'have'} no value yet: add it in Settings › Secrets "
                               f"({where})") from e

    def _provider(self) -> str:
        return str((self._profile["connection"].get("options") or {}).get("provider") or "twilio").lower()

    def client(self) -> "TwilioClient | PlivoClient":
        o = self._options()
        provider = self._provider()
        account = str(o.get("auth_id") or o.get("account_sid") or "")
        if provider == "twilio":
            return TwilioClient(account, str(o.get("auth_token") or ""), str(o.get("api_base") or TWILIO_API))
        if provider == "plivo":
            return PlivoClient(account, str(o.get("auth_token") or ""), str(o.get("api_base") or PLIVO_API))
        raise ConnectError(f"phone provider {provider!r} is not supported (twilio and plivo are)")

    async def _pace(self) -> None:
        """Calls started together (load tests) go out about one a second, the providers' default limit."""
        cls = type(self)
        if cls._dial_lock is None:
            cls._dial_lock = asyncio.Lock()
        async with cls._dial_lock:
            wait = cls._last_dial + MIN_GAP_S - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            cls._last_dial = time.monotonic()

    async def open(self, ctx: CallContext | None, recorder: Recorder) -> PhoneCallSession:
        if recorder.sr != SAMPLE_RATE:
            raise ConnectError(f"phone calls are 8 kHz; the recorder runs at {recorder.sr} Hz")
        conn, o = self._profile["connection"], self._options()
        to = check_number(conn.get("target", ""), "The agent's phone number")
        client = self.client()
        from_ = check_number(o.get("from_number", ""), f"Your {client.label} number")
        ring = int(o.get("ring_timeout_s") or 30)
        public = str(o.get("public_url") or "").strip()
        # With the team's own public address, their tunnel or proxy points at a fixed port on this
        # computer (line_port, default 8766). With Vocto's own Cloudflare link any free port will do.
        port = int(o.get("line_port") or (LINE_PORT if public and public != "local" else 0))
        line = await PhoneLine.get(self._root, public, port)
        token = secrets.token_urlsafe(18)
        waiting = line.expect(token)
        await self._pace()
        try:
            ref = await asyncio.to_thread(client.place, to, from_, line, token, ring, ctx.test_id if ctx else None)
        except ProviderError as e:
            line.forget(token)
            raise ConnectError(f"{client.label} could not place the call: {e}") from e
        # Twilio's API answers with the call's id; Plivo's with a request id (the call id comes later).
        meta = {"provider": client.name, "call_sid": ref if client.name == "twilio" else "", "to": to, "from": from_}
        deadline = time.monotonic() + ring + 25
        try:
            while True:
                try:
                    ws, start, done = await asyncio.wait_for(asyncio.shield(waiting), 2.0)
                    break
                except asyncio.TimeoutError:
                    pass
                status = ""
                try:
                    status = await asyncio.to_thread(client.live_status, ref, line, token)
                except ProviderError:
                    pass
                if status in ENDED and not waiting.done():
                    raise ConnectError(f"{STATUS_WORDS.get(status, status)} ({to})")
                if time.monotonic() > deadline:
                    raise ConnectError(f"the call to {to} was answered but its audio never reached Vocto"
                                       if status == "in-progress" else f"no one answered {to} in time")
        except BaseException:
            line.forget(token)
            try:
                await asyncio.to_thread(client.end, ref)
            except Exception:  # noqa: BLE001
                pass
            raise
        if hasattr(client, "remember"):        # Plivo: the call's id, from its answer request
            client.remember(ref, str((line.answered.get(token) or {}).get("CallUUID") or ""))
        line.forget(token)
        return PhoneCallSession(ws, start, done, recorder, client, meta,
                                jitter_ms=int(o.get("jitter_buffer_ms", JITTER_MS)), ref=ref)
