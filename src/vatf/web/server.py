"""`vatf ui` and `vatf serve`: the dashboard. A small web server (Python standard library only) that
serves the page in static/ and a JSON API over the same files and commands the terminal uses.

`vatf ui` listens on 127.0.0.1 only; `vatf serve` on the network, for a team. Both need a signed-in user
(auth.py; the first visit creates the admin). Requests that change anything need the X-VATF header, which
a page from another site cannot send without the browser asking first, and the Host header must be one
the server answers to. Scripts and CI use an API key (Authorization: Bearer vk_...) instead.
"""

from __future__ import annotations

import json
import mimetypes
import os
import queue
import re
import secrets
import shutil
import socket
import struct
import sys
import threading
import time
import traceback
import webbrowser
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from . import data, judging, loadtest, monitoring, plans, regress, safety, setup
from ..loader import ProjectError
from .httpbase import BoundedHTTPServer
from .auth import SESSION_COOKIE, Auth, AuthError, at_least, code_settings
from .jobs import JobError, JobManager
from ..ratelimit import RateLimiter
from .schedule import HealthScheduler

STATIC = Path(__file__).parent / "static"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
MAX_BODY = 5 * 1024 * 1024              # JSON requests
MAX_UPLOAD = 50 * 1024 * 1024           # a caller recording (POST /api/setup/recording)
# Requests that cost money or a lot of work (an AI request, a test call to the agent): fewer a minute.
HEAVY = re.compile(r"^/api/(project/[\w.\-]+/(plan/write|variations|safety/write|resilience/write|monitor/import"
                   r"|monitor/check)|runs/[\w-]+/analysis|setup/test-(connection|backend|logs)|alerts/test"
                   r"|project/[\w.\-]+/alerts/test)$")
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Content-Security-Policy": ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                                "img-src 'self' data: blob:; media-src 'self' blob:; font-src 'self' data:; "
                                "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
                                "frame-ancestors 'none'"),
}


def _limit(name: str, default: float) -> float:
    try:
        return max(1.0, float(os.environ.get(name, default)))
    except ValueError:
        return default
TYPES = {".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".html": "text/html",
         ".json": "application/json", ".jsonl": "text/plain", ".txt": "text/plain", ".wav": "audio/wav",
         ".png": "image/png", ".ico": "image/x-icon"}


# Who may do what: GET is reading (viewer), anything else changes something (editor), except these.
ADMIN_ONLY = re.compile(r"^/api/(auth/users(/.*)?|settings/secrets|settings/api-key|audit)$")
OWN_ACCOUNT = re.compile(r"^/api/auth/(me|logout|password|keys(/\d+/delete)?)$")       # any signed-in user
PUBLIC_API = {("GET", "/api/auth/status"), ("POST", "/api/auth/login"), ("POST", "/api/auth/setup")}


class Forbidden(Exception):
    """Signed in, but this needs a higher role."""


def needed_role(method: str, path: str) -> str:
    if ADMIN_ONLY.match(path):
        return "admin"
    if OWN_ACCOUNT.match(path) or method == "GET":
        return "viewer"
    return "editor"


class App:
    def __init__(self, root: Path, runs_dir: Path, auth: Auth | None = None):
        self.root = root
        self.auth = auth          # None: no sign-in (Vocto's own tests); serve() always sets one
        self.runs_rel = runs_dir
        self.runs = root / runs_dir
        self.jobs = JobManager(root, runs_dir)
        self.monitor_hub: monitoring.MonitorHub | None = None   # started by serve()
        self.allowed_hosts = set(LOCAL_HOSTS)
        # Per client (signed-in user, else address): every API request, and the costly ones apart.
        self.api_limit = RateLimiter(_limit("VOCTO_API_PER_MINUTE", 600), burst=_limit("VOCTO_API_BURST", 200))
        self.heavy_limit = RateLimiter(_limit("VOCTO_HEAVY_PER_MINUTE", 20), burst=_limit("VOCTO_HEAVY_BURST", 5))
        self._edit_locks: dict[str, threading.Lock] = {}

    def active_runs(self) -> set[str]:
        return self.jobs.run_ids()

    def audit(self, event: str, h: Any = None, **details: Any) -> None:
        if self.auth is not None:
            self.auth.audit(event, getattr(h, "user", None), h.client_key() if h is not None else "", **details)

    def guarded(self, h: Any, project_path: str, change: Any) -> Any:
        """Make a change to a project's files. Settings that run code or send a secret somewhere
        (auth.code_settings) are an admin's to change: anyone else's change to them is undone and refused."""
        if self.auth is None or at_least((h.user or {}).get("role", ""), "admin"):
            return change()
        folder = data._project_folder(self.root, project_path)
        with self._edit_locks.setdefault(str(folder), threading.Lock()):
            before = _yaml_texts(folder)
            result = change()
            after = _yaml_texts(folder)
            changed = sorted(p for p in set(before) | set(after)
                             if code_settings(_parse(before.get(p))) != code_settings(_parse(after.get(p))))
            if changed:
                for rel in after:
                    target = folder / rel
                    if rel in before:
                        target.write_text(before[rel], encoding="utf-8", newline="")
                    else:
                        target.unlink(missing_ok=True)
                raise Forbidden(f"only an admin may change how the agent is started, Python agents and tools, or "
                                f"where secrets (${{NAME}}) are used ({', '.join(changed)})")
        return result


def _yaml_texts(folder: Path) -> dict[str, str]:
    out = {}
    for p in [*folder.glob("*.yaml"), *(folder / "scenarios").glob("*.yaml"), *(folder / "personas").glob("*.yaml")]:
        try:
            out[p.relative_to(folder).as_posix()] = p.read_text(encoding="utf-8")
        except OSError:
            continue
    return out


def _parse(text: str | None) -> Any:
    import yaml

    if text is None:
        return None
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return {"unreadable": text}


def make_handler(app: App):
    routes: list[tuple[str, re.Pattern, Any]] = []

    def route(method: str, pattern: str):
        def deco(fn):
            routes.append((method, re.compile(f"^{pattern}$"), fn))
            return fn
        return deco

    # ---- API --------------------------------------------------------------------------
    @route("GET", r"/api/system")
    def get_system(h, q):
        info = data.system(app.root, app.runs)
        job = app.jobs.active or next(iter(app.jobs.queued()), None)
        info["active_job"] = job.public() if job else None
        info["jobs"] = app.jobs.stats()
        return info

    @route("GET", r"/api/projects")
    def get_projects(h, q):
        return data.projects(app.root)

    @route("GET", r"/api/projects/files")
    def get_files(h, q):
        return data.project_files(app.root, q["project"])

    @route("GET", r"/api/projects/file")
    def get_file(h, q):
        return {"file": q["file"], "text": data.read_project_file(app.root, q["project"], q["file"])}

    @route("POST", r"/api/projects/check")
    def post_check(h, q):
        b = h.body()
        return data.check_project_text(app.root, b["project"], b["file"], b["text"])

    @route("PUT", r"/api/projects/file")
    def put_file(h, q):
        b = h.body()
        return app.guarded(h, b["project"], lambda: data.write_project_file(app.root, b["project"], b["file"], b["text"],
                                                                            create=bool(b.get("create"))))

    # ---- setting up a project (forms) ----
    @route("POST", r"/api/setup/new-project")
    def post_new_project(h, q):
        out = setup.new_project(app.root, h.body())
        if app.auth is not None and not at_least(h.user["role"], "admin"):
            folder = data._project_folder(app.root, out["path"])
            if any(code_settings(_parse(t)) != code_settings(None) for t in _yaml_texts(folder).values()):
                shutil.rmtree(folder, ignore_errors=True)
                raise Forbidden("only an admin may make an agent that Vocto starts itself, a Python agent or tools, "
                                "or one that uses secrets (${NAME})")
        app.audit("project_created", h, project=out["path"])
        return {**out, "key": data.project_keys(app.root).get(out["path"], out["id"])}

    @route("GET", r"/api/setup/profile")
    def get_setup_profile(h, q):
        return setup.profile_detail(app.root, q["project"], q.get("file", "profile.yaml"))

    @route("PUT", r"/api/setup/profile")
    def put_setup_profile(h, q):
        b = h.body()
        return app.guarded(h, b["project"], lambda: setup.save_profile(app.root, b["project"], b.get("file", "profile.yaml"),
                                                                       b["sections"]))

    @route("GET", r"/api/setup/doc")
    def get_setup_doc(h, q):
        return setup.read_doc(app.root, q["project"], q["file"])

    @route("PUT", r"/api/setup/doc")
    def put_setup_doc(h, q):
        b = h.body()
        return app.guarded(h, b["project"], lambda: setup.save_doc(app.root, b["project"], b["file"], b["doc"],
                                                                   create=bool(b.get("create"))))

    @route("POST", r"/api/setup/delete")
    def post_setup_delete(h, q):
        b = h.body()
        return app.guarded(h, b["project"], lambda: setup.delete_doc(app.root, b["project"], b["file"]))

    @route("POST", r"/api/setup/recording")
    def post_recording(h, q):
        return setup.save_recording(app.root, q["project"], q.get("name", ""), h.raw_body)

    @route("GET", r"/api/setup/recording")
    def get_recording(h, q):
        path = setup.recording_file(app.root, q["project"], q["path"])
        h.send_file(path.parent, path.name)

    @route("POST", r"/api/setup/test-connection")
    def post_test_connection(h, q):
        b = h.body()
        return setup.test_connection(app.root, b["project"], b.get("file", "profile.yaml"), bool(b.get("call")))

    @route("POST", r"/api/setup/test-backend")
    def post_test_backend(h, q):
        b = h.body()
        return setup.test_backend(app.root, b["project"], b.get("file", "profile.yaml"), b.get("probe"),
                                  b.get("binds") or {})

    @route("POST", r"/api/setup/test-logs")
    def post_test_logs(h, q):
        b = h.body()
        return setup.test_logs(app.root, b["project"], b.get("file", "profile.yaml"))

    @route("GET", r"/api/dashboard")
    def get_dashboard(h, q):
        return data.dashboard(app.root, app.runs, app.active_runs())

    @route("GET", r"/api/project/([\w.\-]+)")
    def get_project(h, q, key):
        out = data.project_overview(app.root, app.runs, key, app.active_runs())
        return {**out, "judges": judging.judges_page(app.root, app.runs, key, app.active_runs())["summary"]}

    @route("GET", r"/api/project/([\w.\-]+)/modes")
    def get_project_modes(h, q, key):
        return data.project_modes(app.root, app.runs, key, app.active_runs())

    @route("GET", r"/api/project/([\w.\-]+)/judges")
    def get_judges(h, q, key):
        return judging.judges_page(app.root, app.runs, key, app.active_runs())

    @route("POST", r"/api/project/([\w.\-]+)/judges/examples")
    def post_judge_example(h, q, key):
        return judging.teach(app.root, key, h.body())

    @route("GET", r"/api/project/([\w.\-]+)/safety")
    def get_safety(h, q, key):
        return safety.safety_page(app.root, app.runs, key, q.get("file", "profile.yaml"), app.active_runs())

    @route("POST", r"/api/project/([\w.\-]+)/safety/write")
    def post_safety_write(h, q, key):
        return safety.write_attacks(app.root, key, h.body())

    @route("GET", r"/api/project/([\w.\-]+)/resilience")
    def get_resilience(h, q, key):
        return safety.resilience_page(app.root, key, q.get("file", "profile.yaml"))

    @route("POST", r"/api/project/([\w.\-]+)/resilience/write")
    def post_resilience_write(h, q, key):
        return safety.write_refusals(app.root, key, h.body())

    @route("GET", r"/api/project/([\w.\-]+)/plan")
    def get_plan(h, q, key):
        return plans.plan_page(app.root, key, q.get("file", "profile.yaml"), q.get("test_type"))

    @route("POST", r"/api/project/([\w.\-]+)/plan/write")
    def post_plan_write(h, q, key):
        return plans.generate(app.root, key, h.body())

    @route("POST", r"/api/project/([\w.\-]+)/plan/add")
    def post_plan_add(h, q, key):
        return plans.add_tests(app.root, key, h.body())

    @route("GET", r"/api/project/([\w.\-]+)/coverage")
    def get_coverage(h, q, key):
        return plans.coverage_page(app.root, key, q.get("file", "profile.yaml"))

    @route("POST", r"/api/project/([\w.\-]+)/variations")
    def post_variations(h, q, key):
        return plans.draft_variations(app.root, key, h.body())

    @route("POST", r"/api/project/([\w.\-]+)/variations/add")
    def post_variations_add(h, q, key):
        return plans.add_variations(app.root, key, h.body())

    @route("POST", r"/api/project/([\w.\-]+)/personas/library")
    def post_library_personas(h, q, key):
        return plans.add_personas(app.root, key, h.body())

    @route("GET", r"/api/project/([\w.\-]+)/labels")
    def get_label_queue(h, q, key):
        return judging.label_queue(app.root, app.runs, key, app.active_runs())

    @route("GET", r"/api/runs/(r-[\w-]+)/calls/(t-[\w-]+)/labels")
    def get_call_labels(h, q, run_id, test_id):
        return judging.call_labels(app.root, app.runs, run_id, test_id)

    @route("POST", r"/api/labels")
    def post_labels(h, q):
        if h.body().get("run_id") in app.active_runs():
            raise ValueError("this run is still going; label it when it has finished")
        return judging.save_labels(app.root, app.runs, h.body())

    @route("GET", r"/api/runs")
    def get_runs(h, q):
        rows = data.with_projects(data.list_runs(app.runs, app.active_runs()), data.projects(app.root))
        return [r for r in rows if r["project_key"] == q["project"]] if q.get("project") else rows

    @route("GET", r"/api/runs/(r-[\w-]+)")
    def get_run(h, q, run_id):
        return data.run_detail(app.runs, run_id, app.active_runs(), root=app.root)

    @route("GET", r"/api/runs/(r-[\w-]+)/export.csv")
    def get_run_csv(h, q, run_id):
        from ..readiness import to_csv
        if run_id in app.active_runs():
            raise ValueError("this run is still going; export it when it has finished")
        text = to_csv(data.readiness_of(app.root, app.runs, run_id))
        h.send_bytes(("\ufeff" + text).encode("utf-8"), "text/csv; charset=utf-8", f"vocto-{run_id}.csv")
        return None

    @route("POST", r"/api/runs/(r-[\w-]+)/analysis")
    def post_analysis(h, q, run_id):
        if run_id in app.active_runs():
            raise ValueError("this run is still going; explain it when it has finished")
        return data.run_analysis(app.root, app.runs, run_id, h.body().get("model"))

    @route("GET", r"/api/runs/(r-[\w-]+)/calls/(t-[\w-]+)")
    def get_call(h, q, run_id, test_id):
        return data.call_detail(app.root, app.runs, run_id, test_id)

    @route("GET", r"/api/runs/(r-[\w-]+)/calls/(t-[\w-]+)/waveform")
    def get_wave(h, q, run_id, test_id):
        return data.waveform(app.runs, run_id, test_id, points=int(q.get("points", 1200)))

    # ---- Phase 6: baseline, compare, CI and alerts ----------------------------------------
    @route("GET", r"/api/project/([\w.\-]+)/baseline")
    def get_baseline(h, q, key):
        return regress.baseline_page(app.root, app.runs, key, q.get("file", "profile.yaml"))

    @route("POST", r"/api/project/([\w.\-]+)/baseline/clear")
    def post_baseline_clear(h, q, key):
        b = h.body()
        return regress.clear(app.root, key, b.get("file", "profile.yaml"), b.get("kind") or "run")

    @route("POST", r"/api/runs/(r-[\w-]+)/baseline")
    def post_baseline(h, q, run_id):
        if run_id in app.active_runs():
            raise ValueError("this run is still going; set it as the baseline when it has finished")
        return regress.set_from_run(app.root, app.runs, run_id, str(h.body().get("note") or ""))

    @route("GET", r"/api/runs/(r-[\w-]+)/comparison")
    def get_comparison(h, q, run_id):
        return {"comparison": regress.run_comparison(app.runs, run_id)}

    @route("POST", r"/api/runs/(r-[\w-]+)/keep-test")
    def post_keep_test(h, q, run_id):
        sid = str(h.body().get("scenario_id") or "")
        if not sid:
            raise ValueError("say which test case to keep")
        return regress.keep_as_test(app.root, app.runs, run_id, sid)

    @route("GET", r"/api/compare")
    def get_compare(h, q):
        if not q.get("a") or not q.get("b"):
            raise ValueError("pick two runs")
        return regress.compare_runs(app.root, app.runs, q["a"], q["b"])

    # ---- Phase 7: load tests ------------------------------------------------------------------
    @route("GET", r"/api/project/([\w.\-]+)/load")
    def get_load(h, q, key):
        return loadtest.load_page(app.root, app.runs, key, q.get("file", "profile.yaml"))

    @route("PUT", r"/api/project/([\w.\-]+)/load")
    def put_load(h, q, key):
        b = h.body()
        return app.guarded(h, data.project_by_key(app.root, key)["path"],
                           lambda: loadtest.save(app.root, key, b.get("file", "profile.yaml"), b.get("load") or {}))

    @route("GET", r"/api/project/([\w.\-]+)/ops")
    def get_ops(h, q, key):
        return regress.ops_page(app.root, app.runs, key, q.get("file", "profile.yaml"))

    @route("GET", r"/api/project/([\w.\-]+)/ci")
    def get_ci(h, q, key):
        return regress.ci_file(app.root, key, q)

    @route("POST", r"/api/project/([\w.\-]+)/alerts/test")
    def post_alert_test(h, q, key):
        return regress.test_alert(app.root, key, h.body())

    # ---- Phase 8: real calls -------------------------------------------------------------------
    @route("GET", r"/api/project/([\w.\-]+)/monitor")
    def get_monitor(h, q, key):
        return monitoring.page(app.root, app.runs, key, app.monitor_hub)

    @route("PUT", r"/api/project/([\w.\-]+)/monitor")
    def put_monitor(h, q, key):
        b = h.body()
        return app.guarded(h, data.project_by_key(app.root, key)["path"],
                           lambda: monitoring.save(app.root, app.runs, key, b.get("monitor") or {}, app.monitor_hub))

    @route("GET", r"/api/project/([\w.\-]+)/monitor/calls")
    def get_monitor_calls(h, q, key):
        return monitoring.calls(app.root, app.runs, key, q)

    @route("GET", r"/api/project/([\w.\-]+)/monitor/calls/(c-[\w-]+)")
    def get_monitor_call(h, q, key, call_id):
        return monitoring.call(app.root, app.runs, key, call_id)

    @route("POST", r"/api/project/([\w.\-]+)/monitor/import")
    def post_monitor_import(h, q, key):
        return monitoring.import_files(app.root, app.runs, key, h.body(), app.monitor_hub)

    @route("POST", r"/api/project/([\w.\-]+)/monitor/token")
    def post_monitor_token(h, q, key):
        return monitoring.new_token(app.root, app.runs, key, app.monitor_hub)

    @route("POST", r"/api/project/([\w.\-]+)/monitor/check")
    def post_monitor_check(h, q, key):
        return monitoring.check_now(app.root, app.runs, key, app.monitor_hub)

    @route("POST", r"/api/project/([\w.\-]+)/monitor/delete")
    def post_monitor_delete(h, q, key):
        return monitoring.delete(app.root, app.runs, key, h.body())

    @route("POST", r"/api/project/([\w.\-]+)/monitor/calls/(c-[\w-]+)/labels")
    def post_monitor_labels(h, q, key, call_id):
        return monitoring.label(app.root, app.runs, key, call_id, h.body())

    @route("POST", r"/api/project/([\w.\-]+)/monitor/calls/(c-[\w-]+)/test")
    def post_monitor_test(h, q, key, call_id):
        return monitoring.make_test(app.root, app.runs, key, call_id)

    @route("GET", r"/api/exit-checks")
    def get_exit_checks(h, q):
        rows = data.exit_checks(app.runs)
        if q.get("project"):
            path = data.project_by_key(app.root, q["project"])["path"]
            rows = [r for r in rows if r.get("project") == path]
        return rows

    @route("GET", r"/api/settings/secrets")
    def get_secrets(h, q):
        return data.secrets(app.root)

    @route("POST", r"/api/settings/secrets")
    def post_secret(h, q):
        b = h.body()
        data.save_secret(app.root, str(b.get("name", "")), str(b.get("value", "")))
        app.audit("secret_saved", h, name=str(b.get("name", "")), cleared=not b.get("value"))
        return data.secrets(app.root)

    @route("GET", r"/api/jobs")
    def get_jobs(h, q):
        return [j.public() for j in sorted(app.jobs.jobs.values(), key=lambda j: j.id, reverse=True)]

    @route("POST", r"/api/jobs")
    def post_job(h, q):
        b = h.body()
        project = data.project_by_path(app.root, b.get("project", ""))
        job = app.jobs.start(b, project, user=(h.user or {}).get("username"))
        app.audit("run_started", h, project=project.get("path"), kind=job.kind, job=job.id)
        return job.public()

    @route("POST", r"/api/jobs/preview")
    def post_preview(h, q):
        b = h.body()
        project = data.project_by_path(app.root, b.get("project", ""))
        return {"command": app.jobs.build(b, project)[1]}

    @route("GET", r"/api/jobs/(j-[\w-]+)")
    def get_job(h, q, job_id):
        job = app.jobs.jobs.get(job_id)
        if not job:
            raise data.NotFound("no such run (the dashboard was restarted?)")
        out = job.public()
        out["lines"] = job.lines[-int(q.get("tail", 400)):]
        return out

    @route("POST", r"/api/jobs/(j-[\w-]+)/stop")
    def post_stop(h, q, job_id):
        out = app.jobs.stop(job_id).public()
        app.audit("run_stopped", h, job=job_id)
        return out

    @route("POST", r"/api/settings/api-key")
    def post_key(h, q):
        data.save_api_key(app.root, h.body().get("key", ""))
        app.audit("claude_key_saved", h)
        return data.system(app.root, app.runs)["api_key"]

    @route("GET", r"/api/jobs/(j-[\w-]+)/stream")
    def stream(h, q, job_id):
        h.stream_job(job_id)

    @route("GET", r"/api/jobs/(j-[\w-]+)/listen")
    def listen(h, q, job_id):
        h.listen_job(job_id)

    # ---- signing in, users, API keys, audit log ----
    @route("GET", r"/api/auth/status")
    def auth_status(h, q):
        if app.auth is None:
            return {"enabled": False, "user": None, "setup": False}
        return {"enabled": True, "user": h.user, "setup": not app.auth.has_users()}

    @route("POST", r"/api/auth/setup")
    def auth_setup(h, q):
        if app.auth is None:
            raise ValueError("sign-in is off")
        b = h.body()
        user = app.auth.setup_admin(str(b.get("username", "")), str(b.get("password", "")), str(b.get("name", "")))
        h.user = user
        app.audit("admin_created", h)
        h.set_cookie(app.auth.cookie(app.auth.new_session(user["id"], h.client_key())))
        return {"user": user}

    @route("POST", r"/api/auth/login")
    def auth_login(h, q):
        if app.auth is None:
            raise ValueError("sign-in is off")
        b = h.body()
        name = str(b.get("username", ""))[:64]
        wait = app.auth.login_limit.check(h.client_key())
        if wait:
            raise AuthError(f"too many sign-in attempts: wait {int(wait) + 1} s", 429)
        try:
            user = app.auth.login(name, str(b.get("password", "")))
        except AuthError as e:
            app.auth.audit("sign_in_failed", None, h.client_key(), username=name, why=str(e))
            raise
        h.user = user
        app.audit("signed_in", h)
        h.set_cookie(app.auth.cookie(app.auth.new_session(user["id"], h.client_key())))
        return {"user": user}

    @route("POST", r"/api/auth/logout")
    def auth_logout(h, q):
        if h.session_token:
            app.auth.end_session(h.session_token)
        app.audit("signed_out", h)
        h.set_cookie(app.auth.cookie("", clear=True))
        return {"ok": True}

    @route("GET", r"/api/auth/me")
    def auth_me(h, q):
        return {"user": h.user, "keys": app.auth.keys(h.user["id"])}

    @route("POST", r"/api/auth/password")
    def auth_password(h, q):
        b = h.body()
        if h.user.get("via") != "session":
            raise Forbidden("change your password from the dashboard, not with an API key")
        try:
            app.auth.login(h.user["username"], str(b.get("current", "")))
        except AuthError:
            raise AuthError("your current password is not right", 400) from None
        app.auth.update_user(h.user["id"], password=str(b.get("password", "")))
        app.audit("password_changed", h)
        # Every sign-in ends with a new password; this one carries on with a fresh session.
        h.set_cookie(app.auth.cookie(app.auth.new_session(h.user["id"], h.client_key())))
        return {"ok": True}

    @route("POST", r"/api/auth/keys")
    def auth_new_key(h, q):
        b = h.body()
        key = app.auth.new_key(h.user, str(b.get("name", "")), b.get("role") or None)
        app.audit("api_key_created", h, key=key["name"], role=key["role"], prefix=key["prefix"])
        return key

    @route("GET", r"/api/auth/keys")
    def auth_keys(h, q):
        return app.auth.keys(None if h.user["role"] == "admin" and q.get("all") else h.user["id"])

    @route("POST", r"/api/auth/keys/(\d+)/delete")
    def auth_revoke_key(h, q, key_id):
        out = app.auth.revoke_key(int(key_id), h.user)
        app.audit("api_key_revoked", h, key=out["name"], prefix=out["prefix"])
        return out

    @route("GET", r"/api/auth/users")
    def users(h, q):
        return app.auth.users()

    @route("POST", r"/api/auth/users")
    def new_user(h, q):
        b = h.body()
        user = app.auth.create_user(str(b.get("username", "")), str(b.get("password", "")), str(b.get("role", "viewer")),
                                    str(b.get("name", "")))
        app.audit("user_created", h, target=user["username"], role=user["role"])
        return user

    @route("POST", r"/api/auth/users/(\d+)")
    def edit_user(h, q, user_id):
        b = h.body()
        uid = int(user_id)
        if uid == h.user["id"] and (("role" in b and b["role"] != "admin") or b.get("disabled")):
            raise AuthError("you can't take away your own admin role or switch yourself off")
        user = app.auth.update_user(uid, role=b.get("role"), disabled=b.get("disabled"), name=b.get("name"),
                                    password=b.get("password") or None)
        app.audit("user_changed", h, target=user["username"], role=b.get("role"), disabled=b.get("disabled"),
                  password_reset=bool(b.get("password")) or None)
        return user

    @route("POST", r"/api/auth/users/(\d+)/delete")
    def delete_user(h, q, user_id):
        uid = int(user_id)
        if uid == h.user["id"]:
            raise AuthError("you can't delete your own account")
        user = app.auth.user(uid)
        app.auth.delete_user(uid)
        app.audit("user_deleted", h, target=(user or {}).get("username"))
        return {"ok": True}

    @route("GET", r"/api/audit")
    def audit_log(h, q):
        return app.auth.read_audit(min(int(q.get("limit", 500)), 5000))

    class Handler(BaseHTTPRequestHandler):
        server_version = "vatf-ui"
        sys_version = ""
        protocol_version = "HTTP/1.1"
        timeout = 120            # a client silent this long (mid-request or idle keep-alive) is dropped

        def log_message(self, fmt, *args):  # quiet: the terminal shows only the address and errors
            pass

        # ---- helpers ----
        def body(self) -> dict[str, Any]:
            doc = json.loads(self.raw_body.decode("utf-8") or "{}")
            if not isinstance(doc, dict):
                raise ValueError("expected a JSON object")
            return doc

        def send_json(self, obj: Any, status: int = 200) -> None:
            raw = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def send_bytes(self, raw: bytes, ctype: str, filename: str | None = None) -> None:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(raw)

        def error(self, status: int, message: str) -> None:
            self.send_json({"error": message}, status)

        def end_headers(self) -> None:
            for name, value in SECURITY_HEADERS.items():
                self.send_header(name, value)
            for cookie in getattr(self, "_cookies", ()):
                self.send_header("Set-Cookie", cookie)
            self._cookies = []
            super().end_headers()

        def host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").strip().lower()
            if host.startswith("["):
                host = host[: host.find("]") + 1]
            else:
                host = host.rsplit(":", 1)[0]
            return host in app.allowed_hosts

        def origin_ok(self) -> bool:
            """A browser always says where a request that changes something comes from: it must be this page."""
            origin = self.headers.get("Origin")
            if not origin:
                return True          # not a browser, or a same-site request from an older browser
            return urlparse(origin).netloc.lower() == (self.headers.get("Host") or "").strip().lower()

        def client_key(self) -> str:
            return self.client_address[0] if self.client_address else "?"

        def set_cookie(self, value: str) -> None:
            self._cookies.append(value)

        def find_user(self) -> None:
            """Who is asking: an API key (Authorization: Bearer vk_...), else the session cookie."""
            self.user, self.session_token = None, ""
            if app.auth is None:
                return
            auth = self.headers.get("Authorization") or ""
            if auth[:7].lower() == "bearer ":
                self.user = app.auth.key_user(auth[7:].strip())
                return
            try:
                jar = SimpleCookie(self.headers.get("Cookie") or "")
            except CookieError:
                return
            if SESSION_COOKIE in jar:
                self.session_token = jar[SESSION_COOKIE].value
                self.user = app.auth.session_user(self.session_token)

        def too_many(self, wait: float) -> None:
            self.raw_body = b""
            raw = json.dumps({"error": "too many requests; slow down and try again shortly"}).encode()
            self.send_response(429)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Retry-After", str(max(1, int(wait + 0.999))))
            self.end_headers()
            self.wfile.write(raw)

        # ---- dispatch ----
        def handle_any(self, method: str) -> None:
            url = urlparse(self.path)
            path = unquote(url.path)
            # Always read the whole body first: on a kept-alive connection, unread bytes would
            # be taken as the start of the next request. A body over the limit is refused unread.
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            limit = MAX_UPLOAD if path == "/api/setup/recording" else MAX_BODY
            if n < 0 or n > limit or self.headers.get("Transfer-Encoding"):
                self.close_connection = True
                if limit < n <= 4 * limit:          # read and drop it, so the client sees the answer
                    left = n
                    while left > 0:
                        chunk = self.rfile.read(min(65536, left))
                        if not chunk:
                            break
                        left -= len(chunk)
                return self.error(413 if n > limit else 400, f"request too large ({limit // (1024 * 1024)} MB at most)"
                                  if n > limit else "bad request length")
            self.raw_body = self.rfile.read(n) if n > 0 else b""
            self._cookies = []
            if not self.host_ok():
                return self.error(403, "vatf ui only answers on this computer (open http://127.0.0.1)"
                                  if app.allowed_hosts == LOCAL_HOSTS else
                                  "this server doesn't answer to that host name (add it to VOCTO_ALLOWED_HOSTS)")
            self.find_user()
            by_key = (self.user or {}).get("via") == "key"
            # A script with an API key sends no cookie, so a page on another site can't borrow it: no X-VATF needed.
            if method != "GET" and not by_key and (self.headers.get("X-VATF") != "1" or not self.origin_ok()):
                return self.error(403, "missing X-VATF header" if self.headers.get("X-VATF") != "1"
                                  else "request from another site refused")
            public_page = method == "GET" and (path in ("/", "/index.html", "/favicon.ico") or path.startswith("/static/"))
            if app.auth is not None and not public_page and (method, path) not in PUBLIC_API:
                if self.user is None:
                    if self.headers.get("Authorization"):
                        app.auth.audit("api_key_refused", None, self.client_key(), path=path)
                    return self.error(401, "sign in first")
                if path.startswith("/api/") and not at_least(self.user["role"], needed_role(method, path)):
                    return self.error(403, f"your role ({self.user['role']}) can't do this; "
                                           f"it needs {needed_role(method, path)}")
            if path.startswith("/api/"):
                who = f"user:{self.user['id']}" if self.user else self.client_key()
                wait = app.api_limit.check(who)
                if not wait and method != "GET" and HEAVY.match(path):
                    wait = app.heavy_limit.check(who)
                if wait:
                    return self.too_many(wait)
                q = {k: v[-1] for k, v in parse_qs(url.query).items()}
                for m, pattern, fn in routes:
                    match = pattern.match(path)
                    if m == method and match:
                        try:
                            result = fn(self, q, *match.groups())
                        except data.NotFound as e:
                            return self.error(404, str(e))
                        except AuthError as e:
                            return self.error(e.status, str(e))
                        except Forbidden as e:
                            return self.error(403, str(e))
                        except KeyError as e:
                            return self.error(400, f"missing or unknown value: {e.args[0] if e.args else e}")
                        except (JobError, ValueError, ProjectError) as e:
                            return self.error(400, str(e))
                        except (BrokenPipeError, ConnectionResetError):
                            return None
                        except TimeoutError as e:
                            return self.error(503, f"busy, try again: {e}")
                        except Exception as e:  # noqa: BLE001 - report, keep serving
                            ref = secrets.token_hex(4)
                            print(f"vatf ui: error {ref} on {method} {path}:\n{traceback.format_exc()}", file=sys.stderr)
                            return self.error(500, f"Something went wrong (ref {ref}): {type(e).__name__}: {e}")
                        if result is not None:
                            self.send_json(result)
                        return None
                return self.error(404, "no such API")
            if method != "GET":
                return self.error(405, "method not allowed")
            if path.startswith("/runs/"):
                return self.send_file(app.runs, path[len("/runs/"):], download=False)
            if path in ("/", "/index.html"):
                return self.send_file(STATIC, "index.html")
            if path.startswith("/static/"):
                return self.send_file(STATIC, path[len("/static/"):])
            if path == "/favicon.ico":
                return self.send_file(STATIC, "logo.svg")
            return self.error(404, "not found")

        def do_GET(self):
            self.handle_any("GET")

        def do_POST(self):
            self.handle_any("POST")

        def do_PUT(self):
            self.handle_any("PUT")

        def send_file(self, base: Path, rel: str, download: bool = False) -> None:
            base = base.resolve()
            target = (base / rel).resolve()
            if base not in target.parents or not target.is_file() or any(
                    part.startswith(".") for part in target.relative_to(base).parts):
                return self.error(404, "not found")
            size = target.stat().st_size
            ctype = TYPES.get(target.suffix.lower()) or mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype == "application/json":
                ctype += "; charset=utf-8"
            start, end, status = 0, size - 1, 200
            rng = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range", ""))
            if rng and size:
                a, b = rng.groups()
                if a:
                    start, end = int(a), min(int(b) if b else size - 1, size - 1)
                elif b:
                    start, end = max(0, size - int(b)), size - 1
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return None
                status = 206
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1 if size else 0))
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            with target.open("rb") as f:
                f.seek(start)
                left = end - start + 1 if size else 0
                while left > 0:
                    chunk = f.read(min(65536, left))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        return None
                    left -= len(chunk)
            return None

        # ---- live updates (server-sent events) ----
        def stream_job(self, job_id: str) -> None:
            job = app.jobs.jobs.get(job_id)
            if job is None:
                raise data.NotFound("no such run (the dashboard was restarted?)")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

            def send(kind: str, payload: Any) -> None:
                raw = json.dumps(payload, ensure_ascii=False, default=str)
                self.wfile.write(f"event: {kind}\ndata: {raw}\n\n".encode("utf-8"))
                self.wfile.flush()

            sent_lines, job_sig, run_sigs, call_sigs, last_beat = 0, None, {}, {}, time.monotonic()
            try:
                while True:
                    finished = job.status not in ("running", "queued")
                    if len(job.lines) > sent_lines or sent_lines > len(job.lines):
                        if sent_lines > len(job.lines):
                            sent_lines = 0  # the buffer was trimmed
                        send("log", {"from": sent_lines, "lines": job.lines[sent_lines:]})
                        sent_lines = len(job.lines)
                    for run_id in list(job.run_ids):
                        folder = app.runs / run_id
                        # A load test also rewrites load_live.json every second (its progress).
                        sig = tuple((p.stat().st_mtime_ns if p.exists() else None)
                            for p in (folder / "run.json", folder / "decision.json", folder / "load_live.json"))
                        if run_sigs.get(run_id) != sig:
                            run_sigs[run_id] = sig
                            send("run", data.run_detail(app.runs, run_id, app.active_runs()))
                        # A load test makes hundreds of calls at once: its page shows the load's progress
                        # (load_live.json) and the log, not every call's conversation.
                        calls_dir = folder / "calls"
                        for call in sorted(calls_dir.glob("t-*")) if calls_dir.is_dir() and job.kind != "load" else []:
                            csig = data.call_signature(call)
                            if call_sigs.get(call.name) != csig and (call / "call.json").exists():
                                call_sigs[call.name] = csig
                                try:
                                    send("call", {"run_id": run_id,
                                                  **data.call_detail(app.root, app.runs, run_id, call.name)})
                                except data.NotFound:
                                    call_sigs.pop(call.name, None)
                    pub = job.public()
                    sig = json.dumps(pub, sort_keys=True, default=str)
                    if sig != job_sig:
                        job_sig = sig
                        send("job", pub)
                    if finished and len(job.lines) == sent_lines:
                        send("end", {"status": job.status})
                        return
                    if time.monotonic() - last_beat > 15:
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
                        last_beat = time.monotonic()
                    time.sleep(0.4)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                return

        def listen_job(self, job_id: str) -> None:
            """Listen live: the running call's audio as a stream of length-prefixed tap datagrams."""
            job = app.jobs.jobs.get(job_id)
            if job is None:
                raise data.NotFound("no such run (the dashboard was restarted?)")
            if job.status != "running":
                raise ValueError("this run has finished; play the recording on the call page")
            audio = job.audio
            if audio is None or not audio.port:
                raise ValueError("live listening isn't available on this computer")
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            q = audio.listen()
            try:
                while job.status == "running":
                    try:
                        packet = q.get(timeout=1.0)
                    except queue.Empty:
                        packet = b""            # a heartbeat, so a closed tab is noticed
                    self.wfile.write(struct.pack(">I", len(packet)) + packet)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                return
            finally:
                audio.leave(q)

    return Handler


def server_hosts() -> set[str]:
    """Host names `vatf serve` answers to: this machine's names, localhost, and VOCTO_ALLOWED_HOSTS
    (comma-separated; "*" answers to any, for a server behind a proxy that checks the host itself)."""
    hosts = set(LOCAL_HOSTS)
    for name in (socket.gethostname(), socket.getfqdn()):
        if name:
            hosts.add(name.lower())
    for h in (os.environ.get("VOCTO_ALLOWED_HOSTS") or "").split(","):
        h = h.strip().lower()
        if h:
            hosts.add(h.rsplit(":", 1)[0] if not h.startswith("[") and h.count(":") == 1 else h)
    return hosts


def serve(root: Path, runs_dir: Path, port: int = 8765, open_browser: bool = True, host: str = "127.0.0.1",
          team: bool = False) -> int:
    """`vatf ui` (team=False): this computer only. `vatf serve` (team=True): the network, for a team."""
    if team:
        os.environ.setdefault("VOCTO_URL_POLICY", "server")      # typed-in URLs may not reach this server itself
    app = App(root, runs_dir, auth=Auth(root / runs_dir / ".vocto"))
    if team:
        app.allowed_hosts = server_hosts()
        if host not in ("0.0.0.0", "::", "127.0.0.1", "::1", "localhost"):
            app.allowed_hosts.add(host.lower())
    any_host = "*" in app.allowed_hosts
    if any_host:
        class AnyHost(set):
            def __contains__(self, item):        # behind a proxy that checks the host itself
                return True
        app.allowed_hosts = AnyHost(app.allowed_hosts)
    httpd = None
    tries = range(port, port + 1) if team else range(port, port + 10)
    for p in tries:
        try:
            httpd = BoundedHTTPServer((host, p), make_handler(app))
            break
        except OSError:
            continue
    if httpd is None:
        print(f"error: port {port} is in use. Is Vocto already running?" if team else
              f"error: ports {port}-{port + 9} are all in use. Is vatf ui already running?", file=sys.stderr)
        return 3
    httpd.daemon_threads = True
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    url = f"http://{shown}:{httpd.server_address[1]}/"
    if team:
        print(f"Vocto server running on {host}:{httpd.server_address[1]} (sign-in required).")
        print("Put an HTTPS proxy in front of it and set VOCTO_SECURE_COOKIES=1 (docs/server.md).")
        if not app.auth.has_users():
            print(f"No users yet: open {url} to create the admin account.")
    else:
        print(f"Vocto dashboard running at {url}")
        if not app.auth.has_users():
            print("First visit: create your admin account on that page.")
        print("Keep this window open while you use it. Press Ctrl+C here to stop.")
    if open_browser and not team:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    app.scheduler = HealthScheduler(app.root, app.runs, app.jobs).start()   # health checks while it is open
    app.monitor_hub = monitoring.MonitorHub(app.root, app.runs).start()     # real calls: judging, alerts, webhook
    try:
        httpd.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("\nstopping the dashboard...")
    finally:
        app.jobs.stop_all()
        app.monitor_hub.stop()
        httpd.server_close()
    return 0
