"""Step 4 extras: cost per completed task (costs.py), recorded caller audio in scripted test cases, and
A/B runs of two agent versions on the same tests."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import yaml

from vatf import readiness
from vatf.costs import call_cost, money, prices, summary
from vatf.loader import load_project, schema_errors
from vatf.orchestrator import Orchestrator, RunConfig

ROOT = Path(__file__).resolve().parents[1]


def _v(status, expected, cls, ms, replies=4, tools=1):
    return {"status": status, "outcome": {"expected": expected, "class": cls},
            "metrics": {"duration_ms": ms, "agent_turns": replies, "tool_calls": tools}}


def test_cost_per_completed_task_counts_the_failed_calls_too():
    assert prices({}) is None and prices({"costs": {"currency": "inr"}}) is None   # no prices: no cost
    p = prices({"costs": {"currency": "inr", "per_minute": 6, "per_call": 1}})
    assert p["currency"] == "INR" and p["per_reply"] == 0
    assert call_cost(_v("PASS", "success", "TRUE_SUCCESS", 120_000), p) == 13          # 1 + 2 min x 6
    vs = [_v("PASS", "success", "TRUE_SUCCESS", 60_000), _v("FAIL", "success", "FALSE_SUCCESS", 60_000),
          _v("PASS", "no_action", "TRUE_NO_ACTION", 30_000), _v("ERROR", "success", None, 0)]
    s = summary(vs, p)
    assert (s["calls"], s["task_calls"], s["tasks_done"]) == (3, 2, 1)                 # the setup error is left out
    assert s["per_task"] == 14.0 and s["total"] == 18.0 and s["per_call"] == 6.0      # two task calls (7 each) per booking
    assert summary([_v("FAIL", "success", "FALSE_SUCCESS", 60_000)], p)["per_task"] is None
    assert money(14, "INR") == "₹14.00" and money(0.0421, "USD") == "$0.042" and money(0.5, "AED") == "0.500 AED"


# ---- recorded caller lines ----------------------------------------------------------------------

def _record(path: Path, text: str) -> None:
    """A 'recording' in the test-tone speech the zoo agent hears with (speech engine codec)."""
    from vatf.audio.pcm import from_pcm16, write_wav
    from vatf.speech.codec import CodecTTS

    clip = CodecTTS().synthesize(text, "any", 1.0)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_wav(path, [from_pcm16(clip.pcm16)], clip.sample_rate_hz)


def _voice_run(folder: Path, runs: Path, scenario: str = "clinic.confirm-before-booking"):
    from vatf.speech import SpeechConfig

    proj = load_project(folder, "profile-voice.yaml").with_agent_options({"brain": "rules", "bugs": "", "speech": "codec"})
    cfg = RunConfig(root=ROOT, runs_dir=runs, offline=True, speech=SpeechConfig(engine="codec"), repeats=1,
                    judges=False, scenario_ids=(scenario,), baseline="none")
    decision = asyncio.run(Orchestrator(proj, cfg, log=lambda *_: None).run())
    call = decision["calls"][0]
    run_dir = runs / decision["run_id"] / "calls" / call["test_id"]
    events = [json.loads(x) for x in (run_dir / "events.jsonl").read_text("utf-8").splitlines()]
    return decision, call, events


def test_a_scripted_line_is_played_from_its_recording(tmp_path):
    clinic = tmp_path / "clinic"
    shutil.copytree(ROOT / "examples" / "clinic", clinic, ignore=shutil.ignore_patterns("runs"))
    path = clinic / "scenarios" / "confirm-before-booking.yaml"
    doc = yaml.safe_load(path.read_text("utf-8"))
    _record(clinic / "recordings" / "maria-1.wav", doc["turns"][0]["caller"])
    _record(clinic / "recordings" / "maria-no.wav", "No thanks, not now.")
    doc["turns"][0]["audio"] = "recordings/maria-1.wav"
    assert schema_errors("scenario", doc) == []
    path.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), "utf-8")
    decision, call, events = _voice_run(clinic, tmp_path / "runs")
    said = [e["data"] for e in events if e["type"] == "caller_turn"]
    assert said[0]["recording"] == "recordings/maria-1.wav" and said[0]["text"].startswith("Hi, I'm [full_name]")
    assert "recording" not in said[1]                              # the other lines are synthesized
    assert call["status"] == "PASS"

    # The recording really is what the agent hears: one that says "no" where the line says "yes please".
    doc["turns"][2]["audio"] = "recordings/maria-no.wav"
    path.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), "utf-8")
    decision, call, events = _voice_run(clinic, tmp_path / "runs2")
    assert call["status"] == "FAIL"
    assert not any(e["type"] == "tool_request" and e["data"].get("tool") == "book_appointment" for e in events)

    # A missing recording stops the run before any call, naming the line.
    doc["turns"][1]["audio"] = "recordings/missing.wav"
    path.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), "utf-8")
    from vatf.orchestrator import RunSetupError
    try:
        _voice_run(clinic, tmp_path / "runs3")
        raise AssertionError("expected a setup error")
    except RunSetupError as e:
        assert "clinic.confirm-before-booking line 2: recording recordings/missing.wav: the file is missing" in str(e)


def test_an_uploaded_recording_is_checked_and_kept_in_the_agent_s_folder(tmp_path):
    from vatf import recordings

    _record(tmp_path / "src.wav", "Hello there")
    raw = (tmp_path / "src.wav").read_bytes()
    out = recordings.save(tmp_path, "Maria's Booking (1).WAV", raw)
    assert out["path"] == "recordings/maria-s-booking-1.wav" and out["seconds"] > 0
    assert recordings.save(tmp_path, "Maria's Booking (1).WAV", raw)["path"] == out["path"]   # same file again
    assert "isn't a WAV" in (recordings.wav_problem(b"hello") or "")
    assert recordings.wav_problem(raw) is None
    assert "inside the agent's folder" in str(_raises(lambda: recordings.resolve(tmp_path, "../x.wav")))


def _raises(fn):
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        return e
    raise AssertionError("expected an error")


# ---- cost per task and A/B ----------------------------------------------------------------------

def _clinic(tmp_path: Path, costs: dict | None = None) -> Path:
    folder = tmp_path / "clinic"
    shutil.copytree(ROOT / "examples" / "clinic", folder, ignore=shutil.ignore_patterns("runs", "baselines"))
    if costs:
        path = folder / "profile.yaml"
        doc = yaml.safe_load(path.read_text("utf-8"))
        doc["costs"] = costs
        assert schema_errors("client_profile", doc) == []
        path.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), "utf-8")
    return folder


def _cli(*argv: str) -> int:
    from vatf.cli import main

    return main(list(argv))


def test_a_run_reports_the_cost_per_completed_task(tmp_path):
    folder = _clinic(tmp_path, {"currency": "INR", "per_call": 2, "per_reply": 0.5, "per_tool_call": 0.25})
    proj = load_project(folder, "profile.yaml").with_agent_options({"brain": "rules"})
    cfg = RunConfig(root=ROOT, runs_dir=tmp_path / "runs", offline=True, repeats=1, judges=False,
                    scenario_ids=("clinic.book-checkup", "clinic.book-checkup.tool-timeout"), baseline="none")
    decision = asyncio.run(Orchestrator(proj, cfg, log=lambda *_: None).run())
    c = decision["costs"]
    assert schema_errors("run_decision", {k: v for k, v in decision.items() if not k.startswith("_")}) == []
    # The booking call is a task; the "tool times out" test expects an honest failure, so it isn't one.
    assert (c["currency"], c["calls"], c["task_calls"], c["tasks_done"]) == ("INR", 2, 1, 1)
    verdicts = decision["_verdicts"]
    book = next(v for v in verdicts if v["scenario_id"] == "clinic.book-checkup")
    m = book["metrics"]
    assert c["per_task"] == round(2 + 0.5 * m["agent_turns"] + 0.25 * m["tool_calls"], 4) and m["tool_calls"] >= 2
    report = readiness.build(tmp_path / "runs" / decision["run_id"])
    row = next(r for r in report["metrics"] if r["title"] == "Agent cost per completed task")
    assert row["value"].startswith("₹") and "Running costs" in row["note"]
    assert any(line.startswith("At your prices, each completed task cost ₹") for line in report["summary"])


def test_an_ab_test_runs_both_versions_and_compares_b_with_a(tmp_path):
    folder = _clinic(tmp_path, {"currency": "USD", "per_call": 0.05, "per_reply": 0.01})
    runs = tmp_path / "runs"
    code = _cli("run", str(folder), "--root", str(ROOT), "--runs-dir", str(runs), "--offline", "--no-judges",
                "--repeats", "1", "--agent-opt", "brain=rules", "--scenario", "clinic.book-checkup",
                "--scenario", "clinic.book-checkup.tool-timeout",
                "--ab-with", "profile.yaml", "--ab-agent-opt", "bugs=lie_on_failure", "--quiet")
    assert code == 0
    infos = {json.loads((d / "run.json").read_text("utf-8"))["ab"]["side"]: json.loads((d / "run.json").read_text("utf-8"))
             for d in runs.iterdir() if d.name.startswith("r-")}
    a, b = infos["A"], infos["B"]
    assert a["ab"]["group"] == b["ab"]["group"] and a["ab"]["other"] == b["run_id"] and b["ab"]["other"] == a["run_id"]
    assert a["ab"]["label"] == "profile (brain=rules)" and b["ab"]["label"] == "profile (brain=rules, bugs=lie_on_failure)"
    assert b["agent_options"]["bugs"] == "lie_on_failure" and not a["agent_options"].get("bugs")
    assert a["scenario_ids"] == b["scenario_ids"] and a["baseline"] == b["baseline"] == "none"
    cmp = json.loads((runs / b["run_id"] / "ab.json").read_text("utf-8"))
    fails = [c for c in cmp["changes"] if c["direction"] == "regression"]
    assert [c["id"] for c in fails] == ["clinic.book-checkup.tool-timeout"]
    assert "passed every call with version A (1/1) and failed with version B (0/1)" in fails[0]["message"]
    assert cmp["blocking"] == 0                                   # an A/B test never blocks

    # The dashboard's Compare page words the pair as an A/B test.
    from vatf.web import regress
    page = regress.compare_runs(ROOT, runs, a["run_id"], b["run_id"])
    assert page["ab"]["labels"] == {"A": a["ab"]["label"], "B": b["ab"]["label"]} and page["source"] == "ab"
    assert page["snapshots"]["a"]["cost"]["currency"] == "USD"
    assert page["runs"]["b"]["ab"]["side"] == "B"


def test_cost_changes_between_two_runs():
    from vatf.compare import as_ab, compare

    def snap(per_task, per_call=0.1):
        return {"run_id": "r", "tests": {}, "scores": {}, "quality": {}, "judges": {}, "how": {},
                "cost": {"currency": "USD", "per_task": per_task, "per_call": per_call, "tasks_done": 1, "task_calls": 1}}

    out = compare(snap(0.40), snap(0.30))["changes"]
    assert [(c["id"], c["direction"]) for c in out] == [("per_task", "better")]
    assert "went from $0.400 to $0.300 (-25%)" in out[0]["message"]
    assert compare(snap(0.40), snap(0.42))["changes"] == []          # within 10%
    gone = as_ab(compare(snap(0.40), snap(None)))["changes"]
    assert gone[0]["direction"] == "worse" and "no task done with B" in gone[0]["message"]


def test_the_dashboard_builds_an_ab_run(tmp_path):
    from vatf.web import data
    from vatf.web.jobs import JobError, JobManager

    clinic = data.project_by_path(ROOT, "examples/clinic")
    jobs = JobManager(ROOT, Path("runs"))
    args, command = jobs.build({"command": "run", "profile": "profile.yaml", "ab_with": "profile.yaml",
                                "ab_agent_options": {"model": "claude-sonnet-5-5"}, "offline": True}, clinic)
    i = args.index("--ab-with")
    assert args[i:i + 4] == ["--ab-with", "profile.yaml", "--ab-agent-opt", "model=claude-sonnet-5-5"]
    assert "--ab-with profile.yaml --ab-agent-opt model=claude-sonnet-5-5" in command
    for bad, why in (({"ab_with": "profile.yaml"}, "same as version A"),
                     ({"ab_with": "profile-voice.yaml"}, "both by voice or both by text"),
                     ({"ab_with": "nope.yaml"}, "no profile")):
        try:
            jobs.build({"command": "run", "profile": "profile.yaml", **bad}, clinic)
            raise AssertionError(bad)
        except JobError as e:
            assert why in str(e)
