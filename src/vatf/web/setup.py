"""Setting up a test project from the dashboard: create a project for a new agent, edit its profile
section by section (connection, tools, backend, logs, test settings), write personas and scenarios
from forms, and try the connection, backend and logs before the first run.

Every save goes through the same schemas and loader as `vatf validate`, and YAML files are edited
round-trip (ruamel), so comments and layout written by hand survive a save from a form.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import yaml
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from ..gateway import gateway_address, tool_url, uses_gateway
from ..loader import ProjectError, load_profile, load_project, schema_errors
from ..logs import sample_logs
from ..probe import BackendProbe, ProbeSetupError, check_backend
from ..util import MissingSecret, expand_env
from . import data

PROJECTS_DIR = "projects"
ID = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
SECTIONS = {"name", "description", "agent_prompt", "domain", "languages", "connection", "test_id", "tools", "tool_gateway",
            "backend", "logs", "judges", "safety", "thresholds", "limits", "pii", "agent_process", "planted_bugs"}

# Framework defaults, shown in the forms as what an empty field means (client_profile.schema.yaml).
DEFAULTS = {
    "thresholds": {"repeats": 3, "pass_k_required": None, "latency_p50_ms": 800, "latency_p95_ms": 1500,
                   "min_coverage": 0.9, "judge_min_labels": 20, "judge_min_fails": 5,
                   "judge_min_tpr": 0.9, "judge_min_tnr": 0.9, "max_harness_retries": 2},
    "limits": {"max_turns": 30, "follow_up_turns": 6, "max_call_seconds": 300, "max_reply_tokens": 400, "budget_usd": 5,
               "parallel_calls": 1, "end_of_turn_ms": 1000, "no_reply_ms": 15000},
}


# ---- round-trip YAML ----------------------------------------------------------------------

def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    y.width = 4096
    return y


def _load_rt(text: str) -> Any:
    return _yaml().load(text)


def _dump_rt(doc: Any) -> str:
    buf = io.StringIO()
    _yaml().dump(doc, buf)
    return buf.getvalue()


def deep_update(old: Any, new: Any) -> Any:
    """`new` written over `old`, keeping old's comments, key order and quoting wherever a value is unchanged."""
    if isinstance(old, CommentedMap) and isinstance(new, Mapping):
        for k in [k for k in old if k not in new]:
            del old[k]
        for k, v in new.items():
            old[k] = deep_update(old[k], v) if k in old else v
        return old
    if isinstance(old, CommentedSeq) and isinstance(new, list) and len(old) == len(new):
        for i, v in enumerate(new):
            old[i] = deep_update(old[i], v)
        return old
    if type(old) is type(new) and old == new:
        return old
    if isinstance(old, str) and isinstance(new, str) and str(old) == new:
        return old
    return new


def _plain(doc: Any) -> Any:
    """ruamel's types -> plain JSON-friendly values."""
    return json.loads(json.dumps(doc, default=str))


def _clean(value: Any) -> Any:
    """Drops empty strings, None and empty containers that forms send for unset fields."""
    if isinstance(value, Mapping):
        out = {k: _clean(v) for k, v in value.items()}
        return {k: v for k, v in out.items() if v not in (None, "", [], {})}
    if isinstance(value, list):
        return [_clean(v) for v in value if _clean(v) not in (None, "", {})]
    return value


# ---- projects -------------------------------------------------------------------------------

def _folder(root: Path, project: str) -> Path:
    return data._project_folder(root, project)


def _profile_file(folder: Path, file: str) -> Path:
    if not data.FILE_NAME.match(file) or "/" in file:
        raise ValueError(f"{file!r} is not a profile file name")
    path = folder / file
    if not path.is_file():
        raise data.NotFound(f"{file} not found")
    return path


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40]


def new_project(root: Path, body: Mapping[str, Any]) -> dict[str, Any]:
    """Creates projects/<id>/ with a profile (connection still to fill in), one persona and an empty
    scenarios/ folder. Projects are git-ignored: they hold client addresses."""
    name = str(body.get("name", "")).strip()
    if not name:
        raise ValueError("give the agent a name")
    pid = str(body.get("id") or slug(name))
    if not ID.match(pid):
        raise ValueError("the id may use lower-case letters, digits, dots and dashes only")
    folder = root / PROJECTS_DIR / pid
    if folder.exists():
        raise ValueError(f"there is already a project called {pid!r}; pick another name")
    channel = body.get("channel", "voice")
    if channel == "voice" and body.get("voice_format") == "twilio":
        connection: dict[str, Any] = {"type": "twilio_media_stream", "target": str(body.get("target", ""))}
    elif channel == "voice":
        connection = {"type": "websocket_audio", "target": str(body.get("target", "")),
                      "audio": {"sample_rate_hz": 16000, "encoding": "pcm16"}}
    elif channel == "text":
        connection = {"type": "text_http", "target": str(body.get("target", ""))}
    else:
        raise ValueError("channel must be voice or text")
    profile = {
        "schema_version": "0.1", "kind": "client_profile", "id": pid, "name": name,
        "description": str(body.get("description", "")), "domain": str(body.get("domain", "")) or "general",
        "languages": ["en"], "connection": connection,
        "test_id": {"propagation": ["proxy_header", "session_metadata"], "header_name": "X-Test-Id"},
        "tools": [], "backend": {"access": "none"}, "logs": {"source": "none"},
        "thresholds": {"repeats": 3, "latency_p50_ms": 800, "latency_p95_ms": 1500, "min_coverage": 0.9},
        "limits": {"max_turns": 30, "max_call_seconds": 300, "budget_usd": 5},
    }
    persona = {"schema_version": "0.1", "kind": "persona", "id": "calm-adult",
               "description": "A polite adult caller who answers questions clearly and waits for the agent to finish.",
               "traits": {"patience": "high", "verbosity": "normal", "mood": "calm", "clarity": "high"}}
    header = (f"# Client profile for {name}: how Vocto reaches the agent, its tools, backend and logs,\n"
              "# and the test settings. Made in the dashboard (Projects > New project); edit there or here.\n"
              "# Secrets are written as ${NAME} and kept in the .env file, never here.\n")
    (folder / "scenarios").mkdir(parents=True)
    (folder / "personas").mkdir()
    (folder / "profile.yaml").write_text(header + yaml.safe_dump(profile, sort_keys=False, allow_unicode=True),
                                         encoding="utf-8", newline="\n")
    (folder / "personas" / "calm-adult.yaml").write_text(
        yaml.safe_dump(persona, sort_keys=False, allow_unicode=True), encoding="utf-8", newline="\n")
    _ignore_projects(root)
    return {"path": f"{PROJECTS_DIR}/{pid}", "id": pid}


def _ignore_projects(root: Path) -> None:
    gi = root / ".gitignore"
    lines = gi.read_text(encoding="utf-8").splitlines() if gi.is_file() else []
    if "/projects/" not in lines:
        lines += ["# client test projects (addresses of real agents): kept on this computer only", "/projects/"]
        gi.write_text("\n".join(lines) + "\n", encoding="utf-8")


def profile_detail(root: Path, project: str, file: str) -> dict[str, Any]:
    folder = _folder(root, project)
    path = _profile_file(folder, file)
    raw = _plain(_load_rt(path.read_text(encoding="utf-8"))) or {}
    merged: dict[str, Any] = {}
    problems: list[str] = []
    base: dict[str, Any] = {}
    try:
        merged, _ = load_profile(folder, file)
        problems += [f"{file}: {e}" for e in schema_errors("client_profile", merged)]
        if raw.get("extends"):
            base, _ = load_profile(folder, raw["extends"])
    except ProjectError as e:
        problems.append(str(e))
    try:
        load_project(folder, file)
    except ProjectError as e:
        problems += [p.strip() for p in str(e).splitlines()[1:]] or [str(e)]
    problems = list(dict.fromkeys(problems))
    host, port = gateway_address(merged or raw)
    personas = sorted(p.stem for p in (folder / "personas").glob("*.yaml"))
    return {
        "project": project, "file": file, "folder": str(folder), "raw": raw, "merged": merged or raw, "base": base,
        "extends": raw.get("extends"), "problems": problems, "defaults": DEFAULTS,
        "gateway": {"used": uses_gateway(merged or raw), "host": host, "port": port,
                    "tools": {t["name"]: tool_url(merged or raw, t["name"]) for t in (merged or raw).get("tools", [])}},
        "personas": personas,
        "scenarios": [s for s in data.project_files(root, project) if s["kind"] == "scenario"],
        "persona_files": [s for s in data.project_files(root, project) if s["kind"] == "persona"],
        "profiles": [f["file"] for f in data.project_files(root, project) if f["kind"] == "client_profile"],
    }


def save_profile(root: Path, project: str, file: str, sections: Mapping[str, Any]) -> dict[str, Any]:
    """Replaces the given top-level sections of one profile file (None removes a section).
    Refused, with the reasons, if the profile would no longer pass its schema."""
    folder = _folder(root, project)
    path = _profile_file(folder, file)
    unknown = set(sections) - SECTIONS
    if unknown:
        raise ValueError(f"these sections can't be edited here: {', '.join(sorted(unknown))}")
    doc = _load_rt(path.read_text(encoding="utf-8"))
    for key, value in sections.items():
        value = _clean(value) if isinstance(value, (Mapping, list)) else value
        if value is None or (isinstance(value, (Mapping, list)) and not value):
            doc.pop(key, None)
        elif key in doc:
            doc[key] = deep_update(doc[key], value)
        else:
            doc[key] = value
    text = _dump_rt(doc)
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "project"
        shutil.copytree(folder, copy, ignore=shutil.ignore_patterns("runs", "*.db", "__pycache__"))
        (copy / file).write_text(text, encoding="utf-8")
        try:
            merged, _ = load_profile(copy, file)
            errors = schema_errors("client_profile", merged)
        except ProjectError as e:
            errors = [str(e)]
        # other profiles that extend this one must still load too
        for other in copy.glob("*.yaml"):
            d = data._yaml(other)
            if other.name != file and isinstance(d, dict) and d.get("extends") == file:
                try:
                    errors += [f"{other.name}: {e}" for e in schema_errors("client_profile", load_profile(copy, other.name)[0])]
                except ProjectError as e:
                    errors.append(str(e))
    if errors:
        return {"saved": False, "problems": errors}
    path.write_text(text, encoding="utf-8", newline="\n")
    detail = profile_detail(root, project, file)
    return {"saved": True, "problems": detail["problems"], "detail": detail}


# ---- personas and scenarios -----------------------------------------------------------------

def _doc_path(folder: Path, file: str) -> Path:
    parts = file.split("/")
    if len(parts) != 2 or parts[0] not in ("scenarios", "personas") or not data.FILE_NAME.match(parts[1]):
        raise ValueError(f"{file!r} is not a scenario or persona file")
    return folder / parts[0] / parts[1]


def read_doc(root: Path, project: str, file: str) -> dict[str, Any]:
    path = _doc_path(_folder(root, project), file)
    if not path.is_file():
        raise data.NotFound(f"{file} not found")
    return {"file": file, "doc": _plain(_load_rt(path.read_text(encoding="utf-8")))}


def save_doc(root: Path, project: str, file: str, doc: Mapping[str, Any], create: bool = False) -> dict[str, Any]:
    """Writes a persona or scenario from a form. Refused, with the reasons, if this file would have
    problems (its schema, or what it points at: persona, tools, probes, caller facts)."""
    folder = _folder(root, project)
    path = _doc_path(folder, file)
    kind = "scenario" if file.startswith("scenarios/") else "persona"
    if create and path.exists():
        raise ValueError(f"{file} already exists")
    if not create and not path.is_file():
        raise data.NotFound(f"{file} not found")
    doc = _clean(dict(doc))
    doc = {"schema_version": "0.1", "kind": kind, **{k: v for k, v in doc.items() if k not in ("schema_version", "kind")}}
    if path.is_file():
        old = _load_rt(path.read_text(encoding="utf-8"))
        text = _dump_rt(deep_update(old, doc))
    else:
        text = _dump_rt(doc)
    problems = [f"{file}: {e}" for e in schema_errors(kind, yaml.safe_load(text))]
    if not problems:
        check = data.check_project_text(root, project, file, text)
        problems = [p for p in check["problems"] if file in p]
        others = [p for p in check["problems"] if file not in p]
    else:
        others = []
    if problems:
        return {"saved": False, "problems": problems}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return {"saved": True, "file": file, "problems": others}


def delete_doc(root: Path, project: str, file: str) -> dict[str, Any]:
    path = _doc_path(_folder(root, project), file)
    if not path.is_file():
        raise data.NotFound(f"{file} not found")
    path.unlink()
    return {"deleted": file}


# ---- trying things out ----------------------------------------------------------------------

def _merged(root: Path, project: str, file: str) -> dict[str, Any]:
    folder = _folder(root, project)
    _profile_file(folder, file)
    profile, _ = load_profile(folder, file)
    return profile


def test_connection(root: Path, project: str, file: str) -> dict[str, Any]:
    """Opens one short session with the agent, as a call would, and reports what came back."""
    profile = _merged(root, project, file)
    conn = profile.get("connection", {})
    kind = conn.get("type")
    try:
        if kind == "text_python" or conn.get("launch"):
            return {"ok": True, "message": "Vocto starts this agent itself for each call "
                    f"({conn.get('launch') or conn.get('target')}); there is no address to test."}
        if kind == "text_http":
            return asyncio.run(_test_text_http(profile))
        if kind == "websocket_audio":
            return asyncio.run(_test_websocket(profile))
        if kind == "twilio_media_stream":
            return asyncio.run(_test_twilio(profile))
        return {"ok": False, "message": f"connection type {kind!r} can't be tested yet"}
    except Exception as e:  # noqa: BLE001 - every failure is a plain answer for the form
        return {"ok": False, "message": str(e) or type(e).__name__}


async def _test_text_http(profile: Mapping[str, Any]) -> dict[str, Any]:
    from ..connectors.text_http import TextHttpConnector
    from ..contracts.models import CallContext

    ctx = CallContext(test_id="t-vocto-connection-test", run_id="r-test", profile=profile, scenario={},
                      repeat=1, attempt=1, mode="text")
    session = await TextHttpConnector(profile, Path("."), None).open(ctx)
    greeting = await session.receive()
    await session.close()
    return {"ok": True, "message": "Connected. The agent answered the start message.",
            "details": [f"Agent: {greeting.text or '(no greeting text)'}"]}


async def _test_websocket(profile: Mapping[str, Any]) -> dict[str, Any]:
    import time

    import websockets

    conn = profile["connection"]
    url = conn.get("target", "")
    if not url.startswith(("ws://", "wss://")):
        return {"ok": False, "message": f"the agent's address must start with ws:// or wss://, got {url!r}"}
    rate = int(conn.get("audio", {}).get("sample_rate_hz", 16000))
    try:
        headers = {str(k): str(v) for k, v in expand_env(conn.get("options", {}).get("headers") or {}).items()}
    except MissingSecret as e:
        return {"ok": False, "message": str(e)}
    try:
        ws = await asyncio.wait_for(websockets.connect(url, max_size=None, ping_interval=None,
                                                       additional_headers=headers or None), 10)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": f"could not connect to {url}: {type(e).__name__}: {e}"}
    got_bytes, texts, first_ms = 0, [], None
    start = time.monotonic()
    try:
        meta = {"now": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        if "session_metadata" in profile.get("test_id", {}).get("propagation", []):
            meta["test_id"] = "t-vocto-connection-test"
        await ws.send(json.dumps({"type": "start", "sample_rate": rate, "encoding": "pcm16", "metadata": meta}))
        silence = bytes(rate // 50 * 2)   # 20 ms of silence, so the agent hears a live line
        last_audio = None
        while time.monotonic() - start < 8:
            await ws.send(silence)
            try:
                msg = await asyncio.wait_for(ws.recv(), 0.02)
            except asyncio.TimeoutError:
                msg = None
            if isinstance(msg, (bytes, bytearray)):
                got_bytes += len(msg)
                last_audio = time.monotonic()
                if first_ms is None:
                    first_ms = int((last_audio - start) * 1000)
            elif isinstance(msg, str):
                texts.append(msg[:200])
            if last_audio and time.monotonic() - last_audio > 1.5:
                break
        await ws.send(json.dumps({"type": "stop"}))
    except websockets.ConnectionClosed as e:
        texts.append(f"(the agent closed the connection: {e.rcvd})")
    finally:
        await ws.close()
    seconds = got_bytes / 2 / rate
    details = [f"Audio received: {seconds:.1f} s" + (f", first sound after {first_ms} ms" if first_ms is not None else "")]
    details += [f"Message: {t}" for t in texts[:5]]
    if not got_bytes:
        return {"ok": False, "message": "Connected, but the agent sent no audio in 8 seconds (it should greet the caller).",
                "details": details}
    return {"ok": True, "message": "Connected. The agent spoke first, as a phone agent should.", "details": details}


async def _test_twilio(profile: Mapping[str, Any]) -> dict[str, Any]:
    """A short silent call in the Twilio Media Streams format: does the agent answer and greet?"""
    import numpy as np

    from ..audio.recorder import Recorder, monotonic_clock
    from ..connectors.twilio_stream import SAMPLE_RATE, TwilioStreamConnector
    from ..contracts.models import CallContext

    url = profile["connection"].get("target", "")
    if not url.startswith(("ws://", "wss://")):
        return {"ok": False, "message": f"the agent's stream address must start with ws:// or wss://, got {url!r}"}
    ctx = CallContext(test_id="t-vocto-connection-test", run_id="r-test", profile=profile,
                      scenario={"setup": {"now": datetime.now(timezone.utc).isoformat(timespec="seconds")}},
                      repeat=1, attempt=1, mode="voice")
    rec = Recorder(SAMPLE_RATE, monotonic_clock())
    try:
        session = await TwilioStreamConnector(profile).open(ctx, rec)
    except Exception as e:  # noqa: BLE001 - ConnectError or a missing secret: a plain answer either way
        return {"ok": False, "message": str(e)}
    silence = np.zeros(SAMPLE_RATE // 50, dtype=np.float32)
    first_ms, last_chunks, quiet_since = None, 0, None
    start = rec.clock()
    try:
        while rec.clock() - start < 8 and not session.closed.is_set():
            await session.send_frame(silence)
            await asyncio.sleep(0.02)
            if rec.chunks and first_ms is None:
                first_ms = int((rec.clock() - start) * 1000)
            if rec.chunks != last_chunks:
                last_chunks, quiet_since = rec.chunks, rec.clock()
            elif quiet_since and rec.clock() - quiet_since > 1.5 and not rec.agent_playing():
                break
    finally:
        await session.close()
    heard_s = sum(b - a for a, b in rec.agent_spans(0, rec.agent.length)) / SAMPLE_RATE
    details = [f"Speech heard: {heard_s:.1f} s" + (f", first audio after {first_ms} ms" if first_ms is not None else "")]
    details += [f"Problem: {e}" for e in session.protocol_errors[:5]]
    if session.closed.is_set() and not rec.chunks:
        return {"ok": False, "message": f"The agent ended the stream without speaking: {session.close_reason}.",
                "details": details}
    if not rec.chunks:
        return {"ok": False, "message": "Connected, but the agent sent no audio in 8 seconds (it should greet the caller).",
                "details": details}
    return {"ok": True, "message": "Connected in the Twilio format. The agent spoke first, as a phone agent should.",
            "details": details}


def test_backend(root: Path, project: str, file: str, probe: str | None, binds: Mapping[str, Any]) -> dict[str, Any]:
    profile = _merged(root, project, file)
    try:
        if not probe:
            return {"ok": True, "message": check_backend(profile, root)}
        result = BackendProbe(profile, root).run_with(probe, {k: v for k, v in binds.items() if v != ""})
    except (ProbeSetupError, KeyError) as e:
        return {"ok": False, "message": str(e)}
    if not result.ok:
        return {"ok": False, "message": result.error}
    rows = [dict(r) for r in result.rows] if result.rows is not None else None
    value = result.value if not isinstance(result.value, dict) else None
    msg = f"{len(rows)} record(s) found" if rows is not None else f"value: {value}"
    return {"ok": True, "message": msg, "rows": (rows or [])[:20], "value": value}


def test_logs(root: Path, project: str, file: str) -> dict[str, Any]:
    profile = _merged(root, project, file)
    result = sample_logs(profile, root)
    if not result.ok:
        return {"ok": False, "message": result.error}
    return {"ok": True, "message": f"Read {len(result.lines)} recent line(s) from {result.source}.",
            "lines": result.lines}
