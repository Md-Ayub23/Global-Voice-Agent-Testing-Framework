"""phone_call connector: Vocto rings a real phone number through Twilio and talks over the phone line.

No real call is made here. A fake Twilio runs on this computer: its REST API takes the call, and its
"phone network" answers by opening the stream the call's TwiML names, exactly as Twilio does, with a
small agent on the other end that greets in test tones, waits for the caller, answers and hangs up.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import numpy as np
import pytest

from vatf.audio.g711 import pcm16_to_ulaw, ulaw_to_pcm16
from vatf.audio.pcm import from_pcm16, resample, to_pcm16
from vatf.audio.recorder import Recorder
from vatf.connectors import ConnectError, build_connector, is_voice
from vatf.connectors.phone_call import PhoneCallConnector, check_number
from vatf.connectors.tunnel import ws_url
from vatf.contracts.models import AudioClip, CallContext
from vatf.loader import schema_errors
from vatf.speech.codec import CodecSTT, CodecTTS

ROOT = Path(__file__).resolve().parent.parent
SID = "AC" + "1" * 32
TOKEN = "secret-token"
AGENT, OURS = "+14155550100", "+14155550199"


def tones(text: str) -> np.ndarray:
    clip = CodecTTS().synthesize(text)
    return resample(from_pcm16(clip.pcm16), clip.sample_rate_hz, 8000)


async def paced(frames, send, frame_s: float = 0.02) -> None:
    """Send frames in real time against a clock. A plain sleep(0.02) per frame runs slow on Windows,
    whose timers tick every ~15.6 ms (each sleep lasts ~31 ms), so speech took 1.5x as long."""
    start = time.monotonic()
    for i, f in enumerate(frames):
        await send(f)
        delay = start + (i + 1) * frame_s - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)


def heard(x: np.ndarray) -> str:
    return CodecSTT().transcribe(AudioClip(to_pcm16(x), 8000)).text


class FakeTwilio:
    """Twilio's REST API plus a phone network that answers with a tiny tone-speaking agent."""

    def __init__(self, answer: str = "answer"):
        self.answer = answer            # answer | busy | no-answer
        self.account_type = "Full"
        self.calls: dict[str, dict] = {}
        self.agent_heard: list[str] = []
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _authorised(self):
                want = "Basic " + base64.b64encode(f"{SID}:{TOKEN}".encode()).decode()
                if self.headers.get("Authorization") != want:
                    self._send(401, {"code": 20003, "message": "Authenticate"})
                    return False
                return True

            def do_GET(self):
                if not self._authorised():
                    return
                m = re.search(r"/Calls/(CA\w+)\.json$", self.path)
                if m and m.group(1) in fake.calls:
                    return self._send(200, {k: v for k, v in fake.calls[m.group(1)].items() if not k.startswith("_")})
                if self.path.endswith(f"/Accounts/{SID}.json"):
                    return self._send(200, {"sid": SID, "type": fake.account_type, "status": "active", "friendly_name": "Acme QA"})
                if "/IncomingPhoneNumbers.json" in self.path:
                    return self._send(200, {"incoming_phone_numbers": [{"phone_number": OURS}] if "14155550199" in self.path else []})
                if "/OutgoingCallerIds.json" in self.path:
                    return self._send(200, {"outgoing_caller_ids": []})
                self._send(404, {"code": 20404, "message": "not found"})

            def do_POST(self):
                if not self._authorised():
                    return
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode()).items()}
                m = re.search(r"/Calls/(CA\w+)\.json$", self.path)
                if m:     # hang up
                    call = fake.calls[m.group(1)]
                    if form.get("Status") == "completed" and call["status"] in ("queued", "ringing", "in-progress"):
                        call.update(status="completed", duration="3", price="-0.0140", price_unit="USD")
                        fut = call.pop("_task", None)
                        if fut is not None:
                            fake.loop.call_soon_threadsafe(fut.cancel)
                    return self._send(200, {k: v for k, v in call.items() if not k.startswith("_")})
                sid = "CA" + str(len(fake.calls) + 1).rjust(32, "0")
                call = {"sid": sid, "to": form["To"], "from": form["From"], "status": "queued", "twiml": form["Twiml"],
                        "timeout": form.get("Timeout")}
                fake.calls[sid] = call
                if fake.answer == "answer":
                    url = re.search(r'<Stream url="([^"]+)"', form["Twiml"]).group(1)
                    call["_task"] = asyncio.run_coroutine_threadsafe(fake._agent(sid, url), fake.loop)
                else:
                    call["status"] = fake.answer
                self._send(201, {k: v for k, v in call.items() if not k.startswith("_")})

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.api = f"http://127.0.0.1:{self.http.server_address[1]}/2010-04-01"

    async def _agent(self, sid: str, url: str) -> None:
        """The phone network answers: open the stream, greet, listen, answer, hang up."""
        import websockets

        self.calls[sid]["status"] = "in-progress"
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"}))
            await ws.send(json.dumps({"event": "start", "streamSid": "MZ1", "start": {
                "streamSid": "MZ1", "callSid": sid, "accountSid": SID, "tracks": ["inbound"],
                "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1}}}))

            async def say(text):
                pcm = pcm16_to_ulaw(to_pcm16(tones(text)))
                await paced(range(0, len(pcm), 160), lambda i: ws.send(json.dumps({"event": "media", "streamSid": "MZ1",
                            "media": {"track": "inbound", "payload": base64.b64encode(pcm[i:i + 160]).decode()}})))

            await say("Riverside clinic, how can I help?")
            got: list[np.ndarray] = []
            quiet, spoke = 0, False
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("event") != "media":
                    continue
                assert msg["streamSid"] == "MZ1"
                x = from_pcm16(ulaw_to_pcm16(base64.b64decode(msg["media"]["payload"])))
                got.append(x)
                loud = float(np.abs(x).max()) > 0.05
                spoke, quiet = spoke or loud, 0 if loud else quiet + 1
                if spoke and quiet > 40:        # 800 ms of quiet after the caller spoke
                    break
            self.agent_heard.append(heard(np.concatenate(got)))
            await say("Booked. Goodbye.")
            await ws.send(json.dumps({"event": "stop", "streamSid": "MZ1", "stop": {"callSid": sid}}))
        self.calls[sid].update(status="completed", duration="4", price="-0.0140", price_unit="USD")

    def close(self):
        self.http.shutdown()
        self.loop.call_soon_threadsafe(self.loop.stop)


@pytest.fixture
def twilio():
    fake = FakeTwilio()
    yield fake
    fake.close()


def profile(api: str, **options):
    return {"connection": {"type": "phone_call", "target": AGENT, "options": {
        "provider": "twilio", "account_sid": SID, "auth_token": TOKEN, "from_number": OURS,
        "public_url": "local", "api_base": api, "ring_timeout_s": 5, **options}}}


def ctx():
    return CallContext(test_id="t-20261105-0001-abcd", run_id="r-1", profile={}, scenario={}, repeat=1, attempt=1, mode="voice")


async def _call(p, caller_says: str):
    start = time.monotonic()
    rec = Recorder(8000, lambda: time.monotonic() - start)
    session = await build_connector(p, ROOT, None).open(ctx(), rec)
    # The greeting comes down the phone line: wait until it has been spoken and half a second of quiet follows
    # (how long the call takes to connect differs from computer to computer).
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        await asyncio.sleep(0.1)
        spans = rec.agent_spans(0, rec.now_sample())
        if spans and rec.now_sample() - spans[-1][1] > rec.sr // 2:
            break
    greeting = heard(rec.agent.slice(0, rec.now_sample()))
    # ...the caller answers in real time, 20 ms frames, then stays quiet while the agent answers.
    x = tones(caller_says)
    frames = (np.pad(f := (x[i:i + 160] if i < len(x) else np.zeros(160, np.float32)), (0, 160 - len(f)))
              for i in range(0, len(x) + 160 * 60, 160))
    await paced(frames, session.send_frame)
    await asyncio.wait_for(session.closed.wait(), 10)
    reply = heard(rec.agent.slice(rec.ms(0), rec.now_sample()))
    await session.close()
    return session, greeting, reply


def test_a_real_phone_call_goes_both_ways_and_is_hung_up(twilio):
    session, greeting, reply = asyncio.run(_call(profile(twilio.api), "I need a check-up"))
    assert greeting.startswith("Riverside clinic, how can I help?")
    assert twilio.agent_heard == ["I need a check-up"]
    assert "Booked. Goodbye." in reply
    assert session.closed_by_agent and session.close_reason == "the agent hung up"
    (call,) = twilio.calls.values()
    assert call["to"] == AGENT and call["from"] == OURS and call["timeout"] == "5"
    assert "<Connect><Stream url=\"ws://127.0.0.1:" in call["twiml"]
    assert '<Parameter name="test_id" value="t-20261105-0001-abcd"/>' in call["twiml"]
    assert session.call_meta == {"provider": "twilio", "call_sid": call["sid"], "to": AGENT, "from": OURS,
                                 "status": "completed", "duration_s": 4, "price": 0.014, "price_unit": "USD"}


@pytest.mark.parametrize("answer,words", [("busy", "the number was busy"), ("no-answer", "no one answered")])
def test_a_call_that_is_not_answered_says_why(answer, words):
    fake = FakeTwilio(answer)
    try:
        with pytest.raises(ConnectError, match=words):
            asyncio.run(_call(profile(fake.api), "hello"))
    finally:
        fake.close()


def test_wrong_credentials_and_numbers_are_refused_in_plain_words(twilio):
    with pytest.raises(ConnectError, match="Twilio refused the account SID or auth token"):
        asyncio.run(_call(profile(twilio.api, auth_token="wrong"), "hello"))
    with pytest.raises(ConnectError, match="international format"):
        check_number("0415 555 0100", "The agent's phone number")
    assert check_number("+1 (415) 555-0100", "x") == "+14155550100"
    with pytest.raises(ConnectError, match="not set"):
        asyncio.run(_call(profile(twilio.api, auth_token=""), "hello"))
    assert not twilio.calls          # nothing was dialled
    with pytest.raises(ConnectError, match="must start with https://"):
        asyncio.run(_call(profile(twilio.api, public_url="http://example.com"), "hello"))
    p = profile(twilio.api, auth_token="${VOCTO_TEST_UNSET_TOKEN}")
    with pytest.raises(ConnectError, match=r"^VOCTO_TEST_UNSET_TOKEN has no value yet: add it in Settings › Secrets"):
        asyncio.run(_call(p, "hello"))
    assert not twilio.calls


def test_phone_call_is_a_voice_connection_the_schema_accepts():
    p = {"schema_version": "0.1", "kind": "client_profile", "id": "x", "name": "x", "domain": "general",
         "connection": {"type": "phone_call", "target": AGENT,
                        "options": {"account_sid": "${TWILIO_ACCOUNT_SID}", "auth_token": "${TWILIO_AUTH_TOKEN}",
                                    "from_number": OURS}}}
    assert not [e for e in schema_errors("client_profile", p) if e.startswith("connection")]
    assert is_voice(p) and PhoneCallConnector.sample_rate == 8000
    assert ws_url("https://abc.trycloudflare.com/") == "wss://abc.trycloudflare.com"


def _fake_program(folder: Path, name: str, code: str) -> Path:
    """A stand-in program written in Python: a script with a #! line on Linux and macOS, and on Windows (which
    can't run a script by its #! line) a .cmd file that runs the script with this Python."""
    import os
    import sys

    script = folder / f"{name}.py"
    script.write_text(code)
    if os.name == "nt":
        exe = folder / f"{name}.cmd"
        exe.write_text(f'@"{sys.executable}" "{script}" %*\r\n')
        return exe
    exe = folder / name
    exe.write_text(f"#!{sys.executable}\n{code}")
    exe.chmod(0o755)
    return exe


def test_the_quick_tunnel_address_is_read_from_cloudflared(tmp_path):
    """cloudflared prints the random trycloudflare.com address in its log; Vocto reads it from there.
    (A stand-in program here: Vocto's tests never open a real public tunnel.)"""
    from vatf.connectors.tunnel import QuickTunnel, TunnelError

    fake = tmp_path / "cloudflared"
    fake = _fake_program(tmp_path, "cloudflared",
                         "import sys, time\n"
                         "print('INF Requesting new quick Tunnel on trycloudflare.com...', flush=True)\n"
                         "print('INF |  https://silly-words-here-1234.trycloudflare.com  |', flush=True)\n"
                         "time.sleep(30)\n")
    t = QuickTunnel(fake, 8765)
    try:
        assert t.wait(10) == "https://silly-words-here-1234.trycloudflare.com" and t.alive()
    finally:
        t.stop()
    assert not t.alive()
    broken = _fake_program(tmp_path, "broken", "print('ERR failed to request quick Tunnel: 429 Too Many Requests')\n")
    with pytest.raises(TunnelError, match="429 Too Many Requests"):
        QuickTunnel(broken, 8765).wait(10)


def test_check_the_twilio_account_then_make_a_test_call(twilio):
    from vatf.web.setup import _test_phone

    p = profile(twilio.api)
    out = asyncio.run(_test_phone(p, ROOT, call=False))
    assert out["ok"] and "Make a test call" in out["message"] and not twilio.calls
    assert "Acme QA (active, full)" in out["details"][0]
    out = asyncio.run(_test_phone(p, ROOT, call=True))
    assert out["ok"], out
    assert out["message"] == "Vocto rang the agent's number and it answered and greeted the caller."
    assert any(d.startswith("Speech heard: ") and "first words" in d for d in out["details"])
    (call,) = twilio.calls.values()
    assert call["status"] == "completed"                       # Vocto hung up
    twilio.account_type = "Trial"
    out = asyncio.run(_test_phone(p, ROOT, call=False))
    assert not out["ok"] and any("key press" in d for d in out["details"])
    out = asyncio.run(_test_phone(profile(twilio.api, from_number="+14155550111"), ROOT, call=False))
    assert not out["ok"] and "is not a number in this Twilio account" in out["message"]


def test_a_new_agent_can_be_a_phone_number(tmp_path):
    from vatf.web.setup import new_project

    out = new_project(tmp_path, {"name": "Acme Phone Desk", "channel": "voice", "voice_format": "phone",
                                 "target": " +14155550123 "})
    import yaml

    p = yaml.safe_load((tmp_path / out["path"] / "profile.yaml").read_text(encoding="utf-8"))
    assert p["connection"]["type"] == "phone_call" and p["connection"]["target"] == "+14155550123"
    assert p["connection"]["options"]["auth_token"] == "${TWILIO_AUTH_TOKEN}"     # a secret name, never a value
    assert not [e for e in schema_errors("client_profile", p) if e.startswith("connection")]


# ---- Plivo ------------------------------------------------------------------------------------

AUTH_ID = "MA" + "2" * 18


class FakePlivo:
    """Plivo's Voice API plus a phone network: like Plivo, it fetches the call's instructions from the
    answer URL (GET), streams to the <Stream> address, and reports a call that never connected to the
    hangup URL. Its call records appear a moment after the call ends, as Plivo's do."""

    def __init__(self, answer: str = "answer"):
        self.answer = answer
        self.account_type = "standard"
        self.requests: dict[str, dict] = {}
        self.cdrs: dict[str, dict] = {}
        self.deleted: list[str] = []
        self.answer_xml: list[str] = []
        self.played: list[dict] = []
        self.agent_heard: list[str] = []
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body=None):
                raw = json.dumps(body).encode() if body is not None else b""
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _authorised(self):
                want = "Basic " + base64.b64encode(f"{AUTH_ID}:{TOKEN}".encode()).decode()
                if self.headers.get("Authorization") != want:
                    self._send(401, {"api_id": "x", "error": "Authentication credentials invalid."})
                    return False
                return True

            def do_GET(self):
                if not self._authorised():
                    return
                base = f"/v1/Account/{AUTH_ID}"
                if self.path == base + "/":
                    return self._send(200, {"account_type": fake.account_type, "name": "Acme QA", "auth_id": AUTH_ID,
                                            "cash_credits": "12.50000"})
                if self.path == base + "/Number/14155550199/":
                    return self._send(200, {"number": "14155550199"})
                m = re.fullmatch(base + r"/Call/([\w-]+)/", self.path)
                if m and m.group(1) in fake.cdrs:
                    return self._send(200, fake.cdrs[m.group(1)])
                self._send(404, {"api_id": "x", "error": "not found"})

            def do_POST(self):
                if not self._authorised():
                    return
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                assert self.path == f"/v1/Account/{AUTH_ID}/Call/"
                rid = f"req-{len(fake.requests) + 1}"
                fake.requests[rid] = body
                asyncio.run_coroutine_threadsafe(fake._network(rid, body), fake.loop)
                self._send(201, {"api_id": "x", "message": "call fired", "request_uuid": rid})

            def do_DELETE(self):
                if not self._authorised():
                    return
                fake.deleted.append(self.path)
                self._send(204)

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.api = f"http://127.0.0.1:{self.http.server_address[1]}/v1"

    async def _get(self, url: str) -> str:
        import urllib.request

        def get():
            with urllib.request.urlopen(url, timeout=5) as r:
                assert r.headers.get("Content-Type", "").startswith(("application/xml", "text/plain"))
                return r.headers.get("Content-Type", "") + "\n" + r.read().decode()
        return await asyncio.to_thread(get)

    async def _network(self, rid: str, body: dict) -> None:
        from urllib.parse import urlencode

        import websockets

        await asyncio.sleep(0.3)
        if self.answer != "answer":
            await self._get(body["hangup_url"] + "?" + urlencode({
                "CallStatus": self.answer, "HangupCauseName": {"busy": "Busy Line"}.get(self.answer, "No Answer"),
                "RequestUUID": rid}))
            return
        uuid = f"call-{rid}"
        got = await self._get(body["answer_url"] + "?" + urlencode({"CallUUID": uuid, "CallStatus": "in-progress"}))
        self.answer_xml.append(got)
        url = re.search(r"<Stream [^>]*>([^<]+)</Stream>", got).group(1)
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps({"event": "start", "sequenceNumber": 0, "extra_headers": "test_id=x",
                                      "start": {"callId": uuid, "streamId": "st-1", "accountId": AUTH_ID, "tracks": ["inbound"],
                                                "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000}}}))

            async def say(text):
                pcm = pcm16_to_ulaw(to_pcm16(tones(text)))
                await paced(range(0, len(pcm), 160), lambda i: ws.send(json.dumps({"event": "media", "streamId": "st-1", "media": {
                            "track": "inbound", "timestamp": str(int(time.time() * 1000)), "chunk": i // 160,
                            "payload": base64.b64encode(pcm[i:i + 160]).decode()}})))

            await say("Riverside clinic, how can I help?")
            got_audio: list[np.ndarray] = []
            quiet, spoke = 0, False
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("event") != "playAudio":
                    continue
                if not self.played:
                    self.played.append({k: v for k, v in msg["media"].items() if k != "payload"})
                x = from_pcm16(ulaw_to_pcm16(base64.b64decode(msg["media"]["payload"])))
                got_audio.append(x)
                loud = float(np.abs(x).max()) > 0.05
                spoke, quiet = spoke or loud, 0 if loud else quiet + 1
                if spoke and quiet > 40:
                    break
            self.agent_heard.append(heard(np.concatenate(got_audio)))
            await say("Booked. Goodbye.")
        # the agent hung up: Plivo closes the stream; the call record shows up a moment later
        await asyncio.sleep(1.0)
        self.cdrs[uuid] = {"call_uuid": uuid, "call_duration": 4, "bill_duration": 4, "total_amount": "0.01200",
                           "hangup_cause_name": "Normal Hangup", "hangup_cause_code": 4000}

    def close(self):
        self.http.shutdown()
        self.loop.call_soon_threadsafe(self.loop.stop)


def plivo_profile(api: str, **options):
    return {"connection": {"type": "phone_call", "target": AGENT, "options": {
        "provider": "plivo", "account_sid": AUTH_ID, "auth_token": TOKEN, "from_number": OURS,
        "public_url": "local", "api_base": api, "ring_timeout_s": 5, **options}}}


def test_a_plivo_call_goes_both_ways_with_its_own_stream_messages():
    fake = FakePlivo()
    try:
        session, greeting, reply = asyncio.run(_call(plivo_profile(fake.api), "I need a check-up"))
        assert greeting.startswith("Riverside clinic, how can I help?")
        assert fake.agent_heard == ["I need a check-up"]
        assert "Booked. Goodbye." in reply
        assert session.closed_by_agent
        (req,) = fake.requests.values()
        assert req["to"] == "14155550100" and req["from"] == "14155550199" and req["ring_timeout"] == 5
        assert req["answer_method"] == "GET" and "/answer/" in req["answer_url"] and "/hangup/" in req["hangup_url"]
        (xml,) = fake.answer_xml
        assert xml.startswith("application/xml")
        assert '<Stream bidirectional="true" keepCallAlive="true" contentType="audio/x-mulaw;rate=8000"' in xml
        assert 'extraHeaders="test_id=t-20261105-0001-abcd"' in xml
        assert fake.played == [{"contentType": "audio/x-mulaw", "sampleRate": 8000}]
        assert session.call_meta == {"provider": "plivo", "call_sid": "call-req-1", "to": AGENT, "from": OURS,
                                     "status": "completed", "duration_s": 4, "price": 0.012, "price_unit": "USD",
                                     "hangup_cause": "Normal Hangup"}
        assert not fake.deleted                     # the agent hung up: nothing to hang up
    finally:
        fake.close()


@pytest.mark.parametrize("answer,words", [("busy", "the number was busy"), ("no-answer", "no one answered")])
def test_a_plivo_call_that_is_not_answered_says_why(answer, words):
    fake = FakePlivo(answer)
    try:
        with pytest.raises(ConnectError, match=words):
            asyncio.run(_call(plivo_profile(fake.api), "hello"))
    finally:
        fake.close()


def test_plivo_credentials_and_the_account_check():
    from vatf.web.setup import _test_phone

    fake = FakePlivo()
    try:
        with pytest.raises(ConnectError, match="Plivo refused the Auth ID or Auth token"):
            asyncio.run(_call(plivo_profile(fake.api, auth_token="wrong"), "hello"))
        out = asyncio.run(_test_phone(plivo_profile(fake.api), ROOT, call=False))
        assert out["ok"], out
        assert out["details"][0] == "Plivo account: Acme QA (standard, credit 12.50000 USD)"
        assert "your Plivo number" in out["details"][1] and not fake.requests
        out = asyncio.run(_test_phone(plivo_profile(fake.api, from_number="+14155550111"), ROOT, call=False))
        assert out["ok"] and "is not one of the numbers rented in this Plivo account" in out["details"][-1]
        p = plivo_profile(fake.api, auth_token="${VOCTO_TEST_UNSET_TOKEN}")
        with pytest.raises(ConnectError, match="Plivo console"):
            asyncio.run(_call(p, "hello"))
    finally:
        fake.close()
