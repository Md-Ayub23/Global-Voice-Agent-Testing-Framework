"""Health checks on a schedule while the dashboard is open (Phase 6, health.py).

Once a minute: every profile with health_checks.enabled whose last health check is older than
every_minutes gets one, as an ordinary dashboard run (`python -m vatf health ...`). Only when no other
run is going (runs share the agent's test database); a check that can't start waits for the next
minute. A check that fails before it writes a run is not retried until every_minutes has passed.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from . import regress
from .jobs import JobError, JobManager

TICK_SECONDS = 60


class HealthScheduler:
    def __init__(self, root: Path, runs: Path, jobs: JobManager):
        self.root, self.runs, self.jobs = root, runs, jobs
        self.attempts: dict[tuple[str, str], float] = {}   # (project path, profile file) -> last start
        self.last_error: str | None = None
        self._stop = threading.Event()

    def start(self) -> "HealthScheduler":
        threading.Thread(target=self._loop, daemon=True).start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(TICK_SECONDS):
            try:
                self.tick()
            except Exception as e:  # never take the dashboard down
                self.last_error = f"{type(e).__name__}: {e}"

    def tick(self, now: float | None = None) -> dict[str, Any] | None:
        """Starts at most one due health check; returns the job started (public form) or None."""
        now = time.time() if now is None else now
        if self.jobs.active:
            return None
        for row in regress.health_status(self.root, self.runs, now):
            key = (row["project"]["path"], row["file"])
            tried = self.attempts.get(key)
            if not row["due"] or (tried is not None and now - tried < row["every_minutes"] * 60):
                continue
            self.attempts[key] = now
            req = {"command": "health", "profile": row["file"],
                   "start_agent": row["start_agent"]}
            try:
                return self.jobs.start(req, row["project"]).public()
            except JobError as e:
                self.last_error = str(e)
                return None
        return None
