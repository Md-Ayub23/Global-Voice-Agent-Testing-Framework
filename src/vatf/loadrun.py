"""Load test runner (Phase 7): many ordinary test calls at the same time, in the load's stages.

    load.py settings + stages -> LoadOrchestrator.run()
        each stage keeps `level` calls going (a new call starts when one ends), taking the tests in turn
        every call is orchestrator.run_call: the same connector, caller, tool proxy, checks and metrics
        HarnessMonitor watches Vocto's own event loop, so an overloaded computer is never blamed on the agent
    -> loadstats.summarise -> load.json, decision.apply_load -> decision.json (+ baseline, report, CI, alerts)

The run folder is a normal run folder (run.json has kind "load"), so Results, the call pages and
the transcript work as for any run. load_live.json is rewritten every second while the load runs,
for the dashboard's Live page.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Mapping

from .baseline import BaselineError, load_baseline
from .contracts.orchestrator import PlannedCall
from .decision import LocalDecisionEngine, apply_load
from .evidence import utc_now
from .load import p95_limit, scenario_ids, stages, summary
from .loadstats import call_facts, summarise
from .orchestrator import Orchestrator, RunConfig, RunSetupError
from .probe import ProbeSetupError, load_fixtures
from .proxy import InProcessToolProxy
from .story import load_events
from .util import write_json

TICK_S = 0.1          # the monitor's tick: lag is how late each tick fires
STAGGER_S = 0.25      # new calls start this far apart when a stage rises (not in a spike: that is the point)


class HarnessMonitor:
    """Samples how late Vocto's event loop runs (its lag) and how many calls are in flight.

    The audio of every voice call is paced by this loop. When it runs late, callers' audio goes out
    late and timings drift: then the numbers say more about this computer than about the agent, and
    the stage is marked untrusted instead of failing the agent."""

    def __init__(self) -> None:
        self.samples: list[tuple[float, float, int]] = []   # (t_s since the load started, lag_ms, in_flight)
        self.timeline: list[tuple[float, int, float]] = []  # one per second: (t_s, in_flight, lag max ms)
        self.in_flight = 0
        self._task: asyncio.Task | None = None
        self._t0 = 0.0

    def start(self, t0: float, on_second=None) -> None:
        self._t0 = t0
        self._task = asyncio.get_running_loop().create_task(self._run(on_second))

    async def _run(self, on_second) -> None:
        loop = asyncio.get_running_loop()
        second, worst = 0, 0.0
        while True:
            before = loop.time()
            await asyncio.sleep(TICK_S)
            now = loop.time()
            lag = max(0.0, (now - before - TICK_S) * 1000)
            t = now - self._t0
            self.samples.append((round(t, 2), round(lag, 1), self.in_flight))
            worst = max(worst, lag)
            if int(t) > second:
                second = int(t)
                self.timeline.append((second, self.in_flight, round(worst, 1)))
                worst = 0.0
                if on_second:
                    on_second(t)

    async def stop(self) -> None:
        # One last point when the load ends, so the timeline shows the calls draining (usually to 0).
        try:
            t = asyncio.get_running_loop().time() - self._t0
            if not self.timeline or t > self.timeline[-1][0]:
                self.timeline.append((round(t, 1), self.in_flight, 0.0))
        except (RuntimeError, AttributeError):
            pass
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass


class _FallbackSink:
    """Records the tool calls that could not be matched to a call (several calls, no test id)."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, source: str, type_: str, data: Mapping[str, Any], turn: int | None = None, **_: Any) -> None:
        self.events.append({"t": utc_now(), "type": type_, "data": dict(data)})


class LoadOrchestrator(Orchestrator):
    def __init__(self, project, config: RunConfig, load: Mapping[str, Any], llm: Any = None, log=print):
        self.load = dict(load)
        super().__init__(project, replace(config), llm=llm, log=log)
        self.stage_list = stages(self.load)
        all_ids = scenario_ids(self.load, project.scenarios, self.mode)
        # Only the load's tests, no safety attacks; repeats come from the stages, not the profile.
        self.config.scenario_ids = tuple(all_ids)
        self.config.safety = False
        self.config.fixtures_once = True
        self.config.caller = self.load["caller"]
        self.concurrent = True
        self.monitor = HarnessMonitor()
        self._fallback_sink = _FallbackSink()
        self._live_path: Path | None = None
        self._counts = {"started": 0, "finished": 0, "failed": 0, "harness": 0}
        self._stage_now = 0

    # ---- what the run will do -----------------------------------------------------------
    def plan(self) -> list[PlannedCall]:
        """The mix: one entry per test, taken in turn while the load runs."""
        out = []
        for sc in self.scenarios():
            persona = sc["caller"]["persona"] if sc["style"] == "simulated" else None
            out.append(PlannedCall(sc["id"], persona, 0))
        return out

    def _plan_text(self, plan) -> str:
        top = max(st.level for st in self.stage_list)
        secs = self.stage_list[-1].end_s
        return (f"{self.load['max_calls']} planned call(s) at most: load test, {self.load['shape']} up to {top} "
                f"call(s) at once for {_dur(secs)}, {len(plan)} test(s) taken in turn")

    def _run_info(self, run_id, plan) -> dict[str, Any]:
        info = super()._run_info(run_id, plan)
        info["kind"] = "load"
        info["test_mode"] = "load"
        info["load"] = {**summary(self.load, self.stage_list), "settings": self.load,
                        "p95_limit_ms": p95_limit(self.load, self.profile, self.mode),
                        "launched_per_call": bool(self.profile["connection"].get("launch")) or
                        self.profile["connection"].get("type") == "text_python"}
        info["caller_model"] = None if (self.config.offline or self.load["caller"] == "free") else self.config.caller_model
        info["plan"] = []    # a load run has no fixed plan: calls are listed as they start
        return info

    def _gateway_fallback(self) -> Any:
        proxy = InProcessToolProxy(self.profile, [], real_delays=self.mode == "voice")
        proxy._sink = self._fallback_sink        # forwards untouched; recorded only in load.json's count
        return proxy

    # ---- running ---------------------------------------------------------------------------
    def _fixtures_once(self) -> list[str]:
        """Test data for the whole load, loaded before the first call (reloading it before each call
        would wipe the rows of calls still going). Returns notes for the report."""
        files: list[str] = []
        for sc in self.scenarios():
            for f in (sc.get("setup") or {}).get("fixtures", []):
                if f not in files:
                    files.append(f)
        if not files:
            return []
        try:
            load_fixtures(self.profile, {"setup": {"fixtures": files}}, self.config.root)
        except ProbeSetupError as e:
            raise RunSetupError(f"the test data could not be loaded: {e}") from e
        sets = {tuple((sc.get("setup") or {}).get("fixtures", [])) for sc in self.scenarios()}
        if len(sets) > 1:
            return [f"The tests use different test data; all of it was loaded once, in order ({', '.join(files)}), "
                    "before the first call."]
        return []

    async def _run_plan(self, run_id: str, plan: list[PlannedCall], run_dir: Path) -> dict[str, Any]:
        if self.mode == "voice":
            await self._warm_up_speech(plan)
        notes = self._fixtures_once()
        if self._info["load"]["launched_per_call"]:
            notes.append("Vocto starts this agent fresh for every call, so this load test measures this computer and the "
                         "shared backend, not one shared agent server. For a real capacity test, point the connection "
                         "at the running agent.")
        if self.load["caller"] == "claude" and not self.config.offline:
            notes.append("Claude played the callers: its own answer time is not in the agent's numbers, but many "
                         "calls at once can hit the Claude rate limit (those calls show as Vocto errors).")
        loop = asyncio.get_running_loop()
        top = max(st.level for st in self.stage_list)
        # Speech, the agent's tools and in-process agents run in threads: enough for every call at once.
        loop.set_default_executor(ThreadPoolExecutor(max_workers=max(32, top * 6), thread_name_prefix="vatf-load"))
        self._live_path = run_dir / "load_live.json"
        verdicts = await self._drive(run_id, plan)
        gateway = ({"unattributed": self._gateway.unattributed, "unknown_ids": self._gateway.unknown_ids}
                   if self._gateway else {})
        limit = p95_limit(self.load, self.profile, self.mode)
        result = summarise(self.load, self.stage_list, verdicts, self.monitor.samples, limit, self.profile,
                           self.mode, notes, gateway, self.monitor.timeline)
        write_json(run_dir / "load.json", result)
        self._write_live(final=True)
        self._judge_cost = self._spent()   # the run's whole AI spend (callers, judges): per-call costs can't be split
        decision = LocalDecisionEngine().decide(run_id, verdicts, self.scenarios(), self.profile)
        decision = apply_load(decision, result)
        return self._close(run_id, decision, verdicts, [], run_dir)

    def _stage_at(self, t: float):
        for st in self.stage_list:
            if t < st.end_s:
                return st
        return None

    async def _drive(self, run_id: str, plan: list[PlannedCall]) -> list[dict[str, Any]]:
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        self._t0 = t0
        self.monitor.start(t0, lambda t: self._write_live())
        verdicts: list[dict[str, Any]] = []
        running: set[asyncio.Task] = set()
        cooling = 0                           # simulated callers waiting pause_seconds before their next call
        pause = float(self.load["pause_seconds"])
        repeats: dict[str, int] = {}
        last_start = -1e9
        stage_seen = -1

        def done(task: asyncio.Task) -> None:
            nonlocal cooling
            running.discard(task)
            self.monitor.in_flight = len(running)
            if pause > 0:
                cooling += 1

                def free() -> None:
                    nonlocal cooling
                    cooling -= 1
                loop.call_later(pause, free)

        try:
            while True:
                now = loop.time() - t0
                st = self._stage_at(now)
                if st is None or self._counts["started"] >= self.load["max_calls"]:
                    break
                if st.index != stage_seen:
                    stage_seen = st.index
                    self._stage_now = st.index
                    self.log(f"load: {st.name}, {st.level} call(s) at once for {_dur(st.seconds)}")
                while (len(running) + cooling < st.level and self._counts["started"] < self.load["max_calls"]
                       and (st.kind == "spike" or now - last_start >= STAGGER_S)):
                    pc = plan[self._counts["started"] % len(plan)]
                    repeats[pc.scenario_id] = repeats.get(pc.scenario_id, 0) + 1
                    call = PlannedCall(pc.scenario_id, pc.persona_id, repeats[pc.scenario_id])
                    self._counts["started"] += 1
                    task = loop.create_task(self._one(run_id, call, st, len(running) + 1, now, verdicts))
                    running.add(task)
                    task.add_done_callback(done)
                    self.monitor.in_flight = len(running)
                    last_start = now
                    if st.kind != "spike":
                        break
                await asyncio.sleep(0.05)
            if running:
                why = "the last stage ended" if self._stage_at(loop.time() - t0) is None else "max_calls was reached"
                self.log(f"load: {why}; waiting for the {len(running)} call(s) still going to finish")
                await asyncio.gather(*running, return_exceptions=True)
        except BaseException:
            for task in running:
                task.cancel()
            await asyncio.gather(*running, return_exceptions=True)
            raise
        finally:
            await self.monitor.stop()
        verdicts.sort(key=lambda v: (v["load"]["start_s"], v["test_id"]))
        return verdicts

    async def _one(self, run_id: str, pc: PlannedCall, stage, in_flight: int, start_s: float,
                   out: list[dict[str, Any]]) -> None:
        verdict = await self.run_call(run_id, pc, 1)
        end_s = asyncio.get_running_loop().time() - self._t0
        try:
            events = load_events(self.store.call_dir(run_id, verdict["test_id"]))
        except (OSError, ValueError):
            events = []
        facts = call_facts(events, verdict)
        timed = facts.pop("timed")
        verdict["load"] = {"stage": stage.index, "stage_name": stage.name, "level": stage.level,
                           "in_flight": in_flight, "start_s": round(start_s, 2), "end_s": round(end_s, 2), **facts,
                           "answers": [[round(start_s + t / 1000, 2), ms] for t, ms in timed]}
        self.store.write_verdict(run_id, verdict["test_id"], verdict)
        self._counts["finished"] += 1
        self._counts["failed"] += facts["outcome"] == "failed"
        self._counts["harness"] += facts["outcome"] == "harness"
        out.append(verdict)
        tag = verdict["status"] + (f", {facts['why']}" if facts["why"] else "")
        self.log(f"  {pc.scenario_id} call {pc.repeat} ({stage.level} at once): {tag}  ({verdict['test_id']})")

    def _write_live(self, final: bool = False) -> None:
        if self._live_path is None:
            return
        t = time.monotonic()
        try:
            elapsed = asyncio.get_running_loop().time() - self._t0
        except RuntimeError:
            elapsed = 0.0
        st = self.stage_list[min(self._stage_now, len(self.stage_list) - 1)]
        try:
            write_json(self._live_path, {
                "kind": "load_live", "finished": final, "elapsed_s": round(elapsed, 1),
                "seconds": self.stage_list[-1].end_s, "stage": st.index, "stage_name": st.name, "level": st.level,
                "in_flight": self.monitor.in_flight, **self._counts,
                "lag_ms": self.monitor.timeline[-1][2] if self.monitor.timeline else 0,
                "timeline": [list(x) for x in self.monitor.timeline[-900:]],
                "written": round(t, 1)})
        except OSError:
            pass   # the dashboard reads it next second; the run itself never depends on it

    # ---- baseline: a load run has its own ---------------------------------------------------
    def _compare(self, decision, run_dir):
        mode = self.config.baseline or "auto"
        if mode == "auto":
            try:
                base = load_baseline(Path(self.project.folder), self.project.profile_file, kind="load")
            except BaselineError as e:
                self.log(f"warning: no comparison with the load baseline: {e}")
                return None
            if base is None:
                return None
            return self._compare_with(decision, run_dir, base, "load baseline")
        return super()._compare(decision, run_dir)


def _dur(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 90:
        return f"{seconds} s"
    m, s = divmod(seconds, 60)
    if m < 90:
        return f"{m} min" + (f" {s} s" if s else "")
    h, m = divmod(m, 60)
    return f"{h} h" + (f" {m} min" if m else "")
