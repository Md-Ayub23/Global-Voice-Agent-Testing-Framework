"""The dashboard's project-based views: the home page numbers, one project's overview, project keys,
runs per project, and the secrets page. Runs here are small hand-made run folders, so every number
can be checked exactly."""

import http.client
import json
import shutil
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from vatf.util import write_json
from vatf.web import data
from vatf.web.server import App, make_handler

ROOT = Path(__file__).resolve().parent.parent
TODAY = "2026-10-29"


def make_run(runs: Path, run_id: str, project: str, decision: str | None, mode: str = "text",
             bugs: str | None = None, calls: int = 3, p50: int | None = None, p95: int | None = None) -> None:
    folder = runs / run_id
    folder.mkdir(parents=True)
    d, t = run_id[2:10], run_id[11:17]
    started = f"{d[:4]}-{d[4:6]}-{d[6:]}T{t[:2]}:{t[2:4]}:{t[4:]}Z"
    write_json(folder / "run.json", {"run_id": run_id, "status": "finished", "project": project, "mode": mode,
                                     "started_at": started, "profile_file": "profile.yaml",
                                     "agent_options": {"bugs": bugs} if bugs else {}})
    if decision:
        write_json(folder / "decision.json", {
            "decision": decision, "calls": [{"test_id": f"t-{d}-{i:04d}-abcd", "status": "PASS"} for i in range(calls)],
            "latency": {"p50_ms": p50, "p95_ms": p95}})


@pytest.fixture
def runs(tmp_path):
    r = tmp_path / "runs"
    make_run(r, "r-20261029-100000-aaaa", "examples/clinic", "GO")
    make_run(r, "r-20261029-110000-bbbb", "examples/clinic", "NO_GO", bugs="lie_on_failure")   # a bug test
    make_run(r, "r-20261028-090000-cccc", "examples/clinic", "NO_GO")
    make_run(r, "r-20261027-090000-dddd", "examples/latency-ref", "GO", mode="voice", p50=1200, p95=1500)
    make_run(r, "r-20261029-120000-eeee", "examples/clinic-service", None)                    # no decision
    make_run(r, "r-20261001-090000-ffff", "examples/gone", "GO")                              # a removed project
    return r


def test_project_keys_are_folder_names_and_unique(tmp_path):
    for parent in ("a", "b"):
        shutil.copytree(ROOT / "examples" / "latency-ref", tmp_path / parent / "latency-ref")
    shutil.copytree(ROOT / "examples" / "clinic", tmp_path / "c" / "clinic", ignore=shutil.ignore_patterns("*.db"))
    keys = data.project_keys(tmp_path)
    assert keys == {"a/latency-ref": "a-latency-ref", "b/latency-ref": "b-latency-ref", "c/clinic": "clinic"}
    assert data.project_by_key(tmp_path, "clinic")["path"] == "c/clinic"
    with pytest.raises(data.NotFound):
        data.project_by_key(tmp_path, "latency-ref")


def test_project_info_says_what_the_cards_show():
    p = data.project_by_key(ROOT, "clinic")
    assert p["key"] == "clinic" and p["example"] is True and p["ready"] is True
    assert p["domain"] == "healthcare" and p["channels"] == ["text", "voice"]
    main = next(x for x in p["profiles"] if x["file"] == "profile.yaml")
    assert main["logs"] == "none" and main["backend"] == "sql" and len(main["tools"]) == 4


def test_dashboard_numbers(runs):
    d = data.dashboard(ROOT, runs, today=TODAY)
    t = d["totals"]
    assert t["runs"] == 6 and t["decided"] == 5 and t["go"] == 3 and t["no_go"] == 2
    assert t["bug_runs"] == 1
    assert t["go_rate"] == 0.75            # 3 GO of the 4 runs without bug switches
    assert t["runs_7d"] == 5 and t["calls"] == 0   # no call folders in these hand-made runs
    assert t["projects"] == len(d["projects"]) and t["examples"] >= 3
    assert d["agent_types"]["channels"]["voice"] >= 3
    assert next(x["projects"] for x in d["agent_types"]["domains"] if x["domain"] == "healthcare") >= 2   # + your own
    days = {x["day"]: x for x in d["activity"]}
    assert len(d["activity"]) == 14 and d["activity"][-1]["day"] == TODAY
    assert days[TODAY]["GO"] == 1 and days[TODAY]["NO_GO"] == 1 and days[TODAY]["none"] == 1
    assert days["2026-10-28"]["NO_GO"] == 1 and days["2026-10-27"]["GO"] == 1
    assert "2026-10-01" not in days        # older than 14 days
    assert [r["run_id"] for r in d["recent_runs"]][:2] == ["r-20261029-120000-eeee", "r-20261029-110000-bbbb"]
    gone = next(r for r in d["recent_runs"] if r["run_id"] == "r-20261001-090000-ffff")
    assert gone["project_key"] is None     # its project no longer exists: still counted, shown as removed


def test_project_stats_leave_bug_tests_out_of_the_release_numbers(runs):
    d = data.dashboard(ROOT, runs, today=TODAY)
    clinic = next(p for p in d["projects"] if p["key"] == "clinic")["stats"]
    assert clinic["runs"] == 3 and clinic["decided"] == 3 and clinic["bug_runs"] == 1
    assert clinic["go_rate"] == 0.5 and clinic["history"] == ["NO_GO", "GO"]   # oldest first, bug test left out
    assert clinic["last_run"]["run_id"] == "r-20261029-110000-bbbb"
    assert clinic["last_decided"]["run_id"] == "r-20261029-100000-aaaa"        # the latest real release decision
    assert clinic["latency"] is None and clinic["latency_limits"] == {"p50_ms": 3500, "p95_ms": 6000}
    lat = next(p for p in d["projects"] if p["key"] == "latency-ref")["stats"]
    assert lat["latency"]["p95_ms"] == 1500 and lat["go_rate"] == 1.0


def test_project_overview_only_has_its_own_runs(runs):
    o = data.project_overview(ROOT, runs, "clinic-service")
    assert o["project"]["key"] == "clinic-service"
    assert [r["run_id"] for r in o["recent_runs"]] == ["r-20261029-120000-eeee"]
    assert o["stats"]["runs"] == 1 and o["stats"]["go_rate"] is None
    with pytest.raises(data.NotFound):
        data.project_overview(ROOT, runs, "nope")


def test_secrets_lists_references_and_saves_values(tmp_path, monkeypatch):
    shutil.copytree(ROOT / "examples" / "latency-ref", tmp_path / "proj")
    prof = tmp_path / "proj" / "profile.yaml"
    prof.write_text(prof.read_text(encoding="utf-8") + "# not this one: ${IN_A_COMMENT}\n"
                    + "metadata:\n  note: ${VOCTO_TEST_TOKEN}\n", encoding="utf-8")
    monkeypatch.delenv("VOCTO_TEST_TOKEN", raising=False)
    (tmp_path / ".env").write_text("OTHER=1\n", encoding="utf-8")
    s = data.secrets(tmp_path)
    assert [x["name"] for x in s] == ["VOCTO_TEST_TOKEN"] and s[0]["set"] is False
    assert s[0]["projects"] == ["Latency reference agent"]
    data.save_secret(tmp_path, "VOCTO_TEST_TOKEN", "abcd-efgh-1234")
    s = data.secrets(tmp_path)
    assert s[0]["set"] is True and s[0]["hint"] == "…1234"
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "OTHER=1\nVOCTO_TEST_TOKEN=abcd-efgh-1234\n"
    data.save_secret(tmp_path, "VOCTO_TEST_TOKEN", "new-value-5678")   # replaced, not added twice
    assert (tmp_path / ".env").read_text(encoding="utf-8").count("VOCTO_TEST_TOKEN=") == 1
    for name, value in (("bad name", "x"), ("OK_NAME", ""), ("OK_NAME", "two\nlines")):
        with pytest.raises(ValueError):
            data.save_secret(tmp_path, name, value)


@pytest.fixture
def server(runs):
    app = App(ROOT, runs)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    conn.request("GET", path)
    r = conn.getresponse()
    return r.status, json.loads(r.read())


def test_api_routes(server):
    status, dash = get(server, "/api/dashboard")
    assert status == 200 and dash["totals"]["runs"] == 6
    status, over = get(server, "/api/project/clinic")
    assert status == 200 and over["stats"]["runs"] == 3
    assert get(server, "/api/project/nope")[0] == 404
    status, rows = get(server, "/api/runs?project=clinic")
    assert status == 200 and {r["project_key"] for r in rows} == {"clinic"} and len(rows) == 3
    assert len(get(server, "/api/runs")[1]) == 6
    status, projects = get(server, "/api/projects")
    assert all("key" in p and "path" in p for p in projects)
    assert get(server, "/api/settings/secrets")[0] == 200
    conn = http.client.HTTPConnection("127.0.0.1", server, timeout=30)
    conn.request("POST", "/api/settings/secrets", body=b'{"name": "X", "value": "y"}')   # no X-VATF header
    assert conn.getresponse().status == 403
