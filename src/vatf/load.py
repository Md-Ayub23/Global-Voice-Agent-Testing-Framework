"""Load tests (Phase 7): how a load run is shaped. Settings, stages and which tests it uses.

A load test runs many ordinary test calls at the same time, in a shape the agent's owner picks, and
asks: does the agent still answer fast and correctly when many people call at once? Every call is a
normal call (orchestrator.run_call), so it is recorded, checked and measured as in any run.

Profile `load` (every field optional; the defaults are below):

    load:
      shape: ramp            # ramp | spike | soak | steady | find_limit
      calls_at_once: 5       # the target: the most calls at once (the busiest time you expect); find_limit: the most to try
      peak_calls_per_hour: null  # your busiest hour's calls: with call_minutes, sets calls_at_once (Little's law)
      call_minutes: null     # how long an average call lasts, in minutes
      expected_peak: null    # calls at once at your busiest time (from the two above, or typed): the headroom sentence
      start_at: 1            # ramp: the first level; spike: normal traffic before and after the spike
      steps: 4               # ramp: levels from start_at up to calls_at_once
      step_minutes: 2        # ramp: how long each level runs
      minutes: 10            # steady and soak: how long to hold the target; spike: how long the spike lasts
      before_minutes: 2      # spike: normal traffic before the spike, and after it (the recovery)
      after_minutes: 2
      window_minutes: 5      # soak: read the hold in windows this long, to see drift
      pause_seconds: 0       # each simulated caller waits this long before its next call
      max_calls: 200         # a load test never starts more calls than this
      light: true            # voice: light callers (each line synthesised once and reused, no careful second listen)
      unique_facts: []       # caller facts that get a different value on every call (e.g. full_name, phone): see leaks
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
        recovery: {blocks: false, pct: 25, ms: 200}   # spike: after the spike, back within this of before it
        max_lag_ms: 100      # Vocto's own computer: event-loop lag p95 above this makes a stage untrustworthy
        min_calls: 10        # a stage needs this many finished calls to decide anything ("too few to judge")
        min_answers: 20      # ... and this many timed answers before its p95 can break a limit

Stages: every stage runs at or below the target, so every stage must meet the limits.

    ramp    start_at .. calls_at_once in `steps` levels, step_minutes each
    spike   start_at for before_minutes, calls_at_once for minutes, start_at for after_minutes
    soak    calls_at_once for minutes, read in windows of window_minutes
    steady  calls_at_once for minutes
    find_limit  like ramp, start_at up to calls_at_once in `steps` levels, but it stops at the first level that
            breaks a limit: that level is the breaking point, the one below it the capacity

Calls per hour: a business knows its busiest hour, not "calls at once". By Little's law, calls at once =
calls per minute x minutes per call, so 120 calls an hour of 4 minutes each are 8 calls at once.

Cross-call leaks: every call's own identity details (the caller's name, phone, email, date of birth, booking
reference: its facts) must never turn up in another call that ran at the same time (in what the agent said or
in its tool calls). Any hit blocks: one caller hearing or changing another caller's data is the worst load bug.
`unique_facts` gives the named facts a different value on every call, so calls of the same test can be told apart too.
"""

from __future__ import annotations

import math
import socket
from urllib.parse import urlparse
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

from .attacks import is_safety

SHAPES = ("ramp", "spike", "soak", "steady", "find_limit")
CALLERS = ("free", "claude")
LIMIT_DEFAULTS: dict[str, Any] = {
    "max_failed_pct": 2, "p95_ms": None,
    "slowdown": {"blocks": False, "pct": 50, "ms": 300},
    "pass_drop": {"blocks": False, "points": 20},
    "drift": {"blocks": False, "pct": 50},
    "recovery": {"blocks": False, "pct": 25, "ms": 200},
    "max_lag_ms": 100, "min_calls": 10, "min_answers": 20,
}
DEFAULTS: dict[str, Any] = {
    "shape": "ramp", "calls_at_once": 5, "start_at": 1, "steps": 4, "step_minutes": 2, "minutes": 10,
    "before_minutes": 2, "after_minutes": 2, "window_minutes": 5, "pause_seconds": 0, "max_calls": 200,
    "scenarios": [], "caller": "free", "judges": False, "conditions": "as_written", "start_agent": False,
    "peak_calls_per_hour": None, "call_minutes": None, "expected_peak": None, "light": True, "unique_facts": [],
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
    # Calls per hour: the busiest hour and the average call length give calls at once (Little's law).
    if out["peak_calls_per_hour"]:
        if not out["call_minutes"]:
            raise LoadSettingsError("with peak calls per hour, also give the average call length (call_minutes)")
        out["expected_peak"] = peak_from_hour(out["peak_calls_per_hour"], out["call_minutes"])
        if out["shape"] != "find_limit" and "calls_at_once" not in own and "calls_at_once" not in overrides_set(overrides):
            out["calls_at_once"] = min(MAX_CALLS_AT_ONCE, out["expected_peak"])
    if out["shape"] == "find_limit" and out["expected_peak"] and "calls_at_once" not in own \
            and "calls_at_once" not in overrides_set(overrides):
        out["calls_at_once"] = min(MAX_CALLS_AT_ONCE, max(out["calls_at_once"], 2 * int(out["expected_peak"])))
    _check(out)
    return out


def overrides_set(overrides: Mapping[str, Any]) -> set[str]:
    return {k for k, v in overrides.items() if v is not None}


def peak_from_hour(calls_per_hour: float, call_minutes: float) -> int:
    """Little's law: calls at once = calls per minute x minutes per call, rounded up (120 an hour of 4 min -> 8)."""
    return max(1, math.ceil(float(calls_per_hour) / 60 * float(call_minutes) - 1e-9))


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
    for key in ("peak_calls_per_hour", "call_minutes"):
        if s[key] is not None and (not isinstance(s[key], (int, float)) or isinstance(s[key], bool) or s[key] <= 0):
            raise LoadSettingsError(f"load {key} must be a number above 0, or empty")
    if s["expected_peak"] is not None and (not isinstance(s["expected_peak"], int) or s["expected_peak"] < 1):
        raise LoadSettingsError("load expected_peak must be a whole number of 1 or more, or empty")
    if not isinstance(s["unique_facts"], list) or not all(isinstance(x, str) for x in s["unique_facts"]):
        raise LoadSettingsError("load unique_facts must be a list of caller fact names")
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
    if shape in ("ramp", "find_limit"):
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


def expected_calls(s: Mapping[str, Any], stage_list: Sequence[Stage]) -> int | None:
    """About how many calls the load will make, when the average call length is known (capped by max_calls)."""
    if not s.get("call_minutes"):
        return None
    secs = float(s["call_minutes"]) * 60 + float(s.get("pause_seconds") or 0)
    n = sum(st.level * st.seconds / secs for st in stage_list)
    return min(int(s["max_calls"]), max(1, math.ceil(n)))


def stage_warnings(s: Mapping[str, Any], stage_list: Sequence[Stage]) -> list[str]:
    """Before the run: stages too short to finish enough calls to judge (only when the call length is known)."""
    if not s.get("call_minutes"):
        return []
    need = int(s["limits"]["min_calls"])
    secs = float(s["call_minutes"]) * 60 + float(s.get("pause_seconds") or 0)
    short = [(st, st.level * st.seconds / secs) for st in stage_list if st.level * st.seconds / secs < need]
    if not short:
        return []
    names = ", ".join(f"{st.name} (about {int(n)})" for st, n in short[:4])
    return [f"{len(short)} stage(s) will finish fewer than {need} calls and show as \"too few to judge\": {names}. "
            f"Make the stages longer (at {s['call_minutes']:g} min a call, {need} calls at 1 at once take "
            f"{need * secs / 60:g} min)."]


def phone_warning(profile: Mapping[str, Any], s: Mapping[str, Any], stage_list: Sequence[Stage]) -> dict[str, Any] | None:
    """A load test over real phone lines costs money and providers cap calls at once: what to confirm first."""
    conn = profile.get("connection") or {}
    if conn.get("type") != "phone_call":
        return None
    from .costs import prices

    n = expected_calls(s, stage_list)
    top = max((st.level for st in stage_list), default=0)
    p = prices(profile)
    cost = None
    if p and n is not None:
        cost = round(n * (p["per_call"] + p["per_minute"] * float(s["call_minutes"])), 2)
    provider = str((conn.get("options") or {}).get("provider") or "your phone provider").title()
    count = f"about {n}" if n is not None else f"up to {s['max_calls']}"
    words = (f"This load test rings the agent's real number: {count} calls, up to {top} at the same time. Each call is "
             f"charged by {provider} (Vocto's side) and by the agent's own phone line, and phone accounts cap calls "
             f"at once (trial accounts often allow only one): check your account's limit is at least {top}.")
    return {"calls": n if n is not None else s["max_calls"], "calls_known": n is not None, "at_once": top,
            "cost": cost, "currency": p["currency"] if p else None, "provider": provider, "text": words}


def summary(s: Mapping[str, Any], stage_list: Sequence[Stage], profile: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """What a load run will do, in numbers (run.json and the dashboard's preview)."""
    total = stage_list[-1].end_s if stage_list else 0
    return {"shape": s["shape"], "calls_at_once": s["calls_at_once"], "seconds": total,
            "peak": max((st.level for st in stage_list), default=0), "max_calls": s["max_calls"],
            "expected_peak": s.get("expected_peak"), "expected_calls": expected_calls(s, stage_list),
            "warnings": stage_warnings(s, stage_list),
            **({"phone": w} if profile is not None and (w := phone_warning(profile, s, stage_list)) else {}),
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
