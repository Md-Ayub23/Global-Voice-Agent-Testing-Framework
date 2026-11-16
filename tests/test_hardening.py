"""Production hardening: safe file writes, the AI queue (limits, retries, back-off), the runs' waiting
line, API rate limits, request limits and security headers."""

from __future__ import annotations

import asyncio
import http.client
import json
import threading
import time
from pathlib import Path

import pytest

from vatf import ratelimit
from vatf.ratelimit import LLMGate, RateLimiter, TokenBucket, is_retryable, retry_after_s
from vatf.util import file_lock, read_json, write_json, write_text_atomic
from vatf.web import data
from vatf.web.httpbase import BoundedHTTPServer
from vatf.web.jobs import Job, JobManager
from vatf.web.server import App, make_handler

ROOT = Path(__file__).resolve().parent.parent


# ---- files ---------------------------------------------------------------------------------------

def test_atomic_write_never_leaves_half_a_file(tmp_path):
    path = tmp_path / "run.json"
    write_json(path, {"a": 1})
    stop = threading.Event()
    bad = []

    def reader():
        while not stop.is_set():
            try:
                read_json(path)
            except ValueError:
                bad.append(1)

    t = threading.Thread(target=reader)
    t.start()
    for i in range(300):
        write_json(path, {"a": i, "pad": "x" * 20000})
    stop.set()
    t.join()
    assert not bad and read_json(path)["a"] == 299
    assert not [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]      # no temporary files left behind


def test_file_lock_serialises_read_modify_write(tmp_path):
    path = tmp_path / "count.txt"
    write_text_atomic(path, "0")

    def bump():
        for _ in range(50):
            with file_lock(path):
                n = int(path.read_text())
                write_text_atomic(path, str(n + 1))

    threads = [threading.Thread(target=bump) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert path.read_text() == "200"


# ---- rate limits ---------------------------------------------------------------------------------

def test_token_bucket_and_rate_limiter():
    b = TokenBucket(rate=10, burst=2)
    assert b.take() == 0 and b.take() == 0
    wait = b.take()
    assert 0 < wait <= 0.11
    lim = RateLimiter(per_minute=60, burst=3)
    assert [lim.check("a") for _ in range(3)] == [0, 0, 0]
    assert lim.check("a") > 0               # a's bucket is empty...
    assert lim.check("b") == 0              # ...b has its own


class FakeAPIError(Exception):
    def __init__(self, status: int, retry_after: str | None = None):
        super().__init__(f"status {status}")
        self.status_code = status
        self.response = type("R", (), {"headers": {"retry-after": retry_after} if retry_after else {}})()


def test_retry_rules():
    assert is_retryable(FakeAPIError(429)) and is_retryable(FakeAPIError(529)) and is_retryable(FakeAPIError(503))
    assert not is_retryable(FakeAPIError(400)) and not is_retryable(FakeAPIError(401))
    assert retry_after_s(FakeAPIError(429, "7")) == 7.0
    assert retry_after_s(FakeAPIError(429)) is None


@pytest.fixture
def gate(tmp_path, monkeypatch):
    monkeypatch.setenv("VOCTO_LLM_COOLDOWN_FILE", str(tmp_path / "cooldown.json"))
    return LLMGate(concurrency=2, rpm=6000, retries=3, base_delay_s=0.01, max_delay_s=0.05)


def test_ai_queue_retries_rate_limits_then_succeeds(gate, tmp_path):
    tries = []

    async def send():
        tries.append(1)
        if len(tries) < 3:
            raise FakeAPIError(429, "0.01")
        return "ok"

    assert asyncio.run(gate.call(send)) == "ok"
    assert len(tries) == 3 and gate.retried == 2 and gate.rate_limited == 2
    assert (tmp_path / "cooldown.json").exists()          # other Vocto processes were told to wait too


def test_ai_queue_gives_up_and_never_retries_a_bad_request(gate):
    tries = []

    async def bad():
        tries.append(1)
        raise FakeAPIError(400)

    with pytest.raises(FakeAPIError):
        asyncio.run(gate.call(bad))
    assert len(tries) == 1

    async def overloaded():
        tries.append(1)
        raise FakeAPIError(529)

    tries.clear()
    with pytest.raises(FakeAPIError):
        asyncio.run(gate.call(overloaded))
    assert len(tries) == 4                                  # 1 + 3 retries


def test_ai_queue_caps_requests_at_once_across_threads(gate):
    now, peak, lock = [0], [0], threading.Lock()

    async def send():
        with lock:
            now[0] += 1
            peak[0] = max(peak[0], now[0])
        await asyncio.sleep(0.05)
        with lock:
            now[0] -= 1
        return 1

    async def many():
        return await asyncio.gather(*(gate.call(send) for _ in range(6)))

    threads = [threading.Thread(target=lambda: asyncio.run(many())) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak[0] == 2 and gate.running == 0 and gate.waiting == 0


def test_claude_client_goes_through_the_queue(monkeypatch, tmp_path):
    """The real client: the SDK's own retries are off and the queue retries a 429."""
    pytest.importorskip("anthropic")
    import anthropic

    from vatf.contracts.models import LLMMessage, LLMRequest
    from vatf.llm import Budget, ClaudeClient

    monkeypatch.setenv("VOCTO_LLM_COOLDOWN_FILE", str(tmp_path / "cool.json"))
    ratelimit.reset_llm_gate(LLMGate(concurrency=1, rpm=6000, retries=2, base_delay_s=0.01, max_delay_s=0.02))
    try:
        client = ClaudeClient(Budget(1.0), api_key="sk-test")
        assert client._client.max_retries == 0
        calls = []

        class Msg:
            stop_reason = "end_turn"
            model = "claude-haiku-5-5"
            content = [type("B", (), {"type": "text", "text": "hello"})()]
            usage = type("U", (), {"input_tokens": 10, "output_tokens": 5})()

        async def create(**kw):
            calls.append(kw)
            if len(calls) == 1:
                err = anthropic.RateLimitError.__new__(anthropic.RateLimitError)
                err.status_code = 429
                err.response = type("R", (), {"headers": {"retry-after": "0"}})()
                raise err
            return Msg()

        monkeypatch.setattr(client._client.messages, "create", create)
        out = asyncio.run(client.complete(LLMRequest("judge", "claude-haiku-5-5", "sys", (LLMMessage("user", "hi"),), 100)))
        assert out.text == "hello" and len(calls) == 2
    finally:
        ratelimit.reset_llm_gate(None)


# ---- the runs' waiting line ------------------------------------------------------------------------

class FakeJobs(JobManager):
    """Starts nothing: a launched job just says running until finish()."""

    def _launch(self, job):
        job.status = "running"

    def finish(self, job):
        job.status = "finished"
        self._dispatch()


def _project(path):
    return {"path": path}


def test_runs_wait_in_line_one_per_agent(monkeypatch):
    jobs = FakeJobs(ROOT, Path("runs"), max_running=2, max_queued=2)
    monkeypatch.setattr(jobs, "build", lambda req, project: (["run", project["path"]], "vatf run"))
    a1 = jobs.start({}, _project("a"))
    a2 = jobs.start({}, _project("a"))          # same agent: waits
    b1 = jobs.start({}, _project("b"))          # another agent, a free slot: starts (no waiting behind a2)
    assert (a1.status, a2.status, b1.status) == ("running", "queued", "running")
    c1 = jobs.start({}, _project("c"))          # both slots taken: waits
    assert c1.status == "queued" and [a2.position, c1.position] == [1, 2]
    with pytest.raises(Exception, match="already waiting"):
        jobs.start({}, _project("d"))
    assert jobs.run_ids() == set() and jobs.stats()["queued"] == 2
    jobs.finish(a1)
    assert a2.status == "running" and c1.status == "queued" and c1.position == 1
    jobs.finish(b1)
    assert c1.status == "running"


# ---- the HTTP layer ------------------------------------------------------------------------------

@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("VOCTO_API_PER_MINUTE", "6000")
    monkeypatch.setenv("VOCTO_API_BURST", "30")
    app = App(ROOT, Path("runs"))
    httpd = BoundedHTTPServer(("127.0.0.1", 0), make_handler(app), max_connections=8)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1], app
    httpd.shutdown()
    httpd.server_close()


def req(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"X-VATF": "1", **(headers or {})}
    raw = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
    c.request(method, path, body=raw, headers=h)
    r = c.getresponse()
    out = r.status, dict(r.getheaders()), r.read()
    c.close()
    return out


def test_security_headers_and_cross_site_writes(server):
    port, _ = server
    status, headers, _ = req(port, "GET", "/api/projects")
    assert status == 200
    assert headers["X-Frame-Options"] == "DENY" and headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert "Python" not in headers.get("Server", "")
    status, _, body = req(port, "POST", "/api/settings/secrets", {"name": "X", "value": "y"},
                          {"Origin": "https://evil.example"})
    assert status == 403 and b"another site" in body
    status, _, _ = req(port, "GET", "/api/projects", headers={"Host": "evil.example"})
    assert status == 403


def test_request_size_limit(server):
    port, _ = server
    status, _, body = req(port, "POST", "/api/projects/check", b"x" * (6 * 1024 * 1024))
    assert status == 413


def test_api_rate_limit_answers_429_with_retry_after(server, monkeypatch):
    port, app = server
    app.api_limit = RateLimiter(per_minute=60, burst=3)
    got = [req(port, "GET", "/api/projects")[0] for _ in range(5)]
    assert got[:3] == [200, 200, 200] and got[-1] == 429
    status, headers, _ = req(port, "GET", "/api/projects")
    assert status == 429 and int(headers["Retry-After"]) >= 1


def test_run_list_rows_are_cached_until_the_run_changes(tmp_path):
    runs = tmp_path / "runs"
    folder = runs / "r-20260101-000000-abcd"
    (folder / "calls").mkdir(parents=True)
    write_json(folder / "run.json", {"status": "finished", "profile_name": "one"})
    first = data.run_summary(runs, folder.name)
    assert first["profile_name"] == "one"
    time.sleep(0.01)
    write_json(folder / "run.json", {"status": "finished", "profile_name": "two"})
    assert data.run_summary(runs, folder.name)["profile_name"] == "two"


def test_connection_cap_answers_503_when_full(tmp_path):
    import socket
    from http.server import BaseHTTPRequestHandler

    gate = threading.Event()

    class Slow(BaseHTTPRequestHandler):
        def do_GET(self):
            gate.wait(5)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    httpd = BoundedHTTPServer(("127.0.0.1", 0), Slow, max_connections=4)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    socks = []
    try:
        for _ in range(4):
            s = socket.create_connection(("127.0.0.1", port))
            s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
            socks.append(s)
        time.sleep(0.2)
        s = socket.create_connection(("127.0.0.1", port))
        s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        assert s.recv(100).startswith(b"HTTP/1.1 503")
        s.close()
    finally:
        gate.set()
        for s in socks:
            s.close()
        httpd.shutdown()
        httpd.server_close()
