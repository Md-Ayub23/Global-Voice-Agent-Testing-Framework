"""Runs started from the dashboard: each one is the real command line (`vatf run` or `vatf exit-check`)
in its own process, so the dashboard and the terminal always do exactly the same thing."""

from __future__ import annotations

import os
import queue
import re
import secrets
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..audio.tap import PORT_ENV, unpack
from ..evidence import utc_now
from ..speech import ENGINES
from ..util import read_json, write_json

RUN_LINE = re.compile(r"^run (r-\d{8}-\d{6}-[0-9a-f]{4}): (\d+) planned call")
LABEL_LINE = re.compile(r"^=== (.+) ===$")
NAME = re.compile(r"^[A-Za-z0-9_.\-]+$")
MODEL = re.compile(r"^[A-Za-z0-9_.\-:/]+$")
MAX_LINES = 5000


class JobError(Exception):
    """The request can't be started (bad input, or another run is still going)."""


@dataclass
class Job:
    id: str
    kind: str                        # run | exit-check
    request: dict[str, Any]
    argv: list[str]
    command: str                     # the same thing typed in a terminal
    started_at: str = field(default_factory=utc_now)
    finished_at: str | None = None
    status: str = "running"          # running | finished | failed | stopped
    exit_code: int | None = None
    lines: list[str] = field(default_factory=list)
    run_ids: list[str] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)   # exit check: run id -> clean / bug name
    summary_path: Path | None = None
    stop_requested: bool = False
    proc: subprocess.Popen | None = None

    def public(self) -> dict[str, Any]:
        out = {"id": self.id, "kind": self.kind, "request": self.request, "command": self.command,
               "started_at": self.started_at, "finished_at": self.finished_at, "status": self.status,
               "exit_code": self.exit_code, "run_ids": list(self.run_ids), "labels": dict(self.labels),
               "line_count": len(self.lines)}
        if self.summary_path and self.summary_path.exists():
            try:
                out["summary"] = read_json(self.summary_path)
            except ValueError:
                pass  # being rewritten right now; the next poll reads it
        return out


class LiveAudio:
    """Listen live: receives the running call's audio from the run's process (see audio/tap.py) and
    hands each datagram to every browser that is listening. A listener that falls behind loses
    audio; nothing here ever slows the run, which doesn't know or care whether anyone listens."""

    QUEUE = 50          # about 5 s of audio per listener; more than that behind and it is dropped

    def __init__(self) -> None:
        self.port: int | None = None
        self._listeners: set[queue.Queue] = set()
        self._lock = threading.Lock()
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.bind(("127.0.0.1", 0))
            self.port = self.sock.getsockname()[1]
        except OSError:
            return          # no live listening on this computer; runs are unaffected
        threading.Thread(target=self._receive, daemon=True).start()

    def _receive(self) -> None:
        while True:
            try:
                packet = self.sock.recv(65536)
            except OSError:
                if self.sock.fileno() < 0:
                    return
                continue    # Windows reports an earlier unreachable send this way; keep going
            if unpack(packet) is None:
                continue
            with self._lock:
                listeners = list(self._listeners)
            for q in listeners:
                try:
                    q.put_nowait(packet)
                except queue.Full:
                    pass

    def listen(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=self.QUEUE)
        with self._lock:
            self._listeners.add(q)
        return q

    def leave(self, q: queue.Queue) -> None:
        with self._lock:
            self._listeners.discard(q)

    @property
    def listeners(self) -> int:
        return len(self._listeners)


def _arg(value: str) -> str:
    return value if re.fullmatch(r"[A-Za-z0-9_./:=,\-]+", value) else shlex.quote(value)


class JobManager:
    """One run at a time: runs share the agent's test database, so two at once would clash."""

    def __init__(self, root: Path, runs_dir: Path):
        self.root = root
        self.runs_dir = runs_dir
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self.audio = LiveAudio()

    @property
    def active(self) -> Job | None:
        return next((j for j in self.jobs.values() if j.status == "running"), None)

    def build(self, req: dict[str, Any], project: dict[str, Any]) -> tuple[list[str], str]:
        """The vatf command line for a request from the New run page. `project` is the
        discovered project (data.project_info) the request names; every value is checked here."""
        kind = req.get("command", "run")
        if kind not in ("run", "exit-check"):
            raise JobError(f"unknown command {kind!r}")
        profile_file = req.get("profile")
        profile = next((p for p in project["profiles"] if p["file"] == profile_file), None)
        if profile is None:
            raise JobError(f"{project['path']} has no profile {profile_file!r}")
        if not profile["ok"]:
            raise JobError(f"{profile_file} has problems; fix them on the Projects page first")
        args = [kind, project["path"], "--profile", profile_file]
        known = {s["id"] for s in profile["scenarios"] if s["in_mode"]}
        for sid in (req.get("scenarios") or []) if kind == "run" else []:  # the exit check picks its own
            if sid not in known:
                raise JobError(f"scenario {sid!r} doesn't run in {profile_file}")
            args += ["--scenario", sid]
        repeats = req.get("repeats")
        if repeats not in (None, ""):
            try:
                repeats = int(repeats)
            except (TypeError, ValueError):
                raise JobError("repeats must be a whole number") from None
            if not 1 <= repeats <= 50:
                raise JobError("repeats must be between 1 and 50")
            args += ["--repeats", str(repeats)]
        for key, value in (req.get("agent_options") or {}).items():
            value = "" if value is None else str(value)
            if not NAME.fullmatch(str(key)) or "\n" in value or "\r" in value:
                raise JobError(f"agent option {key!r} is not valid")
            if kind == "exit-check" and key == "bugs":
                continue  # the exit check sets the bug switches itself
            args += ["--agent-opt", f"{key}={value}"]
        if kind == "run" and req.get("start_agent"):
            if not profile.get("agent_process"):
                raise JobError(f"{profile_file} doesn't say how to start the agent (agent_process)")
            args.append("--start-agent")
            bug = req.get("planted_bug") or ""
            if bug:
                if bug not in {b["id"] for b in profile.get("planted_bugs") or []}:
                    raise JobError(f"{profile_file} has no planted bug {bug!r}")
                args += ["--planted-bug", bug]
        elif req.get("planted_bug"):
            raise JobError("a planted bug needs the agent to be started by Vocto")
        if kind == "exit-check" and not (profile.get("bugs") or profile.get("planted_bugs")):
            raise JobError(f"{profile_file} has no bug switches or planted bugs, so there is nothing for the exit check to catch")
        if req.get("offline"):
            args.append("--offline")
        elif req.get("judges") is False:
            args.append("--no-judges")
        model = req.get("model")
        if model and not req.get("offline"):
            if not MODEL.fullmatch(model):
                raise JobError(f"model {model!r} is not valid")
            args += ["--model", model]
        if profile["mode"] == "voice":
            speech = req.get("speech") or "local"
            if speech not in ENGINES:
                raise JobError(f"speech engine must be one of {', '.join(ENGINES)}")
            args += ["--speech", speech]
            for key, flag, default in (("stt_model", "--stt-model", "base.en"),
                                       ("stt_check_model", "--stt-check-model", "small.en")):
                if speech == "local" and req.get(key) and req[key] != default:
                    if not MODEL.fullmatch(req[key]):
                        raise JobError(f"{key} {req[key]!r} is not valid")
                    args += [flag, req[key]]
        if req.get("safety"):
            args.append("--safety")
        if req.get("show_pii"):
            args.append("--show-pii")
        if kind == "run":
            args.append("--quiet")  # the dashboard shows each call itself; transcript.txt still has it all
        command = "uv run vatf " + " ".join(_arg(a) for a in args if a != "--quiet")
        if self.runs_dir != Path("runs"):
            args += ["--runs-dir", str(self.runs_dir)]
            command += f" --runs-dir {_arg(self.runs_dir.as_posix())}"
        return args, command

    def start(self, req: dict[str, Any], project: dict[str, Any]) -> Job:
        with self._lock:
            if self.active:
                raise JobError("a run is already in progress; wait for it or stop it first")
            args, command = self.build(req, project)
            job_id = f"j-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"
            job = Job(job_id, args[0], dict(req), args, command)
            if job.kind == "exit-check":
                job.summary_path = self.root / self.runs_dir / ".ui" / "exit-checks" / f"{job_id}.json"
                job.summary_path.parent.mkdir(parents=True, exist_ok=True)
                job.argv = [*args, "--summary", str(job.summary_path)]
            env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "VATF_UI_CHILD": "1"}
            if self.audio.port:
                env[PORT_ENV] = str(self.audio.port)
            flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
            job.proc = subprocess.Popen(
                [sys.executable, "-m", "vatf.cli", *job.argv, "--root", str(self.root)],
                cwd=self.root, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                creationflags=flags, start_new_session=sys.platform != "win32")
            self.jobs[job_id] = job
            for old in sorted(self.jobs)[:-30]:  # keep the last 30 in memory; runs stay on disk
                if self.jobs[old].status != "running":
                    del self.jobs[old]
            threading.Thread(target=self._read, args=(job,), daemon=True).start()
            return job

    def _read(self, job: Job) -> None:
        label = None
        assert job.proc and job.proc.stdout
        for raw in job.proc.stdout:
            line = raw.rstrip("\r\n")
            m = LABEL_LINE.match(line)
            if m:
                label = m.group(1)
            m = RUN_LINE.match(line)
            if m:
                job.run_ids.append(m.group(1))
                if job.kind == "exit-check" and label:
                    job.labels[m.group(1)] = label
            job.lines.append(line)
            if len(job.lines) > MAX_LINES:
                del job.lines[: len(job.lines) - MAX_LINES]
        code = job.proc.wait()
        job.exit_code = code
        job.finished_at = utc_now()
        if job.stop_requested:
            job.status = "stopped"
        elif code in (0, 1, 2):     # GO / NO_GO / not enough evidence (exit check: 0 passed, 1 not)
            job.status = "finished"
        else:
            job.status = "failed"
        self._seal_runs(job)

    def _seal_runs(self, job: Job) -> None:
        """A run that was stopped (or crashed) still says "running" in its run.json; say what happened."""
        for run_id in job.run_ids:
            path = self.root / self.runs_dir / run_id / "run.json"
            try:
                info = read_json(path)
            except (OSError, ValueError):
                continue
            if info.get("status") == "running":
                info.update(status="stopped" if job.stop_requested else "failed", finished_at=job.finished_at)
                write_json(path, info)

    def stop(self, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if job is None:
            raise JobError("no such run")
        if job.status != "running" or job.proc is None:
            return job
        job.stop_requested = True
        job.lines.append("vatf ui: stopping the run...")
        proc = job.proc
        try:  # Ctrl+C for the run's own process group: vatf marks the run as stopped itself
            if sys.platform == "win32":
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(proc.pid, signal.SIGINT)
        except ProcessLookupError:
            return job
        except OSError:
            proc.kill()
            return job
        # Time to stop the agent it started too (a whole process tree); killed only if it doesn't stop by then.
        threading.Timer(30, lambda: proc.poll() is None and proc.kill()).start()
        return job

    def stop_all(self) -> None:
        for job in list(self.jobs.values()):
            if job.status == "running" and job.proc:
                job.stop_requested = True
                job.proc.kill()
