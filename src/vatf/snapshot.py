"""Snapshot of a finished run: the numbers worth comparing with another run (Phase 6).

A snapshot is small and self-contained (no paths into runs/), so it can be saved as an agent's
baseline in the project folder and committed with it, and CI can compare against it without the
old run's files. Built from decision.json and run.json only.

    run (decision.json + run.json) -> snapshot -> compare.py (with the baseline's snapshot)
"""

from __future__ import annotations

from typing import Any, Mapping

SCHEMA_VERSION = "0.1"


def how_it_ran(info: Mapping[str, Any]) -> dict[str, Any]:
    """What makes two runs comparable like-for-like: same mode, caller conditions and speech engine."""
    options = info.get("agent_options") or {}
    return {
        "mode": info.get("mode"),
        "conditions": info.get("conditions") or "as_written",
        "speech_engine": (info.get("speech") or {}).get("engine"),
        "offline": bool(info.get("offline")),
        "safety": bool(info.get("safety")),
        "bugs": str(options.get("bugs") or ""),
        "caller_model": info.get("caller_model"),
        # Phase 7: a load test is compared only with load tests of the same shape and target.
        "kind": "load" if info.get("kind") == "load" else "run",   # run.json says "run_info" for a normal run
        "load_shape": (f"{info['load'].get('shape')} to {info['load'].get('calls_at_once')} at once"
                       if info.get("kind") == "load" and info.get("load") else None),
    }


def snapshot(decision: Mapping[str, Any], info: Mapping[str, Any],
             titles: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The comparable summary of one run. `titles` maps scenario ids to readable titles."""
    titles = titles or {}
    tests = {}
    for r in decision.get("reliability", []):
        tests[r["scenario_id"]] = {
            "title": titles.get(r["scenario_id"], r["scenario_id"]), "criticality": r["criticality"],
            "k": r["k"], "passed": r["passed"], "failed": r["failed"], "unverified": r["unverified"]}
    lat = decision.get("latency") or {}
    judges = {j["rubric_item"]: {"title": j.get("title", j["rubric_item"]), "passed": j.get("passed", 0),
                                 "failed": j.get("failed", 0)} for j in decision.get("judges", [])}
    quality = {q["id"]: {"title": q["title"], "unit": q["unit"], "better": q["better"], "value": q["value"],
                         "band": q["band"]} for q in decision.get("quality", [])}
    safety = decision.get("safety")
    load = decision.get("load")
    if load:   # under load, test-by-test results and scores reflect contention: the load block is compared instead
        tests, judges, quality = {}, {}, {}
    return {
        "schema_version": SCHEMA_VERSION, "kind": "run_snapshot",
        "run_id": decision["run_id"], "profile_id": decision.get("profile_id"),
        "profile_file": info.get("profile_file", "profile.yaml"),
        "finished_at": info.get("finished_at") or decision.get("created_at"),
        "decision": decision["decision"],
        "how": how_it_ran(info),
        "coverage": {k: (decision.get("coverage") or {}).get(k) for k in ("planned_calls", "verified_calls", "ratio")},
        "tests": tests,
        "scores": {} if load else {name: s.get("score") for name, s in (decision.get("scorecard") or {}).items()},
        "latency": None if load else ({"p50_ms": lat.get("p50_ms"), "p95_ms": lat.get("p95_ms"), "turns": lat.get("turns_measured", 0)}
                    if lat.get("turns_measured") else None),
        "quality": quality,
        "judges": judges,
        "safety": ({"calls": safety["calls"], "failed": safety["failed"],
                    "findings": sorted({f"{f['scenario_id']}|{f['check_id']}" for f in safety["findings"]
                                        if f["result"] == "FAIL"}),
                    "titles": {f["scenario_id"]: f["title"] for f in safety["findings"]}}
                   if safety else None),
        **({"load": {"shape": load.get("shape"), "calls_at_once": load.get("calls_at_once"),
                     "capacity": load.get("capacity"), "calls": load.get("calls"), "failed": load.get("failed"),
                     "levels": _levels(load.get("stages") or [])}} if load else {}),
    }


def _levels(stages) -> dict[str, dict[str, Any]]:
    """Per level of calls at once: p95, failed calls and pass rate, pooled over the stages at that level."""
    out: dict[str, dict[str, Any]] = {}
    for st in stages:
        if not st.get("finished"):
            continue
        key = str(st["level"])
        cur = out.setdefault(key, {"level": st["level"], "finished": 0, "failed": 0, "p95_ms": None, "pass_rate": None})
        cur["finished"] += st["finished"]
        cur["failed"] += st["failed"]
        # The worst stage at that level speaks for it (a spike's recovery and the normal traffic before it).
        if st.get("p95_ms") is not None:
            cur["p95_ms"] = max(cur["p95_ms"] or 0, st["p95_ms"])
        if st.get("pass_rate") is not None:
            cur["pass_rate"] = st["pass_rate"] if cur["pass_rate"] is None else min(cur["pass_rate"], st["pass_rate"])
    for cur in out.values():
        cur["failed_pct"] = round(100 * cur["failed"] / cur["finished"], 1) if cur["finished"] else None
    return out
