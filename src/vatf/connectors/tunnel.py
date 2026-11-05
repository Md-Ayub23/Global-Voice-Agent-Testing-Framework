"""A public address for Vocto's phone line, so a phone provider can reach a laptop.

When Vocto calls a real phone number, the provider (Twilio) answers the call on its side and opens
a WebSocket back to Vocto to stream the call's audio. That WebSocket needs a public wss:// address.

Two ways, in this order:
  1. connection.options.public_url  an address the team already has (their own domain, a
                                    reverse proxy, a running ngrok): wss://... or https://...
  2. a Cloudflare quick tunnel      started by Vocto: `cloudflared tunnel --url http://127.0.0.1:<port>`
                                    gives a random https://<words>.trycloudflare.com address. No
                                    account, no sign-up. The cloudflared program is downloaded once
                                    into models/tools/ from Cloudflare's GitHub releases.

The tunnel lives as long as the Vocto process (one per run or dashboard job) and is stopped on exit.
"""

from __future__ import annotations

import atexit
import os
import platform
import re
import shutil
import stat
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

RELEASES = "https://github.com/cloudflare/cloudflared/releases/latest/download/"
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
START_TIMEOUT_S = 45


class TunnelError(Exception):
    """The public address could not be made. A harness error, never blamed on the agent."""


def _asset() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "amd64"
    if system == "windows":
        return f"cloudflared-windows-{arch}.exe"
    if system == "darwin":
        return f"cloudflared-darwin-{arch}.tgz"
    return f"cloudflared-linux-{arch}"


def cloudflared_path(root: Path) -> Path:
    """The cloudflared program: on PATH, or in models/tools/, downloaded there the first time."""
    found = shutil.which("cloudflared")
    if found:
        return Path(found)
    tools = Path(root) / "models" / "tools"
    exe = tools / ("cloudflared.exe" if platform.system() == "Windows" else "cloudflared")
    if exe.is_file():
        return exe
    tools.mkdir(parents=True, exist_ok=True)
    asset = _asset()
    tmp = tools / (asset + ".part")
    try:
        with urllib.request.urlopen(RELEASES + asset, timeout=120) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
    except Exception as e:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        raise TunnelError(f"could not download cloudflared (needed for a public address): {e}. "
                          "Install it yourself, or set the connection's public address.") from e
    if asset.endswith(".tgz"):
        import tarfile

        with tarfile.open(tmp) as t:
            member = next(m for m in t.getmembers() if m.name.endswith("cloudflared"))
            member.name = exe.name
            t.extract(member, tools)
        tmp.unlink(missing_ok=True)
    else:
        tmp.replace(exe)
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return exe


class QuickTunnel:
    """One `cloudflared tunnel --url` process and the public https address it printed."""

    def __init__(self, exe: Path, port: int):
        self.port = port
        self.url = ""
        self._lines: list[str] = []
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self._proc = subprocess.Popen(
            [str(exe), "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", creationflags=flags)
        self._found = threading.Event()
        threading.Thread(target=self._read, daemon=True).start()
        atexit.register(self.stop)

    def _read(self) -> None:
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            if len(self._lines) < 200:
                self._lines.append(line.rstrip())
            m = URL_RE.search(line)
            if m and not self.url:
                self.url = m.group(0)
                self._found.set()
        self._found.set()   # the process ended

    def wait(self, timeout: float = START_TIMEOUT_S) -> str:
        self._found.wait(timeout)
        if not self.url:
            tail = " | ".join(x for x in self._lines[-4:] if x)
            self.stop()
            raise TunnelError("cloudflared did not give a public address" + (f": {tail}" if tail else ""))
        return self.url

    def alive(self) -> bool:
        return self._proc.poll() is None

    def stop(self) -> None:
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(5)
            except subprocess.TimeoutExpired:
                self._proc.kill()


def wait_reachable(https_url: str, timeout: float = 40.0) -> None:
    """A new quick-tunnel name takes a few seconds to resolve everywhere: wait until /health answers."""
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(https_url.rstrip("/") + "/health", timeout=8) as r:
                if r.status == 200:
                    return
        except Exception as e:  # noqa: BLE001
            last = str(e)
        time.sleep(1.5)
    raise TunnelError(f"the public address {https_url} did not answer in time ({last})")


def ws_url(public: str) -> str:
    """https://x -> wss://x (and http -> ws), without a trailing slash."""
    public = public.strip().rstrip("/")
    if public.startswith("https://"):
        return "wss://" + public[len("https://"):]
    if public.startswith("http://"):
        return "ws://" + public[len("http://"):]
    return public
