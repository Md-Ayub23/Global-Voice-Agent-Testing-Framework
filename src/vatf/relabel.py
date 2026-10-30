"""A person labels a call's judge items: the call's verdict, the run's decision, its report and its
transcript are worked out again with the label in place (Phase 3).

A label decides that item for that call (a person outranks both judges), so a NEEDS_REVIEW call stops
holding the release once it is labelled. Every other judge item of the run is updated to the item's
current calibration as well, so the run shows what the labels made of the judges so far.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from .calibration import add_labels, calibration, label_key, load_labels
from .decision import LocalDecisionEngine
from .evidence import LocalEvidenceStore, utc_now
from .judges import combine, transcript
from .loader import load_project
from .report import write_report
from .rubrics import items_for_call
from .story import call_story_text, load_events, run_story_text
from .util import read_json, write_json
from .verdict import status_of


def run_project(root: Path, runs_dir: Path, run_id: str):
    """The project a run was made with (its folder and profile file, from run.json)."""
    info = read_json(runs_dir / run_id / "run.json")
    folder = Path(info["project"])
    folder = folder if folder.is_absolute() else root / folder
    return load_project(folder, info.get("profile_file") or "profile.yaml"), info


EXCERPT_LINES, EXCERPT_CHARS = 3, 420


def excerpts(verdict: Mapping[str, Any], events: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """{item id: the call lines its judges pointed at}, kept with a label so it can become an example."""
    text, _ = transcript(events)
    lines = {}
    for line in text.splitlines():
        m = re.match(r"\[#(\d+)\] ", line)
        if m:
            lines[int(m.group(1))] = line[m.end():]
    out = {}
    for c in verdict.get("checks", []):
        if c.get("origin") != "judge":
            continue
        cited = [e["event"] for e in c.get("evidence", []) if e.get("event") in lines][:EXCERPT_LINES]
        if cited:
            out[c["check_id"]] = " / ".join(lines[n] for n in cited)[:EXCERPT_CHARS]
    return out


def label_call(root: Path, runs_dir: Path, run_id: str, test_id: str, choices: Mapping[str, str | None],
               by: str = "", notes: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Saves the labels, then re-evaluates the run. Returns the call's new verdict and the run's decision."""
    project, _ = run_project(root, runs_dir, run_id)
    store = LocalEvidenceStore(runs_dir)
    call_dir = store.call_dir(run_id, test_id)
    verdict = read_json(call_dir / "verdict.json")
    said = excerpts(verdict, load_events(call_dir))
    extra = {k: {"excerpt": said.get(k, ""), "note": str((notes or {}).get(k) or "").strip()[:300]} for k in choices}
    add_labels(project.folder, run_id, test_id, verdict["checks"], choices, by, extra)
    decision = reevaluate_run(root, runs_dir, run_id)
    return {"verdict": read_json(store.call_dir(run_id, test_id) / "verdict.json"), "decision": decision}


def apply_judging(verdict: dict[str, Any], labels: Mapping[str, Mapping[str, Any]],
                  status: Mapping[str, Mapping[str, Any]], harness_error: bool) -> dict[str, Any]:
    """The verdict with every judge item worked out again from its votes, a person's label and the
    item's calibration. Hard checks are not touched."""
    for c in verdict["checks"]:
        j = c.get("judge")
        if c.get("origin") != "judge" or j is None or not j.get("votes"):
            continue  # not judged at all (offline, switched off): nothing a label could change
        label = labels.get(label_key(verdict["test_id"], c["check_id"]))
        human = label["label"] if label and label.get("item_hash") == j.get("item_hash") else None
        result, message = combine(j["votes"], human, safety=c.get("section") == "safety")
        st = status.get(c["check_id"]) or {}
        c["result"], c["message"] = result, message
        c["can_block"] = bool(st.get("can_block")) or bool(human)
        j["applicable"] = result != "NOT_APPLICABLE"
        j["kappa"] = st.get("kappa")
        j["tpr"], j["tnr"] = st.get("tpr"), st.get("tnr")
        j["labelled_calls"] = st.get("labelled_calls", 0)
        j["calibration"] = st.get("status", j.get("calibration", ""))
        if human:
            j["human"] = human
        else:
            j.pop("human", None)
    verdict["status"] = status_of(verdict["checks"], harness_error)
    return verdict


def reevaluate_run(root: Path, runs_dir: Path, run_id: str) -> dict[str, Any]:
    project, info = run_project(root, runs_dir, run_id)
    run_dir = runs_dir / run_id
    store = LocalEvidenceStore(runs_dir)
    old = read_json(run_dir / "decision.json")
    scenario_ids = [r["scenario_id"] for r in old["reliability"]]
    scenarios = [s for s in project.scenarios if s["id"] in scenario_ids]
    items = {i.id: i for sc in scenarios for i in items_for_call(project.profile, sc)}
    status = calibration(project.folder, project.profile, list(items.values()))
    labels = load_labels(project.folder)

    final_ids = [c["test_id"] for c in old["calls"]]
    verdicts: dict[str, dict[str, Any]] = {}
    for call_dir in sorted((run_dir / "calls").iterdir()):
        if not (call_dir / "verdict.json").exists():
            continue
        v = read_json(call_dir / "verdict.json")
        record = read_json(call_dir / "call.json")
        v = apply_judging(v, labels, status, record.get("status") == "harness_error")
        store.write_verdict(run_id, v["test_id"], v)
        verdicts[v["test_id"]] = v
    finals = [verdicts[t] for t in final_ids if t in verdicts]
    errors = [v for t, v in verdicts.items() if t not in final_ids]

    decision = LocalDecisionEngine().decide(run_id, finals, scenarios, project.profile)
    decision["created_at"] = old.get("created_at", decision["created_at"])
    decision["relabelled_at"] = utc_now()
    write_json(run_dir / "decision.json", decision)
    info.update(decision=decision["decision"], exit_code=decision["exit_code"])
    write_json(run_dir / "run.json", info)

    shown = {**decision, "_pii_masked": info.get("pii_masked", True), "_mode": info.get("mode")}
    write_report(shown, finals, errors, run_dir, store)

    def with_events(vs):
        out = []
        for v in vs:
            call_dir = store.call_dir(run_id, v["test_id"])
            events = load_events(call_dir)
            (call_dir / "story.txt").write_text(call_story_text(v, events), encoding="utf-8")
            out.append((v, events))
        return out

    (run_dir / "transcript.txt").write_text(run_story_text(shown, with_events(finals), with_events(errors)),
                                            encoding="utf-8")
    return decision
