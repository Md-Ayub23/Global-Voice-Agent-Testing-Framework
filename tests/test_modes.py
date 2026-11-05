"""Test modes (modes.py) and the release readiness report (readiness.py): what each mode runs, how a
run is placed in a mode, the plain-words report with its proof, the CSV export, and the dashboard API."""

import csv
import http.client
import io
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from vatf import modes, readiness
from vatf.cli import main
from vatf.util import read_json
from vatf.web import data
from vatf.web.jobs import JobError, JobManager
from vatf.web.server import App, make_handler

ROOT = Path(__file__).resolve().parent.parent


# ---- modes.py --------------------------------------------------------------------------------

OWN = [{"id": "a"}, {"id": "b", "setup": {"faults": [{"tool": "x", "kind": "timeout"}]}}]
ATTACKS = [{"id": "safety.off-topic.1"}]


@pytest.mark.parametrize("mode,with_safety,expected", [
    ("functional", False, ["a", "b"]),
    ("functional", True, ["a", "b", "safety.off-topic.1"]),
    ("resilience", True, ["b"]),               # only test cases that break a tool, never attacks
    ("safety", False, ["safety.off-topic.1"]),  # only attacks, whatever the switch says
    ("full", True, ["a", "b", "safety.off-topic.1"]),
])
def test_each_mode_picks_its_own_tests(mode, with_safety, expected):
    assert [s["id"] for s in modes.select(mode, OWN, ATTACKS, with_safety)] == expected


def test_runs_from_before_modes_are_placed_by_what_they_ran():
    assert modes.of_run({"test_mode": "timing"}) == "timing"
    assert modes.of_run({"kind": "load"}) == "load"
    assert modes.of_run({"plan": [{"scenario_id": "safety.x.1"}, {"scenario_id": "safety.y.1"}]}) == "safety"
    assert modes.of_run({"safety": True, "plan": [{"scenario_id": "a"}]}) == "full"
    assert modes.of_run({"plan": [{"scenario_id": "a"}]}) == "functional"


def test_catalog_lists_the_full_check_first_and_every_mode_in_plain_words():
    cat = modes.catalog()
    assert cat[0]["id"] == "full"
    assert {m["id"] for m in cat} == {"full", "functional", "timing", "speech", "safety", "resilience", "load",
                                      "regression", "monitor"}
    for m in cat[1:]:
        assert m["question"].endswith("?") and len(m["checks"]) == 4


# ---- runs in a mode (command line) ---------------------------------------------------------

def _run(tmp_path, *extra):
    return main(["run", "examples/clinic", "--root", str(ROOT), "--runs-dir", str(tmp_path / "runs"),
                 "--offline", "--quiet", *extra])


def _only_run(tmp_path) -> Path:
    (run_dir,) = [p for p in (tmp_path / "runs").iterdir() if p.name.startswith("r-")]
    return run_dir


def test_resilience_mode_runs_only_the_broken_tool_tests_and_says_passed_not_ready(tmp_path):
    assert _run(tmp_path, "--test-mode", "resilience") == 0
    run_dir = _only_run(tmp_path)
    info = read_json(run_dir / "run.json")
    assert info["test_mode"] == "resilience"
    assert {p["scenario_id"] for p in info["plan"]} == {"clinic.book-checkup.tool-timeout"}
    rep = readiness.build(run_dir)
    assert rep["mode"]["id"] == "resilience"
    assert rep["verdict"]["level"] == "ready"
    # a focused mode never claims the whole agent is ready to release
    assert rep["verdict"]["headline"] == "Passed the resilience tests"
    assert "Full check" in rep["verdict"]["line"]


def test_safety_mode_runs_only_attack_calls(tmp_path):
    assert _run(tmp_path, "--test-mode", "safety", "--repeats", "1") == 0
    info = read_json(_only_run(tmp_path) / "run.json")
    assert info["test_mode"] == "safety"
    assert info["plan"] and all(p["scenario_id"].startswith("safety.") for p in info["plan"])


def test_voice_only_mode_on_a_text_channel_is_refused(tmp_path, capsys):
    assert _run(tmp_path, "--test-mode", "timing") == 3
    assert "voice" in capsys.readouterr().err.lower()


def test_a_caught_bug_gives_not_ready_with_proof_and_a_csv(tmp_path):
    assert _run(tmp_path, "--test-mode", "functional", "--scenario", "clinic.book-checkup.tool-timeout",
                "--repeats", "1", "--agent-opt", "bugs=lie_on_failure") == 1
    run_dir = _only_run(tmp_path)
    rep = readiness.build(run_dir)
    assert rep["verdict"]["level"] == "not_ready"
    assert rep["verdict"]["headline"].startswith("Not ready: ")
    assert "Bugs were switched on" in rep["verdict"]["line"]
    blocking = [f for f in rep["findings"] if f["blocks"]]
    assert blocking and rep["counts"]["blocking"] == len(blocking)
    for f in blocking:
        assert f["what"] and f["fix"] and f["how_often"]["text"]
        assert f["proofs"], f"{f['title']} has no proof from the calls"
        assert all(p["test_id"].startswith("t-") for p in f["proofs"])
    assert any(r["status"] == "fail" for r in rep["scorecard"])
    assert rep["summary"][0].startswith("Vocto made 1 text conversation")
    rows = list(csv.reader(io.StringIO(readiness.to_csv(rep))))
    assert rows[0][0] == "section" and rows[1][0] == "verdict"
    assert sum(r[0] == "finding" for r in rows) == len(rep["findings"])
    # no jargon in what a person reads
    text = json.dumps([rep["verdict"]["headline"], rep["verdict"]["line"], rep["summary"], [f["title"] for f in rep["findings"]]])
    assert "NO_GO" not in text and "NO-GO" not in text


def test_a_stopped_run_has_no_result(tmp_path):
    run_dir = tmp_path / "r-20990101-000000-0000"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps({"run_id": run_dir.name, "test_mode": "functional"}), encoding="utf-8")
    rep = readiness.build(run_dir)
    assert rep["verdict"]["level"] == "no_result"
    assert not any(f["blocks"] for f in rep["findings"])


# ---- the dashboard ---------------------------------------------------------------------------

def test_the_run_form_passes_the_mode_on(tmp_path):
    jobs = JobManager(ROOT, Path("runs"))
    clinic = data.project_by_path(ROOT, "examples/clinic")
    args, command = jobs.build({"command": "run", "profile": "profile.yaml", "offline": True, "test_mode": "safety"}, clinic)
    assert args[-3:] == ["--test-mode", "safety", "--quiet"]
    assert "--test-mode safety" in command
    with pytest.raises(JobError):
        jobs.build({"command": "run", "profile": "profile.yaml", "test_mode": "loudness"}, clinic)


@pytest.fixture
def server(tmp_path):
    app = App(ROOT, tmp_path / "runs")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
    yield httpd.server_address[1]
    app.jobs.stop_all()
    httpd.shutdown()
    httpd.server_close()


def _get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    conn.request("GET", path, headers={"X-VATF": "1"})
    r = conn.getresponse()
    return r, r.read()


def test_api_gives_the_report_the_csv_and_the_mode_board(tmp_path, server):
    assert _run(tmp_path, "--test-mode", "resilience") == 0
    run_id = _only_run(tmp_path).name
    r, body = _get(server, f"/api/runs/{run_id}")
    run = json.loads(body)
    assert run["summary"]["test_mode"] == "resilience"
    assert run["readiness"]["verdict"]["headline"] == "Passed the resilience tests"
    # the project's own titles reach the report
    assert any("Booking API times out" in t["title"] for f in run["readiness"]["findings"] for t in f["tests"]) \
        or not run["readiness"]["findings"]

    r, body = _get(server, f"/api/runs/{run_id}/export.csv")
    assert r.status == 200 and "attachment" in r.getheader("Content-Disposition")
    assert body.startswith(b"\xef\xbb\xbf") and b"verdict" in body

    r, body = _get(server, "/api/project/clinic")
    board = {m["id"]: m for m in json.loads(body)["modes"]}
    assert board["resilience"]["latest"]["run_id"] == run_id and board["resilience"]["runs"] == 1
    assert board["functional"]["latest"] is None
    assert board["timing"]["available"]["ok"]            # the clinic has a voice channel
    assert not board["regression"]["available"]["ok"]    # no baseline yet
