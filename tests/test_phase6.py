"""Phase 6: baseline, comparing runs, the regression gate, CI reports, alerts and health checks.

The end-to-end tests run the clinic example offline (rule-based caller, no API calls) in a copy of
the project, so the real examples/clinic never gets a baseline file from a test.
"""

import copy
import http.client
import json
import shutil
import threading
import xml.etree.ElementTree as ET
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from vatf import alerts, ciconfig, health
from vatf.baseline import BaselineError, baseline_path, load_baseline, set_baseline
from vatf.cireport import junit_xml, markdown
from vatf.cli import main
from vatf.compare import compare, rules
from vatf.decision import apply_regression
from vatf.loader import schema_errors
from vatf.util import read_json, write_json

ROOT = Path(__file__).resolve().parent.parent


def snap(**over):
    base = {
        "kind": "run_snapshot", "run_id": "r-1", "profile_id": "p", "profile_file": "profile.yaml",
        "finished_at": "2026-11-01T10:00:00Z", "decision": "GO",
        "how": {"mode": "voice", "conditions": "as_written", "speech_engine": "local", "offline": False,
                "safety": False, "bugs": "", "caller_model": "m"},
        "tests": {"book": {"title": "Book", "criticality": "critical", "k": 3, "passed": 3, "failed": 0, "unverified": 0},
                  "cancel": {"title": "Cancel", "criticality": "normal", "k": 3, "passed": 2, "failed": 1, "unverified": 0},
                  "move": {"title": "Move", "criticality": "normal", "k": 3, "passed": 3, "failed": 0, "unverified": 0}},
        "scores": {"accuracy": 0.95, "experience": 0.9},
        "latency": {"p50_ms": 600, "p95_ms": 1000, "turns": 30},
        "quality": {"ttfw_ms": {"title": "Time to first word", "unit": "ms", "better": "lower", "value": 900, "band": "good"}},
        "judges": {"tone": {"title": "Tone", "passed": 9, "failed": 0}},
        "safety": None,
    }
    out = copy.deepcopy(base)
    for k, v in over.items():
        out[k] = v
    return out


# ---- compare.py ------------------------------------------------------------------------------

def test_compare_finds_regressions_and_fixes_and_only_new_failures_block_by_default():
    cur = snap(run_id="r-2")
    cur["tests"]["move"].update(passed=6, failed=2)       # passed 3/3 before, failed 2 of 8 after its reruns: regression
    cur["tests"]["cancel"].update(passed=3, failed=0)     # failed before, clean now: fixed
    cur["tests"]["new"] = {"title": "New", "criticality": "normal", "k": 3, "passed": 3, "failed": 0, "unverified": 0}
    del cur["tests"]["book"]
    cur["latency"] = {"p50_ms": 610, "p95_ms": 1300, "turns": 30}    # +300 ms > max(150, 20% of 1000)
    cur["quality"]["ttfw_ms"].update(value=1700, band="warn")
    cur["judges"]["tone"] = {"title": "Tone", "passed": 6, "failed": 3}   # 100% -> 67%
    c = compare(snap(), cur)
    by = {(x["kind"], x["id"]): x for x in c["changes"]}
    assert by[("test", "move")]["direction"] == "regression" and by[("test", "move")]["blocks"]
    assert by[("test", "cancel")]["direction"] == "better"
    assert by[("test", "new")]["direction"] == "new" and by[("test", "book")]["direction"] == "removed"
    assert by[("latency", "p95_ms")]["direction"] == "worse" and not by[("latency", "p95_ms")]["blocks"]
    assert ("latency", "p50_ms") not in by                       # +10 ms is noise
    assert by[("metric", "ttfw_ms")]["band_after"] == "warn" and by[("judge", "tone")]["direction"] == "worse"
    assert c["blocking"] == 1 and c["changes"][0]["id"] == "move" and c["like_for_like"]
    assert c["counts"] == {"regression": 1, "worse": 3, "suspected": 0, "flaky": 0, "better": 1, "new": 1, "removed": 1,
                           "same": 0, "changed": 0}


def test_owner_can_make_latency_block_and_limits_are_theirs():
    cur = snap(latency={"p50_ms": 600, "p95_ms": 1180, "turns": 30})
    profile = {"regression": {"latency": {"blocks": True, "increase_pct": 10, "increase_ms": 100}}}
    c = compare(snap(), cur, profile)
    assert [x["id"] for x in c["changes"] if x["blocks"]] == ["p95_ms"]
    assert compare(snap(), cur)["blocking"] == 0                 # default limit max(150, 200) not reached
    assert compare(snap(), cur, {"regression": {"enabled": False, "latency": {"blocks": True, "increase_ms": 1}}})["blocking"] == 0


def test_different_conditions_are_compared_but_never_block():
    cur = snap(how={**snap()["how"], "conditions": "hard"})
    cur["tests"]["move"].update(passed=0, failed=3)
    c = compare(snap(), cur)
    assert not c["like_for_like"] and c["blocking"] == 0
    assert c["differences"][0]["field"] == "conditions" and c["changes"][0]["direction"] == "regression"


def test_safety_findings_compared_only_when_both_runs_had_safety_tests():
    how = {**snap()["how"], "safety": True}
    base = snap(how=how, safety={"calls": 4, "failed": 0, "findings": [], "titles": {}})
    cur = snap(how=how, safety={"calls": 4, "failed": 1, "findings": ["safety.secrets.x|canary"],
                                "titles": {"safety.secrets.x": "Leaks the password"}})
    c = compare(base, cur, {"regression": {"safety": {"blocks": True}}})
    assert c["changes"][0]["kind"] == "safety" and c["changes"][0]["blocks"] and "Leaks the password" in c["changes"][0]["message"]
    assert not [x for x in compare(snap(), cur)["changes"] if x["kind"] == "safety"]


def test_rules_keep_defaults_for_unset_fields():
    r = rules({"regression": {"latency": {"blocks": True}}})
    assert r["latency"] == {"blocks": True, "increase_pct": 20, "increase_ms": 150} and r["new_failures"]["blocks"]


# ---- the decision ------------------------------------------------------------------------------

def go_decision():
    return {"schema_version": "0.1", "kind": "run_decision", "run_id": "r-2", "profile_id": "p", "decision": "GO",
            "exit_code": 0, "reasons": [{"rule": "all_targets_met", "message": "All good."}],
            "scorecard": {"accuracy": {"score": 1, "passed": 1, "failed": 0, "total": 1, "coverage": 1},
                          "experience": {"score": None, "passed": 0, "failed": 0, "total": 0, "coverage": 0}},
            "reliability": [], "coverage": {"planned_calls": 3, "verified_calls": 3, "ratio": 1, "min_required": 0.9},
            "basis": {"decided_by": ["all_checks"], "judge_opinions": 0},
            "calls": [{"test_id": "t-1", "scenario_id": "move", "repeat": 1, "status": "FAIL"},
                      {"test_id": "t-2", "scenario_id": "move", "repeat": 2, "status": "PASS"}]}


def test_a_blocking_regression_turns_go_into_no_go_with_refs():
    cur = snap(run_id="r-2")
    cur["tests"]["move"].update(passed=1, failed=2)     # two failures: confirmed without reruns
    cur["latency"] = {"p50_ms": 600, "p95_ms": 1500, "turns": 30}
    d = apply_regression(go_decision(), compare(snap(), cur))
    assert d["decision"] == "NO_GO" and d["exit_code"] == 1
    assert d["reasons"][0]["rule"] == "regression" and d["reasons"][0]["refs"] == ["t-1"]
    assert d["reasons"][-1]["rule"] == "regression_warnings" and "p95" in d["reasons"][-1]["message"]
    assert not any(r["rule"] == "all_targets_met" for r in d["reasons"])
    assert d["basis"]["decided_by"] == ["regression"] and d["baseline"]["blocking"] == 1
    assert schema_errors("run_decision", d) == []


def test_warnings_only_keep_go():
    cur = snap(run_id="r-2", latency={"p50_ms": 600, "p95_ms": 1500, "turns": 30})
    d = apply_regression(go_decision(), compare(snap(), cur))
    assert d["decision"] == "GO" and [r["rule"] for r in d["reasons"]] == ["all_targets_met", "regression_warnings"]
    assert apply_regression(go_decision(), None) == go_decision()


# ---- end to end: offline clinic runs --------------------------------------------------------------

@pytest.fixture
def clinic(tmp_path):
    shutil.copytree(ROOT / "examples" / "clinic", tmp_path / "clinic",
                    ignore=shutil.ignore_patterns("runs", "baselines", "*.db", "__pycache__"))
    return tmp_path / "clinic"


def run(clinic, runs, *extra):
    return main(["run", str(clinic), "--offline", "--quiet", "--repeats", "1", "--root", str(ROOT),
                 "--runs-dir", str(runs), *extra])


def newest(runs):
    # Run ids are timestamped to the second with a random suffix, so two runs in one second don't sort
    # in order by name: the newest is the one whose run.json was written last.
    return max((p for p in runs.iterdir() if p.name.startswith("r-")), key=lambda p: (p / "run.json").stat().st_mtime_ns)


def test_baseline_regression_gate_and_ci_reports_end_to_end(clinic, tmp_path, capsys):
    runs = tmp_path / "runs"
    assert run(clinic, runs) == 0
    good = newest(runs)
    assert not (good / "comparison.json").exists()           # no baseline yet: nothing to compare
    assert main(["baseline", "set", good.name, "--root", str(ROOT), "--runs-dir", str(runs)]) == 0
    assert baseline_path(clinic).is_file() and load_baseline(clinic)["run_id"] == good.name

    junit, md = tmp_path / "ci" / "vocto.xml", tmp_path / "ci" / "vocto.md"
    code = run(clinic, runs, "--agent-opt", "bugs=lie_on_failure", "--junit", str(junit), "--summary", str(md), "--ci")
    bad = newest(runs)
    decision, comparison = read_json(bad / "decision.json"), read_json(bad / "comparison.json")
    assert code == 1 and decision["decision"] == "NO_GO" and schema_errors("run_decision", decision) == []
    assert decision["baseline"]["regressions"] == 1 and decision["baseline"]["run_id"] == good.name
    assert any(r["rule"] == "regression" for r in decision["reasons"])
    assert comparison["changes"][0]["id"] == "clinic.book-checkup.tool-timeout"
    assert read_json(bad / "baseline.json")["run_id"] == good.name and read_json(bad / "run.json")["trigger"] == "ci"
    tree = ET.parse(junit).getroot()
    # 3 failures before the confirm step; the tool-timeout test failed once and its 5 reruns failed too.
    assert tree.get("failures") == "8" and tree.find(".//testcase[@name='vs baseline']/failure") is not None
    assert "NO-GO" in md.read_text(encoding="utf-8") and "Vs baseline" in md.read_text(encoding="utf-8")
    assert "VS BASELINE" in capsys.readouterr().out

    assert run(clinic, runs) == 0                             # the clean agent again: GO, no regressions
    again = read_json(newest(runs) / "decision.json")
    assert again["decision"] == "GO" and again["baseline"]["regressions"] == 0

    assert run(clinic, runs, "--agent-opt", "bugs=lie_on_failure", "--baseline", "none") == 1
    assert "baseline" not in read_json(newest(runs) / "decision.json")
    assert main(["compare", good.name, bad.name, "--root", str(ROOT), "--runs-dir", str(runs)]) == 1


def test_bug_runs_cannot_be_the_baseline(clinic, tmp_path):
    runs = tmp_path / "runs"
    run(clinic, runs, "--agent-opt", "bugs=double_booking")
    with pytest.raises(BaselineError, match="bug switches"):
        set_baseline(clinic, newest(runs))
    assert main(["baseline", "set", newest(runs).name, "--root", str(ROOT), "--runs-dir", str(runs)]) == 3


def test_relabelling_keeps_the_comparison(clinic, tmp_path):
    from vatf.relabel import reevaluate_run
    runs = tmp_path / "runs"
    run(clinic, runs)
    main(["baseline", "set", newest(runs).name, "--root", str(ROOT), "--runs-dir", str(runs)])
    run(clinic, runs, "--agent-opt", "bugs=lie_on_failure")
    bad = newest(runs)
    reevaluate_run(ROOT, runs, bad.name)
    d = read_json(bad / "decision.json")
    assert d["decision"] == "NO_GO" and d["baseline"]["regressions"] == 1 and "relabelled_at" in d


def test_exit_check_never_compares(clinic, tmp_path):
    runs = tmp_path / "runs"
    run(clinic, runs)
    main(["baseline", "set", newest(runs).name, "--root", str(ROOT), "--runs-dir", str(runs)])
    assert main(["exit-check", str(clinic), "--offline", "--repeats", "1", "--root", str(ROOT),
                 "--runs-dir", str(runs)]) == 0
    assert not any((p / "comparison.json").exists() for p in runs.iterdir() if p.name.startswith("r-")
                   and read_json(p / "run.json")["baseline"] == "none")


def test_health_check_runs_the_critical_tests_once(clinic, tmp_path):
    runs = tmp_path / "runs"
    assert main(["health", str(clinic), "--offline", "--quiet", "--root", str(ROOT), "--runs-dir", str(runs)]) == 0
    info = read_json(newest(runs) / "run.json")
    critical = sorted(s["id"] for s in (yaml.safe_load(p.read_text()) for p in (clinic / "scenarios").glob("*.yaml"))
                      if s.get("criticality") == "critical" and "text" in s.get("modes", ["text", "voice"]))
    assert info["trigger"] == "health" and sorted(info["scenario_ids"]) == critical
    assert {p["repeat"] for p in info["plan"]} == {1}


# ---- reports -------------------------------------------------------------------------------------

def test_junit_marks_failures_errors_and_review_as_skipped():
    d = go_decision() | {"decision": "NO_GO", "exit_code": 1, "reasons": [{"rule": "blocker", "message": "Lied."}]}
    vs = [{"test_id": "t-1", "scenario_id": "move", "repeat": 1, "status": "FAIL", "metrics": {"duration_ms": 1500},
           "checks": [{"check_id": "outcome", "result": "FAIL", "severity": "blocker", "message": "Lied & <bad>"}]},
          {"test_id": "t-2", "scenario_id": "move", "repeat": 2, "status": "ERROR", "checks": []},
          {"test_id": "t-3", "scenario_id": "book", "repeat": 1, "status": "NEEDS_REVIEW", "checks": []}]
    root = ET.fromstring(junit_xml(d, vs))
    assert (root.get("tests"), root.get("failures"), root.get("errors"), root.get("skipped")) == ("4", "2", "1", "1")
    assert root.find(".//testcase[@name='repeat 1 (t-1)']/failure").get("message") == "Lied & <bad>"
    assert "| Exit code" not in markdown(d, {"profile_name": "Clinic | test"}) and "Clinic \\| test" in markdown(d, {"profile_name": "Clinic | test"})


# ---- alerts ----------------------------------------------------------------------------------------

def test_alerts_send_on_the_chosen_events_and_never_for_bug_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("HOOK", "https://hooks.example/abc")
    sent = []
    post = lambda url, body: sent.append((url, body)) or 200   # noqa: E731
    runs = tmp_path / "runs"
    (runs / "r-2").mkdir(parents=True)
    profile = {"alerts": [{"name": "team", "type": "slack", "url": "${HOOK}"},
                          {"name": "ops", "type": "webhook", "url": "${HOOK}", "events": ["insufficient"]},
                          {"name": "missing", "type": "discord", "url": "${NOPE}"}]}
    d = go_decision() | {"decision": "NO_GO", "exit_code": 1}
    info = {"project": "examples/x", "profile_name": "Clinic", "agent_options": {}}
    out = alerts.send_alerts(profile, d, info, runs, post)
    assert [(r["name"], r["sent"]) for r in out] == [("team", True), ("missing", False)]
    assert "NOPE" in out[1]["error"] and sent[0][0] == "https://hooks.example/abc" and "NO-GO" in sent[0][1]["text"]
    assert read_json(runs / "r-2" / "alerts.json")["events"] == ["no_go"]
    sent.clear()
    assert alerts.send_alerts(profile, d, info | {"agent_options": {"bugs": "x"}}, runs, post) == [] and not sent


def test_recovered_after_a_failed_run(tmp_path):
    runs = tmp_path / "runs"
    for rid, dec in (("r-20261101-000000-0000", "NO_GO"), ("r-20261101-000001-0000", "GO")):
        (runs / rid).mkdir(parents=True)
        write_json(runs / rid / "run.json", {"project": "examples/x", "profile_file": "profile.yaml", "decision": dec})
    prev = alerts.previous_decision(runs, "r-20261101-000001-0000", "examples/x", "profile.yaml")
    assert prev == "NO_GO" and alerts.events_for(go_decision(), prev) == ["recovered"]
    assert alerts.events_for(go_decision(), "GO") == []


def test_send_test_reports_http_errors(monkeypatch):
    import urllib.error

    def boom(url, body):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
    assert alerts.send_test({"type": "slack", "url": "https://x"}, "Clinic", boom) == {
        "sent": False, "status": 404, "error": "the webhook answered 404 Not Found"}


# ---- CI files ---------------------------------------------------------------------------------------

def test_ci_files_use_python_dash_m_and_only_the_secrets_named():
    profile = {"id": "acme-bot", "name": "Acme bot", "connection": {"options": {"headers": {"A": "Bearer ${ACME_TOKEN}"}}}}
    gh = ciconfig.ci_file("github", profile, "projects/acme", "profile-voice.yaml", mode="voice", conditions="phone")
    doc = yaml.safe_load(gh["text"])
    steps = doc["jobs"]["vocto"]["steps"]
    check = next(s for s in steps if s.get("name") == "Release check")
    assert gh["path"] == ".github/workflows/vocto-acme-bot.yml"
    assert check["run"].startswith("uv run python -m vatf run projects/acme --profile profile-voice.yaml --ci")
    assert "--conditions phone" in check["run"] and "vatf.exe" not in gh["text"]
    assert set(check["env"]) == {"ANTHROPIC_API_KEY", "ACME_TOKEN"} and any(s.get("uses", "").startswith("actions/cache") for s in steps)
    gl = yaml.safe_load(ciconfig.ci_file("gitlab", profile, "projects/acme", "profile.yaml", offline=True)["text"])
    assert gl["vocto-acme-bot"]["artifacts"]["reports"]["junit"] == "reports/vocto.xml"
    assert "--offline" in "".join(gl["vocto-acme-bot"]["script"])
    jf = ciconfig.ci_file("jenkins", profile, "projects/acme", "profile.yaml", schedule=False)["text"]
    assert "credentials('anthropic-api-key')" in jf and "junit" in jf and "python -m vatf run" in jf
    with pytest.raises(ValueError):
        ciconfig.ci_file("travis", profile, "x", "profile.yaml")


# ---- health checks ----------------------------------------------------------------------------------

def test_health_settings_and_tests():
    scs = [{"id": "a", "criticality": "critical"}, {"id": "b"}]
    assert health.scenario_ids({}, scs) == ["a"]
    assert health.scenario_ids({"health_checks": {"scenarios": ["b", "gone"]}}, scs) == ["b"]
    assert health.scenario_ids({}, [{"id": "b"}]) == ["b"]
    assert health.settings({"health_checks": {"every_minutes": 1}})["every_minutes"] == health.MIN_MINUTES


def test_scheduler_starts_one_due_check_and_waits(tmp_path, monkeypatch):
    from vatf.web import regress, schedule

    class FakeJobs:
        active = None
        started = []

        def start(self, req, project):
            self.started.append((req, project["path"]))

            class J:
                def public(self):
                    return {"id": "j-1"}
            return J()

    rows = [{"project": {"path": "examples/clinic"}, "profile": {}, "file": "profile.yaml", "every_minutes": 60,
             "start_agent": False, "last": None, "due": True}]
    monkeypatch.setattr(regress, "health_status", lambda root, runs, now: rows)
    s = schedule.HealthScheduler(tmp_path, tmp_path / "runs", FakeJobs())
    assert s.tick(now=1000.0) == {"id": "j-1"}
    assert s.tick(now=1060.0) is None                   # failed to write a run? not retried within the hour
    assert s.tick(now=1000.0 + 3600) == {"id": "j-1"}
    assert FakeJobs.started[0] == ({"command": "health", "profile": "profile.yaml", "start_agent": False}, "examples/clinic")


# ---- the dashboard API --------------------------------------------------------------------------------

@pytest.fixture
def web(tmp_path):
    from vatf.web.server import App, make_handler
    root = tmp_path / "root"
    shutil.copytree(ROOT / "examples" / "clinic", root / "examples" / "clinic",
                    ignore=shutil.ignore_patterns("runs", "baselines", "*.db", "__pycache__"))
    app = App(root, Path("runs"))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
    yield root, httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def call(port, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port)
    conn.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers={"X-VATF": "1"})
    r = conn.getresponse()
    return r.status, json.loads(r.read())


def test_dashboard_baseline_compare_ci_and_ops(web, monkeypatch):
    root, port = web
    made = []
    for extra in ([], ["--agent-opt", "bugs=lie_on_failure"]):
        main(["run", str(root / "examples" / "clinic"), "--offline", "--quiet", "--repeats", "1",
              "--root", str(ROOT), "--runs-dir", str(root / "runs"), *extra])
        made.append(newest(root / "runs").name)
    good, bad = made
    # run.json records the absolute folder here; the dashboard knows the project by its path from the root
    for rid in (good, bad):
        info = read_json(root / "runs" / rid / "run.json")
        write_json(root / "runs" / rid / "run.json", info | {"project": "examples/clinic"})
    status, page = call(port, "GET", "/api/project/clinic/baseline")
    assert status == 200 and page["baseline"] is None and {c["run_id"] for c in page["candidates"]} == {good, bad}
    assert call(port, "POST", f"/api/runs/{bad}/baseline", {})[0] == 400          # bug switches: refused
    status, out = call(port, "POST", f"/api/runs/{good}/baseline", {"note": "release 1"})
    assert status == 200 and out["baseline"]["run_id"] == good and out["baseline"]["note"] == "release 1"
    assert call(port, "GET", "/api/project/clinic/baseline")[1]["baseline"]["run_id"] == good
    status, cmp = call(port, "GET", f"/api/compare?a={good}&b={bad}")
    # One failure on a run with no reruns (the baseline was set after it): suspected, never confirmed.
    assert status == 200 and cmp["counts"]["suspected"] == 1 and cmp["same_agent"] and cmp["runs"]["b"]["run_id"] == bad
    status, ci = call(port, "GET", "/api/project/clinic/ci?provider=gitlab&offline=1")
    assert status == 200 and ci["path"] == ".gitlab-ci.yml" and "python -m vatf run examples/clinic" in ci["text"]
    status, ops = call(port, "GET", "/api/project/clinic/ops")
    assert status == 200 and ops["health"]["enabled"] is False and ops["regression"]["new_failures"]["blocks"]
    assert ops["health_tests"] and ops["committed_with_project"]
    assert call(port, "POST", "/api/project/clinic/alerts/test", {"target": {"type": "slack"}})[0] == 400
    status, saved = call(port, "PUT", "/api/setup/profile",
                         {"project": "examples/clinic", "file": "profile.yaml", "sections": {"health_checks": {"enabled": True, "every_minutes": 30},
                                       "regression": {"latency": {"blocks": True}},
                                       "alerts": [{"name": "team", "type": "slack", "url": "${SLACK_URL}",
                                                   "events": ["no_go"]}]}})
    assert status == 200 and saved["saved"], saved
    ops = call(port, "GET", "/api/project/clinic/ops")[1]
    assert ops["health"]["every_minutes"] == 30 and ops["regression"]["latency"]["blocks"] and ops["alerts"][0]["name"] == "team"
    assert ops["secrets"]["SLACK_URL"] is False
    status, clear = call(port, "POST", "/api/project/clinic/baseline/clear", {"file": "profile.yaml"})
    assert status == 200 and clear["removed"] and not baseline_path(root / "examples" / "clinic").exists()
    detail = call(port, "GET", f"/api/runs/{bad}")[1]
    assert detail["comparison"] is None and detail["summary"]["trigger"] == "manual"   # run before the baseline was set
