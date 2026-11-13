"""Regression upgrade: confirm a single failure before it blocks, flaky tests, edited tests, version labels,
a rolling baseline, the trend, and keeping a fixed bug as a regression test.

Small fixed snapshots for the rules, and offline clinic runs for the whole path."""

from __future__ import annotations

import copy
import shutil
from pathlib import Path

import pytest
import yaml

from vatf import health
from vatf.cli import main
from vatf.compare import compare
from vatf.history import flaky_tests, past_runs, rolling_baseline, trend
from vatf.loader import schema_errors
from vatf.util import read_json, write_json

ROOT = Path(__file__).resolve().parent.parent


def snap(**over):
    base = {
        "kind": "run_snapshot", "run_id": "r-1", "profile_id": "p", "profile_file": "profile.yaml",
        "finished_at": "2026-11-01T10:00:00Z", "decision": "GO",
        "how": {"mode": "text", "conditions": "as_written", "speech_engine": None, "offline": True,
                "safety": False, "bugs": "", "caller_model": "m"},
        "tests": {"book": {"title": "Book", "criticality": "critical", "k": 3, "passed": 3, "failed": 0, "unverified": 0, "sha": "aa"},
                  "move": {"title": "Move", "criticality": "normal", "k": 3, "passed": 3, "failed": 0, "unverified": 0, "sha": "bb"}},
        "scores": {}, "latency": None, "quality": {}, "safety": None,
        "judges": {"tone": {"title": "Tone", "passed": 9, "failed": 0}},
        "versions": {"agent": "v1", "prompt": "p1", "judge_models": "opus", "vocto": "0.1"},
    }
    out = copy.deepcopy(base)
    out.update(over)
    return out


def by_id(c):
    return {(x["kind"], x["id"]): x for x in c["changes"]}


# ---- compare: suspected, flaky, edited, versions ------------------------------------------------------

def test_one_failure_is_suspected_until_reruns_confirm_it():
    cur = snap(run_id="r-2")
    cur["tests"]["book"].update(passed=2, failed=1)
    ch = by_id(compare(snap(), cur))[("test", "book")]
    assert ch["direction"] == "suspected" and not ch["blocks"] and "wasn't rerun" in ch["message"]

    cur["reruns"] = {"book": 5}
    cur["tests"]["book"].update(k=8, passed=7, failed=1)             # 1 of 8 after 5 reruns: didn't repeat
    ch = by_id(compare(snap(), cur))[("test", "book")]
    assert ch["direction"] == "suspected" and "didn't repeat on 5 reruns" in ch["message"]

    cur["tests"]["book"].update(k=8, passed=6, failed=2)             # 2 of 8: it held up
    c = compare(snap(), cur)
    assert by_id(c)[("test", "book")]["direction"] == "regression" and c["blocking"] == 1

    # Reruns switched off: one failure blocks, as before the confirm step.
    one = snap(run_id="r-3")
    one["tests"]["book"].update(passed=2, failed=1)
    c = compare(snap(), one, {"regression": {"confirm": {"reruns": 0}}})
    assert by_id(c)[("test", "book")]["direction"] == "regression" and c["blocking"] == 1


def test_flaky_and_edited_tests_never_block():
    cur = snap(run_id="r-2")
    cur["tests"]["book"].update(passed=1, failed=2)
    cur["flaky"] = {"book": {"passed": 25, "calls": 30, "runs": 10, "rate": 0.833}}
    cur["tests"]["move"].update(passed=1, failed=2, sha="changed")
    c = compare(snap(), cur)
    b, m = by_id(c)[("test", "book")], by_id(c)[("test", "move")]
    assert b["direction"] == "flaky" and b["flaky_rate"] == 0.833 and not b["blocks"]
    assert m["direction"] == "changed" and not m["blocks"] and "edited" in m["message"]
    assert c["blocking"] == 0 and c["counts"]["flaky"] == 1 and c["counts"]["changed"] == 1


def test_versions_say_what_changed_and_a_new_judge_model_never_blocks():
    cur = snap(run_id="r-2", versions={"agent": "v2", "prompt": "p2", "judge_models": "opus-new", "vocto": "0.1"})
    cur["judges"]["tone"] = {"title": "Tone", "passed": 5, "failed": 4}
    c = compare(snap(), cur, {"regression": {"judges": {"blocks": True}}})
    assert [v["field"] for v in c["versions"]] == ["agent", "prompt", "judge_models"]
    assert c["versions"][0] == {"field": "agent", "title": "agent version", "before": "v1", "after": "v2"}
    j = by_id(c)[("judge", "tone")]
    assert j["direction"] == "worse" and not j["blocks"] and "judge models changed" in j["message"]


# ---- history: flaky list, rolling baseline, trend --------------------------------------------------------

def fake_run(runs, rid, rel, decision="GO", version="v1", bugs=None, mode="text"):
    d = runs / rid
    d.mkdir(parents=True)
    write_json(d / "run.json", {"run_id": rid, "profile_file": "profile.yaml", "mode": mode, "offline": True,
                                "finished_at": f"2026-11-{rid[-2:]}T10:00:00Z", "versions": {"agent": version},
                                "agent_options": {"bugs": bugs} if bugs else {}})
    write_json(d / "decision.json", {"run_id": rid, "decision": decision, "latency": {"p50_ms": 500, "p95_ms": 900, "turns_measured": 10},
                                     "coverage": {"planned_calls": sum(p + f for _, p, f in rel)},
                                     "reliability": [{"scenario_id": s, "criticality": "normal", "k": p + f, "passed": p, "failed": f,
                                                                      "unverified": 0} for s, p, f in rel],
                                     "calls": [], "scorecard": {}})


def test_flaky_list_rolling_baseline_and_trend(tmp_path):
    runs = tmp_path / "runs"
    fake_run(runs, "r-20261101", [("book", 3, 0), ("move", 3, 0)], version="v1")
    fake_run(runs, "r-20261102", [("book", 2, 1), ("move", 3, 0)], decision="NO_GO", version="v1")
    fake_run(runs, "r-20261103", [("book", 3, 0), ("move", 3, 0)], version="v2")
    fake_run(runs, "r-20261104", [("book", 0, 3), ("move", 0, 3)], decision="NO_GO", bugs="x")   # bug run: left out of flaky
    fake_run(runs, "r-20261105", [("book", 3, 0)], mode="voice")                                # another mode
    past = past_runs(runs, "profile.yaml", "text", before="r-20261106")
    assert [r["run_id"] for r in past] == ["r-20261104", "r-20261103", "r-20261102", "r-20261101"]
    flaky = flaky_tests(past)
    assert set(flaky) == {"book"} and flaky["book"]["passed"] == 8 and flaky["book"]["calls"] == 9 and flaky["book"]["runs"] == 3

    how = {"mode": "text", "conditions": "as_written", "speech_engine": None, "offline": True}
    base = rolling_baseline(past, how, 3)
    assert base["rolling"] == ["r-20261103", "r-20261101"]                # only GO runs, no bug switches
    assert base["tests"]["book"]["k"] == 6 and base["tests"]["book"]["passed"] == 6
    assert base["run_id"] == "last 2 good runs" and base["latency"]["p95_ms"] == 900
    assert rolling_baseline(past, how | {"conditions": "hard"}, 3) is None

    t = trend(past)
    assert [p["run_id"] for p in t] == ["r-20261101", "r-20261102", "r-20261103", "r-20261104"]   # oldest first
    assert t[1]["pass_rate"] == round(5 / 6, 4) and t[2]["version"] == "v2" and t[0]["calls"] == 6


# ---- end to end: offline clinic runs ----------------------------------------------------------------------

@pytest.fixture
def clinic(tmp_path):
    shutil.copytree(ROOT / "examples" / "clinic", tmp_path / "clinic",
                    ignore=shutil.ignore_patterns("runs", "baselines", "*.db", "__pycache__"))
    return tmp_path / "clinic"


def run(clinic, runs, *extra):
    return main(["run", str(clinic), "--offline", "--quiet", "--repeats", "1", "--root", str(ROOT),
                 "--runs-dir", str(runs), "--test-mode", "regression", *extra])


def newest(runs):
    return max((p for p in runs.iterdir() if p.name.startswith("r-")), key=lambda p: (p / "run.json").stat().st_mtime_ns)


def test_regression_run_confirms_with_reruns_records_versions_and_trend(clinic, tmp_path):
    from vatf import readiness

    runs = tmp_path / "runs"
    assert run(clinic, runs, "--agent-version", "v1") == 0
    good = newest(runs)
    info = read_json(good / "run.json")
    assert info["versions"]["agent"] == "v1" and info["versions"]["profile"] and info["scenario_sha256"]
    assert main(["baseline", "set", good.name, "--root", str(ROOT), "--runs-dir", str(runs)]) == 0

    # A planted bug: the tool-timeout test fails once, is rerun 5 times, keeps failing: a confirmed regression.
    assert run(clinic, runs, "--agent-version", "v2", "--agent-opt", "bugs=lie_on_failure") == 1
    bad = newest(runs)
    d, cmp = read_json(bad / "decision.json"), read_json(bad / "comparison.json")
    sid = "clinic.book-checkup.tool-timeout"
    assert schema_errors("run_decision", d) == []
    assert d["baseline"]["regressions"] == 1 and d["baseline"]["confirmed_by_reruns"] == {sid: 5}
    assert d["baseline"]["versions"][0] == {"field": "agent", "title": "agent version", "before": "v1", "after": "v2"}
    row = next(r for r in d["reliability"] if r["scenario_id"] == sid)
    assert row["passed"] + row["failed"] == 6 and row["failed"] == 6
    assert [p["version"] for p in d["trend"]] == ["v1", "v2"] and d["trend"][-1]["run_id"] == bad.name
    assert d["flaky"] == [] and cmp["changes"][0]["direction"] == "regression"
    rep = readiness.build(bad)
    assert rep["regression"]["regressions"] == 1 and len(rep["regression"]["trend"]) == 2

    # The clean agent again: nothing to rerun, GO; the earlier fixed test can be kept as a regression test.
    assert run(clinic, runs, "--agent-version", "v3") == 0
    again = read_json(newest(runs) / "decision.json")
    assert again["baseline"]["regressions"] == 0 and "confirmed_by_reruns" not in again["baseline"]
    assert [p["version"] for p in again["trend"]] == ["v1", "v2", "v3"]


def test_an_edited_test_case_is_never_a_regression(clinic, tmp_path):
    runs = tmp_path / "runs"
    run(clinic, runs)
    main(["baseline", "set", newest(runs).name, "--root", str(ROOT), "--runs-dir", str(runs)])
    path = next(p for p in (clinic / "scenarios").glob("*.yaml")
                if yaml.safe_load(p.read_text())["id"] == "clinic.book-checkup.tool-timeout")
    doc = yaml.safe_load(path.read_text())
    doc["tags"] = list(doc.get("tags") or []) + ["edited"]        # any edit changes the test's fingerprint
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    assert run(clinic, runs, "--agent-opt", "bugs=lie_on_failure") == 1     # still NO_GO on its own checks
    d, cmp = read_json(newest(runs) / "decision.json"), read_json(newest(runs) / "comparison.json")
    ch = next(c for c in cmp["changes"] if c["id"] == "clinic.book-checkup.tool-timeout")
    assert ch["direction"] == "changed" and not ch["blocks"]
    assert d["baseline"]["regressions"] == 0 and d["baseline"]["changed"] == 1
    assert "confirmed_by_reruns" not in d["baseline"]                       # an edited test isn't rerun


RID = "r-20261112-100000-abcd"


def test_keep_a_fixed_test_as_a_regression_test(tmp_path):
    from vatf.web import data, regress

    root = tmp_path / "root"
    shutil.copytree(ROOT / "examples" / "clinic", root / "examples" / "clinic",
                    ignore=shutil.ignore_patterns("runs", "baselines", "*.db", "__pycache__"))
    runs = root / "runs"
    (runs / RID).mkdir(parents=True)
    write_json(runs / RID / "run.json", {"run_id": RID, "project": "examples/clinic"})
    out = regress.keep_as_test(root, runs, RID, "clinic.book-checkup.tool-timeout")
    assert out["kept"] and not out["already"]
    doc = yaml.safe_load((root / "examples" / "clinic" / out["file"]).read_text())
    assert "regression" in doc["tags"] and schema_errors("scenario", doc) == []
    assert regress.keep_as_test(root, runs, RID, "clinic.book-checkup.tool-timeout")["already"]
    with pytest.raises(data.NotFound):
        regress.keep_as_test(root, runs, RID, "safety.x")
    # Kept tests join every health check (with the critical ones).
    scs = [{"id": "a", "criticality": "critical"}, {"id": "b", "tags": ["regression"]}, {"id": "c"}]
    assert health.scenario_ids({}, scs) == ["a", "b"]


def test_profile_takes_the_new_regression_settings():
    prof = yaml.safe_load((ROOT / "examples/clinic/profile.yaml").read_text())
    prof["regression"] = {"confirm": {"reruns": 3, "min_failures": 2}, "baseline": {"rolling": 3}}
    assert schema_errors("client_profile", prof) == []
