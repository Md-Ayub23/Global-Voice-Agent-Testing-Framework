"""The release readiness report: a run's result in plain words, for people who don't read test logs.

It is built from what a run already saved (run.json, decision.json, each call's verdict and events),
so it never changes the decision; it only explains it:

  verdict     Ready to release / Ready, with N things to watch / Not ready: N blocking issues /
              Not enough evidence yet (the same GO / NO_GO / not-enough-evidence underneath)
  summary     a few short lines: what was tested, the result, the biggest problems
  scorecard   one row per area (task success, actions and data, speed, safety ...), Good / Watch /
              Problem / Not tested, with the number behind it. No overall score on purpose: an average
              can hide a single data leak.
  findings    one card per distinct problem, grouped across calls and repeats, with how often it
              happened, who is likely at fault (the agent, Vocto's set-up, or judges that disagree),
              where to look, and proof: the exact lines of the call that show it
  metrics     the numbers (rates, times, costs), coverage (what was and wasn't tested) and method
              (how the run was made)

Built on request (the dashboard, the CSV export); nothing here is saved, so old runs get it too.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import drills, modes, stats
from .costs import money
from .attacks import PACKS
from .story import story_rows
from .util import read_json

VERSION = 1
PROOFS_PER_FINDING = 3
QUOTE_CHARS = 240

OUTCOME_TITLES = {
    "NO_ACTION": "The task was not done",
    "FALSE_SUCCESS": "Said the task was done, but it wasn't",
    "FALSE_FAILURE": "Said the task failed, but it was actually done",
    "SILENT_DATA_BUG": "Saved wrong or missing data without noticing",
    "DUPLICATE_ACTION": "Did the same action twice",
    "UNEXPECTED_ACTION": "Did something it should not have done",
    "HONEST_FAILURE": "Gave up on a task that could be done",
    "TRUE_SUCCESS": "Went ahead where it should have stopped",
}
OUTCOME_WHAT = {
    "NO_ACTION": "Nothing was saved and the agent didn't say it was done.",
    "FALSE_SUCCESS": "The agent told the caller it was done, but your system has no correct record of it.",
    "FALSE_FAILURE": "The agent told the caller it failed, but the record was saved.",
    "SILENT_DATA_BUG": "The tool said ok, but the saved record is missing or has wrong details.",
    "DUPLICATE_ACTION": "The same booking or order was sent more than once.",
    "UNEXPECTED_ACTION": "Something was saved when nothing should have been.",
    "HONEST_FAILURE": "The agent told the caller it couldn't be done.",
    "TRUE_SUCCESS": "The agent completed and confirmed the task.",
}
EXPECTED = {
    "success": "The task is done, saved correctly in your system, and confirmed to the caller.",
    "honest_failure": "The agent tells the caller honestly that it could not be done.",
    "no_action": "Nothing is saved and nothing is promised.",
    "recover_or_honest": "The agent gets it done on a retry, or tells the caller honestly that it couldn't. "
                         "It never claims a booking or order that wasn't made, and never makes it twice.",
}
FIX = {
    "NO_ACTION": "Check when the agent's instructions tell it to use the tool, and that the tool is available to it.",
    "FALSE_SUCCESS": "The agent must wait for the tool's answer and only confirm when it says the task succeeded.",
    "FALSE_FAILURE": "Check how the agent reads the tool's answer: it treated a success as a failure.",
    "SILENT_DATA_BUG": "Compare the details sent to the tool with what was saved: the tool or its mapping drops or changes fields.",
    "DUPLICATE_ACTION": "Make the tool safe to retry (a request id) and stop the agent calling it twice.",
    "UNEXPECTED_ACTION": "Tighten the instructions for when the agent may act, e.g. confirm with the caller first.",
    "HONEST_FAILURE": "Check why the agent stopped: a tool error, a missing detail, or an instruction that is too strict.",
    "tool_called": "Check when the agent's instructions tell it to use this tool, and that it has the tool.",
    "tool_not_called": "The agent acted too early or when it shouldn't: tighten its instructions (confirm first).",
    "tool_order": "Check the order of steps in the agent's instructions.",
    "probe": "Compare what the agent sent to the tool with what was saved in your system.",
    "entity_fidelity": "Compare the caller's details with what the agent sent to the tool: misheard or retyped details.",
    "readback": "Check how the agent reads numbers and names back (speak them in small groups).",
    "said": "The agent didn't give the information or wording this test expects; check its instructions.",
    "not_said": "The agent said something it must never say; add a clear rule to its instructions.",
    "no_secrets": "Remove secrets from the agent's prompt, tools and logs: it can't leak what it doesn't have.",
    "latency": "Look at the slowest answers: tool calls, model size and end-of-speech waiting usually cause them.",
    "barge_in": "Check the agent's interruption setting (voice activity detection) so it stops when the caller talks.",
    "handoff": "Check the agent's rules and tool for handing a caller to a person.",
    "max_turns": "The agent took too many turns: it may be asking again for details it already has.",
    "log_contains": "An expected entry is missing from the agent's logs: check what it logs.",
    "log_not_contains": "The agent logged something it shouldn't (an error or private data).",
    "judge": "Read the judges' reason and the quoted lines, then adjust the instruction the agent broke.",
    "side_effects": "Look at the rows that changed: the agent used a tool with the wrong id, or a tool changes more "
                    "than it should. If the change is fine (an audit log, a timestamp), add it to Watch ignore.",
    "wrong_tool": "Check when the agent's instructions allow this tool. If it is fine for this test, add it to the "
                  "test's allowed tools.",
}
TYPE_TITLES = {
    "tool_called": "Didn't use a tool it should have",
    "tool_not_called": "Used a tool it should not have",
    "tool_order": "Did the steps in the wrong order",
    "probe": "Wrong or missing data in your system",
    "entity_fidelity": "Sent wrong details to your system",
    "readback": "Read details back wrongly",
    "said": "Didn't say what it should",
    "not_said": "Said something it must not say",
    "no_secrets": "Leaked a secret",
    "latency": "Answered too slowly",
    "barge_in": "Kept talking when the caller cut in",
    "handoff": "Didn't hand the caller to a person correctly",
    "max_turns": "Took too many turns",
    "log_contains": "An expected log entry is missing",
    "log_not_contains": "Logged something it should not",
    "side_effects": "Changed something else in your system",
    "wrong_tool": "Used a tool this test doesn't expect",
}
STATUS_WORDS = {"pass": "Good", "watch": "Watch", "fail": "Problem", "not_tested": "Not tested"}
VOICE_SPEECH = {"entity_fidelity", "readback"}
SECTION_AREA = {"accuracy": "actions", "experience": "conversation", "security": "safety", "safety": "safety"}
RULE_AREA = {"latency": "speed", "metric": "speed", "coverage": "evidence", "needs_review": "evidence", "latency_sample": "evidence",
             "regression": "regression", "regression_warnings": "regression", "load_evidence": "evidence"}
SPEED_METRICS = {"ttfw_ms", "response_p90_ms", "response_p99_ms", "longest_silence_ms", "barge_in_stop_ms",
                 "checkin_ms", "talked_over", "backchannel_kept"}
LEVELS = {
    "ready": "Ready to release",
    "ready_with_notes": "Ready, with things to watch",
    "not_ready": "Not ready",
    "not_enough_evidence": "Not enough evidence yet",
    "no_result": "No result",
}


# ---- small helpers ---------------------------------------------------------------------------

def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _secs(ms: float | int | None) -> str:
    if ms is None:
        return "-"
    return f"{ms / 1000:.1f} s" if ms >= 1000 else f"{ms:.0f} ms"


def _clock(ms: float | int | None) -> str | None:
    if ms is None:
        return None
    s = int(ms // 1000)
    return f"{s // 60}:{s % 60:02d}"


def _range(part: int, whole: int) -> str | None:
    """The likely range of a rate, in plain words ("likely 74% to 97% (from 30)"): stats.rate."""
    r = stats.rate(int(part), int(whole)) if whole else None
    return r["text"] if r else None


def _pct(part: int, whole: int) -> float | None:
    return round(part / whole, 3) if whole else None


def _short(text: str, n: int = QUOTE_CHARS) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _read_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError:
                break
    return out


def _safe(path: Path) -> Any:
    try:
        return read_json(path)
    except (OSError, ValueError):
        return None


def _scenario_title(titles: Mapping[str, str], sid: str) -> str:
    if sid in titles:
        return titles[sid]
    tail = sid.split(".", 1)[-1] if "." in sid else sid
    return tail.replace("-", " ").replace("_", " ").capitalize()


def _check_type(c: Mapping[str, Any]) -> str:
    if c.get("origin") == "outcome" or c.get("check_id") == "outcome":
        return "outcome"
    if c.get("origin") == "judge":
        return "judge"
    return c.get("type") or "check"


def _outcome_block(v: Mapping[str, Any], check_id: str) -> Mapping[str, Any]:
    """The outcome block a check reports on: the task's, or one further step of it (more_outcomes)."""
    if check_id == "outcome":
        return v.get("outcome") or {}
    return next((b for b in v.get("more_outcomes") or [] if b.get("check_id") == check_id), {})


def _tool_in(message: str) -> str | None:
    m = re.match(r"^([A-Za-z_][\w.-]*) was (?:called|not called)", message or "")
    return m.group(1) if m else None


# ---- findings --------------------------------------------------------------------------------

class _Finding:
    __slots__ = ("key", "area", "title", "kind", "status", "severity", "blocks", "what", "expected", "fix",
                 "cause", "caution", "scenarios", "failed_calls", "proofs", "messages", "of_calls", "order")

    def __init__(self, key, area, title, kind, status, severity, blocks, what, expected="", fix="", cause="agent"):
        self.key, self.area, self.title, self.kind = key, area, title, kind
        self.status, self.severity, self.blocks = status, severity, blocks
        self.what, self.expected, self.fix, self.cause = what, expected, fix, cause
        self.caution = ""
        self.scenarios: dict[str, int] = {}
        self.failed_calls: list[str] = []
        self.proofs: list[dict[str, Any]] = []
        self.messages: list[str] = []
        self.of_calls = 0
        self.order = 0


CAUSE_TEXT = {
    "agent": "The agent",
    "setup": "Vocto's test set-up (not the agent)",
    "judges": "Unclear: the AI judges disagreed, a person should decide",
    "opinion": "The AI judges think so, but they aren't trusted for this yet",
    "evidence": "Not enough evidence to decide",
    "test": "The test case itself (it may expect the wrong thing)",
}


class _Calls:
    """The final calls of a run, with their events read once when a proof needs them."""

    def __init__(self, run_dir: Path, verdicts: Sequence[Mapping[str, Any]]):
        self.run_dir = run_dir
        self.by_id = {v["test_id"]: v for v in verdicts}
        self._events: dict[str, list[dict[str, Any]]] = {}

    def events(self, test_id: str) -> list[dict[str, Any]]:
        if test_id not in self._events:
            folder = self.run_dir / "calls" / test_id
            record = _safe(folder / "call.json") or {}
            self._events[test_id] = _read_events(folder / record.get("events_file", "events.jsonl"))
        return self._events[test_id]

    def proof(self, test_id: str, check: Mapping[str, Any] | None, note: str = "",
              seq: int | None = None) -> list[dict[str, Any]]:
        """The lines of the call that show a check's result (its evidence events), quoted."""
        events = self.events(test_id)
        by_seq = {e["seq"]: e for e in events}
        rows = {n: (who, text) for n, who, text in story_rows(events)}
        refs = [(seq, note)] if seq is not None else [(r.get("event"), r.get("note", "")) for r in (check or {}).get("evidence", [])]
        out = []
        for s, n in refs[:3]:
            if s is None:
                continue
            who, text = rows.get(s, (None, None))
            e = by_seq.get(s)
            extra = ""
            if text and text.endswith("]") and "  [" in text:
                text, extra = text[: text.rindex("  [")], text[text.rindex("  [") + 3:-1]
                extra = "; ".join(x for x in extra.split("; ") if not x.startswith(("heard at", "spoke ")))
            if n in ("judge", "") and extra:
                n = extra
            elif n == "judge":
                n = "the judges cite this line"
            out.append({"test_id": test_id, "check_id": (check or {}).get("check_id"), "seq": s,
                        "at": _clock(e.get("t_ms")) if e else None, "who": (who or (e or {}).get("type", "")).lower(),
                        "quote": _short(text) if text else None, "note": n or ""})
        if not out:
            out.append({"test_id": test_id, "check_id": (check or {}).get("check_id"), "seq": None, "at": None,
                        "who": "", "quote": None, "note": note or ((check or {}).get("message") or "")})
        return out


def _area_of(check: Mapping[str, Any], scenario_id: str, voice: bool, faults: bool) -> str:
    kind = _check_type(check)
    if scenario_id.startswith("safety.") or kind == "no_secrets":
        return "safety"
    if kind in ("latency", "barge_in"):
        return "speed"
    if voice and kind in VOICE_SPEECH:
        return "speech"
    if faults and kind in ("outcome", "tool_called", "tool_not_called", "probe", "said", "not_said"):
        return "resilience"
    if kind == "outcome":
        return "task"
    if kind in ("tool_called", "tool_not_called", "tool_order", "probe", "entity_fidelity", "log_contains",
                "log_not_contains", "side_effects", "wrong_tool"):
        return "actions"
    if kind == "judge":
        return "conversation"
    return SECTION_AREA.get(check.get("section", ""), "conversation")


def _check_findings(verdicts, calls: _Calls, titles, critical_failed: set[str], voice: bool,
                    faulty: set[str]) -> list[_Finding]:
    found: dict[tuple, _Finding] = {}
    per_scenario: dict[str, int] = defaultdict(int)
    judged: dict[str, int] = defaultdict(int)
    for v in verdicts:
        if v["status"] == "ERROR":
            continue
        per_scenario[v["scenario_id"]] += 1
        for c in v.get("checks", []):
            if c.get("origin") == "judge" and c["result"] in ("PASS", "FAIL", "NEEDS_REVIEW"):
                judged[c["check_id"]] += 1
    for v in verdicts:
        if v["status"] == "ERROR":
            continue
        sid = v["scenario_id"]
        seen_in_call: set[tuple] = set()
        for c in v.get("checks", []):
            if c["result"] not in ("FAIL", "NEEDS_REVIEW"):
                continue
            kind = _check_type(c)
            judge = kind == "judge"
            trusted = c.get("can_block", True)
            if judge:
                key = ("judge", c["check_id"], sid.startswith("safety."), c["result"] == "NEEDS_REVIEW", trusted)
            elif kind in ("outcome", "check"):
                key = (sid, c["check_id"])
            else:
                key = (sid, kind, _tool_in(c.get("message", "")), c["result"], trusted)
            if key in seen_in_call:
                continue
            seen_in_call.add(key)
            f = found.get(key)
            if f is None:
                area = _area_of(c, sid, voice, sid in faulty)
                if c["result"] == "NEEDS_REVIEW":
                    status, cause, severity, blocks = "needs_review", "judges", c.get("severity", "major"), False
                elif judge and not trusted:
                    status, cause, severity, blocks = "opinion", "opinion", "advice", False
                else:
                    blocks = (c.get("severity") == "blocker" and trusted) or sid in critical_failed
                    status, cause, severity = "problem", "agent", "blocker" if blocks else c.get("severity", "major")
                if kind == "outcome":
                    blk = _outcome_block(v, c["check_id"])
                    cls = blk.get("class", "")
                    title = OUTCOME_TITLES.get(cls, "The result was not what this test expects")
                    what = OUTCOME_WHAT.get(cls, c["message"])
                    if c["check_id"] != "outcome" and blk.get("tool"):
                        title = f"{title} ({blk['tool'].replace('_', ' ')})"
                        what = c["message"]
                    expected = EXPECTED.get(blk.get("expected", ""), "")
                    fix = FIX.get(cls, "")
                    drill = drills.split(sid)
                    if drill and (advice := drills.FAILURES[drill[1]].get("fix", {}).get(cls)):
                        fix = advice     # a failure drill knows what really went wrong (e.g. saved, answer lost)
                elif judge:
                    title = (c.get("judge") or {}).get("title") or c["check_id"]
                    what = c.get("message", "")
                    q = (c.get("judge") or {}).get("question", "")
                    expected, fix = (f"The judges ask: {q}" if q else ""), FIX["judge"]
                else:
                    tool = _tool_in(c.get("message", ""))
                    title = TYPE_TITLES.get(kind, "A check failed")
                    if tool and kind == "tool_called":
                        title = f"Didn't use {tool} as expected"
                    elif tool and kind == "tool_not_called":
                        title = f"Used {tool} when it should not have"
                    what, expected, fix = c["message"], "", FIX.get(kind, "")
                if sid.startswith("safety.") and not judge:
                    pack = PACKS.get(sid.split(".")[1], {}).get("title")
                    title = f"{pack}: {title[0].lower() + title[1:]}" if pack else title
                f = _Finding(key, area, title, kind, status, severity, blocks, what, expected, fix, cause)
                if voice and kind in VOICE_SPEECH:
                    f.caution = ("On a voice call this can also be Vocto's caller mishearing the agent: "
                                 "listen to the recording at the quoted time before changing the agent.")
                f.order = len(found)
                found[key] = f
            f.scenarios[sid] = f.scenarios.get(sid, 0) + 1
            f.failed_calls.append(v["test_id"])
            if c["message"] not in f.messages:
                f.messages.append(c["message"])
            if len(f.proofs) < PROOFS_PER_FINDING:
                # The first call gives up to two quoted lines; each later call one more, from a different call.
                f.proofs = (f.proofs + calls.proof(v["test_id"], c)[: 1 if f.proofs else 2])[:PROOFS_PER_FINDING]
    for f in found.values():
        f.of_calls = judged[f.key[1]] if f.kind == "judge" else sum(per_scenario[s] for s in f.scenarios)
        if f.kind not in ("outcome", "judge") and len(f.messages) > 1:
            f.what = " ".join(f.messages[:4]) + (f" And {len(f.messages) - 4} more." if len(f.messages) > 4 else "")
    return list(found.values())


def _error_finding(verdicts, calls: _Calls, titles) -> _Finding | None:
    errs = [v for v in verdicts if v["status"] == "ERROR"]
    if not errs:
        return None
    why = []
    for v in errs:
        w = (v.get("load") or {}).get("why") or next((c["message"] for c in v.get("checks", [])
                                                       if c["result"] in ("ERROR", "NOT_VERIFIED")), "")
        if w and w not in why:
            why.append(w)
    f = _Finding(("error",), "evidence", "Some calls could not be completed",
                 "error", "evidence", "major", False,
                 "Vocto couldn't finish these calls, so they say nothing about the agent: "
                 + ("; ".join(_short(w, 120) for w in why[:3]) or "see each call for the error") + ".",
                 fix="Check that the agent is running and reachable, then run again. These never count against the agent.",
                 cause="setup")
    for v in errs:
        f.scenarios[v["scenario_id"]] = f.scenarios.get(v["scenario_id"], 0) + 1
        f.failed_calls.append(v["test_id"])
    f.of_calls = len(verdicts)
    f.proofs = [{"test_id": v["test_id"], "check_id": None, "seq": None, "at": None, "who": "",
                 "quote": None, "note": "call not completed"} for v in errs[:PROOFS_PER_FINDING]]
    return f


def _latency_finding(decision, verdicts, calls: _Calls) -> _Finding | None:
    lat = decision.get("latency") or {}
    if lat.get("within_limits") is not False:
        return None
    f = _Finding(("latency",), "speed", "Answers are too slow", "latency", "problem", "blocker", True,
                 f"Callers waited {_secs(lat.get('p50_ms'))} for a typical answer and {_secs(lat.get('p95_ms'))} "
                 f"for the slowest 5% ({lat.get('turns_measured', 0)} answers timed).",
                 expected=f"Typical answer within {_secs(lat.get('p50_limit_ms'))}, slowest 5% within "
                          f"{_secs(lat.get('p95_limit_ms'))} (your limits in Pass rules).",
                 fix=FIX["latency"])
    limit = lat.get("p95_limit_ms") or 0
    slow = []
    for v in verdicts:
        if v["status"] == "ERROR":
            continue
        turns = [t for t in (v.get("metrics") or {}).get("latency_turns", []) if t.get("counted")]
        over = [t for t in turns if t["ms"] > limit]
        if over:
            worst = max(over, key=lambda t: t["ms"])
            slow.append((worst["ms"], v, worst))
            f.scenarios[v["scenario_id"]] = f.scenarios.get(v["scenario_id"], 0) + 1
            f.failed_calls.append(v["test_id"])
    slow.sort(key=lambda x: -x[0])
    for ms, v, t in slow[:PROOFS_PER_FINDING]:
        p = calls.proof(v["test_id"], None, note=f"answered after {_secs(ms)}", seq=t.get("seq"))[0]
        f.proofs.append(p)
    f.of_calls = sum(v["status"] != "ERROR" for v in verdicts)
    return f


def _metric_findings(decision, verdicts, calls: _Calls) -> list[_Finding]:
    out = []
    for q in decision.get("quality") or []:
        if q.get("band") not in ("warn", "bad") or q.get("value") is None:
            continue
        blocks = bool(q.get("blocks")) and q["band"] == "bad" and decision.get("decision") == "NO_GO"
        area = "speed" if q["id"] in SPEED_METRICS else "conversation"
        unit = {"ms": "", "%": "%", "wpm": " words a minute", "turns": " turns", "s": " s"}.get(q.get("unit"), "")
        value = _secs(q["value"]) if q.get("unit") == "ms" else f"{q['value']:g}{unit}"
        good = _secs(q["good"]) if q.get("unit") == "ms" else f"{q['good']:g}{unit}"
        word = "at least" if q.get("better") == "higher" else "at most"
        f = _Finding(("metric", q["id"]), area, f"{q['title']}: {value}", "metric", "problem",
                     "blocker" if blocks else ("major" if q["band"] == "bad" else "minor"), blocks,
                     q.get("about", ""), expected=f"Good is {word} {good}.",
                     fix="Open the calls listed below and look at the moments this measures.")
        bad = [v for v in verdicts if v.get("status") != "ERROR"
               and ((v.get("metrics") or {}).get("quality") or {}).get(q["id"], {}).get("band") in ("bad", "warn")]
        for v in bad:
            f.scenarios[v["scenario_id"]] = f.scenarios.get(v["scenario_id"], 0) + 1
            f.failed_calls.append(v["test_id"])
        f.proofs = [{"test_id": v["test_id"], "check_id": None, "seq": None, "at": None, "who": "", "quote": None,
                     "note": f"{q['title']} in this call: "
                             + str(((v.get("metrics") or {}).get("quality") or {}).get(q["id"], {}).get("value", "?"))}
                    for v in bad[:PROOFS_PER_FINDING]]
        f.of_calls = sum(v.get("status") != "ERROR" for v in verdicts)
        out.append(f)
    return out


def _reason_findings(decision) -> list[_Finding]:
    """Decision reasons that aren't about one check: evidence, regression and load."""
    out = []
    for i, r in enumerate(decision.get("reasons", [])):
        rule = r["rule"]
        if rule in ("coverage", "needs_review", "load_evidence", "latency_sample"):
            title = {"needs_review": "Calls waiting for a person to check",
                     "latency_sample": "Too few answers to judge the slowest ones"}.get(rule, "Not enough evidence to decide")
            f = _Finding(("reason", i), "evidence", title, rule, "evidence", "major", False, r["message"],
                         fix=("Label them in AI judges > Review calls; the result updates by itself." if rule == "needs_review"
                              else "Run again (more repeats, or once the agent is reachable) so every call gets a result."),
                         cause="evidence")
        elif rule in ("regression", "regression_warnings"):
            blocks = rule == "regression"
            f = _Finding(("reason", i), "regression", "Worse than the baseline" if blocks else "Some results are worse than the baseline",
                         rule, "problem", "blocker" if blocks else "minor", blocks, r["message"],
                         fix="Open Compare runs to see each change side by side.")
        elif rule.startswith("load_") and rule not in ("load_ok",):
            blocks = rule not in ("load_warnings",)
            title = {"load_failed_calls": "Calls failed under load", "load_latency": "Too slow under load",
                     "load_blocker": "Did something it must never do under load",
                     "load_warnings": "Got worse under load"}.get(rule, "A load limit was broken")
            f = _Finding(("reason", i), "load", title, rule, "problem", "blocker" if blocks else "minor", blocks,
                         r["message"], fix="Open the Load tab for each stage's numbers.")
        else:
            continue
        for ref in r.get("refs", []) or []:
            test = ref.split("/")[0]
            if test not in f.failed_calls:
                f.failed_calls.append(test)
        f.proofs = [{"test_id": t, "check_id": None, "seq": None, "at": None, "who": "", "quote": None, "note": ""}
                    for t in f.failed_calls[:PROOFS_PER_FINDING]]
        out.append(f)
    return out


SEVERITY_ORDER = {"blocker": 0, "major": 1, "minor": 2, "advice": 3}
STATUS_ORDER = {"problem": 0, "evidence": 1, "needs_review": 2, "opinion": 3}


def _finish(findings: list[_Finding], titles: Mapping[str, str]) -> list[dict[str, Any]]:
    findings.sort(key=lambda f: (not f.blocks, STATUS_ORDER[f.status], SEVERITY_ORDER.get(f.severity, 4),
                                 -len(f.failed_calls), f.order))
    out = []
    for i, f in enumerate(findings, 1):
        n = len(set(f.failed_calls))
        often = (f"in {n} of {f.of_calls} calls" if f.of_calls else f"in {_plural(n, 'call')}")
        if f.kind not in ("judge", "metric", "error") and len(f.scenarios) == 1 and f.of_calls:
            often = f"in {n} of {_plural(f.of_calls, 'repeat')}"
        out.append({
            "id": f"F{i}", "title": f.title, "area": f.area, "area_title": modes.AREAS.get(f.area, f.area),
            "kind": f.kind, "status": f.status, "severity": f.severity, "blocks": f.blocks,
            "what": f.what, "expected": f.expected, "fix": f.fix, "caution": f.caution,
            "cause": f.cause, "cause_text": CAUSE_TEXT[f.cause],
            "how_often": {"calls": n, "of": f.of_calls, "text": often},
            "tests": [{"id": s, "title": _scenario_title(titles, s), "calls": c} for s, c in f.scenarios.items()],
            "calls": list(dict.fromkeys(f.failed_calls))[:50],
            "messages": f.messages[:5],
            "proofs": f.proofs,
        })
    return out


# ---- scorecard and metrics ------------------------------------------------------------------

def _row(area: str, status: str, value: str, detail: str = "", **extra) -> dict[str, Any]:
    return {"area": area, "title": modes.AREAS[area], "status": status, "status_text": STATUS_WORDS[status],
            "value": value, "detail": detail, **extra}


def _rate(checks: Iterable[Mapping[str, Any]]) -> tuple[int, int]:
    ok = bad = 0
    for c in checks:
        ok += c["result"] == "PASS"
        bad += c["result"] == "FAIL"
    return ok, ok + bad


def _scorecard(decision, verdicts, findings, info, voice, faulty) -> list[dict[str, Any]]:
    live = [v for v in verdicts if v["status"] != "ERROR"]
    own = [v for v in live if not v["scenario_id"].startswith("safety.")]
    by_area: dict[str, list[dict]] = defaultdict(list)
    for f in findings:
        by_area[f["area"]].append(f)

    def status_of(area: str, base: str = "pass") -> str:
        fs = [f for f in by_area.get(area, []) if f["status"] == "problem"]
        if any(f["blocks"] for f in fs):
            return "fail"
        if fs or any(f["status"] == "needs_review" for f in by_area.get(area, [])):
            return "watch"
        return base

    rows: dict[str, dict[str, Any]] = {}
    rel = [r for r in decision.get("reliability", []) if not r["scenario_id"].startswith("safety.")
           and r["scenario_id"] not in faulty]
    if rel:
        passed = sum(bool(r["pass_hat_k"]) for r in rel)
        crit_bad = [r for r in rel if r["criticality"] == "critical" and r["failed"]]
        st = "fail" if crit_bad else ("watch" if passed < len(rel) else "pass")
        st = "fail" if st != "fail" and status_of("task") == "fail" else st
        rows["task"] = _row("task", st, f"{passed} of {_plural(len(rel), 'test case')} passed every repeat",
                            f"{_plural(len(crit_bad), 'key test case')} failed" if crit_bad else
                            ("Every key test case passed every repeat" if passed == len(rel) else
                             "Some test cases failed at least one repeat"),
                            percent=_pct(passed, len(rel)), range=_range(passed, len(rel)))
    action_checks = [c for v in own if v["scenario_id"] not in faulty for c in v.get("checks", [])
                     if _area_of(c, v["scenario_id"], voice, False) == "actions"]
    ok, n = _rate(action_checks)
    if n:
        rows["actions"] = _row("actions", status_of("actions"), f"{ok} of {_plural(n, 'check')} passed",
                               "Tools used, details sent and data saved in your system", percent=_pct(ok, n), range=_range(ok, n))
    conv = [c for v in own for c in v.get("checks", []) if _area_of(c, v["scenario_id"], voice, False) == "conversation"
            and c.get("can_block", True)]
    ok, n = _rate(conv)
    opinions = sum(1 for f in by_area.get("conversation", []) if f["status"] in ("opinion", "needs_review"))
    if n or opinions:
        rows["conversation"] = _row("conversation", status_of("conversation"),
                                    f"{ok} of {_plural(n, 'check')} passed" if n else "Judges' opinions only",
                                    (f"{_plural(opinions, 'judge opinion')} to confirm" if opinions else
                                     "What the agent said, tone and staying on topic"), percent=_pct(ok, n), range=_range(ok, n))
    lat = decision.get("latency") or {}
    if voice and lat.get("turns_measured"):
        st = "fail" if lat.get("within_limits") is False else status_of("speed")
        rows["speed"] = _row("speed", st, f"Typical {_secs(lat.get('p50_ms'))}, slowest {_secs(lat.get('p95_ms'))}",
                             f"Limits {_secs(lat.get('p50_limit_ms'))} and {_secs(lat.get('p95_limit_ms'))} · "
                             f"{lat['turns_measured']} answers timed"
                             + (f" (the slowest 5% needs {lat.get('p95_needs', 20)} to judge)"
                                if lat.get("p95_enough") is False else ""))
    heard = decision.get("heard_numbers")
    sp = [c for v in own for c in v.get("checks", []) if voice and _check_type(c) in VOICE_SPEECH]
    ok, n = _rate(sp)
    if voice and info.get("sweep") and decision.get("by_condition"):
        # Speech mode: how many of the caller conditions the agent handled, side by side.
        conds = _conditions(decision, {})
        handled = sum(c["status"] == "handled" for c in conds)
        broke = [c["title"] for c in conds if c["status"] == "broke"]
        weak = [c["title"] for c in conds if c["status"] == "struggled"]
        st = "fail" if broke else ("watch" if weak else status_of("speech"))
        rows["speech"] = _row("speech", st, f"Handled {handled} of {_plural(len(conds), 'caller condition')}",
                              ("Broke with: " + ", ".join(broke) if broke else
                               "Struggled with: " + ", ".join(weak) if weak else "Every condition passed"),
                              percent=_pct(handled, len(conds)), range=_range(handled, len(conds)))
    elif voice and (n or (heard and heard.get("numbers"))):
        val = f"{ok} of {_plural(n, 'detail check')} passed" if n else \
            f"{heard['heard_right']} of {heard['numbers']} numbers heard right"
        rows["speech"] = _row("speech", status_of("speech"), val,
                              f"Conditions: {info.get('conditions') or 'as written'}", percent=_pct(ok, n) if n else None, range=_range(ok, n))
    safety = decision.get("safety")
    if safety:
        st = status_of("safety")
        if st == "pass" and (safety.get("failed") or safety.get("to_review")):
            st = "watch"
        rows["safety"] = _row("safety", st, f"{safety['passed']} of {_plural(safety['calls'], 'attack call')} handled safely",
                              f"{safety.get('failed', 0)} with a finding · {safety.get('to_review', 0)} for you to check",
                              percent=_pct(safety["passed"], safety["calls"]), range=_range(safety["passed"], safety["calls"]))
    fault_rel = [r for r in decision.get("reliability", []) if r["scenario_id"] in faulty]
    if decision.get("by_failure"):
        # Failure drills: one row per kind of failure tried, so the area says which ones broke the agent.
        fails = _failures(decision, {})
        handled = [f for f in fails if f["status"] == "handled"]
        broke = [f["title"] for f in fails if f["status"] == "broke"]
        weak = [f["title"] for f in fails if f["status"] == "struggled"]
        lies = sum(f["false_claims"] for f in fails)
        dups = sum(f["duplicates"] for f in fails)
        st = status_of("resilience", "pass" if len(handled) == len(fails) else "watch")
        if lies or dups:
            st = "fail"
        sub = ("Broke with: " + ", ".join(broke) if broke else
               "Struggled with: " + ", ".join(weak) if weak else "Every failure handled")
        if lies or dups:
            sub += f" · {_plural(lies, 'untrue claim')}, {_plural(dups, 'duplicate')}"
        rows["resilience"] = _row("resilience", st, f"Handled {len(handled)} of {_plural(len(fails), 'kind')} of failure",
                                  sub, percent=_pct(len(handled), len(fails)), range=_range(len(handled), len(fails)))
    elif fault_rel:
        passed = sum(bool(r["pass_hat_k"]) for r in fault_rel)
        st = status_of("resilience", "pass" if passed == len(fault_rel) else "watch")
        rows["resilience"] = _row("resilience", st, f"{passed} of {_plural(len(fault_rel), 'failure test')} handled well",
                                  "A tool broken on purpose: did the agent stay honest?", percent=_pct(passed, len(fault_rel)), range=_range(passed, len(fault_rel)))
    load = decision.get("load")
    if load:
        st = "fail" if decision.get("decision") == "NO_GO" else status_of("load")
        rows["load"] = _row("load", st, f"Handled {load.get('capacity') or 0} calls at once",
                            f"{load.get('calls', 0)} calls · {load.get('failed', 0)} failed · tested up to "
                            f"{load.get('calls_at_once')} at once")
    base = decision.get("baseline")
    if base:
        worse = base.get("regressions", 0) + base.get("worse", 0)
        st = "fail" if base.get("blocking") else ("watch" if worse else "pass")
        rows["regression"] = _row("regression", st, f"{worse} worse, {base.get('better', 0)} better",
                                  f"Against the baseline run {base.get('run_id')}"
                                  + ("" if base.get("like_for_like", True) else " (made differently, so it only informs)"))
    cov = decision.get("coverage") or {}
    if cov:
        enough = cov.get("ratio", 0) >= cov.get("min_required", 0) and not cov.get("needs_review_calls")
        st = "pass" if enough and decision.get("decision") != "NO_GO_INSUFFICIENT_EVIDENCE" else "watch"
        rows["evidence"] = _row("evidence", st, f"{cov.get('verified_calls', 0)} of {_plural(cov.get('planned_calls', 0), 'call')} gave a clear result",
                                (f"{cov['needs_review_calls']} wait for a person · " if cov.get("needs_review_calls") else "")
                                + (f"{cov['error_calls']} not completed (Vocto) · " if cov.get("error_calls") else "")
                                + f"needs {cov.get('min_required', 0):.0%}", percent=cov.get("ratio"))
    order = list(modes.AREAS)
    return [rows[a] for a in order if a in rows]


def _metrics(decision, verdicts, summary_cost: float, voice: bool) -> list[dict[str, Any]]:
    live = [v for v in verdicts if v["status"] != "ERROR"]
    own = [v for v in live if not v["scenario_id"].startswith("safety.")]
    rows: list[dict[str, Any]] = []

    def add(group, title, value, note="", band=None, **extra):
        rows.append({"group": group, "title": title, "value": value, "note": note, "band": band, **extra})

    n_pass = sum(v["status"] == "PASS" for v in live)
    add("Results", "Calls that passed", f"{n_pass} of {len(live)}", "Calls where every check that counts passed",
        rate=_pct(n_pass, len(live)), range=_range(n_pass, len(live)))
    outcome = [c for v in own for c in v.get("checks", []) if _check_type(c) == "outcome"]
    ok, n = _rate(outcome)
    if n:
        add("Results", "Task completion", f"{ok / n:.0%}", f"{ok} of {n} calls ended with the result the test expects", rate=ok / n,
            range=_range(ok, n))
    tool = [c for v in own for c in v.get("checks", []) if _check_type(c) in ("tool_called", "tool_not_called", "tool_order", "entity_fidelity")]
    ok, n = _rate(tool)
    if n:
        add("Results", "Tool use accuracy", f"{ok / n:.0%}", f"{ok} of {n} checks on which tool was used, when, and with what details", rate=ok / n,
            range=_range(ok, n))
    probe = [c for v in own for c in v.get("checks", []) if _check_type(c) == "probe"]
    ok, n = _rate(probe)
    if n:
        add("Results", "Data saved correctly", f"{ok / n:.0%}", f"{ok} of {n} checks of your system after the call", rate=ok / n,
            range=_range(ok, n))
    safety = decision.get("safety")
    if safety and safety.get("calls"):
        add("Results", "Attacks handled safely", f"{safety['passed'] / safety['calls']:.0%}",
            f"{safety['passed']} of {safety['calls']} attack calls", rate=safety["passed"] / safety["calls"],
            range=_range(safety["passed"], safety["calls"]))
    lat = decision.get("latency") or {}
    if voice and lat.get("turns_measured"):
        add("Speed", "Typical answer time (p50)", _secs(lat.get("p50_ms")), f"Limit {_secs(lat.get('p50_limit_ms'))}",
            band="good" if (lat.get("p50_ms") or 0) <= (lat.get("p50_limit_ms") or 0) else "bad")
        few = lat.get("p95_enough") is False
        add("Speed", "Slowest answers (p95)", _secs(lat.get("p95_ms")),
            f"Limit {_secs(lat.get('p95_limit_ms'))}" + (f" · too few answers to judge ({lat['turns_measured']} of "
                                                          f"{lat.get('p95_needs', 20)} needed)" if few else ""),
            band=None if few and (lat.get("p95_ms") or 0) > (lat.get("p95_limit_ms") or 0) else
            ("good" if (lat.get("p95_ms") or 0) <= (lat.get("p95_limit_ms") or 0) else "bad"))
    for q in decision.get("quality") or []:
        if q.get("value") is None:
            continue
        unit = {"ms": "", "%": "%", "wpm": " wpm", "turns": " turns", "s": " s"}.get(q.get("unit"), "")
        value = _secs(q["value"]) if q.get("unit") == "ms" else f"{q['value']:g}{unit}"
        note = q.get("about", "") + (f" Too few to judge: {(q.get('detail') or {}).get('note', '')}."
                                     if (q.get("detail") or {}).get("too_few") else "")
        add("Call quality", q["title"], value, note, band=q.get("band"))
    durations = [(v.get("metrics") or {}).get("duration_ms") for v in live if (v.get("metrics") or {}).get("duration_ms")]
    if durations:
        add("Calls", "Average call length", _secs(sum(durations) / len(durations)))
    turns = [(v.get("metrics") or {}).get("turns") for v in live if (v.get("metrics") or {}).get("turns")]
    if turns:
        add("Calls", "Average turns per call", f"{sum(turns) / len(turns):.1f}")
    ac = decision.get("costs")
    if ac:
        cur = ac["currency"]
        if ac["task_calls"]:
            add("Cost", "Agent cost per completed task", money(ac["per_task"], cur) if ac["per_task"] is not None else "No task done",
                f"What the {ac['task_calls']} calls that should have done a task cost, divided by the {ac['tasks_done']} "
                "that got it done, at your prices (Set up › Connection › Running costs)")
        add("Cost", "Agent cost per call", money(ac["per_call"], cur),
            f"{money(ac['total'], cur)} for {ac['calls']} calls ({ac['minutes']:g} minutes), at your prices")
    add("Cost", "Vocto's AI cost for this run", f"${summary_cost:.2f}",
        "Claude playing the callers and judging" + ("" if ac else "; the agent's own costs are not included (enter your prices in Set up › Connection › Running costs)"))
    if live:
        add("Cost", "Vocto's cost per call", f"${summary_cost / len(live):.3f}")
    return rows


def _coverage(info, decision, verdicts, calls: _Calls, project_scenarios, tools: Sequence[str]) -> dict[str, Any]:
    ran = sorted({v["scenario_id"] for v in verdicts})
    own_ran = [s for s in ran if not s.startswith("safety.")]
    own_all = [s for s in project_scenarios]
    not_run = [s for s in own_all if s not in own_ran]
    used: set[str] = set()
    scanned = 0
    if tools and len(verdicts) <= 400:
        for v in verdicts:
            for e in calls.events(v["test_id"]):
                if e.get("type") == "tool_request":
                    used.add(str((e.get("data") or {}).get("tool") or (e.get("data") or {}).get("name")))
            scanned += 1
    safety = decision.get("safety") or {}
    return {
        "test_cases_run": len(own_ran), "test_cases_total": len(own_all) or len(own_ran), "not_run": not_run,
        "attack_calls": safety.get("calls", 0), "attack_kinds": sorted((safety.get("packs") or {}).keys()),
        "tools_total": len(tools), "tools_used": sorted(t for t in tools if t in used),
        "tools_never_used": sorted(t for t in tools if t not in used) if scanned else [],
        "conditions": [c["label"] for c in decision.get("by_condition") or []] or ([info.get("conditions")] if info.get("conditions") else []),
        "calls_planned": (decision.get("coverage") or {}).get("planned_calls", len(verdicts)),
        "calls_verified": (decision.get("coverage") or {}).get("verified_calls"),
    }


def _method(info, summary, decision) -> list[dict[str, str]]:
    mode = modes.mode(modes.of_run(info))
    voice = info.get("mode") == "voice"
    judges = info.get("judges") or {}
    rows = [
        ("Test mode", mode["title"]),
        ("Agent", info.get("profile_name") or info.get("profile_id") or "-"),
        ("Channel", "Voice call" if voice else "Text messages"),
        ("Caller", "Rules (free dry run)" if info.get("offline") else f"Claude{(' · ' + info['caller_model']) if info.get('caller_model') else ''}"),
    ]
    if voice and info.get("speech"):
        rows.append(("Caller's speech", "Real speech (Piper voice, Whisper ears)" if info["speech"].get("engine") == "local"
                     else "Test tones (for Vocto's own tests only)"))
        rows.append(("Caller conditions", str(info.get("conditions") or "as_written").replace("_", " ")))
    rows += [
        ("Repeats", str(info.get("repeats") or "from the agent's settings")),
        ("AI judges", "Off" if judges.get("enabled") is False else
         (f"{judges.get('primary_model', '')} and {judges.get('secondary_model', '')}".strip() or "On")),
        ("Safety tests", "Included" if info.get("safety") else "Not included"),
        ("Started", summary.get("started_at") or "-"),
        ("Finished", summary.get("finished_at") or "-"),
        ("Started by", {"ci": "CI", "health": "A scheduled health check"}.get(info.get("trigger"), "A person")),
        ("Personal data", "Masked in the saved run" if info.get("pii_masked", True) else "NOT masked"),
        ("Vocto version", str(info.get("framework_version") or "-")),
        ("Run id", info.get("run_id") or summary.get("run_id") or "-"),
    ]
    bugs = (info.get("agent_options") or {}).get("bugs")
    if bugs:
        rows.insert(4, ("Bugs switched on", str(bugs)))
    return [{"label": a, "value": b} for a, b in rows]


# ---- verdict and summary --------------------------------------------------------------------

# A focused mode tests one thing, so a clean result there is not a release decision on its own.
FOCUSED_PASS = {
    "functional": "Passed the functional tests", "timing": "Passed the timing tests", "speech": "Passed the speech tests", "safety": "Passed the safety tests",
    "resilience": "Passed the resilience tests", "load": "Passed the load test",
}


def _verdict(decision, findings, bugs: str | None, mode_id: str = "full") -> dict[str, Any]:
    if not decision:
        return {"level": "no_result", "headline": LEVELS["no_result"],
                "line": "The run stopped before the end, so there is no result. The calls that finished are listed below.",
                "code": None}
    code = decision["decision"]
    blocking = [f for f in findings if f["blocks"]]
    problems = [f for f in findings if f["status"] == "problem" and not f["blocks"]]
    to_check = [f for f in findings if f["status"] in ("needs_review", "opinion")]
    note = (" Bugs were switched on on purpose for this run, so \"Not ready\" means Vocto caught them."
            if bugs else "")
    if mode_id in FOCUSED_PASS:
        note += f" This run tested {modes.mode(mode_id)['title'].lower()} only: run the Release check before a release."
    if code == "GO":
        if problems:
            return {"level": "ready_with_notes", "code": code,
                    "headline": f"{'Passed' if mode_id in FOCUSED_PASS else 'Ready'}, with {_plural(len(problems), 'thing')} to watch",
                    "line": "Nothing blocks the release. "
                            f"Look at {problems[0]['title'].lower()} before the next release." + note}
        extra = f" {_plural(len(to_check), 'AI judge opinion')} to confirm, which don't change this." if to_check else ""
        return {"level": "ready", "code": code, "headline": FOCUSED_PASS.get(mode_id, LEVELS["ready"]),
                "line": ("Every attack call was handled safely." if mode_id == "safety" else
                         "Every check that counts passed, and every key test case passed every repeat.") + extra + note}
    if code == "NO_GO":
        n = len(blocking) or 1
        first = blocking[0]["title"] if blocking else decision["reasons"][0]["message"]
        return {"level": "not_ready", "code": code, "headline": f"Not ready: {_plural(n, 'blocking issue')}",
                "line": f"Fix first: {first[0].lower() + first[1:] if first else ''}." + note}
    reason = decision["reasons"][0]["message"] if decision.get("reasons") else ""
    return {"level": "not_enough_evidence", "code": code, "headline": LEVELS["not_enough_evidence"],
            "line": (("Vocto couldn't give a clear answer yet: " + reason[0].lower() + reason[1:]) if reason else
                     "Vocto couldn't give a clear answer yet.") + note}


COND_WORDS = {"handled": "Handled", "struggled": "Struggled", "broke": "Broke", "no_result": "No result"}


def _conditions(decision, titles: Mapping[str, str]) -> list[dict[str, Any]]:
    """Results by caller condition (voice), in plain words: did the agent handle the clean line, a phone
    line, noise, a quiet or fast caller, an accent? Speech mode compares them side by side."""
    rows = decision.get("by_condition") or []
    clean = next((r for r in rows if r.get("key") in ("as_written", "clean line")), None)
    out = []
    for r in rows:
        rate = r.get("pass_rate")
        status = ("no_result" if rate is None else "handled" if rate >= 1 else "struggled" if rate >= 0.5 else "broke")
        parts = []
        if r.get("details_checked"):
            parts.append(f"{r['details_right']} of {_plural(r['details_checked'], 'detail check')} right")
        if r.get("readback_checked"):
            parts.append(f"{r['readback_right']} of {_plural(r['readback_checked'], 'read-back')} right")
        if r.get("understood_wer") is not None:
            parts.append(f"{r['understood_wer']:g}% of the caller's words misheard")
        worse = bool(clean and clean is not r and rate is not None and clean.get("pass_rate") is not None
                     and rate < clean["pass_rate"])
        out.append({
            "key": r.get("key") or r["label"], "title": r["label"], "lines": r.get("lines") or [],
            "status": status, "status_text": COND_WORDS[status],
            "result": (f"{r['passed']} of {_plural(r['passed'] + r['failed'], 'call')} passed" if rate is not None
                       else "no call finished"),
            "pass_rate": rate, "understanding": "; ".join(parts), "understood_wer": r.get("understood_wer"),
            "p50": _secs(r.get("p50_ms")) if r.get("p50_ms") is not None else None,
            "worse_than_clean": worse,
            "failed_tests": [_scenario_title(titles, s) for s in r.get("failed_tests") or []],
            "proof": (r.get("failed_calls") or [None])[0],
        })
    return out


def _failures(decision, titles: Mapping[str, str]) -> list[dict[str, Any]]:
    """Resilience failure drills, in plain words: per kind of tool failure, how the calls ended."""
    out = []
    for r in decision.get("by_failure") or []:
        n = r["passed"] + r["failed"]
        status = ("no_result" if not n else "handled" if r["failed"] == 0 else
                  "struggled" if r["passed"] else "broke")
        ended = []
        done = "done on a retry" if r.get("expected") == "recover_or_honest" else "done and confirmed"
        for key, words in (("recovered", done), ("honest", "honest that it couldn't"),
                           ("false_claims", "said something untrue about the result"),
                           ("duplicates", "made it twice"), ("unverified", "couldn't be checked")):
            if r.get(key):
                ended.append(f"{r[key]} {words}")
        out.append({
            "key": r["failure"], "title": r["title"], "about": r.get("about", ""),
            "status": status, "status_text": COND_WORDS[status],
            "result": f"{r['passed']} of {_plural(n, 'call')} passed" if n else "no call finished",
            "pass_rate": r.get("pass_rate"), "ended": "; ".join(ended),
            "recovered": r.get("recovered", 0), "honest": r.get("honest", 0),
            "false_claims": r.get("false_claims", 0), "duplicates": r.get("duplicates", 0),
            "expected": EXPECTED.get(r.get("expected", ""), ""),
            "failed_tests": [_scenario_title(titles, s) for s in r.get("failed_tests") or []],
            "proof": (r.get("failed_calls") or [None])[0],
        })
    return out


GOOD_ENDS = {"TRUE_SUCCESS", "HONEST_FAILURE", "NO_ACTION"}
UNTRUE = {"FALSE_SUCCESS", "FALSE_FAILURE"}
END_WORDS = {
    "TRUE_SUCCESS": "Done and confirmed", "HONEST_FAILURE": "Honest it couldn't", "NO_ACTION": "Nothing done, nothing promised",
    "FALSE_SUCCESS": "Said done, wasn't", "FALSE_FAILURE": "Said it failed, it worked", "SILENT_DATA_BUG": "Saved wrong or missing",
    "DUPLICATE_ACTION": "Made twice", "UNEXPECTED_ACTION": "Acted when it shouldn't", "NOT_VERIFIED": "Couldn't be checked",
}


def _functional(verdicts, titles: Mapping[str, str], intents: Mapping[str, str]) -> dict[str, Any] | None:
    """The job itself, in numbers: tasks done right, untrue claims, details saved right, other changes.
    Every task step counts (the outcome and each further step). None when no call checked a task."""
    steps: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for v in verdicts:
        if v["status"] == "ERROR":
            continue
        for b in [v.get("outcome")] + list(v.get("more_outcomes") or []):
            if b:
                steps.append((v, b))
    checked = [(v, b) for v, b in steps if b.get("class") != "NOT_VERIFIED"]
    right = sum(b.get("result") == "PASS" for _, b in checked)
    untrue = sum(b.get("class") in UNTRUE for _, b in checked)
    saves = [(v, b) for v, b in checked if b.get("expected") in ("success", "recover_or_honest") and b.get("did", {}).get("ok_calls")]
    saved_right = sum(b.get("stored") == "match" for _, b in saves)
    side = [c for v in verdicts if v["status"] != "ERROR" for c in v.get("checks", []) if c.get("check_id") == "nothing-else-changed"
            and c["result"] in ("PASS", "FAIL")]
    side_bad = [c for c in side if c["result"] == "FAIL"]
    wrong = sum(1 for v in verdicts if v["status"] != "ERROR" for c in v.get("checks", [])
                if c.get("check_id") == "only-expected-writes" and c["result"] == "FAIL")
    by_ai = sum(1 for _, b in steps if b.get("said_heard_by") == "ai")
    if not steps and not side:
        return None
    cases: dict[str, dict[str, int]] = {}
    for v, b in steps:
        row = cases.setdefault(v["scenario_id"], {})
        row[b.get("class", "NOT_VERIFIED")] = row.get(b.get("class", "NOT_VERIFIED"), 0) + 1
    per_intent: dict[str, dict[str, int]] = {}
    for v, b in checked:
        it = intents.get(v["scenario_id"]) or intents.get(drills.split(v["scenario_id"])[0] if drills.split(v["scenario_id"]) else "")
        if not it:
            continue
        r = per_intent.setdefault(it, {"passed": 0, "failed": 0})
        r["passed" if b.get("result") == "PASS" else "failed"] += 1
    return {
        "tasks": {"k": right, "n": len(checked), "range": _range(right, len(checked)), "unchecked": len(steps) - len(checked)},
        "untrue_claims": untrue,
        "saved": {"k": saved_right, "n": len(saves), "range": _range(saved_right, len(saves))},
        "other_changes": {"calls": len(side_bad), "of": len(side)},
        "wrong_tools": wrong,
        "read_by_ai": by_ai,
        "ends": [{"key": sid, "title": _scenario_title(titles, sid),
                  "parts": [{"class": k, "label": END_WORDS.get(k, k), "count": n, "good": k in GOOD_ENDS}
                            for k, n in sorted(row.items(), key=lambda kv: (kv[0] not in GOOD_ENDS, kv[0]))]}
                 for sid, row in cases.items()],
        "intents": [{"id": it, "title": it.replace("_", " ").capitalize(), **r,
                     "range": _range(r["passed"], r["passed"] + r["failed"])} for it, r in sorted(per_intent.items())],
    }


def _summary(info, decision, verdicts, findings, verdict, scorecard, voice) -> list[str]:
    if not verdicts:
        return [verdict["line"]]
    live = [v for v in verdicts if v["status"] != "ERROR"]
    # A failure drill is its test case run again, so it counts as that test case.
    own = sorted({(drills.split(v["scenario_id"]) or (v["scenario_id"],))[0] for v in verdicts
                  if not v["scenario_id"].startswith("safety.")})
    attacks = sum(v["scenario_id"].startswith("safety.") for v in verdicts)
    what = f"Vocto made {_plural(len(verdicts), 'voice call' if voice else 'text conversation')} to " \
           f"{info.get('profile_name') or 'the agent'}"
    parts = []
    if own:
        parts.append(_plural(len(own), "test case"))
    if attacks:
        parts.append(_plural(attacks, "attack call"))
    lines = [what + (f": {' and '.join(parts)}" if parts else "") + "."]
    lines.append(f"{verdict['headline']}. {verdict['line']}")
    for f in [f for f in findings if f["status"] == "problem"][:2]:
        lines.append(f"{'Blocking' if f['blocks'] else 'Problem'}: {f['title']} ({f['how_often']['text']}).")
    lat = decision.get("latency") or {}
    if voice and lat.get("turns_measured"):
        lines.append(f"Callers waited {_secs(lat.get('p50_ms'))} for a typical answer and {_secs(lat.get('p95_ms'))} "
                     f"for the slowest (limits {_secs(lat.get('p50_limit_ms'))} and {_secs(lat.get('p95_limit_ms'))}).")
    if decision.get("by_failure"):
        fails = _failures(decision, {})
        rec = sum(f["recovered"] for f in fails)
        hon = sum(f["honest"] for f in fails)
        lies = sum(f["false_claims"] for f in fails)
        dups = sum(f["duplicates"] for f in fails)
        said = [x for x in (f"got the task done in {_plural(rec, 'call')}" if rec else "",
                            f"was honest that it couldn't in {_plural(hon, 'call')}" if hon else "",
                            f"said something untrue about the result in {_plural(lies, 'call')}" if lies else "",
                            f"made the booking or order twice in {_plural(dups, 'call')}" if dups else "") if x]
        line = f"With its booking or order tool failing on purpose ({_plural(len(fails), 'kind')} of failure), the agent "
        lines.append(line + (", ".join(said[:-1]) + " and " + said[-1] if len(said) > 1 else said[0] if said
                             else "finished no call with a clear result") + ".")
    if info.get("sweep") and decision.get("by_condition"):
        conds = _conditions(decision, {})
        ok = [c for c in conds if c["status"] == "handled"]
        bad = [c for c in conds if c["status"] in ("struggled", "broke")]
        line = f"The agent handled {len(ok)} of {_plural(len(conds), 'caller condition')}"
        parts = []
        for status, words in (("broke", "it broke with"), ("struggled", "it struggled with")):
            these = [f"{c['title'].lower()} ({c['result']})" for c in bad if c["status"] == status]
            if these:
                parts.append(f"{words} {', '.join(these[:4])}")
        if parts:
            line += ": " + "; ".join(parts)
        lines.append(line + ".")
    safety = decision.get("safety")
    if safety:
        lines.append(f"{safety['passed']} of {_plural(safety['calls'], 'attack call')} were handled safely.")
    ac = decision.get("costs")
    if ac and ac["task_calls"]:
        cur = ac["currency"]
        lines.append(f"At your prices, each completed task cost {money(ac['per_task'], cur)} "
                     f"({ac['tasks_done']} of {_plural(ac['task_calls'], 'call')} got the task done)."
                     if ac["per_task"] is not None else
                     f"No call got its task done, so there is no cost per task ({money(ac['total'], cur)} spent on {_plural(ac['calls'], 'call')}).")
    load = decision.get("load")
    if load:
        lines.append(f"It handled up to {_plural(load.get('capacity') or 0, 'call')} at once within your limits.")
    base = decision.get("baseline")
    if base:
        lines.append(f"Against the baseline: {base.get('regressions', 0) + base.get('worse', 0)} worse, "
                     f"{base.get('better', 0)} better.")
    to_check = [f for f in findings if f["status"] in ("needs_review",)]
    if to_check:
        lines.append(f"{_plural(len(to_check), 'item')} need a person to decide (the AI judges disagreed).")
    errors = len(verdicts) - len(live)
    if errors:
        lines.append(f"{_plural(errors, 'call')} could not be completed by Vocto and don't count against the agent.")
    return lines


# ---- the report ------------------------------------------------------------------------------

def build(run_dir: Path, summary: Mapping[str, Any] | None = None, titles: Mapping[str, str] | None = None,
          project_scenarios: Sequence[str] = (), tools: Sequence[str] = (),
          faulty: Iterable[str] = (), intents: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The readiness report of one run (see the module docstring).

    summary: data.run_summary of the run (dates, cost); titles: scenario id -> title; project_scenarios:
    the agent's own test case ids in this channel (for coverage); tools: the agent's tool names;
    faulty: ids of test cases that break a tool on purpose.
    """
    info = _safe(run_dir / "run.json") or {}
    decision = _safe(run_dir / "decision.json")
    summary = dict(summary or {})
    titles = dict(titles or {})
    final_ids = [c["test_id"] for c in (decision or {}).get("calls", [])]
    verdicts = []
    for tid in final_ids:
        v = _safe(run_dir / "calls" / tid / "verdict.json")
        if v:
            verdicts.append(v)
    if not decision:
        verdicts = [v for v in (_safe(p / "verdict.json") for p in sorted((run_dir / "calls").glob("t-*"))) if v] \
            if (run_dir / "calls").is_dir() else []
    voice = info.get("mode") == "voice" or any("latency_turns" in (v.get("metrics") or {}) for v in verdicts)
    faulty = set(faulty)
    for v in verdicts:          # failure drills break a tool on purpose, and are named after their test case
        if (parts := drills.split(v["scenario_id"])):
            faulty.add(v["scenario_id"])
            base, failure = parts
            titles.setdefault(v["scenario_id"], f"{titles.get(base, base)} · {drills.FAILURES[failure]['title']}")
    calls = _Calls(run_dir, verdicts)
    mode_id = modes.of_run(info)
    m = modes.mode(mode_id)
    critical_failed = set()
    for r in (decision or {}).get("reasons", []):
        if r["rule"] == "reliability":
            critical_failed.update(v["scenario_id"] for v in verdicts if v["test_id"] in (r.get("refs") or []))
    raw: list[_Finding] = []
    if decision or verdicts:
        raw += _check_findings(verdicts, calls, titles, critical_failed, voice, faulty)
        if (e := _error_finding(verdicts, calls, titles)):
            raw.append(e)
    if decision:
        if (lf := _latency_finding(decision, verdicts, calls)):
            raw.append(lf)
        raw += _metric_findings(decision, verdicts, calls)
        raw += _reason_findings(decision)
    if not decision:
        for f in raw:   # no decision was made, so nothing "blocks"; the severity still says how bad it is
            f.blocks = False
    findings = _finish(raw, titles)
    bugs = (info.get("agent_options") or {}).get("bugs")
    verdict = _verdict(decision, findings, bugs, mode_id)
    scorecard = _scorecard(decision, verdicts, findings, info, voice, faulty) if decision else []
    lead = [a for a in m["areas"] if any(r["area"] == a for r in scorecard)]
    scorecard = [r for a in lead for r in scorecard if r["area"] == a] + [r for r in scorecard if r["area"] not in lead]
    cost = float(summary.get("cost_usd") or 0)
    return {
        "version": VERSION,
        "run_id": info.get("run_id") or run_dir.name,
        "mode": {"id": mode_id, "title": m["title"], "question": m["question"]},
        "agent": info.get("profile_name") or info.get("profile_id"),
        "channel": "voice" if voice else "text",
        "verdict": verdict,
        "summary": _summary(info, decision or {}, verdicts, findings, verdict, scorecard, voice),
        "scorecard": scorecard,
        "findings": findings,
        "counts": {
            "blocking": sum(f["blocks"] for f in findings),
            "problems": sum(f["status"] == "problem" for f in findings),
            "to_review": sum(f["status"] == "needs_review" for f in findings),
            "opinions": sum(f["status"] == "opinion" for f in findings),
            "evidence": sum(f["status"] == "evidence" for f in findings),
        },
        "metrics": _metrics(decision or {}, verdicts, cost, voice) if verdicts else [],
        "coverage": _coverage(info, decision or {}, verdicts, calls, project_scenarios, tools),
        "conditions": _conditions(decision or {}, titles) if voice else [],
        "failures": _failures(decision or {}, titles),
        "functional": _functional(verdicts, titles, intents or {}),
        "method": _method(info, summary, decision or {}),
    }


def to_csv(report: Mapping[str, Any]) -> str:
    """The findings, scorecard and numbers as one CSV (opens in Excel / Google Sheets)."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["section", "id", "title", "area", "status", "severity", "blocks release", "how often", "who to look at",
                "what happened", "expected", "where to look", "test cases", "calls"])
    v = report["verdict"]
    w.writerow(["verdict", "", v["headline"], "", v["level"], "", "", "", "", v["line"], "", "", "", ""])
    for row in report["scorecard"]:
        w.writerow(["scorecard", "", row["title"], row["area"], row["status_text"], "", "", "", "", row["value"],
                    "", row["detail"], "", ""])
    for f in report["findings"]:
        w.writerow(["finding", f["id"], f["title"], f["area_title"], f["status"], f["severity"],
                    "yes" if f["blocks"] else "no", f["how_often"]["text"], f["cause_text"], f["what"], f["expected"],
                    f["fix"], "; ".join(t["title"] for t in f["tests"]), " ".join(f["calls"])])
    for m in report["metrics"]:
        w.writerow(["metric", "", m["title"], m["group"], m.get("band") or "", "", "", "", "", m["value"], "", m["note"], "", ""])
    return buf.getvalue()
