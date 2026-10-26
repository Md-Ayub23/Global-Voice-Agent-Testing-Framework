"""Phase 0 checks: the example files follow the schemas and point at each other correctly."""

import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent.parent
SCHEMAS = ROOT / "schemas"
CLINIC = ROOT / "examples" / "clinic"
RUN = CLINIC / "runs" / "r-20261102-0001"


def schema(name):
    data = yaml.safe_load((SCHEMAS / f"{name}.schema.yaml").read_text())
    Draft202012Validator.check_schema(data)
    return Draft202012Validator(data)


def load(path):
    text = path.read_text()
    return json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)


def assert_valid(validator, doc, where):
    errors = sorted(validator.iter_errors(doc), key=lambda e: list(e.path))
    assert not errors, f"{where}: " + "; ".join(f"{list(e.path)}: {e.message}" for e in errors)


@pytest.mark.parametrize("path", sorted(SCHEMAS.glob("*.schema.yaml")), ids=lambda p: p.name)
def test_schema_is_valid_json_schema(path):
    Draft202012Validator.check_schema(yaml.safe_load(path.read_text()))


def test_profile():
    assert_valid(schema("client_profile"), load(CLINIC / "profile.yaml"), "profile.yaml")


@pytest.mark.parametrize("path", sorted((CLINIC / "personas").glob("*.yaml")), ids=lambda p: p.name)
def test_persona(path):
    assert_valid(schema("persona"), load(path), path.name)


@pytest.mark.parametrize("path", sorted((CLINIC / "scenarios").glob("*.yaml")), ids=lambda p: p.name)
def test_scenario_and_references(path):
    sc = load(path)
    assert_valid(schema("scenario"), sc, path.name)

    profile = load(CLINIC / "profile.yaml")
    personas = {load(p)["id"] for p in (CLINIC / "personas").glob("*.yaml")}
    tools = {t["name"]: t for t in profile["tools"]}
    probes = {p["id"] for p in profile["backend"].get("probes", [])}
    facts = sc.get("caller", {}).get("facts", {})

    assert sc["profile"] == profile["id"]
    if "caller" in sc:
        assert sc["caller"]["persona"] in personas
    for fault in sc.get("setup", {}).get("faults", []):
        assert fault["tool"] in tools
    checks = list(sc.get("checks", [])) + [c for t in sc.get("turns", []) for c in t.get("expect", [])]
    for c in checks:
        for key in ("tool", "first", "then"):
            if key in c:
                assert c[key] in tools, f"unknown tool {c[key]}"
        if "probe" in c:
            assert c["probe"] in probes
        for name in c.get("entities", []):
            assert name in facts, f"entity {name} is not a caller fact"
    if "outcome" in sc:
        out = sc["outcome"]
        assert tools[out["action"]["tool"]]["effect"] == "write"
        if "record" in out:
            assert out["record"]["probe"] in probes
        refs = list(out["action"].get("args_match", {}).values()) + list(
            out.get("record", {}).get("expect_fields", {}).values()
        )
        for value in refs:
            if isinstance(value, str) and value.startswith("$"):
                assert value[1:] in facts, f"{value} is not a caller fact"


def test_example_run_trace():
    call_dir = next((RUN / "calls").iterdir())
    record = load(call_dir / "call.json")
    assert_valid(schema("call_record"), record, "call.json")

    validator = schema("trace_event")
    lines = (call_dir / record["events_file"]).read_text().splitlines()
    events = [json.loads(line) for line in lines]
    for e in events:
        assert_valid(validator, e, f"event {e.get('seq')}")
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert all(a["t_ms"] <= b["t_ms"] for a, b in zip(events, events[1:]))
    assert events[0]["type"] == "call_started"
    assert events[0]["data"]["test_id"] == record["test_id"] == call_dir.name
    assert record["event_count"] == len(events)

    verdict = load(call_dir / "verdict.json")
    assert_valid(schema("call_verdict"), verdict, "verdict.json")
    seqs = {e["seq"] for e in events}
    cited = [r["event"] for c in verdict["checks"] for r in c["evidence"]]
    cited += [r["event"] for r in verdict.get("outcome", {}).get("evidence", [])]
    assert set(cited) <= seqs, "verdict cites events that do not exist"


def test_example_decision():
    decision = load(RUN / "decision.json")
    assert_valid(schema("run_decision"), decision, "decision.json")
    assert decision["exit_code"] == {"GO": 0, "NO_GO": 1, "NO_GO_INSUFFICIENT_EVIDENCE": 2}[decision["decision"]]


def test_judge_results_are_ai_opinion():
    v = schema("call_verdict")
    bad = {
        "check_id": "x", "origin": "judge", "result": "FAIL", "severity": "major",
        "section": "experience", "message": "m", "evidence": [{"event": 1}],
        "evidence_level": "backend",
        "judge": {"rubric_item": "r", "applicable": True, "votes": []},
    }
    doc = {"schema_version": "0.1", "kind": "call_verdict", "test_id": "t-1", "run_id": "r",
           "scenario_id": "s", "repeat": 1, "status": "FAIL", "checks": [bad]}
    assert list(v.iter_errors(doc)), "a judge result must not claim backend evidence"


def test_pass_without_evidence_is_rejected():
    v = schema("call_verdict")
    check = {"check_id": "x", "origin": "hard_check", "result": "PASS", "severity": "major",
             "section": "accuracy", "message": "m", "evidence": [], "evidence_level": "none"}
    doc = {"schema_version": "0.1", "kind": "call_verdict", "test_id": "t-1", "run_id": "r",
           "scenario_id": "s", "repeat": 1, "status": "PASS", "checks": [check]}
    assert list(v.iter_errors(doc)), "PASS with no evidence must be invalid"


def test_contracts_import():
    import vatf.contracts.caller  # noqa: F401
    import vatf.contracts.connector  # noqa: F401
    import vatf.contracts.evaluation  # noqa: F401
    import vatf.contracts.evidence  # noqa: F401
    import vatf.contracts.llm  # noqa: F401
    import vatf.contracts.observers  # noqa: F401
    import vatf.contracts.orchestrator  # noqa: F401
    import vatf.contracts.speech  # noqa: F401


def _null_keys(node, path=""):
    if isinstance(node, dict):
        for key, value in node.items():
            if value is None:
                yield f"{path}/{key}"
            yield from _null_keys(value, f"{path}/{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _null_keys(value, f"{path}[{i}]")


@pytest.mark.parametrize("path", sorted(SCHEMAS.glob("*.schema.yaml")), ids=lambda p: p.name)
def test_schema_has_no_stray_keys(path):
    # An unquoted comma inside a {flow: mapping} silently creates a key with no value.
    assert not list(_null_keys(yaml.safe_load(path.read_text())))
