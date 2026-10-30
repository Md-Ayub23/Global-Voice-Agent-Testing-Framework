"""Phase 1 checks: the text-mode pipeline end to end, offline (rules brain, no API calls)."""

import asyncio
import json
from pathlib import Path

import pytest

from vatf.cli import EXIT_CHECK
from vatf.contracts.models import OutcomeClass, Result, TraceEvent
from vatf.decision import LocalDecisionEngine
from vatf.evidence import LocalEvidenceStore
from vatf.llm import Budget, ClaudeClient, ScriptedLLM
from vatf.loader import load_project, schema_errors
from vatf.orchestrator import Orchestrator, RunConfig
from vatf.outcome import evaluate_outcome
from vatf.util import mask_token

ROOT = Path(__file__).resolve().parent.parent
CLINIC = ROOT / "examples" / "clinic"
TIMEOUT = "clinic.book-checkup.tool-timeout"
HAPPY = "clinic.book-checkup"
SCRIPTED = "clinic.confirm-before-booking"


def project(**agent_options):
    return load_project(CLINIC).with_agent_options({"brain": "rules", "bugs": "", **agent_options})


def run(tmp_path, proj=None, llm=None, offline=True, **config):
    config.setdefault("judges", False)  # the AI judges have their own tests (test_judges.py)
    cfg = RunConfig(root=ROOT, runs_dir=tmp_path / "runs", offline=offline, **config)
    orch = Orchestrator(proj or project(), cfg, llm=llm, log=lambda *_: None)
    return orch, asyncio.run(orch.run())


def verdicts_of(orch, decision):
    store = orch.store
    return [json.loads((store.call_dir(decision["run_id"], c["test_id"]) / "verdict.json").read_text("utf-8"))
            for c in decision["calls"]]


# ---- exit criterion -------------------------------------------------------------------------

def test_clean_agent_goes_with_no_false_alarms(tmp_path):
    _, decision = run(tmp_path)
    assert decision["decision"] == "GO", decision["reasons"]
    assert decision["exit_code"] == 0
    assert all(c["status"] == "PASS" for c in decision["calls"])
    assert decision["coverage"]["ratio"] == 1.0


@pytest.mark.parametrize("bug", sorted(EXIT_CHECK))
def test_each_bug_is_caught_in_every_repeat(tmp_path, bug):
    orch, decision = run(tmp_path, project(bugs=bug))
    assert decision["decision"] == "NO_GO"
    rel = {r["scenario_id"]: r for r in decision["reliability"]}[EXIT_CHECK[bug]]
    assert rel["failed"] == rel["k"] == 3


def test_lie_on_failure_is_classed_false_success_with_evidence(tmp_path):
    orch, decision = run(tmp_path, project(bugs="lie_on_failure"), scenario_ids=(TIMEOUT,), repeats=1)
    v = verdicts_of(orch, decision)[0]
    assert v["outcome"]["class"] == "FALSE_SUCCESS"
    assert v["outcome"]["said"] == "success" and v["outcome"]["stored"] == "missing"
    assert len(v["outcome"]["evidence"]) == 3  # failed write, the claim, the empty backend


def test_double_booking_is_classed_duplicate_action(tmp_path):
    orch, decision = run(tmp_path, project(bugs="double_booking"), scenario_ids=(HAPPY,), repeats=1)
    assert verdicts_of(orch, decision)[0]["outcome"]["class"] == "DUPLICATE_ACTION"


# ---- evidence and file formats ---------------------------------------------------------------

def test_every_written_file_matches_its_schema(tmp_path):
    orch, decision = run(tmp_path, project(bugs="lie_on_failure"), repeats=1)
    assert not schema_errors("run_decision", {k: v for k, v in decision.items() if not k.startswith("_")})
    for c in decision["calls"]:
        folder = orch.store.call_dir(decision["run_id"], c["test_id"])
        assert not schema_errors("call_record", json.loads((folder / "call.json").read_text("utf-8")))
        events = [json.loads(line) for line in (folder / "events.jsonl").read_text("utf-8").splitlines()]
        assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
        for e in events:
            assert not schema_errors("trace_event", e), e
        verdict = json.loads((folder / "verdict.json").read_text("utf-8"))
        assert not schema_errors("call_verdict", verdict)
        seqs = {e["seq"] for e in events}
        cited = {r["event"] for ch in verdict["checks"] for r in ch["evidence"]}
        assert cited <= seqs
    assert (tmp_path / "runs" / decision["run_id"] / "report.html").exists()


def test_pii_is_masked_and_the_lock_detects_tampering(tmp_path):
    orch, decision = run(tmp_path, scenario_ids=(HAPPY,), repeats=1)
    test_id = decision["calls"][0]["test_id"]
    folder = orch.store.call_dir(decision["run_id"], test_id)
    text = (folder / "events.jsonl").read_text("utf-8")
    assert "1985-03-14" not in text
    assert mask_token("Maria Lopez", test_id) in text  # masked consistently, so checks still compare
    assert orch.store.verify_lock(decision["run_id"], test_id)
    (folder / "events.jsonl").write_text(text.replace("Dr Patel", "Dr Chen"), encoding="utf-8")
    assert not orch.store.verify_lock(decision["run_id"], test_id)


# ---- harness errors ----------------------------------------------------------------------------

def test_agent_that_cannot_start_is_a_harness_error_not_an_agent_failure(tmp_path):
    proj = project()
    proj.profile["connection"]["target"] = "zoo.clinic.agent:no_such_function"
    orch, decision = run(tmp_path, proj, scenario_ids=(SCRIPTED,), repeats=1)
    assert decision["decision"] == "NO_GO_INSUFFICIENT_EVIDENCE"
    assert decision["calls"][0]["status"] == "ERROR"
    runs = list((tmp_path / "runs" / decision["run_id"] / "calls").iterdir())
    assert len(runs) == 3  # first try + max_harness_retries (2)


def test_agent_crash_mid_call_is_a_blocking_failure(tmp_path, monkeypatch):
    from zoo.clinic import brain_rules

    def boom(self, text):
        raise RuntimeError("agent bug")

    monkeypatch.setattr(brain_rules.RulesBrain, "reply", boom)
    orch, decision = run(tmp_path, scenario_ids=(SCRIPTED,), repeats=1)
    v = verdicts_of(orch, decision)[0]
    assert v["status"] == "FAIL"
    assert any(c["check_id"] == "agent-did-not-crash" and c["result"] == "FAIL" for c in v["checks"])


# ---- simulated caller and validator (fake LLM) --------------------------------------------------

def scripted_caller_llm(validator_answers):
    lines = iter(["Hi, I'd like a check-up with Dr Patel next Tuesday morning.",
                  "Maria Lopez, born 14 March 1985.",
                  "Nine thirty works.",
                  "Yes please.",
                  "No, that's all. Bye. [HANG UP]"])
    answers = iter(validator_answers)

    def reply(request):
        if request.purpose == "validator":
            return next(answers)
        return next(lines)
    return reply


def test_simulated_call_with_llm_caller_and_validator(tmp_path):
    llm = ScriptedLLM(scripted_caller_llm(['{"on_script": true, "problems": []}']))
    orch, decision = run(tmp_path, llm=llm, offline=False, scenario_ids=(HAPPY,), repeats=1)
    v = verdicts_of(orch, decision)[0]
    assert v["status"] == "PASS", v["checks"]
    assert v["outcome"]["class"] == "TRUE_SUCCESS"
    assert any(c["check_id"] == "simulator-on-script" and c["result"] == "PASS" for c in v["checks"])
    caller_requests = [r for r in llm.requests if r.purpose == "caller"]
    assert caller_requests[0].messages[0].content == "Riverside Clinic, how can I help?"
    assert "Maria Lopez" in caller_requests[0].system  # the fact sheet reaches the caller
    folder = orch.store.call_dir(decision["run_id"], v["test_id"])
    assert len(list((folder / "llm").glob("*.json"))) == 6  # 5 caller turns + 1 validation, all logged


def test_off_script_caller_is_an_error_and_the_call_is_rerun(tmp_path):
    def reply(request):
        if request.purpose == "validator":
            reply.validations += 1
            if reply.validations == 1:
                return '{"on_script": false, "problems": ["gave a different date of birth"]}'
            return '{"on_script": true, "problems": []}'
        return reply.lines.pop(0)
    reply.validations = 0
    base = ["Hi, I'd like a check-up with Dr Patel next Tuesday morning.", "Maria Lopez, born 14 March 1985.",
            "Nine thirty works.", "Yes please.", "No, that's all. Bye. [HANG UP]"]
    reply.lines = base + base
    orch, decision = run(tmp_path, llm=ScriptedLLM(reply), offline=False, scenario_ids=(HAPPY,), repeats=1)
    assert decision["calls"][0]["status"] == "PASS"
    assert len(list((tmp_path / "runs" / decision["run_id"] / "calls").iterdir())) == 2


def test_claude_requests_send_no_sampling_settings(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    from vatf.contracts.models import LLMMessage, LLMRequest

    client = ClaudeClient(Budget(1.0))
    kwargs = client.request_kwargs(LLMRequest("caller", "claude-haiku-5-5", "sys", (LLMMessage("user", "hi"),), 100))
    assert "temperature" not in kwargs
    assert kwargs["model"] == "claude-haiku-5-5"


# ---- outcome classes and decision rules (unit level) --------------------------------------------

def ev(seq, type_, data, turn=None):
    return TraceEvent(seq, seq * 10, "tool_proxy", type_, data, turn)


SCENARIO = {"outcome": {
    "expected": "success", "action": {"tool": "book_appointment"},
    "record": {"probe": "p", "expect_count": 1},
    "claim": {"success_patterns": [r"\bbooked\b"], "failure_patterns": [r"\bcouldn't\b"]}}}
PROFILE = {"tools": [{"name": "book_appointment", "effect": "write", "idempotency_key": ["slot"]}],
           "backend": {"access": "sql"}}


def outcome_class(ok, said, rows, expected="success"):
    events = [ev(1, "tool_request", {"call_id": "c1", "tool": "book_appointment", "args": {"slot": "9"}}),
              ev(2, "tool_response", {"call_id": "c1", "tool": "book_appointment", "ok": ok, "status": 201,
                                      "duration_ms": 1, "reached_backend": ok}),
              ev(3, "agent_turn", {"heard_text": said}),
              ev(4, "probe_result", {"probe": "p", "ok": True, "rows": rows})]
    sc = {"outcome": {**SCENARIO["outcome"], "expected": expected}}
    block, check = evaluate_outcome(sc, PROFILE, events, {}, "salt")
    return block["class"], check.result


@pytest.mark.parametrize("ok,said,rows,expected,cls,result", [
    (True, "You're booked.", [{"id": 1}], "success", OutcomeClass.TRUE_SUCCESS, Result.PASS),
    (False, "You're booked.", [], "success", OutcomeClass.FALSE_SUCCESS, Result.FAIL),
    (True, "You're booked.", [], "success", OutcomeClass.SILENT_DATA_BUG, Result.FAIL),
    (False, "Sorry, I couldn't book it.", [], "honest_failure", OutcomeClass.HONEST_FAILURE, Result.PASS),
    (True, "Sorry, I couldn't book it.", [{"id": 1}], "success", OutcomeClass.FALSE_FAILURE, Result.FAIL),
    (True, "Hmm, let me see.", [{"id": 1}], "success", OutcomeClass.NOT_VERIFIED, Result.NOT_VERIFIED),
    (True, "booked, or maybe I couldn't", [{"id": 1}], "success", OutcomeClass.NOT_VERIFIED, Result.NOT_VERIFIED),
])
def test_outcome_classes(ok, said, rows, expected, cls, result):
    assert outcome_class(ok, said, rows, expected) == (cls.value, result)


def test_honest_failure_without_any_attempt_is_not_a_pass():
    # Seen in a real run: the agent never reached booking, so the injected fault never
    # fired, yet "sorry, I couldn't" matched the failure words and the call passed.
    events = [ev(1, "agent_turn", {"heard_text": "Sorry, I couldn't find a free slot."}),
              ev(2, "probe_result", {"probe": "p", "ok": True, "rows": []})]
    sc = {"outcome": {**SCENARIO["outcome"], "expected": "honest_failure"}}
    block, check = evaluate_outcome(sc, PROFILE, events, {}, "salt")
    assert (block["class"], check.result) == (OutcomeClass.NOT_VERIFIED.value, Result.NOT_VERIFIED)
    assert "never attempted" in check.message


def verdict(test_id, scenario, status, blocker_fail=False):
    checks = []
    if blocker_fail:
        checks.append({"check_id": "outcome", "origin": "outcome", "result": "FAIL", "severity": "blocker",
                       "section": "accuracy", "message": "bad", "evidence": [{"event": 1}],
                       "evidence_level": "backend", "can_block": True})
    return {"test_id": test_id, "scenario_id": scenario, "repeat": 1, "status": status, "checks": checks}


SCENARIOS = [{"id": "a", "criticality": "critical"}, {"id": "b", "criticality": "normal"}]


@pytest.mark.parametrize("statuses,blocker,decision", [
    (["PASS", "PASS"], False, "GO"),
    (["FAIL", "PASS"], True, "NO_GO"),
    (["FAIL", "PASS"], False, "NO_GO"),             # critical scenario failed its repeat
    (["PASS", "FAIL"], False, "GO"),                # normal scenario failures only feed the scores
    (["ERROR", "PASS"], False, "NO_GO_INSUFFICIENT_EVIDENCE"),
    (["NOT_VERIFIED", "PASS"], False, "NO_GO_INSUFFICIENT_EVIDENCE"),
])
def test_decision_rules(statuses, blocker, decision):
    vs = [verdict("t-1", "a", statuses[0], blocker), verdict("t-2", "b", statuses[1])]
    profile = {"id": "p", "thresholds": {"repeats": 1, "min_coverage": 0.9}}
    result = LocalDecisionEngine().decide("r", vs, SCENARIOS, profile)
    assert result["decision"] == decision
    assert not schema_errors("run_decision", result)


def test_evidence_store_refuses_writes_after_lock(tmp_path):
    from vatf.contracts.models import CallContext
    from vatf.evidence import EvidenceLocked

    store = LocalEvidenceStore(tmp_path)
    ctx = CallContext("t-x-1", "r-x", {"id": "p", "tools": []}, {"id": "s"}, 1)
    w = store.open_call(ctx)
    w.emit("connector", "call_started", {"test_id": "t-x-1", "mode": "text"})
    w.lock("completed")
    with pytest.raises(EvidenceLocked):
        w.emit("connector", "call_ended", {"ended_by": "caller"})


def test_clinic_llm_brain_runs_its_tool_loop(monkeypatch):
    """The Claude brain without network: a fake client asks for one tool, then answers."""
    from types import SimpleNamespace as NS

    import anthropic
    from zoo.clinic.brain_llm import LLMBrain

    replies = [
        NS(stop_reason="tool_use", content=[NS(type="tool_use", id="tu1", name="find_patient",
                                               input={"full_name": "Maria Lopez", "date_of_birth": "1985-03-14"})]),
        NS(stop_reason="end_turn", content=[NS(type="text", text="Thanks Maria, which doctor?")]),
    ]
    sent = []

    class FakeClient:
        def __init__(self, *a, **k):
            self.messages = NS(create=lambda **kw: (sent.append(kw), replies.pop(0))[1])

    class Tools:
        def call(self, name, args):
            return {"ok": True, "status": 200, "body": {"patient_id": "P-1042"}}

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    from datetime import datetime
    brain = LLMBrain(Tools(), datetime(2026, 11, 2, 9), confirm_before_booking=True, model="claude-haiku-5-5")
    assert brain.reply("I'm Maria Lopez, born 14 March 1985") == "Thanks Maria, which doctor?"
    history = sent[1]["messages"]  # user, assistant tool_use, tool_result, final answer
    assert history[2]["content"][0] == {"type": "tool_result", "tool_use_id": "tu1",
                                        "content": '{"patient_id": "P-1042"}', "is_error": False}
    assert "temperature" not in sent[0] and sent[0]["model"] == "claude-haiku-5-5"
    assert "Shall I book it?" in sent[0]["system"]


def test_clinic_llm_brain_without_a_key_fails_at_start(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    orch, decision = run(tmp_path, project(brain="llm"), scenario_ids=(SCRIPTED,), repeats=1)
    assert decision["calls"][0]["status"] == "ERROR"  # a setup problem, never blamed on the agent


def test_dotenv_is_loaded_but_never_overrides_the_shell(tmp_path, monkeypatch):
    from vatf.cli import load_dotenv

    (tmp_path / ".env").write_text('# key\nVATF_TEST_A="from-file"\nexport VATF_TEST_B=file\n', encoding="utf-8")
    monkeypatch.delenv("VATF_TEST_A", raising=False)
    monkeypatch.setenv("VATF_TEST_B", "from-shell")
    assert load_dotenv(tmp_path / ".env") == ["VATF_TEST_A"]
    import os
    assert os.environ["VATF_TEST_A"] == "from-file" and os.environ["VATF_TEST_B"] == "from-shell"
    monkeypatch.delenv("VATF_TEST_A")


def test_dotenv_is_git_ignored():
    assert ".env" in (ROOT / ".gitignore").read_text(encoding="utf-8").split()


def test_offline_flag_switches_the_agent_to_its_offline_options():
    from types import SimpleNamespace as NS

    from vatf.cli import _project

    args = NS(project=str(CLINIC), offline=True, agent_opt=None)
    assert _project(args).profile["connection"]["options"]["brain"] == "rules"
    args = NS(project=str(CLINIC), offline=False, agent_opt=["brain=rules", "bugs=double_booking"])
    assert _project(args).profile["connection"]["options"]["bugs"] == "double_booking"


def test_empty_validator_answer_is_a_harness_error_that_gets_rerun(tmp_path):
    def reply(request):
        if request.purpose == "validator":
            return ""
        return reply.lines.pop(0)
    base = ["Hi, I'd like a check-up with Dr Patel tomorrow morning.", "Maria Lopez, born 14 March 1985.",
            "Nine thirty works.", "Yes please.", "No, that's all. Bye. [HANG UP]"]
    reply.lines = base * 3
    orch, decision = run(tmp_path, llm=ScriptedLLM(reply), offline=False, scenario_ids=(HAPPY,), repeats=1)
    assert decision["calls"][0]["status"] == "ERROR"
    assert decision["decision"] == "NO_GO_INSUFFICIENT_EVIDENCE"  # never blamed on the agent
    calls = sorted((tmp_path / "runs" / decision["run_id"] / "calls").iterdir())
    events = (calls[0] / "events.jsonl").read_text("utf-8")
    assert '"code": "llm_error"' in events


def test_failed_outcome_message_says_what_was_expected(tmp_path):
    orch, decision = run(tmp_path, project(bugs="lie_on_failure"), scenario_ids=(TIMEOUT,), repeats=1)
    msg = verdicts_of(orch, decision)[0]["checks"][0]["message"]
    assert msg.startswith("Expected HONEST_FAILURE, got FALSE_SUCCESS.")


def test_transcript_shows_conversation_tools_database_and_judgement(tmp_path):
    orch, decision = run(tmp_path, project(bugs="lie_on_failure"), scenario_ids=(TIMEOUT,), repeats=1)
    text = Path(decision["_transcript"]).read_text(encoding="utf-8")
    for expected in ["AGENT            Riverside Clinic", "CALLER", "AGENT uses tool  book_appointment(",
                     "vatf breaks tool book_appointment: injected timeout", "FAILED 504",
                     "database check   appointments_for_test: 0 row(s)", "Said     : success",
                     "Outcome  : FALSE_SUCCESS  -> FAIL", "DECISION: NO_GO"]:
        assert expected in text, expected
    call_dir = orch.store.call_dir(decision["run_id"], decision["calls"][0]["test_id"])
    assert (call_dir / "story.txt").exists()
    assert orch.store.verify_lock(decision["run_id"], decision["calls"][0]["test_id"])
    html = Path(decision["_report"]).read_text(encoding="utf-8")
    assert "Conversation and actions" in html and "How it was judged" in html


def test_validator_uses_its_final_answer_when_it_corrects_itself():
    # Seen in a real run: a first JSON verdict, a correction, then a second JSON verdict.
    from vatf.caller import _last_json_object

    text = ('{"on_script": false, "problems": ["gave the date of birth early"]}\n\n'
            'Correction: on reflection, the caller stayed within its brief.\n\n{"on_script": true, "problems": []}')
    assert _last_json_object(text) == {"on_script": True, "problems": []}
    assert _last_json_object("no json here") is None


def test_a_retry_is_judged_on_how_it_ended():
    # First attempt times out and the agent says so; the caller asks again and the retry works.
    events = [ev(1, "tool_request", {"call_id": "c1", "tool": "book_appointment", "args": {"slot": "9"}}),
              ev(2, "tool_response", {"call_id": "c1", "tool": "book_appointment", "ok": False, "status": 504,
                                      "duration_ms": 1, "reached_backend": False}),
              ev(3, "agent_turn", {"heard_text": "Sorry, I couldn't book it. Shall I try once more?"}),
              ev(4, "tool_request", {"call_id": "c2", "tool": "book_appointment", "args": {"slot": "9"}}),
              ev(5, "tool_response", {"call_id": "c2", "tool": "book_appointment", "ok": True, "status": 201,
                                      "duration_ms": 1, "reached_backend": True}),
              ev(6, "agent_turn", {"heard_text": "That worked, you're booked."}),
              ev(7, "probe_result", {"probe": "p", "ok": True, "rows": [{"id": 1}]})]
    block, check = evaluate_outcome(SCENARIO, PROFILE, events, {}, "salt")
    assert (block["class"], check.result) == (OutcomeClass.TRUE_SUCCESS.value, Result.PASS)


def test_doctor_title_does_not_matter():
    from vatf.util import mask_token, same_value

    assert same_value("Patel", "Dr Patel", "s") and same_value("Dr. Patel", "dr patel", "s")
    assert same_value(mask_token("Maria Lopez", "s"), "maria  lopez", "s")
    assert not same_value("Dr Chen", "Dr Patel", "s")


def test_names_and_spoken_birth_dates_are_masked_in_transcripts(tmp_path):
    orch, decision = run(tmp_path, scenario_ids=("clinic.confirm-before-booking",), repeats=1)
    folder = orch.store.call_dir(decision["run_id"], decision["calls"][0]["test_id"])
    text = (folder / "events.jsonl").read_text(encoding="utf-8")
    assert "Maria" not in text and "Lopez" not in text and "14 March 1985" not in text
    assert "[full_name]" in text and "[date_of_birth]" in text
    assert decision["calls"][0]["status"] == "PASS"


def test_show_pii_keeps_names_readable(tmp_path):
    orch, decision = run(tmp_path, scenario_ids=("clinic.confirm-before-booking",), repeats=1, mask_pii=False)
    folder = orch.store.call_dir(decision["run_id"], decision["calls"][0]["test_id"])
    text = (folder / "events.jsonl").read_text(encoding="utf-8")
    assert "Maria Lopez" in text and "[masked:" not in text
    assert decision["calls"][0]["status"] == "PASS"
    assert "NOT masked" in Path(decision["_report"]).read_text(encoding="utf-8")
