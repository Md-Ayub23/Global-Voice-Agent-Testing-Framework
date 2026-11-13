"""An agent's past runs, for Regression: which tests are flaky, a rolling baseline, and the trend.

  past_runs(runs_dir, profile_file, mode, before)  -> the finished normal runs of this profile, newest first
  flaky_tests(runs)                                 -> tests that both passed and failed over the last 10 runs
  rolling_baseline(runs, how, n)                    -> one snapshot pooling the last n good runs made the same way
  trend(runs)                                       -> one point per run (pass rate, p95, versions) for the chart

Only runs.  No AI: every number is a count over decision.json files.
"""

from __future__ import annotations

from pathlib import Path
from statistics import median
from typing import Any, Mapping, Sequence

from .snapshot import how_it_ran, snapshot
from .util import read_json

FLAKY_RUNS = 10                 # how far back a test's pass rate is read


def past_runs(runs_dir: Path, profile_file: str, mode: str | None, before: str | None = None,
              limit: int = FLAKY_RUNS) -> list[dict[str, Any]]:
    """[{run_id, info, decision}] of finished normal (not load) runs of this profile file and mode, newest
    first, started before the given run id. A run id starts with its start time to the second
    (r-YYYYMMDD-HHMMSS-xxxx) and ends with a random part, so runs started in the same second sort by when
    they finished."""
    out = []
    root = Path(runs_dir)
    if not root.is_dir():
        return out
    stamp = (lambda name: name[:17])                      # "r-YYYYMMDD-HHMMSS"
    found = []
    for d in root.iterdir():
        if not (d.is_dir() and d.name.startswith("r-")) or d.name == before or (before and stamp(d.name) > stamp(before)):
            continue
        found.append(d)
    for d in sorted(found, key=lambda p: (stamp(p.name), _mtime(p / "decision.json"), p.name), reverse=True):
        try:
            info, decision = read_json(d / "run.json"), read_json(d / "decision.json")
        except (OSError, ValueError):
            continue
        if info.get("profile_file", "profile.yaml") != profile_file or info.get("kind") == "load" or decision.get("load"):
            continue
        if mode and info.get("mode") != mode:
            continue
        out.append({"run_id": d.name, "info": info, "decision": decision})
        if len(out) >= limit:
            break
    return out


def _mtime(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def flaky_tests(runs: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Tests whose calls both passed and failed across these runs (bug-switch runs left out: they are
    meant to fail). {scenario id: {passed, calls, rate, runs}} for tests below 100% and above 0%."""
    seen: dict[str, dict[str, int]] = {}
    for r in runs:
        if (r["info"].get("agent_options") or {}).get("bugs"):
            continue
        for row in r["decision"].get("reliability") or []:
            s = seen.setdefault(row["scenario_id"], {"passed": 0, "calls": 0, "runs": 0})
            n = row["passed"] + row["failed"]
            if n:
                s["passed"] += row["passed"]
                s["calls"] += n
                s["runs"] += 1
    return {sid: {**s, "rate": round(s["passed"] / s["calls"], 3)} for sid, s in seen.items()
            if s["calls"] and 0 < s["passed"] < s["calls"]}


def rolling_baseline(runs: Sequence[Mapping[str, Any]], how: Mapping[str, Any], n: int = 3,
                     titles: Mapping[str, str] | None = None) -> dict[str, Any] | None:
    """One snapshot pooling the last n good (GO) runs made the same way as this one (mode, conditions, speech
    engine, offline): tests summed over the runs, times and scores the median. None if there are none."""
    keys = ("mode", "conditions", "speech_engine", "offline")
    good = [r for r in runs if r["decision"].get("decision") == "GO" and not (r["info"].get("agent_options") or {}).get("bugs")
            and all(how_it_ran(r["info"]).get(k) == how.get(k) for k in keys)][:n]
    if not good:
        return None
    snaps = [snapshot(r["decision"], r["info"], titles) for r in good]
    base = dict(snaps[0])                     # the newest run's quality bands, judges, cost and versions
    tests: dict[str, dict[str, Any]] = {}
    for s in snaps:
        for sid, t in s["tests"].items():
            cur = tests.setdefault(sid, {**t, "k": 0, "passed": 0, "failed": 0, "unverified": 0})
            for k in ("k", "passed", "failed", "unverified"):
                cur[k] += t[k]
    lats = [s["latency"] for s in snaps if s.get("latency")]
    base.update(
        run_id=f"last {len(snaps)} good runs", tests=tests,
        rolling=[s["run_id"] for s in snaps],
        latency=({"p50_ms": int(median(l["p50_ms"] for l in lats if l["p50_ms"] is not None)) if any(l["p50_ms"] is not None for l in lats) else None,
                  "p95_ms": int(median(l["p95_ms"] for l in lats if l["p95_ms"] is not None)) if any(l["p95_ms"] is not None for l in lats) else None,
                  "turns": sum(l.get("turns", 0) for l in lats)} if lats else None),
        scores={k: (round(median(v), 4) if (v := [s["scores"].get(k) for s in snaps if s["scores"].get(k) is not None]) else None)
                for k in snaps[0].get("scores", {})},
    )
    return base


def trend(runs: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Oldest first: each run's pass rate, p95, decision and version label, for the trend chart."""
    out = []
    for r in reversed(list(runs)):
        d, info = r["decision"], r["info"]
        cov = d.get("coverage") or {}
        rel = d.get("reliability") or []
        passed, n = sum(x["passed"] for x in rel), sum(x["passed"] + x["failed"] for x in rel)
        out.append({"run_id": r["run_id"], "at": info.get("finished_at") or info.get("started_at"),
                    "decision": d.get("decision"), "pass_rate": round(passed / n, 4) if n else None,
                    "p95_ms": (d.get("latency") or {}).get("p95_ms"), "calls": cov.get("planned_calls"),
                    "version": ((info.get("versions") or {}).get("agent")) or None})
    return out
