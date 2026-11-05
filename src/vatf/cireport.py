"""Reports for CI (Phase 6): JUnit XML and a Markdown summary of one run.

JUnit XML is read by GitHub Actions (test reporter actions), GitLab (artifacts:reports:junit),
Jenkins (junit step) and Azure DevOps (PublishTestResults). One test case per final call, grouped
by test (scenario); the decision and the comparison with the baseline are an extra test case each,
so a CI page shows why the run failed even when every call passed (a regression in response time).

The Markdown summary is what a person reads first. On GitHub Actions it is also appended to the
job summary ($GITHUB_STEP_SUMMARY), so it shows on the run's page with nothing else to set up.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Mapping, Sequence

TITLE = {"GO": "GO", "NO_GO": "NO-GO", "NO_GO_INSUFFICIENT_EVIDENCE": "NOT ENOUGH EVIDENCE"}
MARK = {"GO": "✅", "NO_GO": "❌", "NO_GO_INSUFFICIENT_EVIDENCE": "⚠️"}


def _failed_checks(v: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [c for c in v.get("checks", []) if c["result"] in ("FAIL", "ERROR") and c.get("can_block", True)]


def junit_xml(decision: Mapping[str, Any], verdicts: Sequence[Mapping[str, Any]],
              comparison: Mapping[str, Any] | None = None, name: str = "") -> str:
    by_sc: dict[str, list[Mapping[str, Any]]] = {}
    for v in verdicts:
        by_sc.setdefault(v["scenario_id"], []).append(v)
    root = ET.Element("testsuites", name=f"vocto {name or decision.get('profile_id', '')}".strip())
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    if decision.get("load"):
        # A load run: one test case per stage (the calls themselves are many, and contention makes
        # single-call failures expected; the stage limits are what decide).
        by_sc = {}
        suite = ET.SubElement(root, "testsuite", name="load test")
        counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
        for st in decision["load"]["stages"]:
            case = ET.SubElement(suite, "testcase", classname="load",
                                 name=f"{st['name']} ({st['level']} at once, {st['finished']} calls)")
            counts["tests"] += 1
            if st["meets_limits"] is False:
                counts["failures"] += 1
                msgs = [r["message"] for r in decision["reasons"] if r["message"].startswith(st["name"] + " (")]
                el = ET.SubElement(case, "failure", message=(msgs[0] if msgs else "a limit was broken")[:500], type="load")
                el.text = "\n".join(msgs)
            elif not st["trusted"]:
                counts["skipped"] += 1
                ET.SubElement(case, "skipped", message="this stage could not be judged (see the run's reasons)")
        for k, n in counts.items():
            suite.set(k, str(n))
            totals[k] += n
    for sid, vs in by_sc.items():
        suite = ET.SubElement(root, "testsuite", name=sid)
        counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
        for v in sorted(vs, key=lambda v: v.get("repeat", 0)):
            case = ET.SubElement(suite, "testcase", classname=sid, name=f"repeat {v.get('repeat', 1)} ({v['test_id']})",
                                 time=f"{((v.get('metrics') or {}).get('duration_ms') or 0) / 1000:.3f}")
            counts["tests"] += 1
            status = v["status"]
            if status == "FAIL":
                counts["failures"] += 1
                bad = _failed_checks(v)
                el = ET.SubElement(case, "failure", message=(bad[0]["message"] if bad else "the call failed")[:500],
                                   type=bad[0]["check_id"] if bad else "FAIL")
                el.text = "\n".join(f"[{c['severity']}] {c['check_id']}: {c['message']}" for c in bad)
            elif status == "ERROR":
                counts["errors"] += 1
                ET.SubElement(case, "error", message="the call could not run (harness error)", type="ERROR")
            elif status in ("NOT_VERIFIED", "NEEDS_REVIEW"):
                counts["skipped"] += 1
                ET.SubElement(case, "skipped", message="needs a person's review" if status == "NEEDS_REVIEW"
                              else "the result could not be verified")
        for k, n in counts.items():
            suite.set(k, str(n))
            totals[k] += n
    gate = ET.SubElement(root, "testsuite", name="release decision")
    g = {"tests": 1, "failures": 0, "errors": 0, "skipped": 0}
    case = ET.SubElement(gate, "testcase", classname="release", name="decision")
    if decision["decision"] != "GO":
        g["failures"] += 1
        el = ET.SubElement(case, "failure", message=f"{TITLE[decision['decision']]}: {decision['reasons'][0]['message']}"[:500],
                           type=decision["decision"])
        el.text = "\n".join(f"[{r['rule']}] {r['message']}" for r in decision["reasons"])
    if comparison:
        g["tests"] += 1
        case = ET.SubElement(gate, "testcase", classname="release", name="vs baseline")
        blocking = [c for c in comparison["changes"] if c["blocks"]]
        if blocking:
            g["failures"] += 1
            el = ET.SubElement(case, "failure", message=f"{len(blocking)} change(s) for the worse block the release",
                               type="regression")
            el.text = "\n".join(c["message"] for c in blocking)
        else:
            worse = [c for c in comparison["changes"] if c["direction"] in ("regression", "worse")]
            if worse:
                ET.SubElement(case, "system-out").text = "\n".join(c["message"] for c in worse)
    for k, n in g.items():
        gate.set(k, str(n))
        totals[k] += n
    for k, n in totals.items():
        root.set(k, str(n))
    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


def _cell(s: Any) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def markdown(decision: Mapping[str, Any], info: Mapping[str, Any], comparison: Mapping[str, Any] | None = None) -> str:
    d = decision
    name = info.get("profile_name") or d.get("profile_id")
    out = [f"## {MARK[d['decision']]} {_cell(name)}: {TITLE[d['decision']]}", ""]
    how = [f"run `{d['run_id']}`", info.get("mode") or ""]
    if info.get("conditions") and info["conditions"] != "as_written":
        how.append(f"conditions {info['conditions']}")
    if info.get("offline"):
        how.append("offline dry run")
    out += [" · ".join(h for h in how if h), ""]
    out += [f"- {_cell(r['message'])}" for r in d["reasons"]] + [""]
    cov = d.get("coverage") or {}
    rows = [("Calls verified", f"{cov.get('verified_calls', 0)} of {cov.get('planned_calls', 0)}")]
    for section, s in (d.get("scorecard") or {}).items():
        rows.append((section.title(), "n/a" if s["score"] is None else f"{s['score']:.0%}"))
    lat = d.get("latency") or {}
    if lat.get("turns_measured"):
        rows.append(("Response time", f"p50 {lat['p50_ms']} ms, p95 {lat['p95_ms']} ms "
                                      f"(limits {lat['p50_limit_ms']} / {lat['p95_limit_ms']} ms)"))
    load = d.get("load")
    if load:
        rows = [("Load", f"{load['shape']} up to {load['calls_at_once']} calls at once, {load['calls']} calls"),
                ("Capacity", f"{load['capacity']} calls at once within the limits")]
    out += ["| | |", "|---|---|"] + [f"| {a} | {_cell(b)} |" for a, b in rows] + [""]
    if load:
        out += ["| Stage | At once | Calls | Failed | p50 | p95 | Pass rate | Result |", "|---|---|---|---|---|---|---|---|"]
        for st in load["stages"]:
            res = "met" if st["meets_limits"] else ("not judged" if not st["trusted"] else "**broke a limit**")
            out.append(f"| {_cell(st['name'])} | {st['level']} | {st['finished']} | "
                       f"{'-' if st['failed_pct'] is None else str(st['failed_pct']) + '%'} | "
                       f"{'-' if st['p50_ms'] is None else str(st['p50_ms']) + ' ms'} | "
                       f"{'-' if st['p95_ms'] is None else str(st['p95_ms']) + ' ms'} | "
                       f"{'-' if st['pass_rate'] is None else str(st['pass_rate']) + '%'} | {res} |")
        out.append("")
    tests = [] if load else [r for r in d.get("reliability", []) if r["failed"] or r["unverified"]]
    if tests:
        out += ["**Tests with failures or unverified calls**", "", "| Test | Passed | Failed | Unverified |",
                "|---|---|---|---|"]
        out += [f"| {_cell(r['scenario_id'])}{' (critical)' if r['criticality'] == 'critical' else ''} | {r['passed']}/{r['k']} "
                f"| {r['failed']} | {r['unverified']} |" for r in tests] + [""]
    if comparison:
        b = comparison["baseline"]
        worse = [c for c in comparison["changes"] if c["direction"] in ("regression", "worse")]
        better = [c for c in comparison["changes"] if c["direction"] == "better"]
        out += [f"**Vs baseline** `{b['run_id']}`" + ("" if comparison["like_for_like"] else
                " (not like-for-like: " + ", ".join(x["title"] for x in comparison["differences"]
                                                    if x["field"] in ("mode", "conditions", "speech_engine", "offline",
                                                                      "kind", "load_shape"))
                + " differ, so nothing here blocks)"), ""]
        if not worse and not better:
            out += ["No changes beyond the limits.", ""]
        if worse:
            out += ["| Worse | Blocks |", "|---|---|"] + [f"| {_cell(c['message'])} | {'yes' if c['blocks'] else 'no'} |"
                                                       for c in worse] + [""]
        if better:
            out += [f"Better: " + " ".join(_cell(c["message"]) for c in better[:8]), ""]
    out.append(f"Exit code {d['exit_code']} (0 GO, 1 NO-GO, 2 not enough evidence).")
    return "\n".join(out) + "\n"


def write_ci_reports(decision: Mapping[str, Any], verdicts: Sequence[Mapping[str, Any]], info: Mapping[str, Any],
                     comparison: Mapping[str, Any] | None, junit: str | None = None,
                     summary: str | None = None) -> list[str]:
    """Writes the reports asked for; the Markdown also goes to $GITHUB_STEP_SUMMARY when it is set."""
    written = []
    if junit:
        path = Path(junit)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(junit_xml(decision, verdicts, comparison, info.get("profile_name") or ""), encoding="utf-8")
        written.append(str(path))
    md = markdown(decision, info, comparison)
    if summary:
        path = Path(summary)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(md, encoding="utf-8")
        written.append(str(path))
    step = os.environ.get("GITHUB_STEP_SUMMARY")
    if step:
        try:
            with open(step, "a", encoding="utf-8") as f:
                f.write(md + "\n")
        except OSError:
            pass
    return written
