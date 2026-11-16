"""Sign-in, roles, API keys, the admin-only settings guard and the audit log (web/auth.py, web/server.py)."""

from __future__ import annotations

import http.client
import json
import shutil
import threading
from pathlib import Path

import pytest

from vatf.web.auth import Auth, AuthError, check_password, code_settings, hash_password, password_problem
from vatf.web.httpbase import BoundedHTTPServer
from vatf.web.server import App, make_handler, needed_role

ROOT = Path(__file__).resolve().parents[1]
GOOD = "correct horse battery"


@pytest.fixture
def site(tmp_path):
    root = tmp_path / "root"
    shutil.copytree(ROOT / "examples" / "clinic", root / "clinic", ignore=shutil.ignore_patterns("runs"))
    app = App(root, Path("runs"), auth=Auth(root / "runs" / ".vocto"))
    httpd = BoundedHTTPServer(("127.0.0.1", 0), make_handler(app))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
    yield httpd.server_address[1], app, root
    app.jobs.stop_all()
    httpd.shutdown()
    httpd.server_close()


class Client:
    def __init__(self, port: int):
        self.port, self.cookie, self.key = port, "", ""

    def __call__(self, method, path, body=None, xvatf=True, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {**({"X-VATF": "1"} if xvatf else {}), **({"Cookie": self.cookie} if self.cookie else {}),
             **({"Authorization": f"Bearer {self.key}"} if self.key else {}), **(headers or {})}
        conn.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=h)
        r = conn.getresponse()
        raw = r.read()
        for name, value in r.getheaders():
            if name.lower() == "set-cookie":
                self.cookie = value.split(";")[0] if "Max-Age=0" not in value else ""
                self.set_cookie = value
        conn.close()
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, raw


# ---- units --------------------------------------------------------------------------------------

def test_passwords_are_salted_scrypt_hashes():
    a, b = hash_password(GOOD), hash_password(GOOD)
    assert a != b and a.startswith("scrypt$16384$8$1$") and GOOD not in a
    assert check_password(GOOD, a) and not check_password("nope", a) and not check_password(GOOD, "garbage")
    assert password_problem("short") and password_problem("password123") and password_problem("aaaaaaaaaaaa")
    assert password_problem("maria-lopez-2026", "maria") and password_problem(GOOD) is None


def test_code_settings_cover_what_runs_code_or_sends_a_secret():
    base = {"connection": {"type": "websocket_audio", "target": "ws://x", "headers": {"A": "${TOKEN}"}},
            "tools": [{"name": "book", "upstream": "http://x"}], "limits": {"max_turns": 9}}
    same = code_settings(base)
    assert code_settings({**base, "limits": {"max_turns": 3}}) == same                 # ordinary edits are fine
    for change in ({"agent_process": {"command": ["evil"]}},
                   {"connection": {**base["connection"], "launch": "os:system"}},
                   {"connection": {"type": "text_python", "target": "os:system"}},
                   {"tools": [{"name": "book", "upstream": "python:os:system"}]},
                   {"connection": {**base["connection"], "target": "https://evil/?k=${TOKEN}"}}):
        assert code_settings({**base, **change}) != same, change


def test_roles_needed():
    assert needed_role("GET", "/api/runs") == "viewer" and needed_role("POST", "/api/jobs") == "editor"
    assert needed_role("GET", "/api/settings/secrets") == "admin" and needed_role("POST", "/api/auth/users/3") == "admin"
    assert needed_role("POST", "/api/auth/keys") == "viewer" and needed_role("POST", "/api/auth/keys/4/delete") == "viewer"
    assert needed_role("GET", "/api/audit") == "admin"


def test_last_admin_stays_and_lockout(tmp_path):
    auth = Auth(tmp_path)
    admin = auth.setup_admin("ram", GOOD)
    with pytest.raises(AuthError, match="already has an admin"):
        auth.setup_admin("other", GOOD)
    with pytest.raises(AuthError, match="last admin"):
        auth.update_user(admin["id"], role="editor")
    with pytest.raises(AuthError, match="last admin"):
        auth.delete_user(admin["id"])
    for _ in range(5):
        with pytest.raises(AuthError, match="wrong"):
            auth.login("RAM", "wrong password!")
    with pytest.raises(AuthError, match="too many wrong passwords") as e:
        auth.login("ram", GOOD)                     # locked, even with the right password
    assert e.value.status == 429


# ---- over HTTP ----------------------------------------------------------------------------------

def test_first_visit_creates_the_admin_then_everything_needs_sign_in(site):
    port, app, _ = site
    c = Client(port)
    assert c("GET", "/api/auth/status")[1] == {"enabled": True, "user": None, "setup": True}
    assert c("GET", "/")[0] == 200                                  # the page itself loads, to show sign-in
    for path in ("/api/system", "/api/projects", "/runs/x/report.html"):
        assert c("GET", path)[0] == 401, path
    assert c("POST", "/api/auth/setup", {"username": "ram", "password": "short"})[0] == 400
    status, out = c("POST", "/api/auth/setup", {"username": "ram", "password": GOOD, "name": "Ram"})
    assert status == 200 and out["user"]["role"] == "admin"
    assert "HttpOnly" in c.set_cookie and "SameSite=Strict" in c.set_cookie
    assert c("GET", "/api/system")[0] == 200
    assert Client(port)("POST", "/api/auth/setup", {"username": "evil", "password": GOOD})[0] == 409
    assert c("POST", "/api/auth/logout", {})[0] == 200 and c.cookie == ""
    assert c("GET", "/api/system")[0] == 401
    events = [e["event"] for e in app.auth.read_audit()]
    assert events[:2] == ["signed_out", "admin_created"]


def test_roles_keys_and_the_admin_only_guard(site):
    port, app, root = site
    admin = Client(port)
    admin("POST", "/api/auth/setup", {"username": "ram", "password": GOOD})
    for name, role in (("vee", "viewer"), ("eddie", "editor")):
        assert admin("POST", "/api/auth/users", {"username": name, "password": GOOD, "role": role})[0] == 200
    viewer, editor = Client(port), Client(port)
    assert viewer("POST", "/api/auth/login", {"username": "vee", "password": GOOD})[0] == 200
    assert editor("POST", "/api/auth/login", {"username": "EDDIE", "password": GOOD})[0] == 200

    assert viewer("GET", "/api/projects")[0] == 200
    status, out = viewer("PUT", "/api/projects/file", {"project": "clinic", "file": "profile.yaml", "text": "x"})
    assert status == 403 and "viewer" in out["error"]
    assert editor("GET", "/api/settings/secrets")[0] == 403 and editor("GET", "/api/auth/users")[0] == 403
    assert editor("GET", "/api/audit")[0] == 403 and admin("GET", "/api/audit")[0] == 200

    # An editor may change ordinary settings, but not how the agent is started or a Python agent/tool.
    profile = root / "clinic" / "profile.yaml"
    text = profile.read_text("utf-8")
    ok = text.replace("max_turns: 16", "max_turns: 15") if "max_turns: 16" in text else text + "\n# note\n"
    assert editor("PUT", "/api/projects/file", {"project": "clinic", "file": "profile.yaml", "text": ok})[1]["saved"]
    evil = ok.replace('target: "zoo.clinic.agent:build_agent"', 'target: "os:system"')
    assert evil != ok
    status, out = editor("PUT", "/api/projects/file", {"project": "clinic", "file": "profile.yaml", "text": evil})
    assert status == 403 and "only an admin" in out["error"]
    assert profile.read_text("utf-8") == ok                      # undone
    leak = ok + "\nagent_process:\n  command: [python, -c, 'print(1)']\n  ready_url: http://127.0.0.1:1/\n"
    assert editor("PUT", "/api/projects/file", {"project": "clinic", "file": "profile.yaml", "text": leak})[0] == 403
    assert admin("PUT", "/api/projects/file", {"project": "clinic", "file": "profile.yaml", "text": evil})[0] == 200
    assert admin("PUT", "/api/projects/file", {"project": "clinic", "file": "profile.yaml", "text": ok})[0] == 200

    # API keys: shown once, work without the browser header, never outrank their owner, can be revoked.
    status, key = editor("POST", "/api/auth/keys", {"name": "CI", "role": "admin"})
    assert status == 403
    status, key = editor("POST", "/api/auth/keys", {"name": "CI"})
    assert status == 200 and key["key"].startswith("vk_") and key["role"] == "editor"
    script = Client(port)
    script.key = key["key"]
    r = script("PUT", "/api/projects/file", {"project": "clinic", "file": "profile.yaml", "text": ok}, xvatf=False)
    assert r[0] == 200 and r[1]["saved"], r
    assert script("GET", "/api/settings/secrets")[0] == 403
    listed = editor("GET", "/api/auth/keys")[1]
    assert [k["prefix"] for k in listed] == [key["prefix"]] and "key" not in listed[0] and "key_hash" not in listed[0]
    assert editor("POST", f"/api/auth/keys/{key['id']}/delete", {})[0] == 200
    assert script("GET", "/api/projects")[0] == 401
    script.key = "vk_made-up"
    assert script("GET", "/api/projects")[0] == 401

    # Admin turns a user off: signed out at once.
    eddie = next(u for u in admin("GET", "/api/auth/users")[1] if u["username"] == "eddie")
    assert admin("POST", f"/api/auth/users/{eddie['id']}", {"disabled": True})[0] == 200
    assert editor("GET", "/api/projects")[0] == 401
    me = next(u for u in admin("GET", "/api/auth/users")[1] if u["username"] == "ram")
    assert admin("POST", f"/api/auth/users/{me['id']}", {"role": "viewer"})[0] == 400       # not yourself
    events = {e["event"] for e in app.auth.read_audit()}
    assert {"user_created", "signed_in", "api_key_created", "api_key_revoked", "user_changed", "api_key_refused"} <= events


def test_password_change_and_failed_sign_ins(site):
    port, app, _ = site
    c = Client(port)
    c("POST", "/api/auth/setup", {"username": "ram", "password": GOOD})
    other = Client(port)
    assert other("POST", "/api/auth/login", {"username": "ram", "password": GOOD})[0] == 200
    assert c("POST", "/api/auth/password", {"current": "wrong", "password": "a new good secret"})[0] == 400
    assert c("POST", "/api/auth/password", {"current": GOOD, "password": "a new good secret"})[0] == 200
    assert c("GET", "/api/system")[0] == 200                       # this browser carries on
    assert other("GET", "/api/system")[0] == 401                   # every other sign-in ended
    bad = Client(port)
    assert bad("POST", "/api/auth/login", {"username": "ram", "password": GOOD})[0] == 401
    failed = [e for e in app.auth.read_audit() if e["event"] == "sign_in_failed"]
    assert failed and failed[0]["username"] == "ram" and GOOD not in json.dumps(failed)


def test_trusted_proxy_forwarded_address(site, monkeypatch):
    port, app, _ = site
    c = Client(port)
    c("POST", "/api/auth/setup", {"username": "ram", "password": GOOD})
    fwd = {"X-Forwarded-For": "6.6.6.6, 203.0.113.9"}
    Client(port)("POST", "/api/auth/login", {"username": "ram", "password": "wrong one!"}, headers=fwd)
    assert app.auth.read_audit()[0]["ip"] == "127.0.0.1"           # not trusted: the header is ignored
    monkeypatch.setenv("VOCTO_TRUSTED_PROXIES", "127.0.0.0/8, nonsense")
    from vatf.web.server import trusted_proxies
    app.trusted_proxies = trusted_proxies()
    Client(port)("POST", "/api/auth/login", {"username": "ram", "password": "wrong one!"}, headers=fwd)
    assert app.auth.read_audit()[0]["ip"] == "203.0.113.9"         # the hop the proxy saw, not the client-made one
    Client(port)("POST", "/api/auth/login", {"username": "ram", "password": "wrong one!"}, headers={"X-Forwarded-For": "junk"})
    assert app.auth.read_audit()[0]["ip"] == "127.0.0.1"


def test_the_browser_never_sees_server_folders_or_crash_details(site, monkeypatch):
    """No folder path in /api/system for anyone; machine details only for an admin; a crash answers
    with a reference, not the exception's words (which can name files)."""
    port, app, root = site
    admin = Client(port)
    admin("POST", "/api/auth/setup", {"username": "ram", "password": GOOD})
    admin("POST", "/api/auth/users", {"username": "vee", "password": GOOD, "role": "viewer"})
    viewer = Client(port)
    viewer("POST", "/api/auth/login", {"username": "vee", "password": GOOD})
    for who in (admin, viewer):
        status, sys_ = who("GET", "/api/system")
        assert status == 200
        text = json.dumps(sys_)
        assert str(root) not in text and "runs_dir" not in sys_ and "root" not in sys_ and "folder" not in sys_["models"]
    assert "platform" in admin("GET", "/api/system")[1]
    vs = viewer("GET", "/api/system")[1]
    assert "platform" not in vs and "python" not in vs and vs["api_key"]["hint"] is None

    from vatf.web import data
    def boom(*a, **k):
        raise RuntimeError(f"cannot open {root}/secret/thing.yaml")
    monkeypatch.setattr(data, "projects", boom)
    status, out = admin("GET", "/api/projects")
    assert status == 500 and "ref " in out["error"] and str(root) not in out["error"] and "thing.yaml" not in out["error"]


def test_error_words_name_files_by_their_place_in_the_project(tmp_path):
    from vatf.web.server import scrub_paths
    root = tmp_path / "proj"
    msg = f"bad yaml in {root}/clinic/profile.yaml and {root}"
    out = scrub_paths(msg, root)
    assert str(root) not in out and "clinic/profile.yaml" in out.replace("\\", "/")
