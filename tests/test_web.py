"""Dashboard checks (`vatf ui`): the command it builds, the files it reads and writes, and its
HTTP API, including a real run started and followed through the API (offline, no API calls)."""

import http.client
import json
import re
import shutil
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from vatf.cli import main
from vatf.util import read_json
from vatf.web import data
from vatf.web.jobs import Job, JobError, JobManager
from vatf.web.server import App, make_handler

ROOT = Path(__file__).resolve().parent.parent
HAPPY = "clinic.book-checkup"


@pytest.fixture(scope="module")
def clinic():
    return data.project_by_path(ROOT, "examples/clinic")


@pytest.fixture
def jobs(tmp_path):
    return JobManager(ROOT, Path("runs"))


# ---- the command line built from the New run form ---------------------------------------

def test_run_command(jobs, clinic):
    args, command = jobs.build({"command": "run", "profile": "profile.yaml", "scenarios": [HAPPY], "repeats": 2,
                                "agent_options": {"bugs": "lie_on_failure"}, "offline": True,
                                "model": "claude-haiku-5-5"}, clinic)
    assert args == ["run", "examples/clinic", "--profile", "profile.yaml", "--scenario", HAPPY, "--repeats", "2",
                    "--agent-opt", "bugs=lie_on_failure", "--offline", "--quiet"]
    assert command == ("uv run vatf run examples/clinic --profile profile.yaml --scenario clinic.book-checkup "
                       "--repeats 2 --agent-opt bugs=lie_on_failure --offline")  # --quiet is the dashboard's own


def test_exit_check_command_ignores_scenarios_and_bugs(jobs, clinic):
    args, _ = jobs.build({"command": "exit-check", "profile": "profile.yaml", "scenarios": [HAPPY],
                          "agent_options": {"bugs": "double_booking", "brain": "rules"}, "model": "claude-haiku-5-5"}, clinic)
    assert args == ["exit-check", "examples/clinic", "--profile", "profile.yaml", "--agent-opt", "brain=rules",
                    "--model", "claude-haiku-5-5"]


def test_voice_command_shows_only_non_default_speech_models(jobs, clinic):
    req = {"command": "run", "profile": "profile-voice.yaml", "speech": "local", "stt_model": "base.en",
           "stt_check_model": "medium.en"}
    args, _ = jobs.build(req, clinic)
    assert args[-5:] == ["--speech", "local", "--stt-check-model", "medium.en", "--quiet"]
    args, _ = jobs.build({**req, "speech": "codec"}, clinic)
    assert "--stt-check-model" not in args


@pytest.mark.parametrize("req, message", [
    ({"command": "rm"}, "unknown command"),
    ({"profile": "nope.yaml"}, "has no profile"),
    ({"scenarios": ["clinic.nope"]}, "doesn't run in"),
    ({"repeats": 0}, "between 1 and 50"),
    ({"repeats": "two"}, "whole number"),
    ({"agent_options": {"a b": "x"}}, "not valid"),
    ({"agent_options": {"bugs": "x\n--show-pii"}}, "not valid"),
    ({"model": "x; rm -rf /"}, "not valid"),
])
def test_bad_requests_are_refused(jobs, clinic, req, message):
    with pytest.raises(JobError, match=message):
        jobs.build({"command": "run", "profile": "profile.yaml", **req}, clinic)


def test_only_one_run_at_a_time(jobs, clinic):
    jobs.jobs["j-busy"] = Job("j-busy", "run", {}, [], "")
    with pytest.raises(JobError, match="already"):
        jobs.start({"command": "run", "profile": "profile.yaml"}, clinic)


def test_project_lists_profiles_scenarios_and_bug_switches(clinic):
    profiles = {p["file"]: p for p in clinic["profiles"]}
    assert profiles["profile.yaml"]["mode"] == "text" and profiles["profile-voice.yaml"]["mode"] == "voice"
    assert all(p["ok"] for p in profiles.values())
    bugs = {b["name"] for b in profiles["profile.yaml"]["bugs"]}
    assert {"lie_on_failure", "skip_confirmation", "double_booking", "misread_reference"} <= bugs
    with pytest.raises(data.NotFound):
        data.project_by_path(ROOT, "../")


# ---- the project editor -----------------------------------------------------------------

@pytest.fixture
def root_copy(tmp_path):
    shutil.copytree(ROOT / "examples" / "clinic", tmp_path / "examples" / "clinic",
                    ignore=shutil.ignore_patterns("runs", "*.db", "__pycache__"))
    return tmp_path


def test_editor_refuses_broken_yaml_and_reports_schema_problems(root_copy):
    rel = "scenarios/book-checkup.yaml"
    original = data.read_project_file(root_copy, "examples/clinic", rel)
    broken = data.write_project_file(root_copy, "examples/clinic", rel, original + "\n  bad: [unclosed\n")
    assert broken["saved"] is False and broken["problems"][0].startswith("YAML syntax")
    assert data.read_project_file(root_copy, "examples/clinic", rel) == original
    wrong = data.check_project_text(root_copy, "examples/clinic", rel,
                                    original.replace("criticality: critical", "criticality: banana"))
    assert wrong["syntax"] and not wrong["ok"] and any("banana" in p for p in wrong["problems"])


def test_editor_creates_files_only_in_the_project(root_copy):
    text = data.read_project_file(root_copy, "examples/clinic", "personas/calm-adult.yaml").replace(
        "id: calm-adult", "id: night-owl")
    made = data.write_project_file(root_copy, "examples/clinic", "personas/night-owl.yaml", text, create=True)
    assert made["saved"] and made["ok"], made
    with pytest.raises(ValueError, match="already exists"):
        data.write_project_file(root_copy, "examples/clinic", "personas/night-owl.yaml", text, create=True)
    for rel in ("../escape.yaml", "scenarios/../../escape.yaml", "agent.py", "scenarios/deep/x.yaml"):
        with pytest.raises((ValueError, data.NotFound)):
            data.write_project_file(root_copy, "examples/clinic", rel, text, create=True)
    assert not (root_copy / "examples" / "escape.yaml").exists()


def test_api_key_is_saved_to_dotenv_and_never_shown(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    (tmp_path / ".env").write_text("OTHER=1\nANTHROPIC_API_KEY=old\n", encoding="utf-8")
    data.save_api_key(tmp_path, "sk-ant-test-1234567890abcd")
    assert (tmp_path / ".env").read_text(encoding="utf-8").splitlines() == ["OTHER=1", "ANTHROPIC_API_KEY=sk-ant-test-1234567890abcd"]
    key = data.system(tmp_path, tmp_path / "runs")["api_key"]
    assert key == {"set": True, "hint": "…abcd", "source": ".env"}


# ---- run.json and the exit-check summary --------------------------------------------------

def test_exit_check_writes_summary_and_run_info(tmp_path):
    summary = tmp_path / "summary.json"
    code = main(["exit-check", "examples/clinic", "--profile", "profile.yaml", "--repeats", "1", "--offline",
                 "--root", str(ROOT), "--runs-dir", str(tmp_path / "runs"), "--summary", str(summary)])
    doc = read_json(summary)
    assert code == 0 and doc["finished"] and doc["passed"]
    assert [r["label"] for r in doc["rows"]] == ["clean", "lie_on_failure", "skip_confirmation", "double_booking"]
    for row in doc["rows"]:
        info = read_json(tmp_path / "runs" / row["run_id"] / "run.json")
        assert info["status"] == "finished" and info["decision"] == row["decision"]
        assert info["offline"] and info["caller_model"] is None
        assert info["scenario_ids"] == [] and len(info["plan"]) == 3  # [] = every scenario of the profile
    runs = {r["run_id"]: r for r in data.list_runs(tmp_path / "runs")}
    assert {r["run_id"] for r in doc["rows"]} == set(runs)
    assert data.exit_checks(tmp_path / "runs") == []  # only the dashboard's own exit checks are listed


# ---- the HTTP API -------------------------------------------------------------------------

@pytest.fixture
def server(tmp_path):
    app = App(ROOT, tmp_path / "runs")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
    yield httpd.server_address[1], app
    app.jobs.stop_all()
    httpd.shutdown()
    httpd.server_close()


def call(conn, method, path, body=None, headers=None):
    raw = json.dumps(body).encode() if body is not None else None
    conn.request(method, path, body=raw, headers={"X-VATF": "1", **(headers or {})})
    r = conn.getresponse()
    payload = r.read()
    try:
        return r.status, json.loads(payload)
    except ValueError:
        return r.status, payload


def test_api_guards(server):
    port, _ = server
    conn = http.client.HTTPConnection("127.0.0.1", port)
    assert call(conn, "GET", "/api/system", headers={"Host": "evil.example"})[0] == 403
    conn.request("POST", "/api/jobs/preview", body=b"{}")  # no X-VATF header
    assert conn.getresponse().status == 403
    conn = http.client.HTTPConnection("127.0.0.1", port)
    for path in ("/runs/../pyproject.toml", "/static/../server.py", "/runs/%2e%2e/pyproject.toml", "/api/nope"):
        assert call(conn, "GET", path)[0] == 404, path
    assert call(conn, "GET", "/api/projects/file?project=examples/clinic&file=../../pyproject.toml")[0] in (400, 404)


def test_the_built_page_and_its_assets_are_served(server):
    # The page is built from ui/ (React) into static/ and committed, so `vatf ui` needs no Node.
    # A stale or partial build would show a blank page; this catches it.
    port, _ = server
    conn = http.client.HTTPConnection("127.0.0.1", port)
    status, page = call(conn, "GET", "/")
    assert status == 200 and b'<div id="root">' in page
    assets = re.findall(rb'(?:src|href)="(/static/[^"]+)"', page)
    assert any(a.endswith(b".js") for a in assets) and any(a.endswith(b".css") for a in assets)
    for a in assets:
        conn.request("GET", a.decode())
        r = conn.getresponse()
        body = r.read()
        assert r.status == 200 and body, a
        if a.endswith(b".js"):
            assert r.getheader("Content-Type").startswith("text/javascript")


def test_post_body_is_read_even_when_refused(server):
    # Unread request bodies on a kept-alive connection used to corrupt the next request.
    port, _ = server
    conn = http.client.HTTPConnection("127.0.0.1", port)
    conn.request("POST", "/api/jobs/j-20260101-000000-0000/stop", body=b'{"x": 1}')  # refused: no X-VATF
    assert conn.getresponse().read() and True
    assert call(conn, "POST", "/api/jobs/j-20260101-000000-0000/stop", {})[0] == 400
    status, system = call(conn, "GET", "/api/system")
    assert status == 200 and "api_key" in system


def test_a_run_started_from_the_api_is_followed_to_its_report(server):
    port, _ = server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    req = {"command": "run", "project": "examples/clinic", "profile": "profile.yaml", "scenarios": [HAPPY],
           "repeats": 1, "offline": True, "agent_options": {"bugs": "double_booking"}}
    status, preview = call(conn, "POST", "/api/jobs/preview", req)
    assert status == 200 and preview["command"].startswith("uv run vatf run examples/clinic")
    status, job = call(conn, "POST", "/api/jobs", req)
    assert status == 200, job

    # the live stream sends the job, its run, its call and an end event
    stream = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    stream.request("GET", f"/api/jobs/{job['id']}/stream")
    resp = stream.getresponse()
    assert resp.getheader("Content-Type").startswith("text/event-stream")
    kinds, deadline = set(), time.time() + 120
    while "end" not in kinds and time.time() < deadline:
        line = resp.readline().decode()
        if line.startswith("event:"):
            kinds.add(line.split(":", 1)[1].strip())
    assert {"job", "run", "call", "log", "end"} <= kinds

    status, job = call(conn, "GET", f"/api/jobs/{job['id']}")
    assert job["status"] == "finished" and job["exit_code"] == 1 and len(job["run_ids"]) == 1
    run_id = job["run_ids"][0]
    status, run = call(conn, "GET", f"/api/runs/{run_id}")
    assert run["decision"]["decision"] == "NO_GO" and run["info"]["status"] == "finished"
    assert run["info"]["agent_options"]["bugs"] == "double_booking"
    test_id = run["calls"][0]["test_id"]
    status, detail = call(conn, "GET", f"/api/runs/{run_id}/calls/{test_id}")
    assert status == 200 and detail["verdict"]["status"] == "FAIL" and detail["pii_masked"]
    assert detail["record"]["locked"] and detail["events"] and detail["story"]
    status, raw = call(conn, "GET", f"/runs/{run_id}/calls/{test_id}/verdict.json")
    assert status == 200 and raw["status"] == "FAIL"
