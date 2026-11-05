"""Load tests (Phase 7): how a load run is shaped. Settings, stages and which tests it uses.

A load test runs many ordinary test calls at the same time, in a shape the agent's owner picks, and
asks: does the agent still answer fast and correctly when many people call at once? Every call is a
normal call (orchestrator.run_call), so it is recorded, checked and measured as in any run.

Profile `load` (every field optional; the defaults are below):

    load:
      shape: ramp            # ramp | spike | soak | steady
      calls_at_once: 5       # the target: the most calls at once (the busiest time you expect)
      start_at: 1            # ramp: the first level; spike: normal traffic before and after the spike
      steps: 4               # ramp: levels from start_at up to calls_at_once
      step_minutes: 2        # ramp: how long each level runs
      minutes: 10            # steady and soak: how long to hold the target; spike: how long the spike lasts
      before_minutes: 2      # spike: normal traffic before the spike, and after it (the recovery)
      after_minutes: 2
      window_minutes: 5      # soak: read the hold in windows this long, to see drift
      pause_seconds: 0       # each simulated caller waits this long before its next call
      max_calls: 200         # a load test never starts more calls than this
      scenarios: []          # empty = the tests in this mode, without planted tool faults or safety attacks
      caller: free           # free: scripted lines and the rule-based caller; claude: Claude plays the callers
      judges: false          # AI judges on every load call cost money; off unless switched on
      conditions: as_written # voice: caller conditions laid over every call
      start_agent: false     # start the agent first (agent_process) and stop it after
      limits:
        max_failed_pct: 2    # NO-GO when more calls than this fail in a stage (couldn't connect, agent error, stopped answering)
        p95_ms: null         # NO-GO when the answer time p95 in a stage is over this. null: voice uses thresholds.latency_p95_ms, text has none
        slowdown: {blocks: false, pct: 50, ms: 300}   # p95 at the top level vs the lowest level
        pass_drop: {blocks: false, points: 20}        # pass rate at the top level vs the lowest level
        drift: {blocks: false, pct: 50}               # soak: the last window vs the first
        max_lag_ms: 100      # Vocto's own computer: event-loop lag p95 above this makes a stage untrustworthy
        min_calls: 3         # a stage needs this many finished calls to decide anything

Stages: every stage runs at or below the target, so every stage must meet the limits.

    ramp    start_at .. calls_at_once in `steps` levels, step_minutes each
    spike   start_at for before_minutes, calls_at_once for minutes, start_at for after_minutes
    soak    calls_at_once for minutes, read in windows of window_minutes
    steady  calls_at_once for minutes
"""

from __future__ import annotations

import math
import socket
from urllib.parse import urlparse
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

from .attacks import is_safety

SHAPES = ("ramp", "spike", "soak", "steady")
CALLERS = ("free", "claude")
LIMIT_DEFAULTS: dict[str, Any] = {
    "max_failed_pct": 2, "p95_ms": None,
    "slowdown": {"blocks": False, "pct": 50, "ms": 300},
    "pass_drop": {"blocks": False, "points": 20},
    "drift": {"blocks": False, "pct": 50},
    "max_lag_ms": 100, "min_calls": 3,
}
DEFAULTS: dict[str, Any] = {
    "shape": "ramp", "calls_at_once": 5, "start_at": 1, "steps": 4, "step_minutes": 2, "minutes": 10,
    "before_minutes": 2, "after_minutes": 2, "window_minutes": 5, "pause_seconds": 0, "max_calls": 200,
    "scenarios": [], "caller": "free", "judges": False, "conditions": "as_written", "start_agent": False,
}
MAX_CALLS_AT_ONCE = 200
MAX_TOTAL_MINUTES = 24 * 60


class LoadSettingsError(ValueError):
    """The profile's load section can't make a load test (a wrong value). Nothing was called."""


@dataclass(frozen=True)
class Stage:
    index: int
    name: str          # what the stage is, in words: "Step 2 of 4", "Spike", "Recovery", "Window 3"
    kind: str          # step | normal | spike | recovery | hold | window
    level: int         # calls at once
    start_s: float     # from the start of the load
    seconds: float

    @property
    def end_s(self) -> float:
        return self.start_s + self.seconds

    def public(self) -> dict[str, Any]:
        return {**asdict(self), "end_s": self.end_s}


def settings(profile: Mapping[str, Any], **overrides: Any) -> dict[str, Any]:
    """The profile's load settings over the defaults, with command-line overrides on top, checked."""
    own = dict(profile.get("load") or {})
    out: dict[str, Any] = {**DEFAULTS, **{k: v for k, v in own.items() if k != "limits"}}
    out.update({k: v for k, v in overrides.items() if v is not None})
    limits = dict(LIMIT_DEFAULTS)
    for key, value in (own.get("limits") or {}).items():
        limits[key] = {**LIMIT_DEFAULTS[key], **value} if isinstance(LIMIT_DEFAULTS.get(key), dict) else value
    out["limits"] = limits
    _check(out)
    return out


def _check(s: Mapping[str, Any]) -> None:
    if s["shape"] not in SHAPES:
        raise LoadSettingsError(f"load shape must be one of {', '.join(SHAPES)}, not {s['shape']!r}")
    if s["caller"] not in CALLERS:
        raise LoadSettingsError(f"load caller must be free or claude, not {s['caller']!r}")
    for key in ("calls_at_once", "start_at", "steps", "max_calls"):
        if not isinstance(s[key], int) or isinstance(s[key], bool) or s[key] < 1:
            raise LoadSettingsError(f"load {key} must be a whole number of 1 or more")
    if s["calls_at_once"] > MAX_CALLS_AT_ONCE:
        raise LoadSettingsError(f"load calls_at_once can be at most {MAX_CALLS_AT_ONCE}")
    if s["start_at"] > s["calls_at_once"] and s["shape"] in ("ramp", "spike"):
        raise LoadSettingsError("load start_at must not be above calls_at_once")
    for key in ("step_minutes", "minutes", "before_minutes", "after_minutes", "window_minutes", "pause_seconds"):
        if not isinstance(s[key], (int, float)) or isinstance(s[key], bool) or s[key] < 0:
            raise LoadSettingsError(f"load {key} must be a number of 0 or more")
    lim = s["limits"]
    if lim["p95_ms"] is not None and (not isinstance(lim["p95_ms"], (int, float)) or lim["p95_ms"] <= 0):
        raise LoadSettingsError("load limits.p95_ms must be a positive number of milliseconds, or empty")


def levels(start: int, target: int, steps: int) -> list[int]:
    """Ramp levels from start to target, evenly spread and never repeated: (1, 5, 4) -> [1, 2, 4, 5]."""
    if steps <= 1 or start >= target:
        return [target]
    out: list[int] = []
    for i in range(steps):
        lv = round(start + (target - start) * i / (steps - 1))
        if not out or lv > out[-1]:
            out.append(lv)
    return out


def stages(s: Mapping[str, Any]) -> list[Stage]:
    """The load's stages, one after the other, from its settings (see the module docstring)."""
    out: list[Stage] = []
    t = 0.0

    def add(name: str, kind: str, level: int, minutes: float) -> None:
        nonlocal t
        secs = float(minutes) * 60
        if secs <= 0:
            return
        out.append(Stage(len(out), name, kind, int(level), t, secs))
        t += secs

    target = s["calls_at_once"]
    shape = s["shape"]
    if shape == "ramp":
        lv = levels(s["start_at"], target, s["steps"])
        for i, level in enumerate(lv):
            add(f"Step {i + 1} of {len(lv)}", "step", level, s["step_minutes"])
    elif shape == "spike":
        normal = min(s["start_at"], target)
        add("Before the spike", "normal", normal, s["before_minutes"])
        add("Spike", "spike", target, s["minutes"])
        add("Recovery", "recovery", normal, s["after_minutes"])
    elif shape == "soak":
        total, window = float(s["minutes"]), float(s["window_minutes"]) or float(s["minutes"])
        n = max(1, math.ceil(total / window - 1e-9))
        for i in range(n):
            add(f"Window {i + 1} of {n}", "window", target, min(window, total - i * window))
    else:
        add("Hold", "hold", target, s["minutes"])
    if not out:
        raise LoadSettingsError("this load test has no time in it: set its minutes above 0")
    if t > MAX_TOTAL_MINUTES * 60:
        raise LoadSettingsError(f"a load test can last at most {MAX_TOTAL_MINUTES // 60} hours")
    return out


def scenario_ids(s: Mapping[str, Any], scenarios: Sequence[Mapping[str, Any]], mode: str) -> list[str]:
    """The tests a load test runs, taken in turn. `scenarios` are all the agent's tests.

    Chosen ones must exist and run in this mode. With none chosen: the tests in this mode without
    planted tool faults (they slow the agent on purpose, which a capacity test must not measure)
    and without safety attacks.
    """
    in_mode = [sc for sc in scenarios if mode in sc.get("modes", ["text", "voice"])]
    known = {sc["id"] for sc in in_mode}
    if s["scenarios"]:
        unknown = [x for x in s["scenarios"] if x not in known]
        if unknown:
            raise LoadSettingsError(f"load test(s) not found in this mode: {', '.join(unknown)}")
        return list(dict.fromkeys(s["scenarios"]))
    plain = [sc["id"] for sc in in_mode if not is_safety(sc)
             and not (sc.get("setup") or {}).get("faults")]
    if not plain:
        raise LoadSettingsError("no test can be used for load: every test in this mode has planted tool "
                                "faults or is a safety attack. Pick tests in the load settings.")
    return plain


def p95_limit(s: Mapping[str, Any], profile: Mapping[str, Any], mode: str) -> float | None:
    """The answer-time p95 limit per stage: the load's own, else the voice release limit, else none."""
    own = s["limits"].get("p95_ms")
    if own is not None:
        return float(own)
    if mode == "voice":
        return float((profile.get("thresholds") or {}).get("latency_p95_ms", 1500))
    return None


def summary(s: Mapping[str, Any], stage_list: Sequence[Stage]) -> dict[str, Any]:
    """What a load run will do, in numbers (run.json and the dashboard's preview)."""
    total = stage_list[-1].end_s if stage_list else 0
    return {"shape": s["shape"], "calls_at_once": s["calls_at_once"], "seconds": total,
            "peak": max((st.level for st in stage_list), default=0), "max_calls": s["max_calls"],
            "stages": [st.public() for st in stage_list]}


def unreachable(profile: Mapping[str, Any], timeout: float = 3.0) -> str | None:
    """Before a load test: is anything listening at the agent's address? Returns why not, or None.

    One plain network connection (not a call), so a load test with no agent running stops at once with
    one clear message instead of hundreds of "couldn't connect" calls. Agents Vocto starts for every
    call (launch, text_python) have nothing to check here.
    """
    conn = profile.get("connection") or {}
    if conn.get("launch") or conn.get("type") == "text_python":
        return None
    target = str(conn.get("target") or "")
    url = urlparse(target)
    if url.scheme not in ("ws", "wss", "http", "https") or not url.hostname:
        return None
    port = url.port or (443 if url.scheme in ("wss", "https") else 80)
    try:
        with socket.create_connection((url.hostname, port), timeout=timeout):
            return None
    except OSError as e:
        why = getattr(e, "strerror", None) or str(e) or type(e).__name__
        start = ("Start it first, or let Vocto start it: switch on \"Start the agent for me\" on the Load test page, "
                 "or add --start-agent to the command."
                 if profile.get("agent_process") else
                 "Start it first (this agent has no agent_process, so Vocto can't start it).")
        return f"nothing is answering at {target} ({why}): the agent isn't running. {start}"
