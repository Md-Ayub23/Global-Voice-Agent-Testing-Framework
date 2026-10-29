"""Logs access (L5, platform logs): the agent's own log lines for each test call.

The profile's `logs` section says where they are:

  source: file    path: a log file the agent writes (relative to the repo folder, or absolute).
                  match: during_call (default) keeps the lines written while the call ran;
                         test_id keeps only those lines that contain the call's test id.
  source: http    url: a log search URL with {test_id}, {started_at} and {ended_at} in it, e.g.
                  https://logs.example.com/api/search?q={test_id}&from={started_at}&to={ended_at}
                  The answer is plain text (one line per line) or JSON: a list, or the list at
                  lines_field; each item is a string or an object whose message_field is the text.
                  headers: e.g. {Authorization: "Bearer ${LOGS_TOKEN}"} (names, not secrets).

Lines become platform_event evidence ({kind: log}) after the call, with one summary event first
({kind: log_collected}), so the log_contains / log_not_contains checks can cite them.
Logs rank below backend state and tool traffic: they say what the agent reported, not what happened.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from .util import MissingSecret, expand_env

DEFAULT_MAX_LINES = 300


class LogSetupError(Exception):
    """The logs could not be read. A harness problem, never the agent's fault."""


@dataclass
class LogResult:
    ok: bool
    source: str
    lines: list[str] = field(default_factory=list)
    truncated: bool = False
    error: str = ""


def log_source(profile: Mapping[str, Any]) -> str:
    return (profile.get("logs") or {}).get("source", "none")


class LogCollector:
    def __init__(self, profile: Mapping[str, Any], root: Path):
        self._cfg = dict(profile.get("logs") or {})
        self._root = root
        self._offset = 0

    @property
    def enabled(self) -> bool:
        return self._cfg.get("source", "none") in ("file", "http")

    def _path(self) -> Path:
        path = Path(self._cfg.get("path", ""))
        return path if path.is_absolute() else self._root / path

    def where(self) -> str:
        return str(self._cfg.get("path") if self._cfg.get("source") == "file" else self._cfg.get("url", ""))

    def begin(self) -> None:
        """Called when the call starts: remembers where the log file ends now."""
        if self._cfg.get("source") == "file":
            try:
                self._offset = self._path().stat().st_size
            except OSError:
                self._offset = 0

    def collect(self, test_id: str, started_at: str, ended_at: str) -> LogResult:
        """Called after the call: the lines that belong to it."""
        settle = int(self._cfg.get("settle_ms", 500))
        if settle > 0:
            time.sleep(settle / 1000)   # let the agent flush its last lines
        try:
            if self._cfg.get("source") == "file":
                lines = self._file_lines(test_id)
            elif self._cfg.get("source") == "http":
                lines = self._http_lines(test_id, started_at, ended_at)
            else:
                return LogResult(False, "none", error="no logs configured")
        except LogSetupError as e:
            return LogResult(False, self.where(), error=str(e))
        limit = int(self._cfg.get("max_lines", DEFAULT_MAX_LINES))
        return LogResult(True, self.where(), lines[-limit:], truncated=len(lines) > limit)

    def _file_lines(self, test_id: str) -> list[str]:
        path = self._path()
        if not path.exists():
            raise LogSetupError(f"the log file {path} does not exist")
        with path.open("rb") as f:
            size = path.stat().st_size
            if size < self._offset:      # the file was rotated or truncated during the call
                self._offset = 0
            f.seek(self._offset)
            text = f.read().decode("utf-8", errors="replace")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if self._cfg.get("match", "during_call") == "test_id":
            lines = [ln for ln in lines if test_id in ln]
        return lines

    def _http_lines(self, test_id: str, started_at: str, ended_at: str) -> list[str]:
        values = {"test_id": test_id, "started_at": started_at, "ended_at": ended_at}
        template = str(self._cfg.get("url", ""))
        try:
            url = expand_env(template)
            for k, v in values.items():
                url = url.replace("{" + k + "}", urllib.parse.quote(v, safe=""))
            headers = {str(k): str(v) for k, v in expand_env(self._cfg.get("headers") or {}).items()}
        except MissingSecret as e:
            raise LogSetupError(str(e)) from e
        if not url.startswith(("http://", "https://")):
            raise LogSetupError(f"logs.url must be an http(s) URL, got {template!r}")
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                ctype = resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            raise LogSetupError(f"the log URL answered HTTP {e.code}") from e
        except (urllib.error.URLError, OSError) as e:
            raise LogSetupError(f"could not reach the log URL: {getattr(e, 'reason', e)}") from e
        doc: Any = None
        if "json" in ctype or raw.lstrip()[:1] in ("[", "{"):
            try:
                doc = json.loads(raw)
            except ValueError:
                doc = None
        if doc is None:
            return [ln for ln in raw.splitlines() if ln.strip()]
        for part in str(self._cfg.get("lines_field", "")).split("."):
            if part and isinstance(doc, dict):
                doc = doc.get(part)
        if not isinstance(doc, list):
            raise LogSetupError("the log answer is JSON but not a list (set lines_field to where the list is)")
        msg = self._cfg.get("message_field", "message")
        out = []
        for item in doc:
            if isinstance(item, dict):
                item = item.get(msg, json.dumps(item, ensure_ascii=False, default=str))
            out.append(str(item))
        return out


def sample_logs(profile: Mapping[str, Any], root: Path, n: int = 20) -> LogResult:
    """For the dashboard's "Test" button: the last lines of the file, or a search for the last 10 minutes."""
    collector = LogCollector(profile, root)
    cfg = profile.get("logs") or {}
    if cfg.get("source") == "file":
        path = collector._path()
        if not path.exists():
            return LogResult(False, collector.where(), error=f"the log file {path} does not exist")
        lines = [ln for ln in path.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
        return LogResult(True, collector.where(), lines[-n:], truncated=len(lines) > n)
    if cfg.get("source") == "http":
        now = datetime.now(timezone.utc)
        iso = lambda t: t.isoformat(timespec="seconds").replace("+00:00", "Z")  # noqa: E731
        try:
            lines = collector._http_lines("t-vocto-connection-test", iso(now - timedelta(minutes=10)), iso(now))
        except LogSetupError as e:
            return LogResult(False, collector.where(), error=str(e))
        return LogResult(True, collector.where(), lines[-n:], truncated=len(lines) > n)
    return LogResult(False, "none", error="no logs configured")
