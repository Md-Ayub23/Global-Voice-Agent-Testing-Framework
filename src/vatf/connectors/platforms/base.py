"""What every hosted-platform connection shares: the platform's HTTP API, keeping its key secret, the live
call session, and the call record the platform keeps after the call (its "report").

A platform connection is a voice connection like websocket_audio: the session has send_frame() for the
caller's 20 ms frames, records the agent's audio in the Recorder, and says when the agent hung up. On top:

  transcripts   (sample, text) of what the platform says its agent said (kept for comparison only)
  heard         (sample, text) of what the platform's speech recognition heard the caller say
  live_tools    {tool call id: call-clock ms} for tool calls the platform announced while the call ran
  handled_tools tool call ids Vocto answered itself through its tool proxy (ElevenLabs client tools)
  call_meta     the platform's call id and a few facts, shown with the call
  report()      after close(): the platform's own record of the call (tool calls with arguments and
                results, why it ended, cost, latency), read from its API (PlatformReport)

Keys: a platform key is only ever read from the environment / .env (${NAME} in the profile), sent only to
the platform's own API host, and scrubbed from every message Vocto keeps or shows (Secrets.scrub).
The API hosts are fixed; VOCTO_<PLATFORM>_API_BASE may change one (tests, a private deployment), but only
on the machine running Vocto, never from an agent's settings, so nobody can point a key at another server.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping

import numpy as np

from ... import netguard
from ...audio.recorder import Recorder, play_agent
from ...util import MissingSecret, expand_env
from .. import ConnectError

SAMPLE_RATE = 16000          # every platform session runs at 16 kHz PCM, the rate all four accept
JITTER_MS = 60
HTTP_TIMEOUT_S = 20
REPORT_WAIT_S = 45           # how long to wait for the platform to finish its record of a call
REPORT_POLL_S = 1.5
MASK = "•••"


class PlatformError(ConnectError):
    """The platform refused or could not be reached. A harness error: never the agent's fault."""


class Secrets:
    """The secret values a session uses, so no message Vocto keeps or shows ever contains one."""

    def __init__(self) -> None:
        self._values: set[str] = set()

    def add(self, value: str | None) -> str | None:
        if value and len(value) >= 6:
            self._values.add(value)
        return value

    def scrub(self, text: Any) -> str:
        s = str(text)
        for v in sorted(self._values, key=len, reverse=True):
            s = s.replace(v, MASK)
        return s


def api_base(platform: str, default: str) -> str:
    """The platform's API address. Only the environment of the machine running Vocto can change it."""
    return (os.environ.get(f"VOCTO_{platform.upper()}_API_BASE") or default).rstrip("/")


def option(profile: Mapping[str, Any], name: str, default: Any = None) -> Any:
    return (profile.get("connection", {}).get("options") or {}).get(name, default)


def secret_option(profile: Mapping[str, Any], name: str, secrets: Secrets, *, required: bool = True,
                  label: str = "") -> str | None:
    """A secret from the connection's options: written as ${NAME} in the profile, the value from .env.
    A literal value is refused, so a key typed into an agent's settings never sits in a project file."""
    raw = option(profile, name)
    if raw in (None, ""):
        if required:
            raise PlatformError(f"{label or name} is not set: save it in Connect (it is kept in Settings › Secrets)")
        return None
    if not (isinstance(raw, str) and raw.strip().startswith("${") and raw.strip().endswith("}")):
        raise PlatformError(f"{label or name} must be a saved secret (${{NAME}}), not the key itself: "
                            "paste the key in Connect and Vocto keeps it in Settings › Secrets")
    try:
        value = str(expand_env(raw)).strip()
    except MissingSecret as e:
        raise PlatformError(str(e)) from e
    if not value:
        raise PlatformError(f"{label or name} ({raw}) is empty: paste the key again in Connect")
    return secrets.add(value)


def call_variables(ctx: Any, profile: Mapping[str, Any]) -> dict[str, str]:
    """Values the platform puts into the agent's prompt ({{name}} variables): the connection's own
    `variables` (a "$fact" takes the test caller's fact), the test id when the profile passes it to the
    agent (session_metadata), and the scenario's "now"."""
    from .. import call_metadata

    sc = getattr(ctx, "scenario", None) or {}
    facts = (sc.get("caller") or {}).get("facts") or {}
    out: dict[str, str] = {}
    for k, v in (option(profile, "variables") or {}).items():
        if isinstance(v, str) and v.startswith("$") and v[1:] in facts:
            v = facts[v[1:]]
        try:
            v = expand_env(v)
        except MissingSecret:
            continue
        out[str(k)] = v if isinstance(v, str) else json.dumps(v)
    if ctx is not None and "session_metadata" in (profile.get("test_id") or {}).get("propagation", []):
        out["vocto_test_id"] = ctx.test_id
    now = (sc.get("setup") or {}).get("now")
    if now and "now" not in out and option(profile, "send_now", False):
        out["now"] = str(now)
    meta = call_metadata(ctx)
    if meta.get("from"):
        out.setdefault("caller_number", str(meta["from"]))
    return out


class PlatformAPI:
    """A small JSON client for one platform's REST API. Every request goes to `base` (a fixed platform host),
    through the SSRF guard; errors say what the platform answered, never the key."""

    def __init__(self, platform: str, base: str, headers: Mapping[str, str], secrets: Secrets):
        self.platform, self.base, self.headers, self.secrets = platform, base, dict(headers), secrets

    def url(self, path: str, query: Mapping[str, Any] | None = None) -> str:
        q = ("?" + urllib.parse.urlencode({k: v for k, v in (query or {}).items() if v is not None})) if query else ""
        return self.base + "/" + path.lstrip("/") + q

    def _do(self, method: str, path: str, body: Any, query: Mapping[str, Any] | None, timeout: float) -> Any:
        url = self.url(path, query)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Accept": "application/json", "User-Agent": "vocto",
                                              **({"Content-Type": "application/json"} if data is not None else {}),
                                              **self.headers})
        try:
            with netguard.urlopen(req, timeout=timeout) as resp:
                raw = netguard.read_limited(resp).decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = netguard.read_limited(e, 4000).decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001 - the status alone still says enough
                pass
            raise PlatformError(self._explain(e.code, detail)) from None
        except (urllib.error.URLError, OSError) as e:
            raise PlatformError(self.secrets.scrub(
                f"could not reach {self.platform} ({self.base}): {getattr(e, 'reason', e)}")) from None
        if not raw.strip():
            return None
        try:
            return json.loads(raw)
        except ValueError:
            raise PlatformError(f"{self.platform} answered something that is not JSON") from None

    def _explain(self, code: int, detail: str) -> str:
        msg = _message(detail)
        name = self.platform
        if code in (401, 403):
            head = f"{name} refused the API key (HTTP {code})"
        elif code == 404:
            head = f"{name} has no such agent or call (HTTP 404): check the agent id"
        elif code == 429:
            head = f"{name} says too many requests (HTTP 429): try again in a minute, or check the account's limits"
        elif code == 402:
            head = f"{name} says the account needs credit (HTTP 402)"
        else:
            head = f"{name} answered HTTP {code}"
        return self.secrets.scrub(head + (f": {msg}" if msg else ""))

    async def request(self, method: str, path: str, body: Any = None, query: Mapping[str, Any] | None = None,
                      timeout: float = HTTP_TIMEOUT_S) -> Any:
        return await asyncio.to_thread(self._do, method, path, body, query, timeout)


def _message(detail: str) -> str:
    """The useful part of an error body: its message field, else its first 200 characters."""
    try:
        doc = json.loads(detail)
    except ValueError:
        return detail.strip()[:200]
    if isinstance(doc, Mapping):
        for k in ("message", "error", "detail", "error_message"):
            v = doc.get(k)
            if isinstance(v, str) and v:
                return v[:300]
            if isinstance(v, list) and v:
                return json.dumps(v)[:300]
            if isinstance(v, Mapping) and v.get("message"):
                return str(v["message"])[:300]
    return json.dumps(doc)[:200]


@dataclass
class PlatformReport:
    """The platform's own record of one call, in Vocto's terms.

    call         the call as intake.py reads platform exports: items (turns and tools in order, times in ms from
                 the platform's call start), ended_reason, transferred, duration_ms ...
    summary      what is shown with the call and kept in call_ended.platform: platform, call_id, ended_reason,
                 cost, currency, duration_s, latency_ms, recording (whether the platform kept one), link
    error        why the record could not be read (the call itself still counts; only the extra evidence is missing)
    """

    platform: str
    call_id: str | None
    call: dict[str, Any] | None = None
    summary: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class PlatformSession:
    """The live call. Subclasses fill _read() / send_frame() / _hang_up() / fetch_report()."""

    platform = "platform"

    def __init__(self, recorder: Recorder, secrets: Secrets, *, jitter_ms: int = JITTER_MS):
        self.rec = recorder
        self.secrets = secrets
        self.closed = asyncio.Event()
        self.closed_by_agent = False
        self.close_reason = ""
        self.transcripts: list[tuple[int, str]] = []
        self.heard: list[tuple[int, str]] = []
        self.protocol_errors: list[str] = []
        self.call_meta: dict[str, Any] = {}
        self.live_tools: dict[str, int] = {}
        self.handled_tools: set[str] = set()
        self.call_id: str | None = None
        self.t0_ms = recorder.ms(recorder.now_sample())      # the platform's call starts about here
        self._jitter = jitter_ms * recorder.sr // 1000
        self._report: PlatformReport | None = None
        self._closing = False

    # ---- agent side ----
    def play(self, x: np.ndarray) -> None:
        play_agent(self.rec, x, self._jitter)

    def ended(self, reason: str, by_agent: bool = True) -> None:
        if not self.close_reason:
            self.closed_by_agent, self.close_reason = by_agent and not self._closing, self.secrets.scrub(reason)
        self.closed.set()

    def error(self, text: str) -> None:
        if len(self.protocol_errors) < 20:
            self.protocol_errors.append(self.secrets.scrub(text))

    def now_ms(self) -> int:
        return self.rec.ms(self.rec.now_sample())

    async def send_text(self, text: str) -> None:
        raise NotImplementedError(f"{self.platform} is a voice connection: the caller speaks")

    # ---- after the call ----
    async def fetch_report(self) -> PlatformReport:
        return PlatformReport(self.platform, self.call_id, error="this platform keeps no record Vocto can read")

    async def report(self, wait_s: float | None = None) -> PlatformReport:
        """The platform's record of the call (read once, after close()). Never raises."""
        if self._report is None:
            try:
                self._report = await asyncio.wait_for(self.fetch_report(), (wait_s or REPORT_WAIT_S) + HTTP_TIMEOUT_S)
            except asyncio.TimeoutError:
                self._report = PlatformReport(self.platform, self.call_id,
                                              error=f"{self.platform} had not finished its record of the call in time")
            except Exception as e:  # noqa: BLE001 - the call still counts; only the platform's evidence is missing
                self._report = PlatformReport(self.platform, self.call_id,
                                              error=self.secrets.scrub(f"{type(e).__name__}: {e}"))
            if self._report.error:
                self._report.error = self.secrets.scrub(self._report.error)
        return self._report


async def poll(fetch, done, wait_s: float, every_s: float = REPORT_POLL_S) -> Any:
    """Calls fetch() until done(result) or wait_s passes; returns the last result."""
    deadline = time.monotonic() + wait_s
    while True:
        doc = await fetch()
        if done(doc) or time.monotonic() >= deadline:
            return doc
        await asyncio.sleep(every_s)


def report_wait(profile: Mapping[str, Any]) -> float:
    try:
        return max(0.0, min(300.0, float(option(profile, "report_wait_s", REPORT_WAIT_S))))
    except (TypeError, ValueError):
        return REPORT_WAIT_S


def pcm_from_format(raw: bytes, fmt: str) -> tuple[np.ndarray, int]:
    """Platform audio formats ("pcm_16000", "pcm_24000", "ulaw_8000") -> float samples and their rate."""
    from ...audio.g711 import ulaw_to_pcm16
    from ...audio.pcm import from_pcm16

    kind, _, rate = (fmt or "pcm_16000").partition("_")
    sr = int(rate) if rate.isdigit() else SAMPLE_RATE
    if kind == "ulaw":
        return from_pcm16(ulaw_to_pcm16(raw)), sr
    if len(raw) % 2:
        raw = raw[:-1]
    return from_pcm16(raw), sr


def pcm_to_format(x: np.ndarray, sr: int, fmt: str) -> bytes:
    from ...audio.g711 import pcm16_to_ulaw
    from ...audio.pcm import resample, to_pcm16

    kind, _, rate = (fmt or "pcm_16000").partition("_")
    want = int(rate) if rate.isdigit() else SAMPLE_RATE
    y = resample(x, sr, want) if want != sr else x
    return pcm16_to_ulaw(to_pcm16(y)) if kind == "ulaw" else to_pcm16(y)
