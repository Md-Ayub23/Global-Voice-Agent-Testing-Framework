"""Side effects: did the call change anything in the backend that the test didn't ask for?

The outcome check proves the asked-for record is right. It can't see the agent cancelling a second
booking, editing another customer's phone number or writing to the wrong table. This takes a copy of
the watched tables right before the call (after the test data is loaded) and again after the probes,
and compares them row by row.

Which tables are watched (backend in the profile):
  watch: [bookings, customers]        the tables to copy; left out, the tables the probes read
  watch: false                        turns the check off
  watch_ignore: [audit_log, sessions.updated_at]   tables, or table.column, that may always change
For a REST backend, watch lists "GET /path" reads that return the whole collection.

A change is expected when:
  - the added or changed row holds every value of a row a probe of this call returned (the record
    the outcome check looked at), or
  - its table is the one the outcome record is read from and the test expects that many new rows
    (a probe that only counts can't show the row), or
  - one of the expected write calls was sent the row's id (cancel_booking(booking_id=7) removes row 7), or
  - the scenario allows the table (allow_changes: [table]).
Everything else is an unexpected change, a blocker: it is real data the agent changed.

Only reads: SQLite is opened read-only, Postgres sessions are read-only transactions, REST is GET.
Skipped for load tests (many calls change the tables at once) and when nothing is watched.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from typing import Any, Mapping, Sequence

from .contracts.models import CheckResult, EvidenceLevel, EvidenceRef, Result, TraceEvent

MAX_ROWS = 5000           # per table; a bigger table is compared on its first 5000 rows and says so
MAX_SHOWN = 50            # changed rows kept per table in the trace (more are counted, never shown)
NAME = re.compile(r"^[A-Za-z_][\w]*(\.[A-Za-z_][\w]*)?$")
TABLES_IN = re.compile(r"\b(?:FROM|JOIN|INTO|UPDATE)\s+([A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)?)", re.I)
KEYS = ("id", "uuid", "pk")


class WatchError(Exception):
    """The tables could not be copied. A harness problem, never the agent's fault."""


# ---- what is watched ---------------------------------------------------------------------------

def tables_of(profile: Mapping[str, Any]) -> list[str]:
    """The tables (or REST reads) to watch, [] when off."""
    backend = profile.get("backend") or {}
    access = backend.get("access", "none")
    watch = backend.get("watch")
    if access not in ("sql", "rest") or watch is False:
        return []
    if isinstance(watch, list) and watch:
        names = [str(w).strip() for w in watch]
    elif access == "rest":
        return []              # REST has no table names to find: only explicit watch reads
    else:
        names = []
        for p in backend.get("probes", []):
            names += TABLES_IN.findall(p.get("query", ""))
    ignore = {str(i).strip().lower() for i in backend.get("watch_ignore") or [] if "." not in str(i)}
    keep = []
    for n in dict.fromkeys(names):
        if n.lower() in ignore:
            continue
        if access == "rest" and not n.upper().startswith("GET "):
            continue
        if access == "sql" and not NAME.match(n):
            continue
        keep.append(n)
    return keep


def ignored_columns(profile: Mapping[str, Any]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for i in (profile.get("backend") or {}).get("watch_ignore") or []:
        table, _, col = str(i).strip().partition(".")
        if col:
            out.setdefault(table.lower(), set()).add(col.lower())
    return out


# ---- copying the tables ------------------------------------------------------------------------

def _plain(v: Any) -> Any:
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, (bytes, bytearray, memoryview)):
        return "<bytes " + hashlib.sha256(bytes(v)).hexdigest()[:12] + ">"
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)


def snapshot(profile: Mapping[str, Any], root, tables: Sequence[str]) -> dict[str, Any]:
    """{table: {"rows": [row, ...], "cut": bool}} for each watched table. Raises WatchError."""
    from .probe import ProbeSetupError, backend_kind, sqlite_path, _pg_connect, BackendProbe

    try:
        kind = backend_kind(profile)
    except ProbeSetupError as e:
        raise WatchError(str(e)) from e
    out: dict[str, Any] = {}
    if kind == "sqlite":
        path = sqlite_path(profile, root)
        if not path.exists():
            raise WatchError(f"the database file {path} does not exist")
        try:
            conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                for t in tables:
                    rows = [{k: _plain(r[k]) for k in r.keys()}
                            for r in conn.execute(f'SELECT * FROM {_quote(t)} LIMIT {MAX_ROWS + 1}')]
                    out[t] = {"rows": rows[:MAX_ROWS], "cut": len(rows) > MAX_ROWS}
            finally:
                conn.close()
        except sqlite3.Error as e:
            raise WatchError(f"SQLite: {e}") from e
        return out
    if kind == "postgres":
        try:
            from psycopg.rows import dict_row
            conn = _pg_connect(profile)
        except (ImportError, ProbeSetupError) as e:
            raise WatchError(str(e)) from e
        try:
            with conn.cursor(row_factory=dict_row) as cur:
                for t in tables:
                    cur.execute(f"SELECT * FROM {_quote(t)} LIMIT {MAX_ROWS + 1}")
                    rows = [{k: _plain(v) for k, v in r.items()} for r in cur.fetchall()]
                    out[t] = {"rows": rows[:MAX_ROWS], "cut": len(rows) > MAX_ROWS}
            conn.rollback()
        except Exception as e:  # noqa: BLE001 - psycopg errors
            raise WatchError(f"Postgres: {e}") from e
        finally:
            conn.close()
        return out
    if kind == "rest":
        probe = BackendProbe(profile, root)
        for t in tables:
            res = probe.run_with({"id": f"watch:{t}", "query": t, "returns": "rows"}, {})
            if not res.ok:
                raise WatchError(res.error or f"could not read {t}")
            rows = [{k: _plain(v) for k, v in dict(r).items()} for r in res.rows or []]
            out[t] = {"rows": rows[:MAX_ROWS], "cut": len(rows) >= MAX_ROWS}
        return out
    raise WatchError("backend.access is none: there is nothing to watch")


def _quote(table: str) -> str:
    return ".".join(f'"{p}"' for p in table.split("."))


# ---- comparing ---------------------------------------------------------------------------------

def _key_of(rows: Sequence[Mapping[str, Any]]) -> str | None:
    cols = list(rows[0].keys()) if rows else []
    for k in KEYS:
        for c in cols:
            if c.lower() == k:
                return c
    return cols[0] if cols else None


def _digest(row: Mapping[str, Any], skip: set[str]) -> str:
    kept = {k: row[k] for k in sorted(row) if k.lower() not in skip}
    return hashlib.sha256(json.dumps(kept, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def diff(before: Mapping[str, Any], after: Mapping[str, Any], skip_cols: Mapping[str, set[str]] | None = None
         ) -> dict[str, Any]:
    """What changed per table: added, removed and changed rows (changed = same key, other values)."""
    skip_cols = skip_cols or {}
    tables = []
    for t in before:
        if t not in after:
            continue
        b_rows, a_rows = before[t]["rows"], after[t]["rows"]
        skip = skip_cols.get(t.lower(), set())
        key = _key_of(b_rows or a_rows)
        b = {}
        for r in b_rows:
            b.setdefault(_digest(r, skip), r)
        a = {}
        for r in a_rows:
            a.setdefault(_digest(r, skip), r)
        gone = [b[h] for h in b if h not in a]
        new = [a[h] for h in a if h not in b]
        changes: list[dict[str, Any]] = []
        if key:
            gone_by_key = {str(r.get(key)): r for r in gone}
            still_new = []
            for r in new:
                old = gone_by_key.pop(str(r.get(key)), None)
                if old is not None:
                    cols = sorted(c for c in set(old) | set(r) if old.get(c) != r.get(c) and c.lower() not in skip)
                    changes.append({"kind": "changed", "key": r.get(key), "before": old, "after": r, "columns": cols})
                else:
                    still_new.append(r)
            new, gone = still_new, list(gone_by_key.values())
        changes += [{"kind": "added", "key": r.get(key) if key else None, "after": r} for r in new]
        changes += [{"kind": "removed", "key": r.get(key) if key else None, "before": r} for r in gone]
        tables.append({"table": t, "key": key, "rows_before": len(b_rows), "rows_after": len(a_rows),
                       "cut": bool(before[t].get("cut") or after[t].get("cut")),
                       "count": len(changes), "changes": changes[:MAX_SHOWN]})
    return {"tables": tables}


# ---- the check ---------------------------------------------------------------------------------

def _outcomes(scenario: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    first = scenario.get("outcome")
    return ([first] if first else []) + list(scenario.get("more_outcomes") or [])


def expected_write_tools(scenario: Mapping[str, Any]) -> set[str]:
    """The write tools this test expects or allows the agent to call."""
    tools = {o["action"]["tool"] for o in _outcomes(scenario) if o.get("action", {}).get("tool")}
    checks = list(scenario.get("checks", [])) + [c for t in scenario.get("turns", []) for c in t.get("expect", [])]
    tools |= {c["tool"] for c in checks if c.get("type") == "tool_called" and c.get("tool")}
    tools |= set(scenario.get("allow_tools") or [])
    return tools


def _probe_tables(profile: Mapping[str, Any], probe_id: str) -> set[str]:
    spec = next((p for p in (profile.get("backend") or {}).get("probes", []) if p.get("id") == probe_id), None)
    if not spec:
        return set()
    q = spec.get("query", "")
    if q.upper().startswith("GET "):
        return {q.strip()}
    return {t.lower() for t in TABLES_IN.findall(q)}


def _holds(row: Mapping[str, Any], probe_row: Mapping[str, Any]) -> bool:
    if not probe_row:
        return False
    low = {str(k).lower(): v for k, v in row.items()}
    for k, v in probe_row.items():
        k = str(k).lower()
        if k not in low or str(low[k]) != str(v):
            return False
    return True


def _why_expected(change: Mapping[str, Any], table: str, probe_rows: list[Mapping[str, Any]],
                  record_room: dict[str, int], arg_values: set[str], allowed: set[str]) -> str | None:
    if table.lower() in allowed:
        return "the test allows this table to change"
    row = change.get("after") or change.get("before") or {}
    if change["kind"] in ("added", "changed") and any(_holds(change["after"], p) for p in probe_rows):
        return "it is the record the outcome check read"
    if change["kind"] == "added" and record_room.get(table.lower(), 0) > 0:
        record_room[table.lower()] -= 1
        return "the test expects a new record here"
    if change.get("key") is not None and str(change["key"]) in arg_values:
        return "an expected write call was sent its id"
    if change["kind"] == "removed" and row and any(str(v) in arg_values for v in row.values() if v not in (None, "")):
        return "an expected write call named it"
    return None


def _brief(change: Mapping[str, Any], table: str) -> str:
    key = change.get("key")
    what = f"{table} row {key}" if key is not None else f"a {table} row"
    if change["kind"] == "changed":
        return f"{what} changed ({', '.join(change.get('columns', [])[:4]) or 'values'})"
    return f"{what} {'was added' if change['kind'] == 'added' else 'was removed'}"


def side_effects_check(scenario: Mapping[str, Any], profile: Mapping[str, Any],
                       events: Sequence[TraceEvent]) -> CheckResult | None:
    """The "nothing else changed" check, or None when nothing was watched on this call."""
    found = [e for e in events if e.type == "state_diff"]
    if not found:
        return None
    e = found[-1]
    data = e.data
    base = dict(check_id="nothing-else-changed", origin="hard_check", severity="blocker", section="accuracy",
                type="side_effects")
    if data.get("error"):
        return CheckResult(**base, result=Result.NOT_VERIFIED, evidence=(EvidenceRef(e.seq),),
                           evidence_level=EvidenceLevel.NONE,
                           message=f"Could not check for other changes: {data['error']}")
    outcomes = _outcomes(scenario)
    probe_rows = [r for p in events if p.type == "probe_result" and p.data.get("ok")
                  for r in (p.data.get("rows") or []) if isinstance(r, dict)]
    record_room: dict[str, int] = {}
    for o in outcomes:
        rec = o.get("record")
        if not rec or o.get("expected") not in ("success", "recover_or_honest"):
            continue
        for t in _probe_tables(profile, rec["probe"]):
            record_room[t] = record_room.get(t, 0) + int(rec.get("expect_count", 1))
    expected_tools = expected_write_tools(scenario)
    ok_ids = {r.data["call_id"] for r in events if r.type == "tool_response" and r.data.get("ok")}
    arg_values = {str(v) for r in events if r.type == "tool_request" and r.data.get("tool") in expected_tools
                  and r.data.get("call_id") in ok_ids for v in (r.data.get("args") or {}).values()
                  if isinstance(v, (str, int)) and str(v).strip()}
    allowed = {str(t).lower() for t in scenario.get("allow_changes") or []}

    unexpected: list[str] = []
    explained = 0
    for t in data.get("tables", []):
        for ch in t.get("changes", []):
            if _why_expected(ch, t["table"], probe_rows, record_room, arg_values, allowed):
                explained += 1
            else:
                unexpected.append(_brief(ch, t["table"]))
        hidden = max(0, t.get("count", 0) - len(t.get("changes", [])))
        if hidden and t["table"].lower() not in allowed:
            unexpected.append(f"{hidden} more {t['table']} rows changed")
    n = len(data.get("tables", []))
    cut = [t["table"] for t in data.get("tables", []) if t.get("cut")]
    if unexpected:
        more = f" (and {len(unexpected) - 5} more)" if len(unexpected) > 5 else ""
        return CheckResult(**base, result=Result.FAIL, evidence=(EvidenceRef(e.seq, "backend before and after the call"),),
                           evidence_level=EvidenceLevel.BACKEND,
                           message="Something else changed in your system: " + "; ".join(unexpected[:5]) + more + ".")
    note = f" Only the first {MAX_ROWS} rows of {', '.join(cut)} were compared." if cut else ""
    what = f"{explained} expected change{'s' if explained != 1 else ''}" if explained else "no changes"
    return CheckResult(**base, result=Result.PASS, evidence=(EvidenceRef(e.seq, "backend before and after the call"),),
                       evidence_level=EvidenceLevel.BACKEND,
                       message=f"Nothing else changed in {n} watched table{'s' if n != 1 else ''} ({what}).{note}")


def wrong_tool_check(scenario: Mapping[str, Any], profile: Mapping[str, Any],
                     events: Sequence[TraceEvent]) -> CheckResult | None:
    """The agent used a write tool the test didn't expect (it changed, or tried to change, something
    else). Only for tests with an outcome, which say what the call is for."""
    if not _outcomes(scenario):
        return None
    writes = {t["name"] for t in profile.get("tools", []) if t.get("effect") == "write"}
    expected = expected_write_tools(scenario)
    # Tools the scenario already rules on itself (must_not_call) are its own check.
    checks = list(scenario.get("checks", [])) + [c for t in scenario.get("turns", []) for c in t.get("expect", [])]
    own = {c.get("tool") for c in checks if c.get("type") == "tool_not_called"}
    responses = {e.data.get("call_id"): e for e in events if e.type == "tool_response"}
    bad = [e for e in events if e.type == "tool_request" and e.data.get("tool") in writes
           and e.data.get("tool") not in expected and e.data.get("tool") not in own]
    base = dict(check_id="only-expected-writes", origin="hard_check", section="accuracy", type="wrong_tool")
    if not bad:
        if not writes - expected:
            return None    # no other write tool exists: nothing to check
        used = [e for e in events if e.type == "tool_request" and e.data.get("tool") in writes]
        ended = [e for e in events if e.type == "call_ended"]
        cite = [EvidenceRef(e.seq, f"{e.data['tool']} was called") for e in used[:3]] or [
            EvidenceRef(e.seq, "no write tool used in the call") for e in ended[-1:]]
        if not cite:
            return None
        return CheckResult(**base, result=Result.PASS, severity="major", evidence=tuple(cite),
                           evidence_level=EvidenceLevel.TOOL_TRAFFIC,
                           message="The agent used no write tool other than the ones this test expects.")
    worked = [e for e in bad if (responses.get(e.data.get("call_id")) or e).data.get("ok")]
    names = ", ".join(dict.fromkeys(e.data["tool"] for e in bad))
    return CheckResult(**base, result=Result.FAIL, severity="blocker" if worked else "major",
                       evidence=tuple(EvidenceRef(e.seq, f"{e.data['tool']} was called") for e in bad[:3]),
                       evidence_level=EvidenceLevel.TOOL_TRAFFIC,
                       message=(f"{names} was called, which this test doesn't expect"
                                + (", and it went through." if worked else " (the call failed, so nothing changed).")))
