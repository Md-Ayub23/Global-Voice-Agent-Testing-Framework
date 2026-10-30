"""Judge calibration (Phase 3): human labels, and how well the judges agree with them.

A person labels a call's judge items PASS, FAIL or NOT_APPLICABLE. Each label keeps a copy of the
judges' votes, so calibration still works after the run folder is deleted. Per item, the judges'
combined result (what the run actually used) is measured against the person, both ways:

  - catches real failures (TPR): of the calls the person failed, how many the judges failed too;
  - passes good calls (TNR): of the calls the person passed, how many the judges passed too.

One agreement score (kappa) hides which way a judge is wrong and swings wildly when nearly every call
passes, so it is shown but no longer decides. An item may block a release (it is "trusted") only with
at least `judge_min_labels` labels for its current wording, `judge_min_fails` of them failures and as
many passes, and both rates at or above `judge_min_tpr` / `judge_min_tnr`. Until then it is an opinion.
Labels that became worked examples in the judges' prompt (judges.examples) teach and are not measured.

Labels live in the project folder (labels.json), next to the profile they were made for.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .evidence import utc_now
from .judges import combine
from .rubrics import RubricItem
from .util import read_json, write_json

LABELS_FILE = "labels.json"
CHOICES = ("PASS", "FAIL", "NOT_APPLICABLE")
DEFAULT_MIN_KAPPA = 0.6      # shown for information; no longer decides
DEFAULT_MIN_LABELS = 20
DEFAULT_MIN_FAILS = 5        # failures and passes each, so both rates mean something
DEFAULT_MIN_RATE = 0.9
JUDGE_KEYS = ("primary", "secondary", "tiebreak")


def labels_path(project_folder: Path) -> Path:
    return Path(project_folder) / LABELS_FILE


def load_labels(project_folder: Path) -> dict[str, dict[str, Any]]:
    """{"<test_id>:<item_id>": label record}"""
    path = labels_path(project_folder)
    if not path.exists():
        return {}
    data = read_json(path)
    return dict(data.get("labels", {}))


def save_labels(project_folder: Path, labels: Mapping[str, Mapping[str, Any]]) -> None:
    write_json(labels_path(project_folder), {"schema_version": "0.1", "kind": "judge_labels",
                                             "labels": dict(sorted(labels.items()))})


def label_key(test_id: str, item_id: str) -> str:
    return f"{test_id}:{item_id}"


def vote_of(check: Mapping[str, Any], judge: str) -> str | None:
    for v in (check.get("judge") or {}).get("votes", []):
        if v.get("judge") == judge:
            return v.get("result")
    return None


def add_labels(project_folder: Path, run_id: str, test_id: str, checks: Sequence[Mapping[str, Any]],
               choices: Mapping[str, str | None], by: str = "",
               extra: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    """Save a person's labels for one call. `choices` maps item id -> PASS | FAIL | NOT_APPLICABLE,
    or None to remove a label. `checks` are the call's judge results (they carry the votes and hash).
    `extra` per item: what the agent said there (excerpt), the judges' reasons and the person's note,
    kept so a label can later become a worked example for the judges."""
    by_item = {c["check_id"]: c for c in checks if c.get("origin") == "judge"}
    labels = load_labels(project_folder)
    for item_id, choice in choices.items():
        if item_id not in by_item:
            raise KeyError(f"this call has no judge item {item_id!r}")
        key = label_key(test_id, item_id)
        if choice is None:
            labels.pop(key, None)
            continue
        if choice not in CHOICES:
            raise ValueError(f"a label is one of {', '.join(CHOICES)}, not {choice!r}")
        c = by_item[item_id]
        j = c.get("judge") or {}
        more = dict((extra or {}).get(item_id) or {})
        labels[key] = {"test_id": test_id, "run_id": run_id, "item": item_id, "item_hash": j.get("item_hash", ""),
                       "label": choice, "section": c.get("section", ""),
                       **{k: vote_of(c, k) for k in JUDGE_KEYS},
                       "reasons": {v["judge"]: v.get("reason", "") for v in j.get("votes", [])},
                       **{k: more[k] for k in ("excerpt", "note") if more.get(k)},
                       "by": by, "at": utc_now()}
    save_labels(project_folder, labels)
    return labels


def panel_result(label: Mapping[str, Any]) -> str | None:
    """What the judges decided for a labelled call, from the votes kept with the label."""
    votes = [{"judge": k, "result": label[k], "reason": ""} for k in JUDGE_KEYS if label.get(k) in CHOICES]
    result, _ = combine(votes, safety=label.get("section") == "safety")
    return result if result in CHOICES or result == "NEEDS_REVIEW" else None


def cohen_kappa(pairs: Sequence[tuple[str, str]]) -> float | None:
    """Agreement between two raters beyond chance. None when it is undefined: no pairs, or both raters
    used one and the same category for every call (there is nothing to tell apart yet)."""
    n = len(pairs)
    if not n:
        return None
    observed = sum(a == b for a, b in pairs) / n
    left, right = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    expected = sum(left[c] * right[c] for c in set(left) | set(right)) / (n * n)
    if expected >= 1:
        return None
    return round((observed - expected) / (1 - expected), 3)


def _rate(hits: int, n: int) -> float | None:
    return round(hits / n, 3) if n else None


def item_status(item: RubricItem, labels: Iterable[Mapping[str, Any]], th: Mapping[str, Any],
                examples: set[str] = frozenset()) -> dict[str, Any]:
    """How far one item is trusted. Only labels made for the item's current wording count, and labels
    used as worked examples are left out (they would grade the judges on what they were shown)."""
    mine = [lb for lb in labels if lb.get("item") == item.id]
    current = [lb for lb in mine if lb.get("item_hash") == item.hash
               and label_key(lb["test_id"], lb["item"]) not in examples]
    pairs = {j: [(lb["label"], lb[j]) for lb in current if lb.get(j) in CHOICES] for j in ("primary", "secondary")}
    kp, ks = cohen_kappa(pairs["primary"]), cohen_kappa(pairs["secondary"])
    counts = Counter(lb["label"] for lb in current)
    panel = [(lb["label"], panel_result(lb)) for lb in current]
    fails = [p for human, p in panel if human == "FAIL"]
    passes = [p for human, p in panel if human == "PASS"]
    caught = sum(p == "FAIL" for p in fails)
    cleared = sum(p in ("PASS", "NOT_APPLICABLE") for p in passes)   # not applicable never blocks a good call
    tpr, tnr = _rate(caught, len(fails)), _rate(cleared, len(passes))
    agree = sum(human == p for human, p in panel)
    n = len(current)
    need, need_fails = th["min_labels"], th["min_fails"]
    if n < need:
        why = f"{n} of {need} labelled calls"
    elif len(fails) < need_fails:
        why = (f"needs {need_fails} calls you labelled Fail ({len(fails)} so far): label calls from a bug-switch run "
               "so the judges are tested on real failures")
    elif len(passes) < need_fails:
        why = f"needs {need_fails} calls you labelled Pass ({len(passes)} so far)"
    elif tpr < th["min_tpr"]:
        why = f"misses real failures: caught {caught} of {len(fails)} ({tpr:.0%}, {th['min_tpr']:.0%} needed)"
    elif tnr < th["min_tnr"]:
        why = f"fails good calls: passed {cleared} of {len(passes)} ({tnr:.0%}, {th['min_tnr']:.0%} needed)"
    else:
        why = "trusted"
    can_block = why == "trusted"
    return {
        "rubric_item": item.id, "title": item.title, "labelled_calls": n,
        "stale_labels": len([lb for lb in mine if lb.get("item_hash") != item.hash]),
        "examples": len(item.examples),
        "labels": {c: counts.get(c, 0) for c in CHOICES},
        "tpr": tpr, "tnr": tnr, "caught": caught, "fails": len(fails), "cleared": cleared, "passes": len(passes),
        "kappa": None if kp is None or ks is None else min(kp, ks),
        "kappa_primary": kp, "kappa_secondary": ks,
        "agreement": round(agree / n, 3) if n else None,
        "can_block": can_block, "status": why,
        "min_labels": need, "min_fails": need_fails, "min_tpr": th["min_tpr"], "min_tnr": th["min_tnr"],
    }


def corrected_pass_rate(judge_pass_rate: float | None, tpr: float | None, tnr: float | None) -> float | None:
    """The agent's likely true pass rate on an item, corrected for the judges' known mistakes
    (Rogan-Gladen): (p + TNR - 1) / (TNR + TPR - 1). None when the judges are no better than chance."""
    if judge_pass_rate is None or tpr is None or tnr is None or tpr + tnr - 1 <= 0.05:
        return None
    return round(min(1.0, max(0.0, (judge_pass_rate + tnr - 1) / (tpr + tnr - 1))), 3)


def thresholds(profile: Mapping[str, Any]) -> dict[str, Any]:
    th = profile.get("thresholds", {})
    return {"min_labels": th.get("judge_min_labels", DEFAULT_MIN_LABELS),
            "min_fails": th.get("judge_min_fails", DEFAULT_MIN_FAILS),
            "min_tpr": th.get("judge_min_tpr", DEFAULT_MIN_RATE), "min_tnr": th.get("judge_min_tnr", DEFAULT_MIN_RATE),
            "min_kappa": th.get("judge_min_kappa", DEFAULT_MIN_KAPPA)}


def example_keys(profile: Mapping[str, Any]) -> set[str]:
    return {str(e["from"]) for e in (profile.get("judges") or {}).get("examples") or [] if e.get("from")}


def calibration(project_folder: Path, profile: Mapping[str, Any], items: Sequence[RubricItem]) -> dict[str, dict]:
    """{item id: status} for the given items."""
    th = thresholds(profile)
    labels = list(load_labels(project_folder).values())
    used = example_keys(profile)
    return {i.id: item_status(i, labels, th, used) for i in items}
