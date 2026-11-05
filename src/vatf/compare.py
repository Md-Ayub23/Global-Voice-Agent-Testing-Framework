"""Compares two run snapshots (snapshot.py): what got worse, what got better (Phase 6).

Used for current run vs the agent's baseline (the regression gate) and for any two runs on the
dashboard's Compare page. Every change has a kind (test, latency, score, metric, judge, safety) and
a direction:

  regression  a test that passed every repeat in the baseline now fails at least once
  worse       a number moved the wrong way by more than its limit (profile `regression`)
  better      fixed tests and numbers that improved by more than the same limit
  new/removed tests in only one of the two runs

Which kinds can turn GO into NO-GO is the owner's choice (profile `regression.<kind>.blocks`).
Defaults block only on regressions: with 3 repeats a small timing or score change is mostly noise.
Two runs made differently (mode, caller conditions, speech engine, offline) are not like-for-like:
the comparison is still shown, but nothing in it blocks.
"""

from __future__ import annotations

from typing import Any, Mapping

DEFAULTS: dict[str, dict[str, Any]] = {
    "new_failures": {"blocks": True},
    "latency": {"blocks": False, "increase_pct": 20, "increase_ms": 150},
    "scores": {"blocks": False, "drop_points": 10},
    "metrics": {"blocks": False},
    "judges": {"blocks": False, "drop_points": 20},
    "safety": {"blocks": False},
    # Phase 7, load runs vs the load baseline, level by level: slower p95 (the latency limits above),
    # more failed calls (failed_points), lower pass rate (pass_drop_points), lower capacity.
    "load": {"blocks": False, "failed_points": 2, "pass_drop_points": 20},
}
KIND_RULE = {"test": "new_failures", "latency": "latency", "score": "scores", "metric": "metrics",
             "judge": "judges", "safety": "safety", "load": "load"}
BAND_RANK = {"good": 0, "warn": 1, "bad": 2}
SCORE_TITLES = {"accuracy": "Accuracy", "experience": "Experience", "security": "Security", "safety": "Safety"}
# Differences in these make two runs not like-for-like (compared, never gated).
LIKE_FOR_LIKE = ("mode", "conditions", "speech_engine", "offline", "kind", "load_shape")
HOW_TITLES = {"mode": "mode", "conditions": "caller conditions", "speech_engine": "speech engine",
              "offline": "offline dry run", "safety": "safety tests", "bugs": "bug switches",
              "caller_model": "caller model", "kind": "kind of run", "load_shape": "load shape"}
HOW_DEFAULTS = {"kind": "run", "load_shape": None}   # baselines saved before Phase 7 have neither


def rules(profile: Mapping[str, Any] | None) -> dict[str, Any]:
    """The profile's `regression` section over the defaults."""
    cfg = (profile or {}).get("regression") or {}
    out: dict[str, Any] = {"enabled": cfg.get("enabled", True)}
    for key, default in DEFAULTS.items():
        out[key] = {**default, **(cfg.get(key) or {})}
    return out


def _rate(passed: int, failed: int) -> float | None:
    return passed / (passed + failed) if passed + failed else None


def _test_changes(base: Mapping[str, Any], cur: Mapping[str, Any]) -> list[dict[str, Any]]:
    out = []
    for sid, c in cur.items():
        b = base.get(sid)
        title = c.get("title", sid)
        if b is None:
            out.append({"kind": "test", "id": sid, "title": title, "direction": "new",
                        "before": None, "after": f"{c['passed']}/{c['k']}",
                        "message": f"{title}: new test, passed {c['passed']} of {c['k']}."})
            continue
        before, after = f"{b['passed']}/{b['k']}", f"{c['passed']}/{c['k']}"
        base_clean = b["k"] > 0 and b["passed"] == b["k"] and not b["failed"]
        if base_clean and c["failed"]:
            out.append({"kind": "test", "id": sid, "title": title, "direction": "regression", "before": before,
                        "after": after, "criticality": c.get("criticality"),
                        "message": f"{title} passed every repeat in the baseline ({before}) and now failed "
                                   f"{c['failed']} of {c['k']}."})
        elif b["failed"] and not c["failed"] and c["passed"] == c["k"] and c["k"]:
            out.append({"kind": "test", "id": sid, "title": title, "direction": "better", "before": before,
                        "after": after, "message": f"{title} is fixed: it failed {b['failed']} of {b['k']} in the "
                                                   f"baseline and passed every repeat now ({after})."})
        elif b["failed"] and c["failed"]:
            out.append({"kind": "test", "id": sid, "title": title, "direction": "same", "before": before,
                        "after": after, "message": f"{title} still fails ({before} before, {after} now)."})
    for sid, b in base.items():
        if sid not in cur:
            out.append({"kind": "test", "id": sid, "title": b.get("title", sid), "direction": "removed",
                        "before": f"{b['passed']}/{b['k']}", "after": None,
                        "message": f"{b.get('title', sid)}: in the baseline, not in this run."})
    return out


def _latency_changes(base: Mapping[str, Any] | None, cur: Mapping[str, Any] | None,
                     r: Mapping[str, Any]) -> list[dict[str, Any]]:
    if not base or not cur:
        return []
    out = []
    for key, title in (("p95_ms", "Response time p95"), ("p50_ms", "Response time p50")):
        b, c = base.get(key), cur.get(key)
        if b is None or c is None:
            continue
        limit = max(float(r["increase_ms"]), b * float(r["increase_pct"]) / 100)
        delta = c - b
        if abs(delta) <= limit:
            continue
        direction = "worse" if delta > 0 else "better"
        out.append({"kind": "latency", "id": key, "title": title, "direction": direction, "before": b, "after": c,
                    "message": f"{title} {'rose' if delta > 0 else 'fell'} from {b:,} ms to {c:,} ms "
                               f"({delta:+,} ms; the limit is {limit:,.0f} ms)."})
    return out


def _score_changes(base: Mapping[str, Any], cur: Mapping[str, Any], r: Mapping[str, Any]) -> list[dict[str, Any]]:
    out = []
    for name, c in cur.items():
        b = base.get(name)
        if b is None or c is None:
            continue
        points = round((c - b) * 100, 1)
        if abs(points) < float(r["drop_points"]):
            continue
        title = SCORE_TITLES.get(name, name.title())
        out.append({"kind": "score", "id": name, "title": title, "direction": "worse" if points < 0 else "better",
                    "before": b, "after": c,
                    "message": f"{title} score {'fell' if points < 0 else 'rose'} from {b:.0%} to {c:.0%} "
                               f"({points:+g} points)."})
    return out


def _metric_changes(base: Mapping[str, Any], cur: Mapping[str, Any]) -> list[dict[str, Any]]:
    """A metric counts as changed only when it moved band (good / warn / bad): bands are the
    owner's own idea of what matters, so small moves inside a band are not reported."""
    out = []
    for mid, c in cur.items():
        b = base.get(mid)
        if not b or b.get("band") not in BAND_RANK or c.get("band") not in BAND_RANK:
            continue
        step = BAND_RANK[c["band"]] - BAND_RANK[b["band"]]
        if not step:
            continue
        out.append({"kind": "metric", "id": mid, "title": c["title"], "direction": "worse" if step > 0 else "better",
                    "before": b["value"], "after": c["value"], "unit": c.get("unit"),
                    "band_before": b["band"], "band_after": c["band"],
                    "message": f"{c['title']} went from {b['band']} ({_num(b['value'], c.get('unit'))}) to "
                               f"{c['band']} ({_num(c['value'], c.get('unit'))})."})
    return out


def _load_changes(base: Mapping[str, Any] | None, cur: Mapping[str, Any] | None, r: Mapping[str, Any],
                  lat: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Load run vs load baseline, level by level (calls at once), plus the capacity."""
    if not base or not cur:
        return []
    out = []
    bl, cl = base.get("levels") or {}, cur.get("levels") or {}
    for key in sorted(set(bl) & set(cl), key=int):
        b, c = bl[key], cl[key]
        at = f"at {key} call{'s' if key != '1' else ''} at once"
        if b.get("p95_ms") is not None and c.get("p95_ms") is not None:
            limit = max(float(lat["increase_ms"]), b["p95_ms"] * float(lat["increase_pct"]) / 100)
            delta = c["p95_ms"] - b["p95_ms"]
            if abs(delta) > limit:
                out.append({"kind": "load", "id": f"p95@{key}", "title": f"Answer time p95 {at}",
                            "direction": "worse" if delta > 0 else "better", "before": b["p95_ms"], "after": c["p95_ms"],
                            "message": f"Answer time p95 {at} {'rose' if delta > 0 else 'fell'} from {b['p95_ms']:,} ms "
                                       f"to {c['p95_ms']:,} ms ({delta:+,} ms; the limit is {limit:,.0f} ms)."})
        if b.get("failed_pct") is not None and c.get("failed_pct") is not None:
            points = round(c["failed_pct"] - b["failed_pct"], 1)
            if abs(points) > float(r["failed_points"]):
                out.append({"kind": "load", "id": f"failed@{key}", "title": f"Failed calls {at}",
                            "direction": "worse" if points > 0 else "better", "before": b["failed_pct"],
                            "after": c["failed_pct"],
                            "message": f"Failed calls {at} went from {b['failed_pct']:g}% to {c['failed_pct']:g}% "
                                       f"({points:+g} points)."})
        if b.get("pass_rate") is not None and c.get("pass_rate") is not None:
            points = round(c["pass_rate"] - b["pass_rate"], 1)
            if abs(points) >= float(r["pass_drop_points"]):
                out.append({"kind": "load", "id": f"pass@{key}", "title": f"Pass rate {at}",
                            "direction": "worse" if points < 0 else "better", "before": b["pass_rate"],
                            "after": c["pass_rate"],
                            "message": f"Pass rate {at} went from {b['pass_rate']:g}% to {c['pass_rate']:g}% "
                                       f"({points:+g} points)."})
    bc, cc = base.get("capacity"), cur.get("capacity")
    if bc is not None and cc is not None and bc != cc:
        out.append({"kind": "load", "id": "capacity", "title": "Capacity", "direction": "worse" if cc < bc else "better",
                    "before": bc, "after": cc,
                    "message": f"Capacity went from {bc} to {cc} call(s) at once within the limits."})
    return out


def _num(v: Any, unit: str | None) -> str:
    if v is None:
        return "no value"
    return f"{v:,.0f} {unit}" if unit in ("ms", "wpm") else f"{v:g}{'' if unit == '%' else ' '}{unit or ''}".strip()


def _judge_changes(base: Mapping[str, Any], cur: Mapping[str, Any], r: Mapping[str, Any]) -> list[dict[str, Any]]:
    out = []
    for item, c in cur.items():
        b = base.get(item)
        if not b:
            continue
        rb, rc = _rate(b["passed"], b["failed"]), _rate(c["passed"], c["failed"])
        if rb is None or rc is None:
            continue
        points = round((rc - rb) * 100, 1)
        if abs(points) < float(r["drop_points"]):
            continue
        out.append({"kind": "judge", "id": item, "title": c.get("title", item),
                    "direction": "worse" if points < 0 else "better", "before": round(rb, 3), "after": round(rc, 3),
                    "message": f"AI judge item {c.get('title', item)}: passed {rc:.0%} of calls, "
                               f"{rb:.0%} in the baseline ({points:+g} points)."})
    return out


def _safety_changes(base: Mapping[str, Any] | None, cur: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not base or not cur:
        return []
    out = []
    titles = {**(base.get("titles") or {}), **(cur.get("titles") or {})}
    before, after = set(base.get("findings", [])), set(cur.get("findings", []))
    for key in sorted(after - before):
        sid, _, check = key.partition("|")
        out.append({"kind": "safety", "id": key, "title": titles.get(sid, sid), "direction": "worse",
                    "before": None, "after": check,
                    "message": f"New safety finding: {titles.get(sid, sid)} failed {check} (it passed in the baseline)."})
    for key in sorted(before - after):
        sid, _, check = key.partition("|")
        out.append({"kind": "safety", "id": key, "title": titles.get(sid, sid), "direction": "better",
                    "before": check, "after": None,
                    "message": f"Safety finding gone: {titles.get(sid, sid)} no longer fails {check}."})
    return out


ORDER = {"regression": 0, "worse": 1, "better": 2, "new": 3, "removed": 4, "same": 5}


def compare(base: Mapping[str, Any], cur: Mapping[str, Any], profile: Mapping[str, Any] | None = None,
            baseline_source: str = "baseline") -> dict[str, Any]:
    """Every change from `base` to `cur`, worst first, and which of them block (the gate)."""
    r = rules(profile)
    hb, hc = {**HOW_DEFAULTS, **(base.get("how") or {})}, {**HOW_DEFAULTS, **(cur.get("how") or {})}
    differences = [{"field": k, "title": HOW_TITLES[k], "before": hb.get(k), "after": hc.get(k)}
                   for k in HOW_TITLES if hb.get(k) != hc.get(k)]
    like = not any(d["field"] in LIKE_FOR_LIKE for d in differences)
    changes = (_test_changes(base.get("tests") or {}, cur.get("tests") or {})
               + _latency_changes(base.get("latency"), cur.get("latency"), r["latency"])
               + _score_changes(base.get("scores") or {}, cur.get("scores") or {}, r["scores"])
               + _metric_changes(base.get("quality") or {}, cur.get("quality") or {})
               + _judge_changes(base.get("judges") or {}, cur.get("judges") or {}, r["judges"])
               + (_safety_changes(base.get("safety"), cur.get("safety")) if hb.get("safety") and hc.get("safety") else [])
               + _load_changes(base.get("load"), cur.get("load"), r["load"], r["latency"]))
    for c in changes:
        rule = r[KIND_RULE[c["kind"]]]
        bad = c["direction"] in ("regression", "worse")
        c["blocks"] = bool(bad and like and r["enabled"] and rule.get("blocks"))
    changes.sort(key=lambda c: (not c["blocks"], ORDER[c["direction"]], c["kind"], c["id"]))
    counts = {d: sum(c["direction"] == d for c in changes) for d in ORDER}
    return {
        "schema_version": "0.1", "kind": "run_comparison", "source": baseline_source,
        "baseline": {"run_id": base.get("run_id"), "finished_at": base.get("finished_at"),
                     "decision": base.get("decision"), "profile_file": base.get("profile_file")},
        "current": {"run_id": cur.get("run_id"), "finished_at": cur.get("finished_at"), "decision": cur.get("decision")},
        "like_for_like": like, "differences": differences,
        "gate_enabled": bool(r["enabled"]),
        "changes": changes, "counts": counts,
        "blocking": sum(c["blocks"] for c in changes),
    }
