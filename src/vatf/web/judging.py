"""What the dashboard's Judges and Labels pages read and write (Phase 3): each judge item of a project
with its calibration and recent results, the queue of calls waiting for a person's labels, and saving
labels (which re-decides the run, relabel.py)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .. import rubrics
from ..calibration import (calibration, corrected_pass_rate, example_keys, label_key, load_labels, panel_result,
                           thresholds)
from ..judges import judge_settings
from ..loader import ProjectError, load_profile, load_project
from ..relabel import label_call
from . import data, setup

RECENT_RUNS = 20      # judge results shown per item come from this many of the project's latest runs
QUEUE_RUNS = 40       # calls offered for labelling come from this many runs


def _project(root: Path, key: str) -> tuple[dict[str, Any], Path]:
    p = data.project_by_key(root, key)
    return p, root / p["path"]


def _items(folder: Path) -> tuple[dict[str, Any], list[tuple[rubrics.RubricItem, list[str]]], list[str]]:
    """The main profile, every judge item with the scenarios that use it, and load problems."""
    problems: list[str] = []
    try:
        project = load_project(folder)
        profile, scenarios = project.profile, project.scenarios
    except ProjectError as e:
        problems.append(str(e))
        profile, scenarios = load_profile(folder, "profile.yaml")[0], []
    used: dict[str, tuple[rubrics.RubricItem, list[str]]] = {
        i.id: (i, []) for i in rubrics.project_items(profile)}
    for sc in scenarios:
        for item in rubrics.items_for_call(profile, sc):
            used.setdefault(item.id, (item, []))[1].append(sc["id"])
    return profile, list(used.values()), problems


def _project_runs(runs_dir: Path, path: str, active: set[str], limit: int) -> list[dict[str, Any]]:
    return [r for r in data.list_runs(runs_dir, active) if r.get("project") == path and r.get("decision")][:limit]


def _final_verdicts(runs_dir: Path, run: Mapping[str, Any]):
    folder = runs_dir / run["run_id"]
    decision = data._safe_json(folder / "decision.json") or {}
    for c in decision.get("calls", []):
        v = data._safe_json(folder / "calls" / c["test_id"] / "verdict.json")
        if v:
            yield v


def _judged(v: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The call's judge items the judges really scored (not skipped)."""
    return [c for c in v.get("checks", []) if c.get("origin") == "judge" and (c.get("judge") or {}).get("votes")]


def judges_page(root: Path, runs_dir: Path, key: str, active: set[str] = frozenset()) -> dict[str, Any]:
    project, folder = _project(root, key)
    profile, items, problems = _items(folder)
    status = calibration(folder, profile, [i for i, _ in items])
    recent: dict[str, dict[str, int]] = {i.id: {"PASS": 0, "FAIL": 0, "NEEDS_REVIEW": 0, "NOT_APPLICABLE": 0,
                                                "NOT_VERIFIED": 0} for i, _ in items}
    runs = _project_runs(runs_dir, project["path"], active, RECENT_RUNS)
    judged_runs = 0
    for r in runs:
        seen = False
        for v in _final_verdicts(runs_dir, r):
            for c in _judged(v):
                if c["check_id"] in recent and c["result"] in recent[c["check_id"]]:
                    recent[c["check_id"]][c["result"]] += 1
                    seen = True
        judged_runs += seen
    th = thresholds(profile)
    j = profile.get("judges") or {}
    settings = judge_settings(profile)
    queue = label_queue(root, runs_dir, key, active)
    labels = load_labels(folder)
    teach = _disagreements(profile, {i.id: i for i, _ in items}, labels)

    def row(item, scs):
        st, rc = status[item.id], recent[item.id]
        judged = rc["PASS"] + rc["FAIL"]
        rate = corrected_pass_rate(rc["PASS"] / judged if judged else None, st["tpr"], st["tnr"]) if st["can_block"] else None
        return {**item.to_dict(), "scenarios": scs, "calibration": st, "recent": rc, "true_pass_rate": rate,
                "examples": [{"said": a, "answer": b, "why": c} for a, b, c in item.examples],
                "disagreements": teach.get(item.id, [])}

    return {
        "project": {k: project[k] for k in ("key", "name", "domain", "example")},
        "settings": {**settings, "items": j.get("items"), "custom": j.get("custom", [])},
        "thresholds": th,
        "items": [row(item, scs) for item, scs in items],
        "summary": {"items": len(items), "calibrated": sum(s["can_block"] for s in status.values()),
                    "labelled_calls": len({lb["test_id"] for lb in labels.values()}),
                    "to_teach": sum(len(v) for v in teach.values()),
                    "to_review": sum(q["needs_review"] > 0 for q in queue), "to_label": len(queue),
                    "judged_runs": judged_runs},
        "catalog": rubrics.catalog(),
        "suggested_pack": rubrics.pack_for_domain(profile.get("domain")),
        "default_items": rubrics.default_item_ids(profile.get("domain")),
        "problems": problems,
    }


TEACH_LIMIT = 5      # disagreements offered per item as worked examples


def _disagreements(profile: Mapping[str, Any], items: Mapping[str, rubrics.RubricItem],
                   labels: Mapping[str, Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Per item, the labelled calls where the judges decided otherwise than the person, newest first:
    the cases worth turning into worked examples (or into a clearer policy line)."""
    used = example_keys(profile)
    out: dict[str, list[dict[str, Any]]] = {}
    for key, lb in sorted(labels.items(), key=lambda kv: kv[1].get("at", ""), reverse=True):
        item = items.get(lb.get("item", ""))
        # Any wording: what a person said about the agent's words still teaches after the item changed.
        if item is None or lb["label"] not in ("PASS", "FAIL"):
            continue
        judged = panel_result(lb)
        if judged == lb["label"] or key in used or not lb.get("excerpt"):
            continue
        rows = out.setdefault(item.id, [])
        if len(rows) < TEACH_LIMIT:
            rows.append({"key": key, "run_id": lb["run_id"], "test_id": lb["test_id"], "label": lb["label"],
                         "judges": judged, "excerpt": lb["excerpt"], "note": lb.get("note", ""),
                         "reasons": lb.get("reasons", {})})
    return out


def teach(root: Path, key: str, body: Mapping[str, Any]) -> dict[str, Any]:
    """Turn a labelled call into a worked example for its item (add), or take one away (remove).
    Written to the profile's judges.examples; the item's wording changes, so trust is measured afresh."""
    project, folder = _project(root, key)
    file = "profile.yaml"
    raw = data._yaml(folder / file) or {}
    judges = dict(raw.get("judges") or {})
    examples = [dict(e) for e in judges.get("examples") or []]
    label = str(body.get("label_key", ""))
    if body.get("remove"):
        examples = [e for e in examples if not (label and e.get("from") == label) and
                    not (e.get("item") == body.get("item") and e.get("said") == body.get("said"))]
    else:
        lb = load_labels(folder).get(label)
        if lb is None:
            raise ValueError("that label no longer exists")
        why = str(body.get("why") or lb.get("note") or "").strip()
        if not why:
            right = [r for j, r in (lb.get("reasons") or {}).items() if lb.get(j) == lb["label"] and r]
            why = right[0] if right else ""
        examples = [e for e in examples if e.get("from") != label]
        examples.append({"item": lb["item"], "said": lb.get("excerpt") or "", "answer": lb["label"],
                         **({"why": why} if why else {}), "from": label})
    if examples:
        judges["examples"] = examples
    else:
        judges.pop("examples", None)
    out = setup.save_profile(root, project["path"], file, {"judges": judges})
    if not out.get("saved"):
        raise ValueError("; ".join(out.get("problems") or ["could not save the example"]))
    return {"saved": True}


def label_queue(root: Path, runs_dir: Path, key: str, active: set[str] = frozenset()) -> list[dict[str, Any]]:
    """Calls with judged items that still lack a person's label, the most useful first: judges that
    disagree, then a judge failure, then calls from bug-switch runs (they hold the failing examples
    calibration needs), then the rest, newest first within each group."""
    project, folder = _project(root, key)
    labels = load_labels(folder)
    out = []
    for r in _project_runs(runs_dir, project["path"], active, QUEUE_RUNS):
        bugs = (r.get("agent_options") or {}).get("bugs") or ""
        for v in _final_verdicts(runs_dir, r):
            judged = [c for c in _judged(v) if _relevant(c)]
            if not judged:
                continue
            done = [c for c in judged if _label_for(labels, v["test_id"], c)]
            if len(done) == len(judged):
                continue
            review = sum(c["result"] == "NEEDS_REVIEW" for c in judged)
            fails = sum(c["result"] == "FAIL" for c in judged)
            reason = ("the judges disagree" if review else "a judge found a problem" if fails
                      else "from a bug test" if bugs else "a normal call")
            out.append({"run_id": v["run_id"], "test_id": v["test_id"], "scenario_id": v["scenario_id"],
                        "repeat": v["repeat"], "status": v["status"], "started_at": r.get("started_at"),
                        "bugs": bugs, "items": len(judged), "labelled": len(done), "needs_review": review,
                        "fails": fails, "reason": reason,
                        "rank": 0 if review else 1 if fails else 2 if bugs else 3})
    out.sort(key=lambda x: (x["rank"], -(int(x["run_id"][2:10] + x["run_id"][11:17]))))
    return out


def _relevant(check: Mapping[str, Any]) -> bool:
    """Worth a person's label: at least one judge thought the item came up in the call."""
    return any(v.get("result") != "NOT_APPLICABLE" for v in (check.get("judge") or {}).get("votes", []))


def _label_for(labels: Mapping[str, Mapping[str, Any]], test_id: str, check: Mapping[str, Any]) -> str | None:
    lb = labels.get(label_key(test_id, check["check_id"]))
    return lb["label"] if lb and lb.get("item_hash") == (check.get("judge") or {}).get("item_hash") else None


def call_labels(root: Path, runs_dir: Path, run_id: str, test_id: str) -> dict[str, Any]:
    """The person's current labels for one call, and which of its items are worth labelling."""
    info = data._safe_json(data._run_dir(runs_dir, run_id) / "run.json") or {}
    v = data._safe_json(data._call_dir(runs_dir, run_id, test_id) / "verdict.json") or {}
    folder = root / info.get("project", "")
    labels = load_labels(folder) if info.get("project") and folder.is_dir() else {}
    judged = _judged(v)
    return {"labels": {c["check_id"]: _label_for(labels, test_id, c) for c in judged},
            "relevant": [c["check_id"] for c in judged if _relevant(c)]}


def save_labels(root: Path, runs_dir: Path, body: Mapping[str, Any]) -> dict[str, Any]:
    run_id, test_id = str(body.get("run_id", "")), str(body.get("test_id", ""))
    data._call_dir(runs_dir, run_id, test_id)          # NotFound for a bad id
    choices = body.get("labels") or {}
    if not isinstance(choices, Mapping) or not choices:
        raise ValueError("choose at least one label")
    notes = body.get("notes") if isinstance(body.get("notes"), Mapping) else {}
    out = label_call(root, runs_dir, run_id, test_id, dict(choices), by=str(body.get("by", "")), notes=notes)
    return {"verdict": out["verdict"], "decision": out["decision"]["decision"],
            **call_labels(root, runs_dir, run_id, test_id)}
