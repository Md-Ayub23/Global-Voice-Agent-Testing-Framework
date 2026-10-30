"""The test plan writer (planner.py) and the persona library, with a scripted model."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest
import yaml

from vatf import personas, planner
from vatf.llm import ScriptedLLM
from vatf.loader import load_project, schema_errors

ROOT = Path(__file__).resolve().parents[1]

ANALYSIS = {
    "summary": "Takes one pizza or sushi order per call.",
    "intents": [{"id": "order_pizza", "description": "Order a pizza", "tools": ["place_order"]},
                {"id": "ask_delivery", "description": "Ask when food arrives", "tools": ["delivery_estimate"]}],
    "slots": [{"name": "size", "description": "pizza size", "example": "large"}],
    "rules": [{"id": "R1", "text": "Read back the order and wait for confirmation before placing it.", "kind": "must"},
              {"id": "R2", "text": "Small $10, medium $15, large $20.", "kind": "fact"},
              {"id": "R3", "text": "Never say an order went through when it failed.", "kind": "must_not"}],
    "workflows": [{"intent": "order_pizza", "steps": ["greet", "choose", "read back", "place"]}],
    "unclear": ["What happens if the kitchen is closed."],
}


def draft(**over):
    d = {"category": "happy", "kind": "happy_path", "title": "Orders a large vegetarian pizza",
         "why": "The main job.", "intent": "order_pizza", "rule_ids": ["R1"], "persona": "clear-caller",
         "goal": "Order a large vegetarian pizza and confirm when it is read back.",
         "facts": [{"name": "pizza", "value": "large vegetarian"}], "end_when": "The order is placed.",
         "max_turns": 12, "criticality": "critical",
         "fault": {"tool": "none", "type": "none", "status": 0},
         "outcome": {"expected": "success", "tool": "place_order", "expect_count": 1,
                     "fields": [{"name": "size", "value": "large"}, {"name": "colour", "value": "red"}],
                     "success_phrases": ["placed"], "failure_phrases": ["couldn't"]},
         "must_call": [], "must_not_call": [], "questions": [], "audio": {"noise": "none", "interrupt_line": ""}}
    d.update(over)
    return d


DRAFTS = [
    draft(),
    draft(category="error", kind="tool_error", title="Order system answers with an error",
          why="Honesty under failure.", rule_ids=["R3"], persona="impatient-caller",
          goal="Order a small cheese pizza and confirm it.", fault={"tool": "place_order", "type": "http_error", "status": 0}),
    draft(category="edge", kind="ambiguous", title="Caller only says they are hungry", rule_ids=[], intent="none",
          criticality="critical", persona="confused-caller", goal="Say you're hungry and see what the agent suggests.",
          outcome={"expected": "none", "tool": "none", "expect_count": 0, "fields": [], "success_phrases": [],
                   "failure_phrases": []}),
    draft(category="audio", kind="interruption", title="Caller cuts in to change the size", rule_ids=["R1"],
          persona="interrupter", audio={"noise": "street", "interrupt_line": "Actually make it a medium."},
          questions=[{"title": "Handles the change", "question": "Did the agent take the new size?",
                      "pass_when": "It reads back a medium.", "fail_when": "It keeps the large."}]),
    draft(title="Orders a large vegetarian pizza please", why="Same as the first."),   # a duplicate
    draft(category="edge", kind="out_of_scope", title="Asks for garlic bread", rule_ids=["R2"],
          persona="clear-caller", goal="Ask for garlic bread.", must_call=["delivery_estimate"],
          must_not_call=["place_order"],
          outcome={"expected": "no_action", "tool": "place_order", "expect_count": 0, "fields": [],
                   "success_phrases": [], "failure_phrases": []}),
]


def scripted(drafts=DRAFTS):
    def reply(req):
        assert req.json_schema is not None
        if req.purpose == "plan_understand":
            return json.dumps(ANALYSIS)
        assert "Write these many tests" in req.messages[0].content
        return json.dumps({"tests": drafts})
    return ScriptedLLM(reply)


@pytest.fixture
def food(tmp_path):
    folder = tmp_path / "pipecat-food"
    shutil.copytree(ROOT / "examples" / "pipecat-food", folder, ignore=shutil.ignore_patterns("runs"))
    return folder


def plan_for(folder, drafts=DRAFTS, count=6):
    # scenario fixtures are repo-relative: load from the copy, but the checks only look at the files
    return asyncio.run(planner.write_plan(load_project(folder), scripted(drafts), count=count))


def test_library_personas_are_valid():
    assert len(personas.LIBRARY) >= 12
    for pid in personas.LIBRARY:
        doc = personas.persona_doc(pid)
        assert schema_errors("persona", doc) == [], pid
        assert "_title" not in doc


def test_split_counts_follows_the_mix():
    assert planner.split_counts(20) == {"happy": 8, "edge": 6, "error": 3, "adversarial": 2, "audio": 1}
    assert sum(planner.split_counts(7).values()) == 7


def test_context_has_the_prompt_tools_and_columns(food):
    text = planner.agent_context(load_project(food))
    assert "ALWAYS use the available functions" in text
    assert "place_order (write)" in text
    assert "food, size, pizza_type" in text
    assert "pipecat-food.order-pizza" in text            # existing tests, so they aren't written again


def test_plan_checks_each_draft(food):
    plan = plan_for(food)
    tests = {t["title"]: t for t in plan["tests"]}
    assert plan["asked"]["mix"] == {"happy": 2, "edge": 2, "error": 1, "adversarial": 1, "audio": 0} and plan["analysis"]["rules"][0]["id"] == "R1"

    happy = tests["Orders a large vegetarian pizza"]
    sc = happy["scenario"]
    assert happy["problems"] == [] and happy["selected"] and happy["criticality"] == "critical"
    assert sc["setup"]["fixtures"] == ["targets/pipecat-food/fixtures/base.sql"]      # taken from the project's tests
    assert sc["outcome"]["record"] == {"probe": "orders_for_test", "expect_count": 1, "expect_fields": {"size": "large"}}
    assert any("colour" in n for n in happy["notes"])                                    # unknown column dropped
    assert sc["outcome"]["claim"]["success_patterns"][0].startswith("\\b(placed")      # the project's own claim words
    assert happy["rules"][0]["kind"] == "must"

    err = tests["Order system answers with an error"]["scenario"]
    assert err["setup"]["faults"] == [{"tool": "place_order", "type": "http_error", "status": 503}]
    assert err["outcome"]["expected"] == "honest_failure" and err["outcome"]["record"]["expect_count"] == 0

    vague = tests["Caller only says they are hungry"]
    assert vague["criticality"] == "exploratory" and "outcome" not in vague["scenario"]
    assert any("exploratory" in n for n in vague["notes"])

    cut = tests["Caller cuts in to change the size"]["scenario"]
    assert cut["modes"] == ["voice"] and cut["audio"]["noise"] == "street"
    assert cut["audio"]["barge_in"][0]["say"] == "Actually make it a medium."
    assert cut["checks"][0]["type"] == "judge" and cut["checks"][0]["pass_when"]

    dup = tests["Orders a large vegetarian pizza please"]
    assert dup["duplicate_of"] and not dup["selected"]

    bread = tests["Asks for garlic bread"]["scenario"]
    assert bread["criticality"] == "normal"                                              # R2 is only a fact
    types = [c["type"] for c in bread["checks"]]
    assert types == ["tool_called"]          # place_order is the outcome's own tool, so no extra check
    assert bread["outcome"]["expected"] == "no_action"

    for t in plan["tests"]:
        assert t["problems"] == [], (t["title"], t["problems"])
        assert t["id"].startswith("gen.") and t["file"].startswith("scenarios/gen-")
    assert len({t["id"] for t in plan["tests"]}) == len(plan["tests"])


def test_accept_writes_files_and_personas(food):
    plan = plan_for(food)
    planner.save_plan(food, plan)
    picked = [t["id"] for t in plan["tests"] if t["selected"]]
    out = planner.accept(food, planner.latest_plan(food), picked, critical={picked[1]: "normal"})
    assert sorted(out["added"]) == sorted(picked) and not out["skipped"]
    assert "impatient-caller" in out["personas"] and (food / "personas" / "impatient-caller.yaml").is_file()
    project = load_project(food)                       # still a valid project
    ids = {s["id"] for s in project.scenarios}
    assert set(picked) <= ids
    text = (food / plan["tests"][0]["file"]).read_text(encoding="utf-8")
    assert text.startswith("# Written by Vocto's test plan writer") and "Rule R1:" in text
    second = yaml.safe_load((food / next(t["file"] for t in plan["tests"] if t["id"] == picked[1])).read_text())
    assert second["criticality"] == "normal"
    assert all(t.get("accepted") for t in planner.latest_plan(food)["tests"] if t["id"] in picked)
    again = planner.accept(food, planner.latest_plan(food), picked[:1])               # already there: skipped
    assert again["added"] == [] and "already exists" in again["skipped"][0]["reason"]


def test_accept_rolls_back_when_the_project_breaks(food):
    plan = plan_for(food)
    bad = plan["tests"][0]
    bad["scenario"]["caller"]["persona"] = "nobody"      # changed after the check
    with pytest.raises(Exception):
        planner.accept(food, plan, [bad["id"]])
    assert not (food / bad["file"]).exists()


def test_broken_drafts_are_shown_not_saved(food):
    broken = draft(title="Bad tool", must_call=["delivery_estimate"], kind="happy_path")
    broken["outcome"]["fields"] = []
    plan = plan_for(food, [broken], count=1)
    t = plan["tests"][0]
    assert t["problems"] == [] and t["selected"]
    # a draft the schema rejects (max_turns forced out of range is clamped, so break the id instead)
    t["scenario"]["criticality"] = "urgent"
    assert schema_errors("scenario", t["scenario"])


def test_needs_something_to_read(food):
    project = load_project(food)
    for key in ("agent_prompt", "description"):
        project.profile.pop(key, None)
    project.profile["judges"].pop("policy")
    with pytest.raises(planner.PlanError):
        asyncio.run(planner.write_plan(project, scripted(), count=3))


def test_bad_model_answer_is_a_plan_error(food):
    llm = ScriptedLLM(lambda req: json.dumps(ANALYSIS) if req.purpose == "plan_understand" else '{"tests": [{"kind": 1}]}')
    with pytest.raises(planner.PlanError, match="wrong shape"):
        asyncio.run(planner.write_plan(load_project(food), llm, count=2))


def test_add_personas(food):
    assert planner.add_personas(food, ["angry-caller", "angry-caller"]) == ["angry-caller"]
    assert "angry-caller" in load_project(food).personas
    with pytest.raises(ValueError):
        planner.add_personas(food, ["nobody"])


# ---- the dashboard API (web/plans.py) ----------------------------------------------------------

@pytest.fixture
def web_root(tmp_path, monkeypatch):
    shutil.copytree(ROOT / "examples" / "pipecat-food", tmp_path / "examples" / "pipecat-food",
                    ignore=shutil.ignore_patterns("runs"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-0000000000")
    from vatf.web import plans
    monkeypatch.setattr(plans, "ClaudeClient", lambda *a, **k: scripted())
    return tmp_path


def test_dashboard_writes_and_adds_a_plan(web_root):
    from vatf.web import data, plans
    key = data.project_by_path(web_root, "examples/pipecat-food")["key"]
    page = plans.plan_page(web_root, key)
    assert page["plan"] is None and "order-taking assistant" in page["agent_prompt"] and page["api_key"]
    assert len(page["library"]) >= 12 and not any(p["added"] for p in page["library"] if p["id"] == "angry-caller")
    page = plans.generate(web_root, key, {"count": 6, "focus": "honesty"})
    assert len(page["plan"]["tests"]) == len(DRAFTS) and page["plan"]["asked"]["focus"] == "honesty"
    pick = [t["id"] for t in page["plan"]["tests"] if t["selected"]][:2]
    out = plans.add_tests(web_root, key, {"ids": pick})
    assert out["added"] == pick
    assert [t["accepted"] for t in out["page"]["plan"]["tests"]][:2] == [True, True]
    assert plans.add_personas(web_root, key, {"ids": ["angry-caller"]}) == {"added": ["angry-caller"]}
    assert next(p for p in plans.plan_page(web_root, key)["library"] if p["id"] == "angry-caller")["added"]


def test_dashboard_needs_a_key(web_root, monkeypatch):
    from vatf.web import data, plans
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    key = data.project_by_path(web_root, "examples/pipecat-food")["key"]
    with pytest.raises(ValueError, match="API key"):
        plans.generate(web_root, key, {})
