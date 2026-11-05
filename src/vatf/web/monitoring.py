"""What the dashboard's Real calls page reads and writes (Phase 8), and the background work behind it:
judging new calls, alert rules and retention every few minutes, and the webhook listener for agents
that switch it on (one port for all agents, only while the dashboard is open)."""

from __future__ import annotations

import base64
import re
import secrets as pysecrets
import threading
from pathlib import Path
from typing import Any, Mapping

from .. import metrics as qm
from ..loader import ProjectError
from ..monitor.intake import IntakeError
from ..monitor.listener import Listener, token_of
from ..monitor.service import Monitor, have_key
from ..monitor.settings import DEFAULTS, DETECTORS
from ..story import story_rows
from ..util import ENV_REF
from . import data, setup

MAX_UPLOAD = 25 * 1024 * 1024
FILTERS = {"all", "flagged", "review", "failed", "escalated", "pending"}


def _monitor(root: Path, runs: Path, key: str) -> tuple[dict[str, Any], Monitor]:
    project = data.project_by_key(root, key)
    return project, Monitor(root, runs, root / project["path"])


def _secret_name(raw: Any) -> str | None:
    m = ENV_REF.search(str(raw or ""))
    return m.group(1) if m else None


def page(root: Path, runs: Path, key: str, hub: "MonitorHub | None" = None) -> dict[str, Any]:
    project, m = _monitor(root, runs, key)
    o = m.overview()
    token_raw = m.settings["intake"].get("token")
    name = _secret_name(token_raw)
    o.update({
        "project": {k: project[k] for k in ("key", "name", "domain")},
        "saved": m.profile.get("monitor") or {}, "defaults": DEFAULTS, "detectors": list(DETECTORS),
        "intake": {"token_name": name, "token_set": bool(token_of(m)), "listen": bool(m.settings["intake"]["listen"]),
                   "port": m.settings["intake"]["port"],
                   "url": hub.url_for(key) if hub else None, "running": bool(hub and hub.running_for(key)),
                   "error": hub.error if hub else None, "received": hub.listener.received if hub and hub.listener else 0},
        "metric_defs": qm.metric_list(m.profile),
        "judging": bool(hub and hub.judging(key)),
    })
    return o


def calls(root: Path, runs: Path, key: str, q: Mapping[str, str]) -> dict[str, Any]:
    _, m = _monitor(root, runs, key)
    rows = m.store.rows()
    f = q.get("filter", "all")
    if f not in FILTERS:
        raise ValueError(f"filter is one of {', '.join(sorted(FILTERS))}")
    if f == "flagged":
        rows = [r for r in rows if r.get("flagged")]
    elif f == "review":
        rows = [r for r in rows if r.get("status") == "NEEDS_REVIEW" or any(x.get("kind") == "review" or x.get("review")
                                                                            for x in r.get("flags") or [])]
    elif f == "failed":
        rows = [r for r in rows if r.get("status") == "FAIL"]
    elif f == "escalated":
        rows = [r for r in rows if r.get("escalated")]
    elif f == "pending":
        rows = [r for r in rows if r.get("judge_state") == "pending"]
    if q.get("version"):
        rows = [r for r in rows if str(r.get("agent_version") or "") == q["version"]]
    if q.get("q"):
        needle = q["q"].lower()
        rows = [r for r in rows if needle in (r.get("summary") or "").lower() or needle in r["call_id"]
                or any(needle in t.lower() for t in r.get("flag_text") or [])]
    limit = min(int(q.get("limit", 50)), 500)
    offset = max(int(q.get("offset", 0)), 0)
    return {"total": len(rows), "rows": [{k: v for k, v in r.items() if k not in ("quality", "latency_ms")}
                                         for r in rows[offset:offset + limit]]}


def _read(m: Monitor, call_id: str):
    try:
        return m.store.read(call_id)
    except KeyError:
        raise data.NotFound(f"no real call {call_id}") from None


def call(root: Path, runs: Path, key: str, call_id: str) -> dict[str, Any]:
    _, m = _monitor(root, runs, key)
    record, events, verdict = _read(m, call_id)
    return {"record": record, "events": events, "verdict": verdict,
            "story": [{"seq": n, "who": who, "text": text} for n, who, text in story_rows(events)],
            "metric_defs": qm.metric_list(m.profile),
            "judge_items": {i.id: i.title for i in m.items()}}


def import_files(root: Path, runs: Path, key: str, body: Mapping[str, Any], hub: "MonitorHub | None" = None) -> dict[str, Any]:
    """body: {files: [{name, text} or {name, base64}]}. Returns per-file results; judging starts in the background."""
    _, m = _monitor(root, runs, key)
    files = body.get("files") or []
    if not files:
        raise ValueError("choose one or more call files")
    results, added = [], 0
    for f in files:
        name = str(f.get("name") or "upload")
        try:
            content = base64.b64decode(f["base64"]) if f.get("base64") else str(f.get("text") or "").encode()
            if len(content) > MAX_UPLOAD:
                raise IntakeError("file too large (25 MB at most); split it")
            out = m.ingest(content, name, via="upload", check_alerts=False)
        except (IntakeError, ValueError) as e:
            results.append({"name": name, "error": str(e)})
            continue
        added += len(out["added"])
        results.append({"name": name, "added": len(out["added"]), "duplicates": len(out["duplicates"]),
                        "problems": out["problems"], "first": out["added"][0] if out["added"] else None})
    alerts = m.check_alerts()
    if hub and added:
        hub.judge_soon(key, root / data.project_by_key(root, key)["path"])
    return {"files": results, "added": added, "alerts": {"active": alerts["active"], "raised": alerts["raised"]},
            "judging": bool(hub and added and m.settings["scoring"]["judges"] and have_key())}


def save(root: Path, runs: Path, key: str, section: Mapping[str, Any], hub: "MonitorHub | None" = None) -> dict[str, Any]:
    project = data.project_by_key(root, key)
    clean = setup._clean(dict(section))
    problems = []
    for p in ((clean.get("privacy") or {}).get("patterns") or []):
        try:
            re.compile(p.get("regex", ""))
        except re.error as e:
            problems.append(f"pattern {p.get('name')!r}: not a valid regular expression ({e})")
    if problems:
        return {"saved": False, "problems": problems}
    out = setup.save_profile(root, project["path"], "profile.yaml", {"monitor": clean or None},
                             replace=frozenset({"monitor"}))
    if out.get("saved") and hub:
        hub.refresh()
    return out


def new_token(root: Path, runs: Path, key: str, hub: "MonitorHub | None" = None) -> dict[str, Any]:
    """A fresh random token: saved in the .env file, named in the profile as ${NAME}. Shown once."""
    project, m = _monitor(root, runs, key)
    name = _secret_name(m.settings["intake"].get("token")) or \
        "VOCTO_CALLS_" + re.sub(r"[^A-Z0-9]+", "_", m.slug.upper()).strip("_") + "_TOKEN"
    value = pysecrets.token_urlsafe(24)
    data.save_secret(root, name, value)
    intake = dict((m.profile.get("monitor") or {}).get("intake") or {})
    intake["token"] = "${" + name + "}"
    raw = dict(m.profile.get("monitor") or {})
    raw["intake"] = intake
    out = setup.save_profile(root, project["path"], "profile.yaml", {"monitor": raw}, replace=frozenset({"monitor"}))
    if not out.get("saved"):
        return out
    if hub:
        hub.refresh()
    return {"saved": True, "name": name, "token": value}


def make_test(root: Path, runs: Path, key: str, call_id: str) -> dict[str, Any]:
    project, m = _monitor(root, runs, key)
    _read(m, call_id)
    file, doc = m.scenario_from(call_id)
    try:
        return setup.save_doc(root, project["path"], file, doc, create=True)
    except ValueError as e:
        if "already exists" in str(e):
            return {"saved": True, "file": file, "existed": True, "problems": []}
        raise


def label(root: Path, runs: Path, key: str, call_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
    _, m = _monitor(root, runs, key)
    _read(m, call_id)
    choices = body.get("labels") or {}
    if not isinstance(choices, Mapping):
        raise ValueError("labels must map judge item ids to PASS, FAIL or NOT_APPLICABLE")
    verdict = m.label(call_id, dict(choices), str(body.get("by") or ""))
    m.check_alerts()
    return {"verdict": verdict}


def delete(root: Path, runs: Path, key: str, body: Mapping[str, Any]) -> dict[str, Any]:
    _, m = _monitor(root, runs, key)
    ids = [r["call_id"] for r in m.store.rows()] if body.get("all") else list(body.get("call_ids") or [])
    n = m.delete(ids)
    m.check_alerts()
    return {"deleted": n}


def check_now(root: Path, runs: Path, key: str, hub: "MonitorHub | None" = None) -> dict[str, Any]:
    project, m = _monitor(root, runs, key)
    out = m.maintain()
    if hub and m.store.rows() and any(r.get("judge_state") == "pending" for r in m.store.rows()):
        hub.judge_soon(key, root / project["path"])
    return {"deleted_old": out["deleted_old"], "active": out["alerts"]["active"], "sent": out["alerts"]["sent"]}


class MonitorHub:
    """The dashboard's background side of monitoring: judging, upkeep and the webhook listener."""

    UPKEEP_SECONDS = 300

    def __init__(self, root: Path, runs: Path):
        self.root, self.runs = root, runs
        self.listener: Listener | None = None
        self.error: str | None = None
        self.port: int | None = None
        self._judging: set[str] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()

    # ---- which agents ----
    def _agents(self) -> list[tuple[dict[str, Any], Monitor]]:
        out = []
        for p in data.projects(self.root):
            try:
                out.append((p, Monitor(self.root, self.runs, self.root / p["path"])))
            except (ProjectError, OSError, ValueError):
                continue
        return out

    def _listening(self) -> dict[str, Monitor]:
        return {p["key"]: m for p, m in self._agents() if m.settings["enabled"] and m.settings["intake"]["listen"]}

    # ---- listener ----
    def refresh(self) -> None:
        """Starts the listener when an agent switches it on, stops it when none does."""
        want = self._listening()
        port = next((m.settings["intake"]["port"] for m in want.values()), None)
        with self._lock:
            if self.listener and (not want or port != self.port):
                self.listener.stop()
                self.listener, self.port = None, None
            if want and not self.listener:
                try:
                    self.listener = Listener(self._resolve, "127.0.0.1", int(port), judge=True,
                                             log=lambda msg: None).start()
                    self.port, self.error = self.listener.port, None
                except OSError as e:
                    self.error = f"couldn't listen on port {port}: {e.strerror or e}. Pick another port."

    def _resolve(self, key: str) -> Monitor | None:
        try:
            p = data.project_by_key(self.root, key)
            m = Monitor(self.root, self.runs, self.root / p["path"])
        except (data.NotFound, ProjectError, OSError, ValueError):
            return None
        return m if m.settings["enabled"] and m.settings["intake"]["listen"] else None

    def url_for(self, key: str) -> str | None:
        return f"http://127.0.0.1:{self.port}/ingest/{key}" if self.listener else None

    def running_for(self, key: str) -> bool:
        return bool(self.listener) and self._resolve(key) is not None

    # ---- judging ----
    def judging(self, key: str) -> bool:
        return key in self._judging

    def judge_soon(self, key: str, folder: Path) -> None:
        if key in self._judging or not have_key():
            return
        self._judging.add(key)

        def work():
            try:
                m = Monitor(self.root, self.runs, folder)
                m.judge_pending()
                m.check_alerts()
            except Exception as e:  # noqa: BLE001 - never take the dashboard down
                self.error = f"judging real calls failed: {type(e).__name__}: {e}"
            finally:
                self._judging.discard(key)
        threading.Thread(target=work, daemon=True).start()

    # ---- upkeep ----
    def start(self) -> "MonitorHub":
        self.refresh()
        threading.Thread(target=self._loop, daemon=True).start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self.listener:
            self.listener.stop()

    def _loop(self) -> None:
        while not self._stop.wait(self.UPKEEP_SECONDS):
            self.upkeep()

    def upkeep(self) -> None:
        """Retention, alert rules and waiting judge work for every agent that has real calls."""
        for p, m in self._agents():
            try:
                if not m.store.index_file.is_file():
                    continue
                m.maintain()
                if any(r.get("judge_state") == "pending" for r in m.store.rows()):
                    self.judge_soon(p["key"], self.root / p["path"])
            except Exception as e:  # noqa: BLE001
                self.error = f"{p['name']}: {type(e).__name__}: {e}"
