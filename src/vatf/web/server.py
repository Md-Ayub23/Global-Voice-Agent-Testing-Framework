"""`vatf ui`: the dashboard. A small local web server (Python standard library only) that serves the
page in static/ and a JSON API over the same files and commands the terminal uses.

It listens on 127.0.0.1 only. Requests that change anything need the X-VATF header, which a page
from another site cannot send without the browser asking first, and the Host header must be local.
"""

from __future__ import annotations

import json
import mimetypes
import re
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from . import data, setup
from ..loader import ProjectError
from .jobs import JobError, JobManager

STATIC = Path(__file__).parent / "static"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
TYPES = {".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".html": "text/html",
         ".json": "application/json", ".jsonl": "text/plain", ".txt": "text/plain", ".wav": "audio/wav",
         ".png": "image/png", ".ico": "image/x-icon"}


class App:
    def __init__(self, root: Path, runs_dir: Path):
        self.root = root
        self.runs_rel = runs_dir
        self.runs = root / runs_dir
        self.jobs = JobManager(root, runs_dir)

    def active_runs(self) -> set[str]:
        job = self.jobs.active
        return set(job.run_ids) if job else set()


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
        job = app.jobs.active
        info["active_job"] = job.public() if job else None
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
        return data.write_project_file(app.root, b["project"], b["file"], b["text"], create=bool(b.get("create")))

    # ---- setting up a project (forms) ----
    @route("POST", r"/api/setup/new-project")
    def post_new_project(h, q):
        out = setup.new_project(app.root, h.body())
        return {**out, "key": data.project_keys(app.root).get(out["path"], out["id"])}

    @route("GET", r"/api/setup/profile")
    def get_setup_profile(h, q):
        return setup.profile_detail(app.root, q["project"], q.get("file", "profile.yaml"))

    @route("PUT", r"/api/setup/profile")
    def put_setup_profile(h, q):
        b = h.body()
        return setup.save_profile(app.root, b["project"], b.get("file", "profile.yaml"), b["sections"])

    @route("GET", r"/api/setup/doc")
    def get_setup_doc(h, q):
        return setup.read_doc(app.root, q["project"], q["file"])

    @route("PUT", r"/api/setup/doc")
    def put_setup_doc(h, q):
        b = h.body()
        return setup.save_doc(app.root, b["project"], b["file"], b["doc"], create=bool(b.get("create")))

    @route("POST", r"/api/setup/delete")
    def post_setup_delete(h, q):
        b = h.body()
        return setup.delete_doc(app.root, b["project"], b["file"])

    @route("POST", r"/api/setup/test-connection")
    def post_test_connection(h, q):
        b = h.body()
        return setup.test_connection(app.root, b["project"], b.get("file", "profile.yaml"))

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
        return data.project_overview(app.root, app.runs, key, app.active_runs())

    @route("GET", r"/api/runs")
    def get_runs(h, q):
        rows = data.with_projects(data.list_runs(app.runs, app.active_runs()), data.projects(app.root))
        return [r for r in rows if r["project_key"] == q["project"]] if q.get("project") else rows

    @route("GET", r"/api/runs/(r-[\w-]+)")
    def get_run(h, q, run_id):
        return data.run_detail(app.runs, run_id, app.active_runs())

    @route("GET", r"/api/runs/(r-[\w-]+)/calls/(t-[\w-]+)")
    def get_call(h, q, run_id, test_id):
        return data.call_detail(app.root, app.runs, run_id, test_id)

    @route("GET", r"/api/runs/(r-[\w-]+)/calls/(t-[\w-]+)/waveform")
    def get_wave(h, q, run_id, test_id):
        return data.waveform(app.runs, run_id, test_id, points=int(q.get("points", 1200)))

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
        return data.secrets(app.root)

    @route("GET", r"/api/jobs")
    def get_jobs(h, q):
        return [j.public() for j in sorted(app.jobs.jobs.values(), key=lambda j: j.id, reverse=True)]

    @route("POST", r"/api/jobs")
    def post_job(h, q):
        b = h.body()
        project = data.project_by_path(app.root, b.get("project", ""))
        job = app.jobs.start(b, project)
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
        return app.jobs.stop(job_id).public()

    @route("POST", r"/api/settings/api-key")
    def post_key(h, q):
        data.save_api_key(app.root, h.body().get("key", ""))
        return data.system(app.root, app.runs)["api_key"]

    @route("GET", r"/api/jobs/(j-[\w-]+)/stream")
    def stream(h, q, job_id):
        h.stream_job(job_id)

    class Handler(BaseHTTPRequestHandler):
        server_version = "vatf-ui"
        protocol_version = "HTTP/1.1"

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

        def error(self, status: int, message: str) -> None:
            self.send_json({"error": message}, status)

        def host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            return host in LOCAL_HOSTS

        # ---- dispatch ----
        def handle_any(self, method: str) -> None:
            # Always read the whole body first: on a kept-alive connection, unread bytes would
            # be taken as the start of the next request.
            n = int(self.headers.get("Content-Length") or 0)
            self.raw_body = self.rfile.read(n) if n > 0 else b""
            if not self.host_ok():
                return self.error(403, "vatf ui only answers on this computer (open http://127.0.0.1)")
            url = urlparse(self.path)
            path = unquote(url.path)
            if method != "GET" and self.headers.get("X-VATF") != "1":
                return self.error(403, "missing X-VATF header")
            if path.startswith("/api/"):
                q = {k: v[-1] for k, v in parse_qs(url.query).items()}
                for m, pattern, fn in routes:
                    match = pattern.match(path)
                    if m == method and match:
                        try:
                            result = fn(self, q, *match.groups())
                        except data.NotFound as e:
                            return self.error(404, str(e))
                        except KeyError as e:
                            return self.error(400, f"missing or unknown value: {e.args[0] if e.args else e}")
                        except (JobError, ValueError, ProjectError) as e:
                            return self.error(400, str(e))
                        except (BrokenPipeError, ConnectionResetError):
                            return None
                        except Exception as e:  # noqa: BLE001 - report, keep serving
                            return self.error(500, f"{type(e).__name__}: {e}")
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
                    finished = job.status != "running"
                    if len(job.lines) > sent_lines or sent_lines > len(job.lines):
                        if sent_lines > len(job.lines):
                            sent_lines = 0  # the buffer was trimmed
                        send("log", {"from": sent_lines, "lines": job.lines[sent_lines:]})
                        sent_lines = len(job.lines)
                    for run_id in list(job.run_ids):
                        folder = app.runs / run_id
                        sig = tuple((p.stat().st_mtime_ns if p.exists() else None)
                            for p in (folder / "run.json", folder / "decision.json"))
                        if run_sigs.get(run_id) != sig:
                            run_sigs[run_id] = sig
                            send("run", data.run_detail(app.runs, run_id, app.active_runs()))
                        for call in sorted((folder / "calls").glob("t-*")) if (folder / "calls").is_dir() else []:
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

    return Handler


def serve(root: Path, runs_dir: Path, port: int = 8765, open_browser: bool = True) -> int:
    app = App(root, runs_dir)
    httpd = None
    for p in range(port, port + 10):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", p), make_handler(app))
            break
        except OSError:
            continue
    if httpd is None:
        print(f"error: ports {port}-{port + 9} are all in use. Is vatf ui already running?", file=sys.stderr)
        return 3
    httpd.daemon_threads = True
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"Vocto dashboard running at {url}")
    print("Keep this window open while you use it. Press Ctrl+C here to stop.")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("\nstopping the dashboard...")
    finally:
        app.jobs.stop_all()
        httpd.server_close()
    return 0
