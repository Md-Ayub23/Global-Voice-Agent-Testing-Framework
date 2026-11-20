"""Files on the server an agent's settings point at (SQLite backend, log file, .sql fixtures): an editor
can't aim them at Vocto's own secrets or users store, and nobody can reach those through SQLite tricks."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.test_auth import GOOD, Client, site  # noqa: F401  (the fixture)
from vatf.logs import sample_logs
from vatf.probe import BackendProbe, ProbeSetupError, load_fixtures, sqlite_path
from vatf.util import private_file_problem
from vatf.web.auth import code_settings


def test_private_files_are_never_an_agents_file(tmp_path):
    for rel in (".env", ".env.local", "runs/.vocto/users.db", "runs/.vocto/audit.jsonl", ".git/config",
                "zoo/../.env", "x/.vocto/anything.db"):
        assert private_file_problem(tmp_path / rel), rel
    for rel in ("zoo/clinic/clinic.db", "logs/agent.log", "fixtures/seed.sql"):
        assert private_file_problem(tmp_path / rel) is None, rel


def test_sqlite_and_log_paths_refuse_private_files(tmp_path):
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-secret\n")
    result = sample_logs({"logs": {"source": "file", "path": ".env"}}, tmp_path)
    assert not result.ok and "private" in result.error and not result.lines
    with pytest.raises(ProbeSetupError):
        sqlite_path({"backend": {"connection": "sqlite:///runs/.vocto/users.db"}}, tmp_path)
    with pytest.raises(ProbeSetupError):
        load_fixtures({"backend": {"access": "sql", "connection": "sqlite:///a.db"}},
                      {"setup": {"fixtures": [".env.sql"]}}, tmp_path)


def test_sqlite_queries_and_fixtures_cannot_attach_other_files(tmp_path):
    other = tmp_path / "other.db"
    sqlite3.connect(other).executescript("CREATE TABLE users(role TEXT); INSERT INTO users VALUES('editor');")
    profile = {"backend": {"access": "sql", "connection": "sqlite:///agent.db"}}
    sqlite3.connect(tmp_path / "agent.db").executescript("CREATE TABLE t(x);")
    with pytest.raises(ProbeSetupError):
        load_fixtures(profile, {"setup": {"fixtures": [
            f"ATTACH DATABASE '{other}' AS u; UPDATE u.users SET role='admin';"]}}, tmp_path)
    with pytest.raises(ProbeSetupError):
        load_fixtures(profile, {"setup": {"fixtures": [f"VACUUM INTO '{tmp_path / 'copy.db'}'"]}}, tmp_path)
    assert sqlite3.connect(other).execute("SELECT role FROM users").fetchall() == [("editor",)]
    assert not (tmp_path / "copy.db").exists()
    load_fixtures(profile, {"setup": {"fixtures": ["INSERT INTO t VALUES (1);"]}}, tmp_path)   # still works
    r = BackendProbe(profile, tmp_path).run_with({"id": "p", "query": f"ATTACH DATABASE '{other}' AS u"}, {})
    assert not r.ok


def test_choosing_server_files_is_an_admins_change():
    base = {"backend": {"access": "sql", "connection": "sqlite:///zoo/clinic/clinic.db"}}
    same = code_settings(base)
    assert code_settings({"backend": {**base["backend"], "connection": "sqlite:///other.db"}}) != same
    assert code_settings({**base, "logs": {"source": "file", "path": "x.log"}}) != same
    assert code_settings({"setup": {"fixtures": ["seed.sql"]}}) != code_settings({})
    assert code_settings({"setup": {"fixtures": ["DELETE FROM t"]}}) == code_settings({})   # inline SQL: editors


def test_an_editor_cannot_read_secrets_or_password_hashes(site):  # noqa: F811
    port, app, root = site
    (root / ".env").write_text("ANTHROPIC_API_KEY=sk-secret-123\n")
    admin = Client(port)
    admin("POST", "/api/auth/setup", {"username": "ram", "password": GOOD})
    admin("POST", "/api/auth/users", {"username": "eddie", "password": GOOD, "role": "editor"})
    editor = Client(port)
    editor("POST", "/api/auth/login", {"username": "eddie", "password": GOOD})
    profile = root / "clinic" / "profile.yaml"
    text = profile.read_text("utf-8")
    for evil in (text + "\nlogs:\n  source: file\n  path: .env\n",
                 text.replace("sqlite:///zoo/clinic/clinic.db", "sqlite:///runs/.vocto/users.db")):
        assert evil != text
        status, out = editor("PUT", "/api/projects/file", {"project": "clinic", "file": "profile.yaml", "text": evil})
        assert status == 403 and "only an admin" in out["error"]
        assert profile.read_text("utf-8") == text
    # Even an admin's setting can't read Vocto's own files.
    assert admin("PUT", "/api/projects/file", {"project": "clinic", "file": "profile.yaml",
                                               "text": text + "\nlogs:\n  source: file\n  path: .env\n"})[0] == 200
    out = admin("POST", "/api/setup/test-logs", {"project": "clinic"})[1]
    assert not out["ok"] and "sk-secret" not in str(out)
