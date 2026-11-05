"""Phase 8: monitoring real production calls. Reading every format, removing personal data before
anything is saved, the timeline and metrics, the real-call checks, judging on a sample within caps,
trends, alert rules (once, and back to normal), retention, turning a call into a test, labels, the
webhook listener, the CLI and the dashboard API."""

import base64
import http.client
import json
import re
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from vatf import alerts
from vatf.cli import main
from vatf.llm import ScriptedLLM
from vatf.loader import load_project, schema_errors
from vatf.monitor import intake, score, trends, watch
from vatf.monitor.intake import IntakeError, parse
from vatf.monitor.listener import Listener
from vatf.monitor.redact import Redactor, caller_hash
from vatf.monitor.service import Monitor
from vatf.monitor.settings import DEFAULTS, settings
from vatf.monitor.store import MonitorStore, agent_slug, to_events

ROOT = Path(__file__).resolve().parent.parent
T0 = 1793610000000          # epoch ms, 2026-11-02 around 09:00 UTC


def recent_start() -> str:
    """09:00 UTC within the last 24 hours: alerts only look at the last window_hours (24 by default)."""
    now = datetime.now(timezone.utc)
    return (now - timedelta(days=1 if now.hour < 10 else 0)).strftime("%Y-%m-%dT09:00:00.000Z")


def vapi_call(call_id="call-abc-123", reply="You're all set, Maria, you're booked for 9 am tomorrow.",
              tool_result="Error: slot service timed out", started="2026-11-02T09:00:00.000Z", version="v12"):
    return {"message": {
        "type": "end-of-call-report", "endedReason": "customer-ended-call", "startedAt": started,
        "endedAt": started.replace("09:00:00", "09:01:10"),
        "call": {"id": call_id, "customer": {"number": "+14155550123"}, "metadata": {"agent_version": version}},
        "artifact": {"recordingUrl": "https://storage.example/rec.wav", "messages": [
            {"role": "system", "message": "You are a clinic receptionist", "time": T0, "secondsFromStart": 0},
            {"role": "bot", "message": "Hello, Riverside Clinic, how can I help?", "time": T0 + 500,
             "endTime": T0 + 2500, "secondsFromStart": 0.5},
            {"role": "user", "message": "Hi, my name is Maria Lopez, born 14 March 1985, email maria.lopez@gmail.com. "
                                        "I want a checkup tomorrow at 9.", "time": T0 + 3000, "endTime": T0 + 7000,
             "secondsFromStart": 3.0},
            {"role": "tool_calls", "toolCalls": [{"id": "tc1", "type": "function", "function": {
                "name": "book_appointment",
                "arguments": json.dumps({"full_name": "Maria Lopez", "date_of_birth": "1985-03-14",
                                         "slot": "2026-11-03T09:00"})}}], "secondsFromStart": 7.6},
            {"role": "tool_call_result", "toolCallId": "tc1", "name": "book_appointment", "result": tool_result,
             "secondsFromStart": 9.0},
            {"role": "bot", "message": reply, "time": T0 + 9800, "endTime": T0 + 13000, "secondsFromStart": 9.8},
            {"role": "user", "message": "Great, thanks, bye.", "time": T0 + 14000, "endTime": T0 + 15000,
             "secondsFromStart": 14.0},
        ]}}}


def retell_call():
    return {"event": "call_analyzed", "call": {
        "call_id": "retell-777", "agent_id": "ag1", "agent_version": 4, "direction": "inbound",
        "from_number": "+919876543210", "to_number": "+14155550000",
        "start_timestamp": T0, "end_timestamp": T0 + 30000, "disconnection_reason": "call_transfer",
        "recording_url": "https://retell.example/r.wav",
        "transcript_with_tool_calls": [
            {"role": "agent", "content": "Hi, this is Ava at Riverside.", "words": [{"word": "Hi", "start": 0.4, "end": 0.6},
                                                                                     {"word": "Riverside.", "start": 1.5, "end": 2.0}]},
            {"role": "user", "content": "I need to talk to someone about my bill, my number is 98765 43210.",
             "words": [{"word": "I", "start": 3.0, "end": 3.1}, {"word": "43210.", "start": 6.0, "end": 6.5}]},
            {"role": "tool_call_invocation", "tool_call_id": "t9", "name": "transfer_call", "arguments": "{\"to\": \"front desk\"}"},
            {"role": "tool_call_result", "tool_call_id": "t9", "content": "{\"ok\": true}"},
            {"role": "agent", "content": "Sure, putting you through now.", "words": [{"word": "Sure", "start": 7.2, "end": 7.4},
                                                                                     {"word": "now.", "start": 8.6, "end": 9.0}]},
        ]}}


def vocto_call(i=0, started=None, status_reply="Your appointment is booked.", ok=True, version=None, slow=False):
    gap = 4000 if slow else 600
    started = started or "2026-11-02T08:00:00Z"
    return {"id": f"v-{i}", "started_at": started, "agent_version": version,
            "turns": [{"role": "agent", "text": "Riverside Clinic, hello.", "start_ms": 300, "end_ms": 1500},
                      {"role": "caller", "text": "Book me a checkup please.", "start_ms": 2000, "end_ms": 4000},
                      {"role": "agent", "text": status_reply, "start_ms": 4000 + gap, "end_ms": 6000 + gap,
                       "tools": [{"name": "book_appointment", "args": {"slot": "x"}, "result": {"ok": ok},
                                  "at_ms": 4100}]},
                      {"role": "caller", "text": "Thanks, bye.", "start_ms": 7000 + gap, "end_ms": 7800 + gap}]}


@pytest.fixture
def clinic(tmp_path):
    root = tmp_path / "root"
    shutil.copytree(ROOT / "examples" / "clinic", root / "examples" / "clinic",
                    ignore=shutil.ignore_patterns("runs", "baselines", "*.db", "__pycache__"))
    return root, root / "examples" / "clinic"


def mon(clinic, **monitor):
    root, folder = clinic
    if monitor:
        text = (folder / "profile.yaml").read_text(encoding="utf-8")
        (folder / "profile.yaml").write_text(text + "\nmonitor: " + json.dumps(monitor) + "\n", encoding="utf-8")
    return Monitor(root, root / "runs", folder)


# ---- intake.py ----------------------------------------------------------------------------------

def test_reads_vapi_retell_vocto_and_transcripts():
    v = parse(json.dumps(vapi_call()))[0]
    assert v["source"] == "vapi" and v["external_id"] == "call-abc-123" and v["agent_version"] == "v12" and v["timed"]
    kinds = [(i["kind"], i.get("role") or i.get("name")) for i in v["items"]]
    assert kinds == [("turn", "agent"), ("turn", "caller"), ("tool", "book_appointment"), ("turn", "agent"), ("turn", "caller")]
    tool = v["items"][2]
    assert tool["ok"] is False and tool["args"]["slot"] == "2026-11-03T09:00" and tool["at_ms"] == 7600
    assert v["items"][0]["start_ms"] == 500 and v["items"][0]["end_ms"] == 2500 and v["duration_ms"] == 70000

    r = parse(json.dumps(retell_call()))[0]
    assert r["source"] == "retell" and r["agent_version"] == "4" and r["caller_number"] == "+919876543210"
    assert r["transferred"] and r["timed"] and r["items"][1]["start_ms"] == 3000 and r["items"][1]["end_ms"] == 6500
    assert [i["kind"] for i in r["items"]] == ["turn", "turn", "tool", "turn"] and r["items"][2]["ok"] is True

    c = parse(json.dumps(vocto_call(ok=False)))[0]
    assert c["source"] == "vocto" and c["items"][2]["kind"] == "tool" and c["items"][2]["ok"] is False

    t = parse("[00:01] Agent: Hello, clinic.\n[00:04] Caller: Hi there\nI need a checkup.\nAgent: Sure.")[0]
    assert t["source"] == "transcript" and not t["timed"] and len(t["items"]) == 3
    assert t["items"][1]["text"] == "Hi there I need a checkup." and t["items"][0]["start_ms"] == 1000

    many = parse("\n".join(json.dumps(vocto_call(i)) for i in range(3)))          # JSON lines
    assert [m["external_id"] for m in many] == ["v-0", "v-1", "v-2"]
    assert len(parse(json.dumps([vocto_call(1), vocto_call(2)]))) == 2           # a JSON list
    assert parse(json.dumps({"message": {"type": "status-update", "status": "in-progress"}})) == []   # Vapi chatter
    assert parse(json.dumps({"event": "call_started", "call": {"call_id": "x"}})) == []


def test_unreadable_input_says_why():
    with pytest.raises(IntakeError, match="speaker labels"):
        parse("just some words without speakers")
    with pytest.raises(IntakeError, match="no turns"):
        parse(json.dumps({"turns": []}))
    with pytest.raises(IntakeError, match="role must be agent or caller"):
        parse(json.dumps({"turns": [{"role": "robot", "text": "x"}]}))
    with pytest.raises(IntakeError, match="not a call"):
        parse(json.dumps({"hello": 1}))
    with pytest.raises(IntakeError, match="empty"):
        parse("   ")


# ---- redact.py ----------------------------------------------------------------------------------

def test_redaction_removes_each_kind_and_keeps_the_rest():
    r = Redactor({}, DEFAULTS["privacy"], "salt")
    texts = ["Hi, my name is Maria Lopez and my email is maria.lopez@gmail.com, call +44 7700 900123.",
             "Card 4111 1111 1111 1111, born on 14th of March, 1985, at 221 Baker Street, NW1 6XE.",
             "Aadhaar 1234 5678 9012, PAN ABCDE1234F, SSN 123-45-6789, IBAN GB82 WEST 1234 5698 7654 32.",
             "Booking at 9:30 on 2026-11-03 for 2 people, price 1,250.00, order 12345."]
    r.learn(texts, [])
    out = [r.text(t) for t in texts]
    assert out[0] == "Hi, my name is [name] and my email is [email], call [phone]."
    assert out[1] == "Card [card], born on [birth_date], at [address], [address]."
    assert out[2] == "Aadhaar [id_number], PAN [id_number], SSN [id_number], IBAN [bank]."
    assert out[3] == texts[3]                      # times, dates, prices and short numbers stay
    assert r.text("Thanks Maria, and Lopez too.") == "Thanks [name], and [name] too."   # the name everywhere
    assert r.text("Card 4111 1111 1111 1112") == "Card 4111 1111 1111 1112"   # fails the Luhn check: not a card


def test_redaction_settings_profile_patterns_and_tool_values():
    profile = {"tools": [{"name": "lookup", "pii_args": ["patient_id"]}],
               "pii": {"mask_patterns": [{"name": "mrn", "regex": r"MRN-\d+"}]}}
    privacy = {**DEFAULTS["privacy"], "redact": ["email"], "patterns": [{"name": "member", "regex": r"MEM\d{4}"}]}
    r = Redactor(profile, privacy, "salt")
    r.learn(["My name is Dev Patel"], [{"patient_id": "P-0042", "name": "Dev Patel"}])
    assert r.text("My name is Dev Patel, MRN-5521, MEM1234, a@b.co, +44 7700 900123") == \
        "My name is [name], [mrn], [member], [email], +44 7700 900123"   # phone off: kept
    assert r.text("Patient P-0042 confirmed") == "Patient [patient_id] confirmed"
    args = r.value({"patient_id": "P-0042", "name": "Dev Patel", "slot": "9am"}, request=True)
    assert args["patient_id"].startswith("[masked:") and args["name"].startswith("[masked:") and args["slot"] == "9am"
    body = r.value({"services": [{"name": "Checkup"}], "note": "sent to a@b.co"})
    assert body == {"services": [{"name": "Checkup"}], "note": "sent to [email]"}   # `name` in an answer is kept
    assert caller_hash("+1 (415) 555-0123", "x") == caller_hash("4155550123", "x") and caller_hash("12", "x") is None


def test_nothing_personal_reaches_the_disk(clinic):
    m = mon(clinic)
    out = m.ingest(json.dumps(vapi_call()), "vapi.json")
    assert len(out["added"]) == 1
    blob = "".join(p.read_text(encoding="utf-8") for p in m.store.dir.rglob("*") if p.is_file())
    for secret in ("Maria", "Lopez", "maria.lopez", "4155550123", "+14155550123", "1985", "storage.example"):
        assert secret not in blob, secret
    record, events, verdict = m.store.read(out["added"][0])
    assert record["pii_redacted"] and record["recording_url"] is None and record["caller_hash"]
    assert record["redacted"]["name"] >= 2 and record["redacted"]["email"] == 1 and record["redacted"]["tool_value"] == 2
    keep = mon(clinic, privacy={"keep_recording_links": True})
    rid = keep.ingest(json.dumps(vapi_call("other-call")), "v.json")["added"][0]
    assert keep.store.read(rid)[0]["recording_url"] == "https://storage.example/rec.wav"


# ---- events, metrics and checks -------------------------------------------------------------------

def test_timeline_and_metrics_from_turn_times(clinic):
    m = mon(clinic)
    cid = m.ingest(json.dumps(vapi_call()), "v")["added"][0]
    record, events, v = m.store.read(cid)
    assert record["mode"] == "voice"
    assert schema_errors("trace_event", events[0]) == [] and all(schema_errors("trace_event", e) == [] for e in events)
    greet, answer = [e for e in events if e["type"] == "agent_turn"]
    assert greet["data"]["answered_at_ms"] == 0 and answer["data"]["latency_ms"] == 2800      # 9800 - 7000
    q = v["metrics"]["quality"]
    assert q["ttfw_ms"]["value"] == 500 and q["ttfw_ms"]["band"] == "good"
    assert q["response_p90_ms"]["value"] == 2800 and q["response_p90_ms"]["band"] == "bad"
    assert v["metrics"]["latency_ms"] == [2800] and q["duration_s"]["value"] == 70.0
    text = m.store.read(m.ingest(parse_text := "Agent: Hello.\nCaller: Hi.\nAgent: Bye.", "t.txt")["added"][0])
    assert text[0]["mode"] == "text" and "response_p90_ms" not in text[2]["metrics"]["quality"] and parse_text


def test_real_call_checks():
    def check(items, ended="customer-ended-call", transferred=False):
        call = {"source": "vocto", "timed": False, "items": items, "ended_reason": ended, "transferred": transferred}
        return {c.check_id: c for c in score.real_checks(to_events(call, "c-20261102-000000-aaaaaa"))}

    turn = lambda role, text: {"kind": "turn", "role": role, "text": text, "start_ms": None, "end_ms": None}  # noqa: E731
    tool = lambda ok: {"kind": "tool", "name": "book", "args": {}, "result": {"ok": ok}, "ok": ok}  # noqa: E731
    lie = check([turn("caller", "Book me in"), tool(False), turn("agent", "Done, you're booked for Monday."),
                 turn("caller", "Thanks, bye")])
    assert lie["real.false_success"].result == "NEEDS_REVIEW" and lie["real.tool_failed"].result == "FAIL"
    assert lie["real.tool_failed"].can_block is False and lie["real.dropped"].result == "PASS"
    honest = check([turn("caller", "Book me in"), tool(False), turn("agent", "Sorry, I couldn't book that, the system failed.")])
    assert honest["real.false_success"].result == "PASS"
    retried = check([turn("caller", "Book me in"), tool(False), tool(True), turn("agent", "You're booked.")])
    assert retried["real.false_success"].result == "PASS"
    clean = check([turn("caller", "Hi"), turn("agent", "Hello")])
    assert clean["real.tool_failed"].result == "NOT_APPLICABLE" and clean["real.false_success"].result == "NOT_APPLICABLE"
    dropped = check([turn("agent", "Hello"), turn("caller", "Hi")], ended="pipeline-error-openai-llm-failed")
    assert dropped["real.dropped"].result == "FAIL" and dropped["real.dropped"].severity == "major"
    hanging = check([turn("agent", "Hello"), turn("caller", "Can I move my appointment?")])
    assert hanging["real.dropped"].result == "FAIL" and hanging["real.dropped"].severity == "minor"
    assert check([turn("agent", "Hello"), turn("caller", "Can I move it?")], transferred=True)["real.dropped"].result == "PASS"


def test_verdict_status_flags_and_duplicates(clinic):
    m = mon(clinic)
    out = m.ingest(json.dumps(vapi_call()), "v")
    v = m.store.read(out["added"][0])[2]
    # tool failure is flagged but not blocking; the possible false success needs a person
    assert v["status"] == "NEEDS_REVIEW" and v["flagged"]
    assert {f["id"] for f in v["flags"]} == {"real.tool_failed", "real.false_success", "response_p90_ms"}
    again = m.ingest(json.dumps(vapi_call()), "v")
    assert again["added"] == [] and again["duplicates"] == out["added"]
    ok = m.store.read(m.ingest(json.dumps(vocto_call(1)), "v")["added"][0])[2]
    assert ok["status"] == "PASS" and not ok["flagged"]


# ---- judges ---------------------------------------------------------------------------------------

def judge_reply(fail_item=None):
    def reply(req):
        ids = req.json_schema["properties"]["items"]["items"]["properties"]["id"]["enum"]
        return json.dumps({"items": [{"id": i, "result": "FAIL" if i == fail_item else "PASS", "reason": "seen",
                                      "cites": [2]} for i in ids]})
    return reply


def test_judges_on_a_sample_within_caps(clinic, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    m = mon(clinic)
    cid = m.ingest(json.dumps(vocto_call(1)), "v")["added"][0]
    assert m.store.index()[cid]["judge_state"] == "pending"
    llm = ScriptedLLM(judge_reply("polite-professional-tone"))
    out = m.judge_pending(llm)
    assert out["judged"] == [cid] and llm.requests and "real customer on a real call" in llm.requests[0].system
    assert "release test" not in llm.requests[0].system and "Not known in advance" in llm.requests[0].messages[0].content
    v = m.store.read(cid)[2]
    judged = [c for c in v["checks"] if c["origin"] == "judge" and c["judge"]["votes"]]
    assert judged and v["judges"]["state"] == "done" and m.store.index()[cid]["judge_state"] == "done"
    tone = next(c for c in v["checks"] if c["check_id"] == "polite-professional-tone")
    assert tone["result"] == "FAIL" and tone["can_block"] is False      # not trusted yet: flagged, status unchanged
    assert v["status"] == "PASS" and any(f["kind"] == "judge" and not f["trusted"] for f in v["flags"])

    # no key, switched off, out of the sample, daily cap
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    rid = m.ingest(json.dumps(vocto_call(2)), "v")["added"][0]
    assert m.store.index()[rid]["judge_state"] == "skipped"
    verdict = {"flagged": False}
    sc = {**DEFAULTS["scoring"], "judge_share_pct": 0}
    assert score.judge_plan("c-x", verdict, [], sc, True)[0] == "skipped"
    assert score.judge_plan("c-x", {"flagged": True}, [], sc, True)[0] == "pending"     # flagged: always judged
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    full = [{"judged_at": today + "T01:00:00Z", "judge_cost_usd": 0.0}] * 50
    assert "limit of 50" in score.judge_plan("c-x", verdict, full, DEFAULTS["scoring"], True)[1]
    spent = [{"judged_at": today + "T01:00:00Z", "judge_cost_usd": 6.0}]
    assert "budget" in score.judge_plan("c-x", verdict, spent, DEFAULTS["scoring"], True)[1]
    assert score.sampled("c-abc", 100) and not score.sampled("c-abc", 0)
    share = sum(score.sampled(f"c-{i}", 30) for i in range(2000)) / 2000
    assert 0.25 < share < 0.35


def test_labels_replace_the_judges(clinic, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    m = mon(clinic)
    cid = m.ingest(json.dumps(vocto_call(1)), "v")["added"][0]
    m.judge_pending(ScriptedLLM(judge_reply("polite-professional-tone")))
    v = m.label(cid, {"polite-professional-tone": "FAIL"})
    tone = next(c for c in v["checks"] if c["check_id"] == "polite-professional-tone")
    assert tone["can_block"] and v["status"] == "FAIL" and m.store.index()[cid]["status"] == "FAIL"
    labels = json.loads((clinic[1] / "labels.json").read_text(encoding="utf-8"))["labels"]
    assert any(lb["test_id"] == cid and lb["run_id"] == "monitor" for lb in labels.values())


# ---- trends, alerts, retention ---------------------------------------------------------------------

def test_trends_daily_week_and_versions(clinic):
    m = mon(clinic)
    now = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
    for i in range(6):
        day = (now - timedelta(days=i % 3)).strftime("%Y-%m-%dT08:00:00Z")
        m.ingest(json.dumps(vocto_call(i, started=day, version="v1" if i < 3 else "v2", slow=i == 5)), "v")
    m.ingest(json.dumps(vocto_call(9, started=(now - timedelta(days=9)).strftime("%Y-%m-%dT08:00:00Z"))), "v")
    rows = m.store.rows()
    days = trends.daily(rows, 14, now)
    assert len(days) == 14 and days[-1]["day"] == "2026-11-02" and days[-1]["calls"] == 2 and sum(d["calls"] for d in days) == 7
    wk = trends.week_compare(rows, now)
    assert wk["this_week"]["calls"] == 6 and wk["last_week"]["calls"] == 1 and wk["this_week"]["pass_rate"] == 100.0
    vs = {v["version"]: v for v in trends.by_version(rows)}
    assert vs["v1"]["calls"] == 3 and vs["v2"]["calls"] == 3 and vs["v2"]["p90_ms"] == 4000
    assert trends.top_flags(rows)[0] == {"kind": "metric", "id": "response_p90_ms", "calls": 1}


def test_alerts_fire_once_and_recover(clinic):
    root, folder = clinic
    text = (folder / "profile.yaml").read_text(encoding="utf-8")
    (folder / "profile.yaml").write_text(text + "\nalerts:\n  - {name: team, type: webhook, url: 'https://hooks.example/x'}\n"
                                         "  - {name: runs only, type: slack, url: 'https://hooks.example/y', events: [no_go]}\n",
                                         encoding="utf-8")
    m = mon(clinic, alerts={"min_calls": 3, "pass_rate_below": 90})
    sent = []
    post = lambda url, body: sent.append((url, body)) or 200  # noqa: E731
    now = datetime.now(timezone.utc)
    stamp = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    m.ingest(json.dumps(vapi_call("a", started=stamp.replace("Z", ".000Z"))), "v", check_alerts=False)
    a = m.check_alerts(post=post)
    assert set(a["active"]) == {"false_success"} and a["raised"] == ["false_success"]   # 1 call < min_calls: no rates
    assert len(sent) == 1 and sent[0][0] == "https://hooks.example/x" and sent[0][1]["events"] == ["monitor"]
    assert "false success" in sent[0][1]["text"].lower()
    assert m.check_alerts(post=post)["raised"] == [] and len(sent) == 1           # still active: not sent again
    for i in range(3):
        m.ingest(json.dumps(vocto_call(i, started=stamp, ok=False, status_reply="Sorry, that failed.")), "v", check_alerts=False)
    a = m.check_alerts(post=post)
    assert "flagged" in a["active"] and len(sent) == 2      # 4 of 4 flagged (tool failures)
    m.delete([r["call_id"] for r in m.store.rows()])
    a = m.check_alerts(post=post)
    assert a["active"] == {} and sent[-1][1]["events"] == ["monitor_recovered"] and len(sent) == 3
    hist = m.store.state()["history"]
    assert {h["id"] for h in hist if h["event"] == "cleared"} >= {"false_success", "flagged"}
    assert [h["id"] for h in hist if h["event"] == "raised"].count("false_success") == 1


def test_metric_and_safety_rules():
    rows = []
    now = datetime.now(timezone.utc)
    for i in range(10):
        rows.append({"call_id": f"c-{i}", "started_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "status": "PASS",
                     "flagged": False, "flags": [], "latency_ms": [3500],
                     "quality": {"response_p90_ms": {"value": 3500, "n": 1}}})
    rules = {**DEFAULTS["alerts"], "min_calls": 10}
    active = watch.evaluate(rows, rules, {}, now)
    assert set(active) == {"metric:response_p90_ms"}
    rows[0]["flags"] = [{"kind": "judge", "id": "no-harmful-advice", "section": "safety"}]
    assert "safety" in watch.evaluate(rows[:1], rules, {}, now)          # safety needs no minimum
    assert watch.evaluate(rows, {**rules, "metrics_bad": False, "safety": False}, {}, now) == {}
    old = [{**r, "started_at": (now - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")} for r in rows]
    assert watch.evaluate(old, rules, {}, now) == {}                     # outside the window


def test_retention_deletes_old_calls(clinic):
    m = mon(clinic, privacy={"retention_days": 7})
    old = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    keep = m.ingest(json.dumps(vocto_call(1)), "v")["added"][0]
    out = m.ingest(json.dumps(vocto_call(2, started=old)), "v")
    assert out["added"] == [] and "retention period" in out["problems"][0]     # never written
    assert list(m.store.index()) == [keep] and len(list(m.store.calls.iterdir())) == 1
    # a call that ages past the period is deleted by the upkeep
    later = datetime.now(timezone.utc) + timedelta(days=8)
    assert m.store.prune(7, now=later) == 1 and m.store.index() == {} and not list(m.store.calls.iterdir())


# ---- turning a call into a test ---------------------------------------------------------------------

def test_scenario_from_a_real_call_is_valid(clinic):
    root, folder = clinic
    m = mon(clinic)
    cid = m.ingest(json.dumps(vapi_call()), "v")["added"][0]
    file, doc = m.scenario_from(cid)
    assert file.startswith("scenarios/real-") and doc["style"] == "scripted" and "from-real-call" in doc["tags"]
    assert doc["turns"][0]["caller"].startswith("Hi, my name is Alex Morgan") and not re.search(r"\[[a-z_]+\]", json.dumps(doc["turns"]))
    assert {c["id"] for c in doc["checks"]} == {"no-false-success"}
    full = {"schema_version": "0.1", "kind": "scenario", **doc}
    assert schema_errors("scenario", full) == []
    from vatf.web import setup
    saved = setup.save_doc(root, "examples/clinic", file, doc, create=True)
    assert saved["saved"], saved
    assert any(s["id"] == doc["id"] for s in load_project(folder).scenarios)


# ---- profile schema ------------------------------------------------------------------------------

def test_profile_monitor_section_validates():
    base = load_project(ROOT / "examples" / "clinic").profile
    ok = {**base, "monitor": {"intake": {"token": "${T}", "listen": True, "port": 8790},
                              "privacy": {"redact": ["email", "phone"], "retention_days": 0,
                                          "patterns": [{"name": "mrn", "regex": "MRN-\\d+"}]},
                              "scoring": {"judge_share_pct": 25}, "alerts": {"pass_rate_below": None}},
          "alerts": [{"url": "${X}", "events": ["monitor", "monitor_recovered"]}]}
    assert schema_errors("client_profile", ok) == []
    for bad in ({"privacy": {"redact": ["shoe_size"]}}, {"intake": {"port": 80}}, {"scoring": {"judge_share_pct": 150}},
                {"surprise": 1}):
        assert schema_errors("client_profile", {**base, "monitor": bad}), bad
    s = settings({"monitor": {"scoring": {"judges": False}}})
    assert s["scoring"]["judges"] is False and s["scoring"]["judge_max_per_day"] == 50 and s["alerts"]["min_calls"] == 10


# ---- webhook listener -----------------------------------------------------------------------------

def post(port, path, body, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("POST", path, body=body if isinstance(body, bytes) else json.dumps(body).encode(),
                 headers={"Content-Type": "application/json", **(headers or {})})
    r = conn.getresponse()
    return r.status, json.loads(r.read() or b"{}")


def test_listener_needs_the_token_and_takes_calls(clinic, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("VOCTO_TEST_TOKEN", "s3cret-token")
    m = mon(clinic, intake={"token": "${VOCTO_TEST_TOKEN}"})
    lst = Listener(lambda key: m if key in ("", "clinic") else None, port=0, log=lambda _m: None).start()
    try:
        port = lst.port
        assert post(port, "/ingest/clinic", vapi_call())[0] == 401
        assert post(port, "/ingest/clinic", vapi_call(), {"Authorization": "Bearer wrong"})[0] == 401
        assert post(port, "/ingest/nobody", vapi_call(), {"X-Vocto-Token": "s3cret-token"})[0] == 404
        assert post(port, "/ingest/clinic", {"message": {"type": "status-update"}},
                    {"X-Vapi-Secret": "s3cret-token"}) == (200, {"ignored": True, "why": "not a finished call"})
        assert post(port, "/ingest/clinic", b"not json, no speakers", {"X-Vocto-Token": "s3cret-token"})[0] == 400
        assert post(port, "/ingest/clinic?token=s3cret-token", vapi_call()) == (202, {"accepted": 1})
        assert post(port, "/ingest", retell_call(), {"Authorization": "Bearer s3cret-token"}) == (202, {"accepted": 1})
        for _ in range(100):
            if len(m.store.index()) == 2:
                break
            time.sleep(0.05)
        assert sorted(r["source"] for r in m.store.rows()) == ["retell", "vapi"]
        assert any(r["escalated"] for r in m.store.rows())                    # the Retell call was transferred
    finally:
        lst.stop()
    m2 = mon(clinic)                                                          # (token settings stay from above)
    monkeypatch.delenv("VOCTO_TEST_TOKEN")
    lst = Listener(lambda key: m2, port=0, log=lambda _m: None).start()
    try:
        assert post(lst.port, "/ingest", vapi_call("zzz"), {"X-Vocto-Token": ""})[0] == 403   # no token value: refused
    finally:
        lst.stop()


# ---- CLI ------------------------------------------------------------------------------------------

def test_cli_import_status_and_check(clinic, tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    root, folder = clinic
    f = tmp_path / "calls.jsonl"
    f.write_text("\n".join(json.dumps(vocto_call(i)) for i in range(2)) + "\n" + json.dumps(vapi_call(started=recent_start())), encoding="utf-8")
    code = main(["monitor", "import", str(folder), str(f), "--root", str(root), "--runs-dir", "runs"])
    out = capsys.readouterr().out
    assert code == 0 and "3 call(s) added" in out and "Possible false success" in out
    assert main(["monitor", "status", str(folder), "--root", str(root)]) == 1      # an alert is active
    assert "3 real call(s) kept" in capsys.readouterr().out
    assert main(["monitor", "check", str(folder), "--root", str(root)]) == 1
    bad = tmp_path / "bad.txt"
    bad.write_text("no speakers here", encoding="utf-8")
    assert main(["monitor", "import", str(folder), str(bad), "--root", str(root)]) == 0
    assert "not imported: a transcript needs speaker labels" in capsys.readouterr().out


# ---- dashboard API ----------------------------------------------------------------------------------

@pytest.fixture
def web(clinic, monkeypatch):
    from vatf.web.server import App, make_handler
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    root, _ = clinic
    (root / ".env").write_text("", encoding="utf-8")
    app = App(root, Path("runs"))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
    yield root, httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def api(port, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port)
    conn.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers={"X-VATF": "1"})
    r = conn.getresponse()
    return r.status, json.loads(r.read())


def test_dashboard_real_calls_api(web, monkeypatch):
    root, port = web
    status, page = api(port, "GET", "/api/project/clinic/monitor")
    assert status == 200 and page["totals"]["calls"] == 0 and len(page["daily"]) == 14 and not page["intake"]["token_set"]
    files = [{"name": "a.json", "text": json.dumps(vapi_call(started=recent_start()))},
             {"name": "b.jsonl", "base64": base64.b64encode("\n".join(json.dumps(vocto_call(i)) for i in range(2)).encode()).decode()},
             {"name": "c.txt", "text": "nonsense"}]
    status, out = api(port, "POST", "/api/project/clinic/monitor/import", {"files": files})
    assert status == 200 and out["added"] == 3 and out["files"][2]["error"].startswith("a transcript needs")
    assert "false_success" in out["alerts"]["active"]
    status, rows = api(port, "GET", "/api/project/clinic/monitor/calls?filter=flagged")
    assert status == 200 and rows["total"] == 1 and "quality" not in rows["rows"][0]
    cid = rows["rows"][0]["call_id"]
    assert api(port, "GET", "/api/project/clinic/monitor/calls?filter=review")[1]["total"] == 1
    assert api(port, "GET", "/api/project/clinic/monitor/calls?q=checkup")[1]["total"] == 3
    assert api(port, "GET", "/api/project/clinic/monitor/calls?filter=nope")[0] == 400
    status, one = api(port, "GET", f"/api/project/clinic/monitor/calls/{cid}")
    assert status == 200 and one["story"] and one["verdict"]["flagged"] and "Maria" not in json.dumps(one)
    assert api(port, "GET", "/api/project/clinic/monitor/calls/c-20990101-000000-abcdef")[0] == 404

    # settings: a bad pattern is refused, a good one saved
    bad = {"privacy": {"patterns": [{"name": "x", "regex": "("}]}}
    assert api(port, "PUT", "/api/project/clinic/monitor", {"monitor": bad})[1]["saved"] is False
    status, saved = api(port, "PUT", "/api/project/clinic/monitor",
                        {"monitor": {"scoring": {"judge_share_pct": 20}, "alerts": {"min_calls": 5}}})
    assert status == 200 and saved["saved"], saved
    assert api(port, "GET", "/api/project/clinic/monitor")[1]["settings"]["scoring"]["judge_share_pct"] == 20

    # a token: saved in .env, named in the profile, shown once
    status, tok = api(port, "POST", "/api/project/clinic/monitor/token")
    assert status == 200 and tok["saved"] and tok["name"] == "VOCTO_CALLS_EXAMPLES_CLINIC_TOKEN" and len(tok["token"]) > 20
    assert tok["token"] in (root / ".env").read_text(encoding="utf-8")
    assert "${VOCTO_CALLS_EXAMPLES_CLINIC_TOKEN}" in (root / "examples" / "clinic" / "profile.yaml").read_text(encoding="utf-8")
    page = api(port, "GET", "/api/project/clinic/monitor")[1]
    assert page["intake"]["token_set"] and page["settings"]["scoring"]["judge_share_pct"] == 20   # kept

    status, made = api(port, "POST", f"/api/project/clinic/monitor/calls/{cid}/test")
    assert status == 200 and made["saved"] and made["file"].startswith("scenarios/real-")
    assert api(port, "POST", f"/api/project/clinic/monitor/calls/{cid}/test")[1].get("existed")
    status, chk = api(port, "POST", "/api/project/clinic/monitor/check")
    assert status == 200 and "false_success" in chk["active"]
    assert api(port, "POST", "/api/project/clinic/monitor/delete", {"call_ids": [cid]})[1]["deleted"] == 1
    assert api(port, "GET", "/api/project/clinic/monitor")[1]["totals"]["calls"] == 2
    assert api(port, "POST", "/api/project/clinic/monitor/delete", {"all": True})[1]["deleted"] == 2


def test_hub_listener_follows_the_settings(clinic, monkeypatch):
    from vatf.web.monitoring import MonitorHub
    root, folder = clinic
    monkeypatch.setenv("HUB_TOKEN", "tok-123")
    hub = MonitorHub(root, root / "runs")
    hub.refresh()
    assert hub.listener is None                                    # nobody switched it on
    text = (folder / "profile.yaml").read_text(encoding="utf-8")
    (folder / "profile.yaml").write_text(text + "\nmonitor: {intake: {token: '${HUB_TOKEN}', listen: true, port: 0}}\n",
                                         encoding="utf-8")
    # port 0 is outside the schema's range but lets the test pick a free port
    hub.refresh()
    try:
        assert hub.listener is not None and hub.url_for("clinic").endswith("/ingest/clinic")
        status, _ = post(hub.port, "/ingest/clinic", vocto_call(5), {"X-Vocto-Token": "tok-123"})
        assert status == 202
        for _ in range(100):
            if Monitor(root, root / "runs", folder).store.index():
                break
            time.sleep(0.05)
        assert len(Monitor(root, root / "runs", folder).store.index()) == 1
    finally:
        hub.stop()


def test_alert_events_include_real_calls():
    assert "monitor" in alerts.EVENTS and "monitor_recovered" in alerts.DEFAULT_ON
    assert agent_slug(ROOT, ROOT / "examples" / "clinic") == "examples-clinic"
    assert MonitorStore(Path("/x"), "a").dir == Path("/x/monitor/a")
    assert re.match(r"^c-\d{8}-\d{6}-[0-9a-f]{6}$", MonitorStore(Path("/x"), "a").new_id(None, "k"))


def test_plain_text_tool_answer_is_an_object_in_the_timeline():
    call = parse(json.dumps(vapi_call()))[0]
    events = to_events(call, "c-20261102-090000-abcdef")
    body = next(e["data"]["body"] for e in events if e["type"] == "tool_response")
    assert body == {"result": "Error: slot service timed out"}
