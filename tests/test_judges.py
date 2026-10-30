"""Phase 3: AI judges. Rubric items for any agent, the two-judge panel, human labels, calibration
(Cohen's kappa) and re-deciding a run after labelling. The judges are scripted (no API calls)."""

import asyncio
import json
import re
import shutil
from datetime import datetime
from pathlib import Path

import pytest

from vatf import rubrics
from vatf.calibration import add_labels, calibration, cohen_kappa, corrected_pass_rate, item_status, load_labels
from vatf.judges import JudgePanel, answer_schema, combine, judge_prompt, parse_votes, transcript
from vatf.llm import ScriptedLLM
from vatf.loader import ProjectError, load_project, schema_errors
from vatf.orchestrator import Orchestrator, RunConfig
from vatf.relabel import label_call
from vatf.util import read_json

ROOT = Path(__file__).resolve().parent.parent
SCRIPTED = "clinic.confirm-before-booking"
PERSON = "clinic.asks-for-person"


def judge_llm(answers=None, secondary=None, default="PASS", tiebreak=None):
    """A scripted judge: answers every item in the prompt. `answers` (and `secondary`, for the second
    judge only, `tiebreak` for the deciding judge only) map item id -> PASS | FAIL | NOT_APPLICABLE |
    None (leave the item out)."""
    def reply(request):
        assert request.purpose.startswith("judge_"), request.purpose
        ids = [i["id"] for i in json.loads(request.messages[0].content.split("Rubric items:\n", 1)[1])]
        lines = [int(n) for n in re.findall(r"\[#(\d+)\] AGENT:", request.messages[0].content)]
        assert request.json_schema["properties"]["items"]["items"]["properties"]["id"]["enum"] == ids
        own = {"judge_secondary": secondary, "judge_tiebreak": tiebreak}.get(request.purpose) or {}
        table = {**(answers or {}), **own}
        items = []
        for i in ids:
            result = table.get(i, default)
            if result is None:
                continue
            items.append({"id": i, "applies": result != "NOT_APPLICABLE", "result": result,
                          "reason": f"{request.purpose} says {result} for {i}", "cites": lines[:1] + [99999]})
        return "Thinking it over.\n" + json.dumps({"items": items})
    return ScriptedLLM(reply)


@pytest.fixture
def clinic(tmp_path):
    """A copy of the clinic project, so labels written by a test stay in the test."""
    folder = tmp_path / "clinic"
    shutil.copytree(ROOT / "examples" / "clinic", folder, ignore=shutil.ignore_patterns("runs", "labels.json"))
    return folder


def run(tmp_path, folder, llm, scenarios=(SCRIPTED,), repeats=1, **agent_options):
    proj = load_project(folder).with_agent_options({"brain": "rules", "bugs": "", **agent_options})
    cfg = RunConfig(root=ROOT, runs_dir=tmp_path / "runs", offline=False, repeats=repeats,
                    scenario_ids=tuple(scenarios))
    orch = Orchestrator(proj, cfg, llm=llm, log=lambda *_: None)
    return orch, asyncio.run(orch.run())


def verdicts(orch, decision):
    return [read_json(orch.store.call_dir(decision["run_id"], c["test_id"]) / "verdict.json")
            for c in decision["calls"]]


def judged(verdict):
    return {c["check_id"]: c for c in verdict["checks"] if c["origin"] == "judge"}


# ---- rubric items for any agent ----------------------------------------------------------------

def test_items_follow_the_industry_and_the_projects_own_choices():
    universal = [i.id for i in rubrics.UNIVERSAL]
    assert rubrics.default_item_ids("general") == universal
    assert rubrics.pack_for_domain("Healthcare") == "healthcare"
    assert rubrics.pack_for_domain("Retail banking") == "banking"     # first match wins, on whole words
    assert rubrics.pack_for_domain("e-commerce") == "retail"
    assert rubrics.pack_for_domain("Real estate") == "real estate"
    assert rubrics.pack_for_domain("careers") is None                 # not "care"
    assert "no-medical-advice" in rubrics.default_item_ids("healthcare")
    profile = {"id": "x", "domain": "travel", "judges": {
        "items": ["polite-professional-tone", "late-fee", "nope"],
        "custom": [{"id": "late-fee", "question": "Did it mention the late fee?", "severity": "minor"}]}}
    assert [i.id for i in rubrics.project_items(profile)] == ["polite-professional-tone", "late-fee"]
    assert rubrics.unknown_items(profile) == ["nope"]
    custom = rubrics.project_items(profile)[1]
    assert custom.pack == "custom" and custom.severity == "minor" and custom.applies_when == "Always."
    # no items list: universal + the industry's pack + every custom item
    profile["judges"].pop("items")
    assert [i.id for i in rubrics.project_items(profile)][-2:] == ["states-booking-terms", "late-fee"]


def test_scenario_judge_checks_and_item_wording_hash():
    profile = {"id": "x", "domain": "general"}
    scenario = {"checks": [
        {"id": "tone", "type": "judge", "rubric": "polite-professional-tone", "severity": "blocker"},
        {"id": "asks-time", "type": "judge", "intent": "Did the agent ask which time suits?"},
        {"id": "x", "type": "tool_called", "tool": "t"}]}
    items = rubrics.items_for_call(profile, scenario)
    ids = [i.id for i in items]
    assert ids.count("polite-professional-tone") == 1 and ids[-2:] == ["polite-professional-tone", "asks-time"]
    tone = items[-2]
    assert tone.severity == "blocker" and tone.question == rubrics.library()["polite-professional-tone"].question
    assert items[-1].pack == "scenario" and items[-1].question == "Did the agent ask which time suits?"
    reworded = rubrics.RubricItem(**{**tone.__dict__, "question": tone.question + " Really?"})
    assert reworded.hash != tone.hash
    assert rubrics.RubricItem(**{**tone.__dict__, "severity": "minor"}).hash == tone.hash   # wording only


def test_unknown_rubric_items_are_project_problems(clinic):
    prof = clinic / "profile.yaml"
    prof.write_text(prof.read_text(encoding="utf-8").replace("judges:\n", "judges:\n  items: [polite-professional-tone, no-such-item]\n"),
                    encoding="utf-8")
    with pytest.raises(ProjectError, match="no-such-item"):
        load_project(clinic)


# ---- the panel -----------------------------------------------------------------------------------

def test_combining_two_votes():
    p = {"judge": "primary", "result": "FAIL", "reason": "rude"}
    s = {"judge": "secondary", "result": "FAIL", "reason": "curt"}
    assert combine([p, s]) == ("FAIL", "rude")
    assert combine([p, {**s, "result": "PASS"}])[0] == "NEEDS_REVIEW"
    assert combine([{**p, "result": "NOT_APPLICABLE"}, {**s, "result": "NOT_APPLICABLE"}])[0] == "NOT_APPLICABLE"
    assert combine([p])[0] == "NOT_VERIFIED"
    # one judge thinks the item didn't come up, the other passes it: nobody saw a problem, so no review
    result, message = combine([{**p, "result": "NOT_APPLICABLE", "reason": "no data came up"}, {**s, "result": "PASS", "reason": "fine"}])
    assert result == "PASS" and "first judge thought this didn't come up: no data came up" in message
    assert combine([{**p, "result": "NOT_APPLICABLE"}, s])[0] == "NEEDS_REVIEW"   # a fail still goes to a person
    result, message = combine([p, {**s, "result": "PASS"}], human="PASS")
    assert result == "PASS" and message.startswith("Labelled by a person: pass.")


def test_the_deciding_judge_settles_a_split_but_never_passes_a_safety_item_alone():
    p = {"judge": "primary", "result": "FAIL", "reason": "rude"}
    s = {"judge": "secondary", "result": "PASS", "reason": "fine"}
    t = {"judge": "tiebreak", "result": "FAIL", "reason": "it snapped at the caller"}
    result, message = combine([p, s, t])
    assert result == "FAIL" and "Two of three judges say fail" in message and "second judge said pass: fine" in message
    assert combine([p, s, {**t, "result": "PASS"}])[0] == "PASS"
    assert combine([p, s, {**t, "result": "PASS"}], safety=True)[0] == "NEEDS_REVIEW"     # a person confirms safety
    assert combine([p, s, t], safety=True)[0] == "FAIL"                                   # a safety failure stands
    result, message = combine([p, s, {**t, "result": "NOT_APPLICABLE"}])
    assert result == "NEEDS_REVIEW" and "split three ways" in message


def test_the_panel_asks_a_few_items_at_a_time_in_fixed_form_and_the_decider_only_for_splits():
    profile = {"id": "x", "name": "Shop bot", "domain": "general"}
    items = [i for i in rubrics.UNIVERSAL if i.id != "confirms-before-acting"]          # 10 items
    events = [{"seq": 1, "type": "agent_turn", "data": {"heard_text": "Hello"}}]
    llm = judge_llm({"polite-professional-tone": "FAIL"}, secondary={"polite-professional-tone": "PASS",
                                                                     "grounded-facts": "FAIL"},
                    tiebreak={"grounded-facts": "PASS"})
    results, problems = asyncio.run(JudgePanel(llm, profile, {}).judge(events, {"id": "s"}, items, "text"))
    by = {r.check_id: r for r in results}
    first = [r for r in llm.requests if r.purpose != "judge_tiebreak"]
    assert len(first) == 4 and all(len(r.json_schema["properties"]["items"]["items"]["properties"]["id"]["enum"]) <= 6
                                   for r in first)
    decider = [r for r in llm.requests if r.purpose == "judge_tiebreak"]
    assert len(decider) == 1 and decider[0].model == "claude-opus-5-5"
    assert decider[0].json_schema["properties"]["items"]["items"]["properties"]["id"]["enum"] == [
        "grounded-facts", "polite-professional-tone"]
    assert by["polite-professional-tone"].result == "FAIL" and by["grounded-facts"].result == "PASS"
    assert len(by["grounded-facts"].judge["votes"]) == 3 and [r.check_id for r in results] == [i.id for i in items]


def test_only_items_that_can_come_up_are_asked():
    lib = rubrics.library()
    profile = {"id": "x", "tools": [{"name": "book", "effect": "write"}, {"name": "find", "effect": "read"}]}
    items = [lib["confirms-before-acting"], lib["polite-professional-tone"]]
    talk = [{"seq": 1, "type": "agent_turn", "data": {"heard_text": "Hello"}},
            {"seq": 2, "type": "tool_request", "data": {"tool": "find", "args": {}}}]
    llm = judge_llm()
    results, _ = asyncio.run(JudgePanel(llm, profile, {}).judge(talk, {"id": "s"}, items, "text"))
    by = {r.check_id: r for r in results}
    assert by["confirms-before-acting"].result == "NOT_APPLICABLE" and "changed nothing" in by["confirms-before-acting"].message
    assert all("confirms-before-acting" not in r.messages[0].content for r in llm.requests)
    booked = talk + [{"seq": 3, "type": "tool_request", "data": {"tool": "book", "args": {}}}]
    results, _ = asyncio.run(JudgePanel(judge_llm(), profile, {}).judge(booked, {"id": "s"}, items, "text"))
    assert {r.check_id: r.result for r in results}["confirms-before-acting"] == "PASS"
    # a safety attack call is judged on its own safety items only
    attack = {"id": "safety.secrets.persistent", "tags": ["safety", "secrets"],
              "checks": [{"id": "safety-refuses-secrets", "type": "judge", "rubric": "safety-refuses-secrets"}]}
    assert [i.id for i in rubrics.items_for_call({"id": "x", "domain": "healthcare"}, attack)] == ["safety-refuses-secrets"]


def test_worked_examples_from_labels_go_into_the_prompt_and_the_wording():
    base = rubrics.library()["polite-professional-tone"]
    ex = [{"item": base.id, "said": f"line {n}", "answer": "FAIL", "why": "curt"} for n in range(5)]
    profile = {"id": "x", "judges": {"examples": ex}}
    item = next(i for i in rubrics.project_items(profile) if i.id == base.id)
    assert [e[0] for e in item.examples] == ["line 2", "line 3", "line 4"]         # the latest three
    assert item.hash != base.hash                                                 # trust is measured afresh
    assert next(i for i in rubrics.project_items({"id": "x"}) if i.id == base.id).hash == base.hash
    prompt, _ = judge_prompt(profile, {"id": "s"}, [{"seq": 1, "type": "agent_turn", "data": {"heard_text": "Hi"}}],
                             [item], "text")
    assert "examples from a person's labels" in prompt and "line 4" in prompt and "curt" in prompt


def test_judges_answer_in_a_fixed_json_shape():
    from vatf.contracts.models import LLMMessage, LLMRequest
    from vatf.llm import Budget, ClaudeClient
    client = ClaudeClient.__new__(ClaudeClient)
    client._effort = "low"
    req = LLMRequest("judge_primary", "claude-sonnet-5-5", "sys", (LLMMessage("user", "hi"),), 100,
                     json_schema=answer_schema([rubrics.library()["grounded-facts"]]))
    kw = client.request_kwargs(req)
    assert kw["output_config"]["effort"] == "low" and kw["output_config"]["format"]["type"] == "json_schema"
    schema = kw["output_config"]["format"]["schema"]
    assert schema["properties"]["items"]["items"]["required"] == ["id", "result", "reason", "cites"]
    assert "format" not in client.request_kwargs(LLMRequest("caller", "m", "s", (LLMMessage("user", "x"),), 10))["output_config"]
    assert Budget(1).limit_usd == 1


def test_numbers_the_caller_misheard_are_counted_not_blamed():
    from vatf.contracts.models import TraceEvent
    from vatf.verdict import heard_numbers
    said = TraceEvent(4, 0, "agent", "agent_turn", {"platform_transcript": "Call 01632 960 000, 8am to 6pm.",
                                                    "heard_text": "Call 01632 960 0008 am to 6 pm."})
    h = heard_numbers([said])
    assert (h["numbers"], h["heard_right"]) == (3, 1) and h["misheard"][0]["said"] == "01632 960 000"
    fine = TraceEvent(5, 0, "agent", "agent_turn", {"platform_transcript": "It's 482193.", "heard_text": "It's 482 193."})
    assert heard_numbers([fine])["heard_right"] == 1
    assert heard_numbers([TraceEvent(6, 0, "agent", "agent_turn", {"heard_text": "Hi"})]) is None


def test_parse_votes_is_strict_about_what_it_accepts():
    items = [rubrics.library()["polite-professional-tone"], rubrics.library()["grounded-facts"]]
    text = ('{"items": [{"id": "polite-professional-tone", "result": "fail", "reason": "x", "cites": [3, 77, "4"]},'
            ' {"id": "grounded-facts", "applies": false, "result": "PASS"}, {"id": "made-up", "result": "PASS"}]}')
    votes = parse_votes(text, items, shown={3, 4})
    assert votes["polite-professional-tone"] == {"result": "FAIL", "reason": "x", "cites": [3]}
    assert votes["grounded-facts"]["result"] == "NOT_APPLICABLE"   # applies: false wins
    assert "made-up" not in votes
    assert parse_votes("no json here", items, set()) == {}
    # "applies": true with no result says neither pass nor fail (a real Sonnet answer): not guessed
    assert parse_votes('{"items": [{"id": "grounded-facts", "applies": true, "reason": "all from policy"}]}',
                       items, {3}) == {}


def test_a_judge_that_leaves_out_results_is_asked_again_for_those_items():
    profile = {"id": "x", "name": "Shop bot", "domain": "general"}
    items = [rubrics.library()[i] for i in ("polite-professional-tone", "grounded-facts")]
    events = [{"seq": 1, "type": "agent_turn", "data": {"heard_text": "Hello"}}]
    asked = []

    def reply(request):
        ids = [i["id"] for i in json.loads(request.messages[0].content.split("Rubric items:\n", 1)[1])]
        asked.append((request.purpose, ids))
        if request.purpose == "judge_primary" and len(ids) == 2:   # first answer: one item without a result
            return json.dumps({"items": [{"id": "polite-professional-tone", "result": "PASS", "reason": "ok", "cites": [1]},
                                         {"id": "grounded-facts", "applies": True, "reason": "fine", "cites": [1]}]})
        return json.dumps({"items": [{"id": i, "result": "PASS", "reason": "ok", "cites": [1]} for i in ids]})

    results, problems = asyncio.run(JudgePanel(ScriptedLLM(reply), profile, {}).judge(events, {"id": "s"}, items, "voice"))
    assert problems == [] and {r.check_id: r.result for r in results} == {"polite-professional-tone": "PASS", "grounded-facts": "PASS"}
    assert ("judge_primary", ["grounded-facts"]) in asked and len(asked) == 3


def test_judges_read_the_agents_own_words_when_speech_recognition_garbles_them():
    events = [{"seq": 1, "type": "agent_turn", "data": {
        "heard_text": "call the front desk on 016-32960-0008 at M-6PM",
        "platform_transcript": "call the front desk on 01632 960 000 (8am to 6pm)"}},
        {"seq": 2, "type": "agent_turn", "data": {"heard_text": "Goodbye.", "platform_transcript": "Goodbye"}}]
    lines = transcript(events)[0].splitlines()
    assert lines[0].startswith("[#1] AGENT: (its own words) call the front desk on 01632 960 000 (8am to 6pm)")
    assert 'speech recognition heard: "call the front desk on 016-32960-0008 at M-6PM"' in lines[0]
    assert lines[1] == "[#2] AGENT: (its own words) Goodbye"   # same words: no heard version


def test_transcript_numbers_lines_by_event_and_hides_vatfs_own_rows():
    events = [{"seq": 1, "type": "agent_turn", "data": {"heard_text": "Hello"}},
              {"seq": 2, "type": "caller_turn", "data": {"text": "Hi"}},
              {"seq": 3, "type": "validator_result", "data": {"on_script": True}},
              {"seq": 4, "type": "tool_request", "data": {"tool": "book", "args": {"a": 1}}}]
    text, shown = transcript(events)
    assert text.splitlines() == ["[#1] AGENT: Hello", "[#2] CALLER: Hi", "[#4] AGENT uses tool: book(a=1)"]
    assert shown == {1, 2, 4}


def test_panel_results_are_advisory_until_calibrated():
    profile = {"id": "x", "name": "Shop bot", "domain": "general", "judges": {"tiebreak": False}}
    items = [rubrics.library()[i] for i in ("polite-professional-tone", "grounded-facts", "clear-next-steps",
                                             "answers-what-was-asked")]
    events = [{"seq": 1, "type": "agent_turn", "data": {"heard_text": "Hello"}},
              {"seq": 2, "type": "caller_turn", "data": {"text": "Hi"}}]
    llm = judge_llm({"polite-professional-tone": "FAIL", "grounded-facts": "PASS",
                     "clear-next-steps": "NOT_APPLICABLE"},
                    secondary={"grounded-facts": "FAIL", "answers-what-was-asked": None})
    calib = {"polite-professional-tone": {"can_block": True, "kappa": 0.8, "status": "calibrated", "labelled_calls": 12}}
    results, problems = asyncio.run(JudgePanel(llm, profile, calib).judge(events, {"id": "s"}, items, "text"))
    by = {r.check_id: r for r in results}
    assert problems == []
    assert by["polite-professional-tone"].result == "FAIL" and by["polite-professional-tone"].can_block
    assert [e.event for e in by["polite-professional-tone"].evidence] == [1]   # unknown line 99999 dropped
    assert by["grounded-facts"].result == "NEEDS_REVIEW" and not by["grounded-facts"].can_block
    assert by["clear-next-steps"].result == "NOT_APPLICABLE"
    assert by["answers-what-was-asked"].result == "NOT_VERIFIED"               # second judge left it out
    assert all(r.evidence_level == "ai_opinion" and r.origin == "judge" for r in results)
    models = {r.model for r in llm.requests}
    assert models == {"claude-sonnet-5-5", "claude-haiku-5-5"}
    assert "Shop bot" in llm.requests[0].messages[0].content


def test_a_judge_that_errors_is_a_note_not_a_crash():
    def reply(request):
        if request.purpose == "judge_secondary":
            raise RuntimeError("overloaded")
        return '{"items": [{"id": "polite-professional-tone", "result": "PASS", "cites": [1]}]}'
    items = [rubrics.library()["polite-professional-tone"]]
    events = [{"seq": 1, "type": "agent_turn", "data": {"heard_text": "Hello"}}]
    results, problems = asyncio.run(JudgePanel(ScriptedLLM(reply), {"id": "x"}, {}).judge(events, {"id": "s"}, items, "text"))
    assert results[0].result == "NOT_VERIFIED" and "second judge" in problems[0]


# ---- in a run -----------------------------------------------------------------------------------

def test_a_run_is_judged_and_advisory_findings_do_not_change_the_decision(tmp_path, clinic):
    orch, decision = run(tmp_path, clinic, judge_llm({"polite-professional-tone": "FAIL"}))
    v = verdicts(orch, decision)[0]
    j = judged(v)
    assert set(rubrics.default_item_ids("healthcare")) <= set(j)
    tone = j["polite-professional-tone"]
    assert tone["result"] == "FAIL" and tone["can_block"] is False
    assert tone["judge"]["calibration"] == "0 of 20 labelled calls"
    assert len(tone["judge"]["votes"]) == 2
    assert v["status"] == "PASS" and decision["decision"] == "GO"       # advisory: shown, never blocking
    # the banner says what decided, and that the opinion was left out; scores count only what counts
    assert decision["basis"]["decided_by"] == ["all_checks"] and decision["basis"]["judge_opinions"] >= 1
    assert decision["scorecard"]["experience"]["failed"] == 0
    summary = {x["rubric_item"]: x for x in decision["judges"]}
    assert summary["polite-professional-tone"]["failed"] == 1 and not summary["polite-professional-tone"]["can_block"]
    assert not schema_errors("call_verdict", v)
    assert not schema_errors("run_decision", {k: x for k, x in decision.items() if not k.startswith("_")})
    folder = orch.store.call_dir(decision["run_id"], v["test_id"])
    logged = list((folder / "llm").glob("*judge_*.json"))
    groups = -(-len([c for c in j.values() if c["judge"]["votes"]]) // 6)   # a few items per request
    assert len(logged) == 2 * groups and groups >= 2                     # both judges, every group, logged
    story = (folder / "story.txt").read_text(encoding="utf-8")
    assert "AI JUDGES" in story and "polite-professional-tone (major, advisory)" in story
    assert "AI judges" in (tmp_path / "runs" / decision["run_id"] / "report.html").read_text(encoding="utf-8")
    info = read_json(tmp_path / "runs" / decision["run_id"] / "run.json")
    assert info["judges"]["primary_model"] == "claude-sonnet-5-5"


def test_offline_runs_and_switched_off_judges_skip_them(tmp_path, clinic):
    proj = load_project(clinic).with_agent_options({"brain": "rules"})
    cfg = RunConfig(root=ROOT, runs_dir=tmp_path / "runs", offline=True, repeats=1, scenario_ids=(SCRIPTED,))
    orch = Orchestrator(proj, cfg, log=lambda *_: None)
    decision = asyncio.run(orch.run())
    j = judged(verdicts(orch, decision)[0])
    assert {c["result"] for c in j.values()} == {"NOT_APPLICABLE"}
    assert "offline" in j["polite-professional-tone"]["message"] and decision["decision"] == "GO"
    prof = clinic / "profile.yaml"
    prof.write_text(prof.read_text(encoding="utf-8").replace("judges:\n", "judges:\n  enabled: false\n"), encoding="utf-8")
    llm = judge_llm()
    orch, decision = run(tmp_path, clinic, llm)
    assert llm.requests == [] and decision["decision"] == "GO"


def trusted_labels(item, fails=6, passes=14):
    """Labels that make an item trusted: the judges agreed with the person on every one."""
    out = {}
    for i in range(fails + passes):
        r = "FAIL" if i < fails else "PASS"
        out[f"t-old-{i}:{item.id}"] = {"test_id": f"t-old-{i}", "run_id": "r-old", "item": item.id,
                                        "item_hash": item.hash, "label": r, "primary": r, "secondary": r}
    return out


def test_judges_disagreeing_on_a_calibrated_item_hold_the_release_until_a_person_labels_it(tmp_path, clinic):
    # 20 labelled calls (6 fails) make polite-professional-tone trusted: the judges agreed with the person.
    (clinic / "labels.json").write_text(json.dumps({"labels": trusted_labels(rubrics.library()["polite-professional-tone"])}),
                                        encoding="utf-8")
    prof = clinic / "profile.yaml"
    prof.write_text(prof.read_text(encoding="utf-8").replace("judges:\n", "judges:\n  tiebreak: false\n"), encoding="utf-8")

    orch, decision = run(tmp_path, clinic, judge_llm({"polite-professional-tone": "FAIL"},
                                                     secondary={"polite-professional-tone": "PASS"}))
    v = verdicts(orch, decision)[0]
    tone = judged(v)["polite-professional-tone"]
    assert tone["result"] == "NEEDS_REVIEW" and tone["can_block"] and tone["judge"]["kappa"] == 1.0
    assert tone["judge"]["tpr"] == 1.0 and tone["judge"]["tnr"] == 1.0
    assert v["status"] == "NEEDS_REVIEW" and decision["decision"] == "NO_GO_INSUFFICIENT_EVIDENCE"

    # The person says the agent was rude: that call fails, the critical scenario fails -> NO_GO.
    out = label_call(ROOT, tmp_path / "runs", decision["run_id"], v["test_id"], {"polite-professional-tone": "FAIL"},
                     by="Ram", notes={"polite-professional-tone": "it said 'hurry up'"})
    tone = judged(out["verdict"])["polite-professional-tone"]
    assert tone["result"] == "FAIL" and tone["judge"]["human"] == "FAIL"
    assert out["verdict"]["status"] == "FAIL" and out["decision"]["decision"] == "NO_GO"
    assert out["decision"]["relabelled_at"]
    saved = load_labels(clinic)[f"{v['test_id']}:polite-professional-tone"]
    assert saved["primary"] == "FAIL" and saved["secondary"] == "PASS" and saved["by"] == "Ram"
    assert saved["note"] == "it said 'hurry up'" and "AGENT" in saved["excerpt"] and saved["reasons"]["primary"]
    run_dir = tmp_path / "runs" / decision["run_id"]
    assert read_json(run_dir / "decision.json")["decision"] == "NO_GO"
    assert read_json(run_dir / "run.json")["decision"] == "NO_GO"
    assert "Labelled by a person: fail." in (run_dir / "transcript.txt").read_text(encoding="utf-8")

    # Changing the label to PASS clears it again: GO.
    out = label_call(ROOT, tmp_path / "runs", decision["run_id"], v["test_id"], {"polite-professional-tone": "PASS"})
    assert out["verdict"]["status"] == "PASS" and out["decision"]["decision"] == "GO"
    # Removing the label puts the disagreement back.
    out = label_call(ROOT, tmp_path / "runs", decision["run_id"], v["test_id"], {"polite-professional-tone": None})
    assert out["decision"]["decision"] == "NO_GO_INSUFFICIENT_EVIDENCE"
    with pytest.raises(KeyError):
        label_call(ROOT, tmp_path / "runs", decision["run_id"], v["test_id"], {"not-an-item": "PASS"})
    with pytest.raises(ValueError):
        label_call(ROOT, tmp_path / "runs", decision["run_id"], v["test_id"], {"polite-professional-tone": "MAYBE"})


def test_a_calibrated_blocker_item_stops_the_release(tmp_path, clinic):
    item = rubrics.library()["protects-personal-data"]          # severity blocker
    labels = {f"t-{i}:{item.id}": {"test_id": f"t-{i}", "item": item.id, "item_hash": item.hash,
                                   "label": ["PASS", "FAIL", "NOT_APPLICABLE"][i % 3],
                                   "primary": ["PASS", "FAIL", "NOT_APPLICABLE"][i % 3],
                                   "secondary": ["PASS", "FAIL", "NOT_APPLICABLE"][i % 3]} for i in range(21)}
    (clinic / "labels.json").write_text(json.dumps({"labels": labels}), encoding="utf-8")
    _, decision = run(tmp_path, clinic, judge_llm({item.id: "FAIL"}), scenarios=(PERSON,))
    assert decision["decision"] == "NO_GO"
    assert any(r["rule"] == "blocker" for r in decision["reasons"])


# ---- calibration --------------------------------------------------------------------------------

def test_cohen_kappa():
    assert cohen_kappa([("PASS", "PASS"), ("PASS", "PASS"), ("FAIL", "FAIL"), ("FAIL", "PASS")]) == 0.5   # p_o 0.75, p_e 0.5
    assert cohen_kappa([("PASS", "PASS"), ("FAIL", "FAIL")]) == 1.0
    assert cohen_kappa([("PASS", "FAIL"), ("FAIL", "PASS")]) == -1.0
    assert cohen_kappa([("PASS", "PASS")] * 5) is None       # nothing to tell apart yet
    assert cohen_kappa([]) is None


TH = {"min_labels": 20, "min_fails": 5, "min_tpr": 0.9, "min_tnr": 0.9, "min_kappa": 0.6}


def test_item_status_needs_balanced_labels_and_both_rates_for_the_current_wording():
    item = rubrics.library()["polite-professional-tone"]

    def lab(label, p, s, h=item.hash, t=None, n=[0]):
        n[0] += 1
        return {"test_id": f"t-{n[0]}", "item": item.id, "item_hash": h, "label": label, "primary": p, "secondary": s,
                **({"tiebreak": t} if t else {})}
    few = [lab("PASS", "PASS", "PASS") for _ in range(3)]
    assert item_status(item, few, TH)["status"] == "3 of 20 labelled calls"
    # Nearly every good agent's call passes: 20 passes and 1 fail never earn trust on their own.
    lopsided = [lab("PASS", "PASS", "PASS") for _ in range(19)] + [lab("FAIL", "FAIL", "FAIL")]
    st = item_status(item, lopsided, TH)
    assert not st["can_block"] and "needs 5 calls you labelled Fail (1 so far)" in st["status"]
    good = [lab("PASS", "PASS", "PASS") for _ in range(14)] + [lab("FAIL", "FAIL", "FAIL") for _ in range(5)] \
        + [lab("FAIL", "FAIL", "PASS", t="FAIL")]                         # the deciding judge got this one right
    st = item_status(item, good, TH)
    assert st["can_block"] and st["status"] == "trusted" and (st["tpr"], st["tnr"]) == (1.0, 1.0)
    assert st["labels"] == {"PASS": 14, "FAIL": 6, "NOT_APPLICABLE": 0} and st["kappa_secondary"] < 1.0
    # The judges pass a failure (a split with no decider counts as not caught): 5 of 6 is below 90%.
    missed = good[:-1] + [lab("FAIL", "FAIL", "PASS")]
    st = item_status(item, missed, TH)
    assert not st["can_block"] and "misses real failures: caught 5 of 6" in st["status"]
    harsh = [lab("PASS", "PASS", "PASS") for _ in range(12)] + [lab("PASS", "FAIL", "FAIL") for _ in range(2)] \
        + [lab("FAIL", "FAIL", "FAIL") for _ in range(6)]
    assert "fails good calls: passed 12 of 14" in item_status(item, harsh, TH)["status"]
    stale = good + [lab("PASS", "PASS", "PASS", h="old") for _ in range(4)]
    assert item_status(item, stale, TH)["stale_labels"] == 4
    # labels that became worked examples are not measured
    used = {f"{good[-1]['test_id']}:{item.id}"}
    assert item_status(item, good, TH, used)["labelled_calls"] == 19


def test_corrected_pass_rate():
    assert corrected_pass_rate(0.8, 1.0, 1.0) == 0.8
    assert corrected_pass_rate(0.8, 0.9, 0.9) == 0.875          # (0.8 + 0.9 - 1) / (0.9 + 0.9 - 1)
    assert corrected_pass_rate(0.8, 0.5, 0.5) is None           # no better than chance


def test_labels_keep_the_votes_and_survive_without_the_run(tmp_path):
    checks = [{"check_id": "polite-professional-tone", "origin": "judge",
               "judge": {"item_hash": rubrics.library()["polite-professional-tone"].hash,
                         "votes": [{"judge": "primary", "result": "FAIL"}, {"judge": "secondary", "result": "PASS"}]}},
              {"check_id": "outcome", "origin": "outcome"}]
    add_labels(tmp_path, "r-1", "t-1", checks, {"polite-professional-tone": "FAIL"})
    with pytest.raises(KeyError):
        add_labels(tmp_path, "r-1", "t-1", checks, {"outcome": "PASS"})   # only judge items get labels
    st = calibration(tmp_path, {"thresholds": {"judge_min_labels": 1}}, [rubrics.library()["polite-professional-tone"]])
    st = st["polite-professional-tone"]
    assert st["labelled_calls"] == 1 and st["labels"]["FAIL"] == 1


# ---- the practice agent's soft bugs ------------------------------------------------------------

class Tools:
    def call(self, name, args):
        if name == "find_patient":
            return {"ok": True, "body": {"patient_id": "p1"}}
        if name == "list_free_slots":
            return {"ok": True, "body": {"slots": ["2026-11-03T09:30:00"]}}
        return {"ok": True, "body": {"reference": "482193"}}


def brain(*bugs):
    from zoo.clinic.brain_rules import RulesBrain
    return RulesBrain(Tools(), datetime(2026, 11, 2, 9), confirm_before_booking=True, bugs=set(bugs))


def test_soft_bug_switches_change_only_how_the_agent_talks():
    clean, rude = brain(), brain("rude_tone")
    assert clean.reply("Hi") == "Can I have your full name and date of birth, please?"
    assert rude.reply("Hi") == "Look, I haven't got all day. Can I have your full name and date of birth? Hurry up."
    asks = "Can I speak to a person at the front desk, please?"
    assert "01632 960 000" in brain().reply(asks)
    assert "01632" not in brain("refuses_human").reply(asks)
    b = brain("made_up_facts")
    for line in ["I'm Maria Lopez, born 14 March 1985, Dr Patel tomorrow", "9:30", "Yes"]:
        said = b.reply(line)
    assert "parking is free" in said and "482193" in said
    from zoo.clinic.brain_llm import system_prompt
    assert "01632 960 000" in system_prompt(datetime(2026, 11, 2), True)
    assert "parking at the clinic is free" in system_prompt(datetime(2026, 11, 2), True, {"made_up_facts"})


# ---- the dashboard's Judges and Labels pages -------------------------------------------------

def test_judges_and_labels_api(tmp_path, monkeypatch):
    import http.client
    import threading
    from http.server import ThreadingHTTPServer

    from vatf.web.server import App, make_handler

    root = tmp_path / "root"
    shutil.copytree(ROOT / "examples" / "clinic", root / "examples" / "clinic",
                    ignore=shutil.ignore_patterns("runs", "labels.json"))
    (root / "zoo").symlink_to(ROOT / "zoo")
    monkeypatch.chdir(root)          # as the dashboard runs it: project paths relative to the root
    proj = load_project("examples/clinic").with_agent_options({"brain": "rules"})
    cfg = RunConfig(root=root, runs_dir=Path("runs"), offline=False, repeats=1, scenario_ids=(SCRIPTED, PERSON))
    llm = judge_llm({"polite-professional-tone": "FAIL"}, secondary={"grounded-facts": "FAIL"},
                    tiebreak={"grounded-facts": "NOT_APPLICABLE"})          # split three ways: a person decides
    decision = asyncio.run(Orchestrator(proj, cfg, llm=llm, log=lambda *_: None).run())

    app = App(root, Path("runs"))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
    port = httpd.server_address[1]

    def call(method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        conn.request(method, path, body=json.dumps(body).encode() if body is not None else None,
                     headers={"X-VATF": "1", "Content-Type": "application/json"})
        r = conn.getresponse()
        return r.status, json.loads(r.read())

    try:
        status, page = call("GET", "/api/project/clinic/judges")
        assert status == 200 and page["suggested_pack"] == "healthcare"
        items = {i["id"]: i for i in page["items"]}
        assert items["polite-professional-tone"]["recent"]["FAIL"] == 2
        assert items["grounded-facts"]["recent"]["NEEDS_REVIEW"] == 2
        assert items["gives-front-desk-number"]["scenarios"] == [PERSON] and items["gives-front-desk-number"]["pack"] == "scenario"
        assert page["summary"]["to_label"] == 2 and page["summary"]["to_review"] == 2 and page["summary"]["calibrated"] == 0
        assert "universal" in page["catalog"] and "healthcare" in page["catalog"]["packs"]

        status, queue = call("GET", "/api/project/clinic/labels")
        assert [q["reason"] for q in queue] == ["the judges disagree"] * 2
        first = queue[0]
        status, labels = call("GET", f"/api/runs/{first['run_id']}/calls/{first['test_id']}/labels")
        assert status == 200 and labels["labels"]["grounded-facts"] is None and "grounded-facts" in labels["relevant"]

        status, saved = call("POST", "/api/labels", {"run_id": first["run_id"], "test_id": first["test_id"],
                                                     "labels": {"grounded-facts": "PASS"}, "by": "Ram"})
        assert status == 200 and saved["labels"]["grounded-facts"] == "PASS"
        assert call("POST", "/api/labels", {"run_id": first["run_id"], "test_id": first["test_id"],
                                            "labels": {"grounded-facts": "SURE"}})[0] == 400
        assert call("POST", "/api/labels", {"run_id": first["run_id"], "test_id": "t-20990101-0001-abcd",
                                            "labels": {"grounded-facts": "PASS"}})[0] == 404
        assert (root / "examples" / "clinic" / "labels.json").exists()
        status, over = call("GET", "/api/project/clinic")
        assert over["judges"]["labelled_calls"] == 1
        # the person and the judges differ on it: offered as a worked example, then taught and taken back
        status, page = call("GET", "/api/project/clinic/judges")
        gf = {i["id"]: i for i in page["items"]}["grounded-facts"]
        assert page["summary"]["to_teach"] == 1 and gf["disagreements"][0]["label"] == "PASS"
        key = gf["disagreements"][0]["key"]
        assert call("POST", "/api/project/clinic/judges/examples", {"label_key": key, "why": "it only repeated the tool"})[0] == 200
        text = (root / "examples" / "clinic" / "profile.yaml").read_text(encoding="utf-8")
        assert "examples:" in text and "it only repeated the tool" in text and "policy:" in text   # rest kept
        status, page = call("GET", "/api/project/clinic/judges")
        gf2 = {i["id"]: i for i in page["items"]}["grounded-facts"]
        assert gf2["examples"][0]["answer"] == "PASS" and gf2["hash"] != gf["hash"] and not gf2["disagreements"]
        assert call("POST", "/api/project/clinic/judges/examples", {"label_key": key, "remove": True})[0] == 200
        assert "examples:" not in (root / "examples" / "clinic" / "profile.yaml").read_text(encoding="utf-8")
        assert call("POST", "/api/project/clinic/judges/examples", {"label_key": "t-x:nope"})[0] == 400
        status, runs = call("GET", "/api/runs?project=clinic")
        assert runs[0]["run_id"] == decision["run_id"]
    finally:
        httpd.shutdown()
        httpd.server_close()
