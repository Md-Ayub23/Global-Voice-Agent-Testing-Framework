"""Testing an agent Vocto does not run itself: the text_http connector, the tool gateway, REST and
Postgres probes, the logs collector and log checks, and setting a project up from the dashboard."""

import asyncio
import json
import os
import shutil
import socket
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from vatf.checks import evaluate_check
from vatf.contracts.models import CallContext, Result, TraceEvent
from vatf.gateway import ToolGateway, uses_gateway
from vatf.loader import load_project
from vatf.orchestrator import Orchestrator, RunConfig, RunSetupError
from vatf.probe import BackendProbe
from vatf.web import setup
from zoo.clinic import service

ROOT = Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(handler) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# ---- an outside agent: text over HTTP, tools through the gateway, a log file ----------------------

@pytest.fixture
def outside_clinic(tmp_path):
    """examples/clinic-service with the clinic service running on free ports, in this process."""
    import logging

    gw_port = free_port()
    log_file = tmp_path / "agent.log"
    handler = logging.FileHandler(log_file, encoding="utf-8")
    service.log.addHandler(handler)
    service.log.setLevel(logging.INFO)
    servers = []

    def start(bugs: str = ""):
        options = {"brain": "rules", "bugs": bugs}
        api = serve(service.api_handler())
        text = serve(service.text_handler(f"http://127.0.0.1:{gw_port}/tools", options))
        servers.extend([api, text])
        folder = tmp_path / f"clinic-service-{len(servers)}"
        shutil.copytree(ROOT / "examples" / "clinic-service", folder)
        p = yaml.safe_load((folder / "profile.yaml").read_text(encoding="utf-8"))
        p["connection"]["target"] = f"http://127.0.0.1:{text.server_address[1]}"
        p["tool_gateway"] = {"port": gw_port}
        for t in p["tools"]:
            t["upstream"] = f"http://127.0.0.1:{api.server_address[1]}/tools/{t['name']}"
        p["logs"] = {"source": "file", "path": str(log_file), "match": "test_id", "settle_ms": 50}
        (folder / "profile.yaml").write_text(yaml.safe_dump(p, sort_keys=False), encoding="utf-8")
        return load_project(folder)

    yield start
    for s in servers:
        s.shutdown()
    service.log.removeHandler(handler)
    handler.close()


def run(project, tmp_path, scenarios):
    cfg = RunConfig(root=ROOT, runs_dir=tmp_path / "runs", offline=True, repeats=1, scenario_ids=tuple(scenarios))
    return asyncio.run(Orchestrator(project, cfg, log=lambda *_: None).run())


def test_outside_agent_is_tested_through_its_addresses_only(outside_clinic, tmp_path):
    project = outside_clinic()
    assert uses_gateway(project.profile)
    decision = run(project, tmp_path, ["clinic-service.book-checkup", "clinic-service.book-checkup.tool-timeout"])
    assert decision["decision"] == "GO", decision["reasons"]
    call = next(c for c in decision["calls"] if c["scenario_id"] == "clinic-service.book-checkup")
    folder = tmp_path / "runs" / decision["run_id"] / "calls" / call["test_id"]
    events = [json.loads(line) for line in (folder / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    types = [e["type"] for e in events]
    # the agent's tool calls were seen on the way through the gateway, and its log lines kept
    assert [e["data"]["tool"] for e in events if e["type"] == "tool_request"] == \
        ["find_patient", "list_free_slots", "book_appointment"]
    collected = next(e for e in events if e["type"] == "platform_event" and e["data"]["kind"] == "log_collected")
    assert collected["data"]["ok"] and collected["data"]["lines"] >= 4
    assert all(call["test_id"] in e["data"]["text"] for e in events
               if e["type"] == "platform_event" and e["data"]["kind"] == "log")
    assert types.index("probe_result") > types.index("call_ended")
    verdict = json.loads((folder / "verdict.json").read_text(encoding="utf-8"))
    assert {c["check_id"]: c["result"] for c in verdict["checks"]}["no-errors-in-agent-log"] == "PASS"


def test_outside_agent_bug_is_caught(outside_clinic, tmp_path):
    decision = run(outside_clinic("lie_on_failure"), tmp_path, ["clinic-service.book-checkup.tool-timeout"])
    assert decision["decision"] == "NO_GO"
    assert any("FALSE_SUCCESS" in r["message"] for r in decision["reasons"])


def test_gateway_refuses_calls_between_test_calls():
    gw = ToolGateway(port=0).start()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{gw.port}/tools/x", data=b"{}", method="POST")
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=5)
        assert e.value.code == 503
        with urllib.request.urlopen(f"http://127.0.0.1:{gw.port}/health", timeout=5) as r:
            assert json.loads(r.read())["call_in_progress"] is False
    finally:
        gw.stop()


def test_missing_secret_stops_the_run_before_any_call(tmp_path, monkeypatch):
    project = load_project(ROOT / "examples" / "clinic-service")
    project.profile["tools"][0]["headers"] = {"Authorization": "Bearer ${VOCTO_TEST_NO_SUCH_TOKEN}"}
    monkeypatch.delenv("VOCTO_TEST_NO_SUCH_TOKEN", raising=False)
    with pytest.raises(RunSetupError, match="VOCTO_TEST_NO_SUCH_TOKEN"):
        run(project, tmp_path, ["clinic-service.book-checkup"])
    assert not (tmp_path / "runs").exists()


# ---- probes ------------------------------------------------------------------------------------

def ctx(facts=None) -> CallContext:
    return CallContext(test_id="t-1", run_id="r-1", profile={}, scenario={"caller": {"facts": facts or {}}},
                       repeat=1, attempt=1, mode="text")


def test_rest_probe(monkeypatch):
    seen = {}

    class API(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            seen["path"], seen["auth"] = self.path, self.headers.get("Authorization")
            if self.path.startswith("/missing"):
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps({"data": {"items": [{"id": 7, "name": "Maria Lopez"}]}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

    server = serve(API)
    monkeypatch.setenv("VOCTO_TEST_TOKEN", "abc")
    profile = {"backend": {"access": "rest", "connection": f"http://127.0.0.1:{server.server_address[1]}",
                           "headers": {"Authorization": "Bearer ${VOCTO_TEST_TOKEN}"}, "probes": [
        {"id": "p", "query": "GET /bookings?name=:full_name&t=:test_id", "rows_field": "data.items"},
        {"id": "n", "query": "GET /bookings?t=:test_id", "rows_field": "data.items", "returns": "count"},
        {"id": "gone", "query": "GET /missing/:test_id"},
        {"id": "gone-ok", "query": "GET /missing/:test_id", "empty_on_404": True}]}}
    try:
        probe = BackendProbe(profile, ROOT)
        r = probe.run("p", ctx({"full_name": "Maria Lopez"}))
        assert r.ok and r.rows == [{"id": 7, "name": "Maria Lopez"}]
        assert seen == {"path": "/bookings?name=Maria%20Lopez&t=t-1", "auth": "Bearer abc"}
        assert probe.run("n", ctx()).value == 1
        assert not probe.run("gone", ctx()).ok           # a 404 is a probe error unless told otherwise
        assert probe.run("gone-ok", ctx()).rows == []
    finally:
        server.shutdown()


PG_BIN = Path("/usr/lib/postgresql/16/bin")


@pytest.mark.skipif(not (PG_BIN / "initdb").exists() or os.name == "nt", reason="needs a local Postgres to start")
def test_postgres_probe_and_fixtures(tmp_path):
    pytest.importorskip("psycopg")
    import tempfile

    tmp_path = Path(tempfile.mkdtemp(prefix="vocto-pg-"))   # Postgres refuses to run as root: as root, run it as nobody
    tmp_path.chmod(0o777)
    data, port = tmp_path / "pg", free_port()
    root_user = os.geteuid() == 0
    if root_user:
        data.mkdir()
        shutil.chown(data, "nobody")
    sudo = ["runuser", "-u", "nobody", "--"] if root_user else []
    subprocess.run([*sudo, str(PG_BIN / "initdb"), "-D", str(data), "-A", "trust", "-U", "vocto"], check=True,
                   capture_output=True)
    subprocess.run([*sudo, str(PG_BIN / "pg_ctl"), "-D", str(data), "-o", f"-p {port} -k {tmp_path}", "-l",
                    str(tmp_path / "pg.log"), "-w", "start"], check=True, capture_output=True)
    try:
        profile = {"backend": {"access": "sql", "connection": f"postgresql://vocto@127.0.0.1:{port}/postgres",
                               "probes": [{"id": "rows", "query": "SELECT id, name, at::text FROM t WHERE name = :name AND note LIKE '50%'"},
                                          {"id": "count", "query": "SELECT COUNT(*) FROM t", "returns": "count"},
                                          {"id": "write", "query": "INSERT INTO t (name) VALUES ('x')"}]}}
        scenario = {"setup": {"fixtures": ["DROP TABLE IF EXISTS t; CREATE TABLE t (id serial, name text, note text, at date DEFAULT '2026-11-03');"
                                           " INSERT INTO t (name, note) VALUES ('Maria', '50% off'), ('Ravi', 'x');"]}}
        from vatf.probe import load_fixtures

        load_fixtures(profile, scenario, ROOT)
        probe = BackendProbe(profile, ROOT)
        r = probe.run("rows", ctx({"name": "Maria"}))
        assert r.ok, r.error
        assert r.rows == [{"id": 1, "name": "Maria", "at": "2026-11-03"}]
        assert probe.run("count", ctx()).value == 2
        w = probe.run("write", ctx())
        assert not w.ok and "read-only" in w.error            # probes can never change the backend
        assert setup.check_backend(profile, ROOT).startswith("Connected to Postgres")
    finally:
        subprocess.run([*sudo, str(PG_BIN / "pg_ctl"), "-D", str(data), "-m", "immediate", "stop"], capture_output=True)
        shutil.rmtree(tmp_path, ignore_errors=True)


# ---- log checks --------------------------------------------------------------------------------

def ev(seq, type_, data):
    return TraceEvent(seq, seq * 10, "platform", type_, data)


def test_log_checks():
    events = [ev(1, "platform_event", {"kind": "log_collected", "ok": True, "from": "x", "lines": 2}),
              ev(2, "platform_event", {"kind": "log", "text": "INFO booked"}),
              ev(3, "platform_event", {"kind": "log", "text": "ERROR db timeout"})]
    check = lambda t, p: evaluate_check({"type": t, "pattern": p}, "c", events, events, {}, "s", "text")  # noqa: E731
    assert check("log_not_contains", r"\bERROR\b").result == Result.FAIL
    assert check("log_not_contains", r"\bCRITICAL\b").result == Result.PASS
    assert check("log_contains", "booked").result == Result.PASS
    assert check("log_contains", "refund").result == Result.FAIL
    failed = [ev(1, "platform_event", {"kind": "log_collected", "ok": False, "from": "x", "lines": 0, "error": "no file"})]
    r = evaluate_check({"type": "log_not_contains", "pattern": "E"}, "c", failed, failed, {}, "s", "text")
    assert r.result == Result.NOT_VERIFIED       # missing evidence is never PASS


# ---- setting a project up from the dashboard ---------------------------------------------------

@pytest.fixture
def root(tmp_path):
    (tmp_path / ".gitignore").write_text("/runs/\n", encoding="utf-8")
    return tmp_path


def test_new_project_then_forms(root):
    made = setup.new_project(root, {"name": "Acme Bank cards", "channel": "voice", "target": "wss://x.example/call"})
    assert made["path"] == "projects/acme-bank-cards"
    assert "/projects/" in (root / ".gitignore").read_text(encoding="utf-8")
    detail = setup.profile_detail(root, made["path"], "profile.yaml")
    assert detail["merged"]["connection"]["type"] == "websocket_audio"
    assert any("no scenarios" in p for p in detail["problems"])

    bad = setup.save_profile(root, made["path"], "profile.yaml", {"tools": [{"name": "block_card", "effect": "maybe"}]})
    assert not bad["saved"] and "maybe" in " ".join(bad["problems"])
    ok = setup.save_profile(root, made["path"], "profile.yaml", {
        "tools": [{"name": "block_card", "effect": "write", "upstream": "https://api.example/block", "headers": {"X-Key": "${ACME_KEY}"}}],
        "backend": {"access": "rest", "connection": "https://api.example", "probes": [{"id": "blocks", "query": "GET /blocks?t=:test_id"}]},
        "thresholds": {"latency_p50_ms": 700, "latency_p95_ms": 1200}, "tool_gateway": {}})
    assert ok["saved"], ok["problems"]
    text = (root / made["path"] / "profile.yaml").read_text(encoding="utf-8")
    assert text.startswith("# Client profile for Acme Bank cards") and "tool_gateway" not in text
    assert ok["detail"]["gateway"]["tools"] == {"block_card": "http://127.0.0.1:8790/tools/block_card"}

    scenario = {"id": "acme-bank-cards.lost-card", "title": "Caller reports a lost card", "profile": "acme-bank-cards",
                "criticality": "critical", "style": "simulated",
                "caller": {"persona": "nobody", "goal": "Block my card", "facts": {"last4": "4417"}}}
    refused = setup.save_doc(root, made["path"], "scenarios/lost-card.yaml", scenario, create=True)
    assert not refused["saved"] and "unknown persona" in " ".join(refused["problems"])
    scenario["caller"]["persona"] = "calm-adult"
    scenario["outcome"] = {"expected": "success", "action": {"tool": "block_card"}, "record": {"probe": "blocks", "expect_count": 1}}
    saved = setup.save_doc(root, made["path"], "scenarios/lost-card.yaml", scenario, create=True)
    assert saved["saved"], saved["problems"]
    assert setup.profile_detail(root, made["path"], "profile.yaml")["problems"] == []
    load_project(root / made["path"])


def test_form_saves_keep_hand_written_comments(root):
    shutil.copytree(ROOT / "examples" / "clinic", root / "examples" / "clinic", ignore=shutil.ignore_patterns("*.db"))
    r = setup.save_profile(root, "examples/clinic", "profile-voice.yaml", {"thresholds": {"latency_p50_ms": 3000, "latency_p95_ms": 6000}})
    assert r["saved"], r["problems"]
    text = (root / "examples/clinic/profile-voice.yaml").read_text(encoding="utf-8")
    assert "# Production voice agents should answer within" in text and "latency_p50_ms: 3000" in text
    assert "# vatf starts the zoo agent's voice front-end" in text


def test_connection_test_against_a_text_agent(outside_clinic, root):
    project = outside_clinic()
    folder = root / "projects" / "c"
    shutil.copytree(project.folder, folder)
    r = setup.test_connection(root, "projects/c", "profile.yaml")
    assert r["ok"] and "Riverside Clinic" in r["details"][0]
    p = yaml.safe_load((folder / "profile.yaml").read_text(encoding="utf-8"))
    p["connection"]["target"] = f"http://127.0.0.1:{free_port()}"
    (folder / "profile.yaml").write_text(yaml.safe_dump(p), encoding="utf-8")
    assert not setup.test_connection(root, "projects/c", "profile.yaml")["ok"]
