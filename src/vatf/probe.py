"""Backend probe (contracts/observers.py BackendProbe): reads real backend state after a call.

Phase 1 supports SQL on SQLite (backend.connection: sqlite:///relative/or/absolute/path.db).
Relative paths are resolved against the project root (the folder vatf runs from).
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from .contracts.models import CallContext
from .contracts.observers import ProbeResult

BIND = re.compile(r"(?<![\w:]):([A-Za-z_]\w*)")


class ProbeSetupError(Exception):
    """The backend cannot be reached or prepared. A harness problem, never the agent's fault."""


def sqlite_path(profile: Mapping[str, Any], root: Path) -> Path:
    conn = profile.get("backend", {}).get("connection", "")
    if not conn.startswith("sqlite:///"):
        raise ProbeSetupError(f"only sqlite:/// backends are supported in Phase 1, got {conn!r}")
    path = Path(conn[len("sqlite:///"):])
    return path if path.is_absolute() else root / path


class SqlProbe:
    def __init__(self, profile: Mapping[str, Any], root: Path):
        self._profile = profile
        self._root = root
        self._by_id = {p["id"]: p for p in profile.get("backend", {}).get("probes", [])}

    def run(self, probe: Mapping[str, Any] | str, ctx: CallContext,
            window: tuple[str, str] | None = None) -> ProbeResult:
        spec = self._by_id[probe] if isinstance(probe, str) else probe
        binds = {"test_id": ctx.test_id}
        if window:
            binds["call_started_at"], binds["call_ended_at"] = window
        binds.update(ctx.scenario.get("caller", {}).get("facts", {}))
        wanted = BIND.findall(spec["query"])
        missing = [n for n in wanted if n not in binds]
        if missing:
            return ProbeResult(spec["id"], ok=False, error=f"no value for :{', :'.join(missing)}")
        try:
            path = sqlite_path(self._profile, self._root)
            conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)  # read only: a probe never changes state
            conn.row_factory = sqlite3.Row
            try:
                rows = [dict(r) for r in conn.execute(spec["query"], {n: binds[n] for n in wanted})]
            finally:
                conn.close()
        except (sqlite3.Error, ProbeSetupError) as e:
            return ProbeResult(spec["id"], ok=False, error=str(e))
        returns = spec.get("returns", "rows")
        if returns == "count":
            value = next(iter(rows[0].values())) if rows else 0
            return ProbeResult(spec["id"], ok=True, value=value)
        if returns == "one":
            return ProbeResult(spec["id"], ok=True, rows=rows[:1], value=rows[0] if rows else None)
        return ProbeResult(spec["id"], ok=True, rows=rows)


def load_fixtures(profile: Mapping[str, Any], scenario: Mapping[str, Any], root: Path) -> None:
    """Runs the scenario's setup.fixtures (SQL files or statements) before a call."""
    fixtures = scenario.get("setup", {}).get("fixtures", [])
    if not fixtures:
        return
    if profile.get("backend", {}).get("access") != "sql":
        raise ProbeSetupError("fixtures need a sql backend")
    path = sqlite_path(profile, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        for item in fixtures:
            if item.endswith(".sql"):
                file = root / item
                if not file.exists():
                    raise ProbeSetupError(f"fixture file {file} not found")
                conn.executescript(file.read_text(encoding="utf-8"))
            else:
                conn.executescript(item)
        conn.commit()
    except sqlite3.Error as e:
        raise ProbeSetupError(f"loading fixtures failed: {e}") from e
    finally:
        conn.close()
