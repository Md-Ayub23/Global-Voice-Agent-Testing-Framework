"""Trends over real calls (Phase 8), read from the index rows only (fast, no call files opened).

  pass rate     PASS / (PASS + FAIL) by the calls' status (checks and trusted judges); calls to review
                and calls with nothing checked are left out
  flagged       share of calls with anything worth a look (score.flags_of)
  escalated     share of calls handed to a person
  p90           90th percentile response time over every answer in the calls (calls with turn times)
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from .. import metrics as qm
from ..latency import percentile
from .store import parse_time


def when(row: Mapping[str, Any]) -> datetime | None:
    return parse_time(row.get("started_at")) or parse_time(row.get("imported_at"))


def summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    passed = sum(r.get("status") == "PASS" for r in rows)
    failed = sum(r.get("status") == "FAIL" for r in rows)
    timed = [ms for r in rows for ms in r.get("latency_ms") or []]
    return {"calls": n, "passed": passed, "failed": failed,
            "review": sum(r.get("status") == "NEEDS_REVIEW" for r in rows),
            "flagged": sum(bool(r.get("flagged")) for r in rows),
            "escalated": sum(bool(r.get("escalated")) for r in rows),
            "judged": sum(r.get("judge_state") == "done" for r in rows),
            "pass_rate": round(100 * passed / (passed + failed), 1) if passed + failed else None,
            "flagged_pct": round(100 * sum(bool(r.get("flagged")) for r in rows) / n, 1) if n else None,
            "escalated_pct": round(100 * sum(bool(r.get("escalated")) for r in rows) / n, 1) if n else None,
            "p90_ms": percentile(timed, 90), "answers": len(timed),
            "repeat_callers": _repeats(rows)}


def _repeats(rows: Sequence[Mapping[str, Any]]) -> int:
    """Callers who called more than once in these calls (from the one-way caller hash)."""
    seen: dict[str, int] = defaultdict(int)
    for r in rows:
        if r.get("caller_hash"):
            seen[r["caller_hash"]] += 1
    return sum(1 for v in seen.values() if v > 1)


def between(rows: Sequence[Mapping[str, Any]], start: datetime, end: datetime) -> list[Mapping[str, Any]]:
    return [r for r in rows if (w := when(r)) is not None and start <= w < end]


def daily(rows: Sequence[Mapping[str, Any]], days: int = 14, now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    today = now.date()
    out = []
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        start = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        s = summary(between(rows, start, start + timedelta(days=1)))
        out.append({"day": d.isoformat(), **{k: s[k] for k in ("calls", "passed", "failed", "flagged", "escalated",
                                                                 "pass_rate", "flagged_pct", "p90_ms")}})
    return out


def week_compare(rows: Sequence[Mapping[str, Any]], now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    this = summary(between(rows, now - timedelta(days=7), now + timedelta(seconds=1)))
    last = summary(between(rows, now - timedelta(days=14), now - timedelta(days=7)))
    return {"this_week": this, "last_week": last}


def pseudo_verdicts(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Index rows in the shape metrics.run_quality reads."""
    return [{"status": r.get("status"), "metrics": {"latency_ms": r.get("latency_ms") or [],
                                                    "quality": r.get("quality") or {}}} for r in rows]


def quality(rows: Sequence[Mapping[str, Any]], profile: Mapping[str, Any]) -> list[dict[str, Any]]:
    return qm.run_quality(pseudo_verdicts(rows), profile, one_rule=False)


def by_version(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Results split by the agent version the calls ran on (only when the calls say which)."""
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for r in rows:
        if r.get("agent_version"):
            groups[str(r["agent_version"])].append(r)
    out = []
    for v, rs in groups.items():
        s = summary(rs)
        first = min((when(r) for r in rs if when(r)), default=None)
        out.append({"version": v, "first_seen": first.isoformat(timespec="seconds").replace("+00:00", "Z") if first else None,
                    **{k: s[k] for k in ("calls", "pass_rate", "flagged_pct", "escalated_pct", "p90_ms")}})
    return sorted(out, key=lambda x: x["first_seen"] or "", reverse=True)


def top_flags(rows: Sequence[Mapping[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    """The most common reasons calls were flagged."""
    count: dict[tuple[str, str], int] = defaultdict(int)
    for r in rows:
        for f in {(f["kind"], f["id"]) for f in r.get("flags") or []}:
            count[f] += 1
    return [{"kind": k, "id": i, "calls": n} for (k, i), n in sorted(count.items(), key=lambda kv: -kv[1])[:limit]]
