"""Starting and stopping an agent that runs on this computer (profile agent_process).

Most agents are already running somewhere and vatf only calls them. An open-source agent, or a
client's agent on a test machine, can instead be started by vatf: for one run (`vatf run
--start-agent`, or "Start the agent for me" in the dashboard), and by the exit check, which
restarts it once with each planted bug (agent_process.planted_bug_env set to the bug's id).

The agent's own output goes to runs/.agents/<profile id>.log. vatf waits until its ready address
answers, and stops the whole process tree afterwards (uv run starts Python as a child process).
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .util import MissingSecret, expand_env

POLL_S = 0.5
PROGRESS_S = 5


class AgentProcessError(Exception):
    """The agent could not be started (or was already running). A harness error, never the agent's fault."""


def _address(url: str) -> tuple[str, int]:
    u = urlparse(url)
    if not u.hostname:
        raise AgentProcessError(f"{url!r} has no host to wait for")
    default = 443 if u.scheme in ("https", "wss") else 80
    return u.hostname, u.port or default


def _listening(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def _http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            return resp.status < 500
    except urllib.error.HTTPError as e:
        return e.code < 500
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel.CloseHandle(handle)
        return bool(ok) and code.value == 259                  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _kill_tree(pid: int) -> None:
    if sys.platform == "win32":
        # The whole tree: uv run starts the agent's Python as a child of uv.
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.killpg(pid, signal.SIGTERM)   # started in its own session, so its group id is its pid
        except (ProcessLookupError, PermissionError):
            pass


class AgentProcess:
    def __init__(self, profile: Mapping[str, Any], root: Path, runs_dir: Path, planted_bug: str | None = None):
        cfg = profile.get("agent_process")
        if not cfg:
            raise AgentProcessError("the profile has no agent_process section, so vatf doesn't know how to start the agent")
        self.cfg = cfg
        self.root = Path(root)
        self.planted_bug = planted_bug
        self.ready_url = str(cfg.get("ready_url") or profile.get("connection", {}).get("target") or "")
        if not self.ready_url:
            raise AgentProcessError("agent_process needs a ready_url (or a connection.target) to know when the agent is up")
        self.timeout_s = int(cfg.get("ready_timeout_s", 120))
        self.log_path = self.root / runs_dir / ".agents" / f"{profile.get('id', 'agent')}.log"
        self.pid_path = self.log_path.with_suffix(".pid")    # so a later run can stop one this run left behind
        self.proc: subprocess.Popen | None = None
        self._log = None

    @property
    def label(self) -> str:
        return f"planted bug {self.planted_bug}" if self.planted_bug else "clean agent"

    def _ready(self) -> bool:
        if self.ready_url.startswith(("http://", "https://")):
            return _http_ok(self.ready_url)
        return _listening(*_address(self.ready_url))

    def start(self) -> "AgentProcess":
        host, port = _address(self.ready_url)
        if _listening(host, port) and self._stop_leftover():
            print(f"vatf: stopped the agent an earlier run left running at {host}:{port}", flush=True)
        if _listening(host, port):
            raise AgentProcessError(
                f"something is already answering at {host}:{port}, and vatf didn't start it. Stop the agent that is "
                "running there first (close its window): vatf has to start the agent itself (with the planted bug, "
                "if any), or it would test the wrong agent.")
        command = [str(c) for c in self.cfg["command"]]
        exe = shutil.which(command[0], path=os.environ.get("PATH"))
        if exe is None:
            raise AgentProcessError(f"can't find the program {command[0]!r} to start the agent (is it installed?)")
        try:
            extra = {str(k): str(v) for k, v in expand_env(self.cfg.get("env") or {}).items()}
        except MissingSecret as e:
            raise AgentProcessError(f"agent_process.env: {e}") from None
        env = {"PYTHONIOENCODING": "utf-8", **os.environ, **extra, "PYTHONUNBUFFERED": "1"}   # its log file is UTF-8
        env.pop("VIRTUAL_ENV", None)   # vatf's own environment must not leak into the agent's
        if self.cfg.get("planted_bug_env"):
            env[str(self.cfg["planted_bug_env"])] = self.planted_bug or ""
        elif self.planted_bug:
            raise AgentProcessError("agent_process.planted_bug_env is not set, so the planted bug can't be passed on")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log = open(self.log_path, "a", encoding="utf-8", errors="replace")
        self._log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} vatf starts the {self.label}: "
                        f"{' '.join(command)}\n")
        self._log.flush()
        win = sys.platform == "win32"
        self.proc = subprocess.Popen(
            [exe, *command[1:]], cwd=self.root / str(self.cfg.get("cwd", ".")), env=env,
            stdout=self._log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if win else 0, start_new_session=not win)
        try:
            self.pid_path.write_text(f"{self.proc.pid} {os.getpid()}", encoding="utf-8")   # the agent, and this vatf
        except OSError:
            pass
        try:
            return self._wait_ready()
        except BaseException:     # stopped (Ctrl+C, the dashboard's Stop) or failed while starting: never leave it running
            self.stop()
            raise

    def _wait_ready(self) -> "AgentProcess":
        assert self.proc is not None
        print(f"vatf: starting the {self.label}; the first start installs its packages and speech models, "
              "which can take several minutes", flush=True)
        started = time.monotonic()
        deadline = started + self.timeout_s
        shown, next_show = "", started + PROGRESS_S
        while time.monotonic() < deadline:
            if time.monotonic() >= next_show:     # the agent's newest output line, so a long install doesn't look frozen
                next_show += PROGRESS_S
                line = self._last_line()
                if line and line != shown:
                    shown = line
                    print(f"vatf: starting the {self.label} ({int(time.monotonic() - started)} s): {line[:140]}", flush=True)
            if self.proc.poll() is not None:
                code = self.proc.returncode
                raise AgentProcessError(f"the agent stopped while starting (exit code {code}). "
                                        f"Last lines of {self.log_path}:\n{self._tail()}")
            if self._ready():
                return self
            time.sleep(POLL_S)
        raise AgentProcessError(f"the agent did not answer at {self.ready_url} within {self.timeout_s} s "
                                f"(agent_process.ready_timeout_s). Its output is in {self.log_path}.")

    def _last_line(self) -> str:
        try:
            with open(self.log_path, "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 4096))
                lines = f.read().decode("utf-8", "replace").splitlines()
        except OSError:
            return ""
        return next((ln.strip() for ln in reversed(lines) if ln.strip()), "")

    def _tail(self, n: int = 15) -> str:
        try:
            lines = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        return "\n".join("    " + line for line in lines[-n:])

    def _stop_leftover(self) -> bool:
        """An agent an earlier vatf run started and could not stop (that run was killed): stop it now.
        An agent whose vatf run is still going is left alone: that run is using it."""
        try:
            pid, owner = (int(x) for x in self.pid_path.read_text(encoding="utf-8").split())
        except (OSError, ValueError):
            return False
        if _alive(owner):
            return False
        _kill_tree(pid)
        self.pid_path.unlink(missing_ok=True)
        host, port = _address(self.ready_url)
        deadline = time.monotonic() + 15
        while _listening(host, port) and time.monotonic() < deadline:
            time.sleep(POLL_S)
        return True

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is not None and proc.poll() is None:
            _kill_tree(proc.pid)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                if sys.platform != "win32":
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                proc.wait(timeout=5)
        if proc is not None:
            self.pid_path.unlink(missing_ok=True)
        if self._log is not None:
            self._log.write(f"===== {time.strftime('%H:%M:%S')} vatf stopped the {self.label}\n")
            self._log.close()
            self._log = None
        # Wait for the port to be free, so the next start (the next planted bug) can listen on it.
        host, port = _address(self.ready_url)
        deadline = time.monotonic() + 15
        while _listening(host, port) and time.monotonic() < deadline:
            time.sleep(POLL_S)

    def __enter__(self) -> "AgentProcess":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()
