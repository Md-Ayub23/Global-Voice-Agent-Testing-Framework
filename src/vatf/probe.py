"""Backend probe (contracts/observers.py BackendProbe): reads real backend state after a call.

backend.access and backend.connection in the profile say where the truth lives:

  sql   sqlite:///relative/or/absolute/path.db    (relative to the repo folder vatf runs from)
        postgresql://user:${PG_PASSWORD}@host:5432/dbname   (needs `uv sync --extra postgres`)
  rest  https://api.example.com                   probes are "GET /path?x=:name" against it

Probes only read: SQLite is opened read-only, Postgres sessions are read-only transactions, and
REST probes are GET requests. Secrets are written as ${NAME} and read from the .env file.
A probe that cannot run is reported as ok=False (a harness problem, never the agent's fault).
"""

from __future__ import annotations

import json
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Mapping

from .contracts.models import CallContext
from .contracts.observers import ProbeResult
from .util import MissingSecret, expand_env

BIND = re.compile(r"(?<![\w:]):([A-Za-z_]\w*)")
MAX_ROWS = 500


class ProbeSetupError(Exception):
    """The backend cannot be reached or prepared. A harness problem, never the agent's fault."""


def backend_kind(profile: Mapping[str, Any]) -> str:
    """sqlite, postgres, rest or none."""
    backend = profile.get("backend", {})
    access = backend.get("access", "none")
    if access == "rest":
        return "rest"
    if access != "sql":
        return "none"
    conn = backend.get("connection", "")
    if conn.startswith("sqlite:///"):
        return "sqlite"
    if conn.startswith(("postgresql://", "postgres://")):
        return "postgres"
    raise ProbeSetupError(f"backend.connection must start with sqlite:/// or postgresql://, got {conn!r}")


def sqlite_path(profile: Mapping[str, Any], root: Path) -> Path:
    conn = profile.get("backend", {}).get("connection", "")
    if not conn.startswith("sqlite:///"):
        raise ProbeSetupError(f"not a sqlite:/// backend: {conn!r}")
    path = Path(conn[len("sqlite:///"):])
    return path if path.is_absolute() else root / path


def _connection(profile: Mapping[str, Any]) -> str:
    try:
        return expand_env(profile.get("backend", {}).get("connection", ""))
    except MissingSecret as e:
        raise ProbeSetupError(str(e)) from e


def _pg_connect(profile: Mapping[str, Any], read_only: bool = True):
    try:
        import psycopg
    except ImportError as e:
        raise ProbeSetupError("Postgres needs the psycopg package: run `uv sync --extra postgres`") from e
    try:
        conn = psycopg.connect(_connection(profile), connect_timeout=10)
    except psycopg.Error as e:
        raise ProbeSetupError(f"could not connect to Postgres: {e}") from e
    conn.read_only = read_only
    return conn


def _pg_query(query: str) -> str:
    """:name binds -> %(name)s, and a literal % doubled, as psycopg expects."""
    return BIND.sub(lambda m: f"%({m.group(1)})s", query.replace("%", "%%"))


class BackendProbe:
    def __init__(self, profile: Mapping[str, Any], root: Path):
        self._profile = profile
        self._root = root
        self._by_id = {p["id"]: p for p in profile.get("backend", {}).get("probes", [])}

    def run(self, probe: Mapping[str, Any] | str, ctx: CallContext,
            window: tuple[str, str] | None = None) -> ProbeResult:
        binds: dict[str, Any] = {"test_id": ctx.test_id}
        if window:
            binds["call_started_at"], binds["call_ended_at"] = window
        binds.update(ctx.scenario.get("caller", {}).get("facts", {}))
        return self.run_with(probe, binds)

    def run_with(self, probe: Mapping[str, Any] | str, binds: Mapping[str, Any]) -> ProbeResult:
        """Runs one probe with the given bind values (the dashboard's "Test" button uses this too)."""
        spec = self._by_id[probe] if isinstance(probe, str) else probe
        wanted = list(dict.fromkeys(BIND.findall(spec["query"])))
        missing = [n for n in wanted if n not in binds]
        if missing:
            return ProbeResult(spec["id"], ok=False, error=f"no value for :{', :'.join(missing)}")
        values = {n: binds[n] for n in wanted}
        try:
            kind = backend_kind(self._profile)
            if kind == "sqlite":
                rows = self._sqlite(spec["query"], values)
            elif kind == "postgres":
                rows = self._postgres(spec["query"], values)
            elif kind == "rest":
                rows = self._rest(spec, values)
                if isinstance(rows, (int, float)) and spec.get("returns") == "count":
                    return ProbeResult(spec["id"], ok=True, value=rows)
            else:
                return ProbeResult(spec["id"], ok=False, error="backend.access is none: there is nothing to probe")
        except ProbeSetupError as e:
            return ProbeResult(spec["id"], ok=False, error=str(e))
        if not isinstance(rows, list):
            return ProbeResult(spec["id"], ok=False, error=f"expected a list of records, got {str(rows)[:120]}")
        rows = rows[:MAX_ROWS]
        returns = spec.get("returns", "rows")
        if returns == "count":
            # SQL: the probe is a SELECT COUNT(*), so the count is its one value. REST: the number of records.
            if kind == "rest":
                value = len(rows)
            else:
                value = next(iter(rows[0].values())) if rows else 0
            return ProbeResult(spec["id"], ok=True, value=value)
        if returns == "one":
            return ProbeResult(spec["id"], ok=True, rows=rows[:1], value=rows[0] if rows else None)
        return ProbeResult(spec["id"], ok=True, rows=rows)

    # ---- one per backend kind ----
    def _sqlite(self, query: str, values: Mapping[str, Any]) -> list[dict[str, Any]]:
        path = sqlite_path(self._profile, self._root)
        if not path.exists():
            raise ProbeSetupError(f"the database file {path} does not exist")
        try:
            conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)  # read only: a probe never changes state
            conn.row_factory = sqlite3.Row
            try:
                return [dict(r) for r in conn.execute(query, values)]
            finally:
                conn.close()
        except sqlite3.Error as e:
            raise ProbeSetupError(f"SQLite: {e}") from e

    def _postgres(self, query: str, values: Mapping[str, Any]) -> list[dict[str, Any]]:
        from psycopg.rows import dict_row

        conn = _pg_connect(self._profile)
        try:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(_pg_query(query), values)
                rows = [{k: _plain(v) for k, v in r.items()} for r in cur.fetchmany(MAX_ROWS)] if cur.description else []
            conn.rollback()
            return rows
        except Exception as e:  # noqa: BLE001 - psycopg.Error and friends
            raise ProbeSetupError(f"Postgres: {e}") from e
        finally:
            conn.close()

    def _rest(self, spec: Mapping[str, Any], values: Mapping[str, Any]) -> Any:
        method, _, path = spec["query"].strip().partition(" ")
        if method.upper() != "GET" or not path.strip():
            raise ProbeSetupError(f"a REST probe is \"GET /path\", got {spec['query']!r}")
        base = _connection(self._profile).rstrip("/")
        if not base.startswith(("http://", "https://")):
            raise ProbeSetupError(f"backend.connection must be an http(s) base URL for rest, got {base!r}")
        url = base + "/" + BIND.sub(lambda m: urllib.parse.quote(str(values[m.group(1)]), safe=""),
                                    path.strip().lstrip("/"))
        try:
            headers = {str(k): str(v) for k, v in expand_env(self._profile["backend"].get("headers") or {}).items()}
        except MissingSecret as e:
            raise ProbeSetupError(str(e)) from e
        req = urllib.request.Request(url, headers={"Accept": "application/json", **headers})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code == 404 and spec.get("empty_on_404"):
                return []
            raise ProbeSetupError(f"GET {url} answered HTTP {e.code}"
                                  + (" (set empty_on_404 if that means 'no record')" if e.code == 404 else "")) from e
        except (urllib.error.URLError, OSError) as e:
            raise ProbeSetupError(f"could not reach {url}: {getattr(e, 'reason', e)}") from e
        try:
            doc = json.loads(raw) if raw.strip() else None
        except ValueError as e:
            raise ProbeSetupError(f"GET {url} did not answer JSON") from e
        if spec.get("rows_field"):
            for part in str(spec["rows_field"]).split("."):
                doc = doc.get(part) if isinstance(doc, dict) else None
        if isinstance(doc, dict):
            return [doc]
        if doc is None:
            return []
        if isinstance(doc, list):
            return [r if isinstance(r, dict) else {"value": r} for r in doc]
        return doc


SqlProbe = BackendProbe  # the Phase 1 name


def _plain(v: Any) -> Any:
    """Postgres values (dates, decimals, UUIDs) as JSON-friendly values."""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)


def check_backend(profile: Mapping[str, Any], root: Path) -> str:
    """Can vatf reach the backend at all? Returns a plain-English line, raises ProbeSetupError."""
    kind = backend_kind(profile)
    if kind == "none":
        return "No backend access: outcome checks that need the database will be NOT_VERIFIED."
    if kind == "sqlite":
        path = sqlite_path(profile, root)
        if not path.exists():
            raise ProbeSetupError(f"the database file {path} does not exist (yet)")
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        finally:
            conn.close()
        return f"Opened {path.name} read-only. Tables: {', '.join(tables) or 'none'}."
    if kind == "postgres":
        conn = _pg_connect(profile)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT current_database(), version()")
                db, version = cur.fetchone()
            conn.rollback()
        finally:
            conn.close()
        return f"Connected to Postgres database {db} ({version.split(',')[0]}), read-only."
    base = _connection(profile).rstrip("/")
    try:
        headers = {str(k): str(v) for k, v in expand_env(profile["backend"].get("headers") or {}).items()}
    except MissingSecret as e:
        raise ProbeSetupError(str(e)) from e
    try:
        with urllib.request.urlopen(urllib.request.Request(base, headers=headers), timeout=10) as resp:
            return f"{base} answered HTTP {resp.status}."
    except urllib.error.HTTPError as e:
        return f"{base} answered HTTP {e.code} (the server is up; probes call their own paths)."
    except (urllib.error.URLError, OSError) as e:
        raise ProbeSetupError(f"could not reach {base}: {getattr(e, 'reason', e)}") from e


def load_fixtures(profile: Mapping[str, Any], scenario: Mapping[str, Any], root: Path) -> None:
    """Runs the scenario's setup.fixtures (SQL files or statements) before a call. Test databases only."""
    fixtures = scenario.get("setup", {}).get("fixtures", [])
    if not fixtures:
        return
    kind = backend_kind(profile)
    if kind not in ("sqlite", "postgres"):
        raise ProbeSetupError("fixtures need a sql backend (SQLite or Postgres)")
    scripts = []
    for item in fixtures:
        if item.endswith(".sql"):
            file = root / item
            if not file.exists():
                raise ProbeSetupError(f"fixture file {file} not found")
            scripts.append(file.read_text(encoding="utf-8"))
        else:
            scripts.append(item)
    if kind == "postgres":
        conn = _pg_connect(profile, read_only=False)
        try:
            with conn.cursor() as cur:
                for script in scripts:
                    cur.execute(script)
            conn.commit()
        except Exception as e:  # noqa: BLE001
            raise ProbeSetupError(f"loading fixtures failed: {e}") from e
        finally:
            conn.close()
        return
    path = sqlite_path(profile, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        for script in scripts:
            conn.executescript(script)
        conn.commit()
    except sqlite3.Error as e:
        raise ProbeSetupError(f"loading fixtures failed: {e}") from e
    finally:
        conn.close()
