"""Run orchestrator (contracts/orchestrator.py): plan, run each call, rerun harness errors only,
evaluate, decide and write the report."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .caller import LLMCaller, LLMValidator, RuleCaller, ScriptedCaller
from .checks import scenario_checks
from .connectors import ConnectError, build_connector
from .contracts.models import CallContext, CallTrace, CheckResult, EvidenceLevel, EvidenceRef, Result
from .contracts.orchestrator import PlannedCall
from .decision import LocalDecisionEngine
from .evidence import LocalEvidenceStore, utc_now
from .llm import DEFAULT_MODEL
from .loader import Project
from .outcome import evaluate_outcome
from .probe import ProbeSetupError, SqlProbe, load_fixtures
from .proxy import InProcessToolProxy
from .report import write_report
from .story import call_story_text, load_events, run_story_text
from .util import write_json
from .verdict import LocalVerdictBuilder


class HarnessError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class RunConfig:
    root: Path = field(default_factory=Path.cwd)  # relative paths in the profile resolve from here
    runs_dir: Path = Path("runs")
    offline: bool = False                         # rule-based caller, no validator, no API calls
    caller_model: str = DEFAULT_MODEL
    validator_model: str = DEFAULT_MODEL
    repeats: int | None = None                    # override the profile's repeats
    scenario_ids: tuple[str, ...] = ()            # empty = all scenarios
    mask_pii: bool = True                         # False (--show-pii) keeps names and dates readable


class Orchestrator:
    def __init__(self, project: Project, config: RunConfig, llm: Any = None,
                 log: Callable[[str], None] = print):
        self.project = project
        self.profile = project.profile
        self.config = config
        self.llm = llm
        self.log = log
        self.store = LocalEvidenceStore(config.root / config.runs_dir)
        self._counter = 0
        self._run_date = ""

    # ---- planning -------------------------------------------------------
    def scenarios(self) -> list[Mapping[str, Any]]:
        chosen = self.project.scenarios
        if self.config.scenario_ids:
            unknown = set(self.config.scenario_ids) - {s["id"] for s in chosen}
            if unknown:
                raise KeyError(f"unknown scenario(s): {', '.join(sorted(unknown))}")
            chosen = [s for s in chosen if s["id"] in self.config.scenario_ids]
        return chosen

    def repeats_for(self, scenario: Mapping[str, Any]) -> int:
        return self.config.repeats or scenario.get("repeats") or self.profile.get("thresholds", {}).get("repeats", 3)

    def plan(self) -> list[PlannedCall]:
        calls = []
        for sc in self.scenarios():
            persona = sc["caller"]["persona"] if sc["style"] == "simulated" else None
            for r in range(1, self.repeats_for(sc) + 1):
                calls.append(PlannedCall(sc["id"], persona, r))
        return calls

    # ---- running --------------------------------------------------------
    async def run(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        run_id = f"r-{now:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
        self._run_date = f"{now:%Y%m%d}"
        plan = self.plan()
        retries = self.profile.get("thresholds", {}).get("max_harness_retries", 2)
        self.log(f"run {run_id}: {len(plan)} planned call(s)")
        finals, error_attempts = [], []
        for pc in plan:
            for attempt in range(1, retries + 2):
                verdict = await self.run_call(run_id, pc, attempt)
                self.log(f"  {pc.scenario_id} repeat {pc.repeat} attempt {attempt}: {verdict['status']}"
                         f"  ({verdict['test_id']})")
                if verdict["status"] != "ERROR" or attempt == retries + 1:
                    finals.append(verdict)
                    break
                error_attempts.append(verdict)

        decision = LocalDecisionEngine().decide(run_id, finals, self.scenarios(), self.profile)
        run_dir = self.store.runs_dir / run_id
        write_json(run_dir / "decision.json", decision)
        decision["_pii_masked"] = self.config.mask_pii
        report = write_report(decision, finals, error_attempts, run_dir, self.store)
        decision["_report"] = str(report)
        decision["_transcript"] = str(self._write_stories(decision, finals, error_attempts, run_dir))
        return decision

    def _write_stories(self, decision, finals, error_attempts, run_dir: Path) -> Path:
        """story.txt per call and transcript.txt for the whole run, in plain English."""
        def with_events(verdicts):
            out = []
            for v in verdicts:
                call_dir = self.store.call_dir(v["run_id"], v["test_id"])
                events = load_events(call_dir)
                (call_dir / "story.txt").write_text(call_story_text(v, events), encoding="utf-8")
                out.append((v, events))
            return out

        path = run_dir / "transcript.txt"
        path.write_text(run_story_text(decision, with_events(finals), with_events(error_attempts)),
                        encoding="utf-8")
        return path

    def _test_id(self) -> str:
        self._counter += 1
        return f"t-{self._run_date}-{self._counter:04d}-{secrets.token_hex(2)}"

    async def run_call(self, run_id: str, pc: PlannedCall, attempt: int) -> dict[str, Any]:
        scenario = self.project.scenario(pc.scenario_id)
        ctx = CallContext(test_id=self._test_id(), run_id=run_id, profile=self.profile, scenario=scenario,
                          repeat=pc.repeat, attempt=attempt, mode="text")
        writer = self.store.open_call(ctx, persona_id=pc.persona_id,
                                      profile_sha256=self.project.profile_sha256,
                                      scenario_sha256=self.project.scenario_sha256.get(scenario["id"], ""),
                                      mask=self.config.mask_pii)
        meta = {"test_id": ctx.test_id, "log_dir": str(writer.llm_dir)}
        cost_before = self._spent()
        status = "completed"
        ended = False
        validator_off_script = None
        session = None
        try:
            try:
                load_fixtures(self.profile, scenario, self.config.root)
            except ProbeSetupError as e:
                raise HarnessError("fixtures_failed", str(e)) from e
            proxy = InProcessToolProxy(self.profile, scenario.get("setup", {}).get("faults", []))
            await proxy.attach(ctx, writer)
            writer.emit("connector", "call_started", {"test_id": ctx.test_id, "mode": "text"})
            caller = self._caller(scenario, meta)
            try:
                session = await build_connector(self.profile, self.config.root, proxy).open(ctx, writer.now_ms)
            except ConnectError as e:
                raise HarnessError("connect_failed", str(e)) from e

            greeting = await session.receive()
            writer.emit("connector", "agent_turn", {"heard_text": greeting.text or ""}, turn=1)
            heard, turn = greeting.text or "", 1
            ended_by, reason = None, ""
            limits = self.profile.get("limits", {})
            max_turns = (len(scenario["turns"]) if scenario["style"] == "scripted"
                         else scenario["caller"].get("max_turns", limits.get("max_turns", 30)))
            deadline = time.monotonic() + limits.get("max_call_seconds", 300)
            caller_turns = 0
            while ended_by is None:
                if time.monotonic() > deadline:
                    ended_by, reason = "timeout", "max_call_seconds reached"
                    break
                if caller_turns >= max_turns and scenario["style"] == "simulated":
                    ended_by, reason = "max_turns", f"{max_turns} caller turns"
                    break
                action = await caller.next_turn(heard)
                if action.say:
                    turn += 1
                    caller_turns += 1
                    writer.emit("caller", "caller_turn", {"text": action.say}, turn=turn)
                if action.hang_up or not action.say:
                    ended_by = "caller"
                    reason = "the script ended" if scenario["style"] == "scripted" else "the caller hung up"
                    break
                proxy.turn = turn
                try:
                    await session.send_text(action.say)
                except Exception as e:  # the agent crashed mid-call: the agent's fault, not ours
                    writer.emit("harness", "harness_event", {
                        "level": "error", "code": "agent_exception",
                        "message": f"{type(e).__name__}: {e}"}, turn=turn)
                    ended_by, reason = "agent", "the agent raised an error"
                    break
                out = await session.receive()
                heard = out.text or ""
                writer.emit("connector", "agent_turn", {"heard_text": heard}, turn=turn)
                if session.has_output() and (await session.receive()).kind == "closed":
                    ended_by, reason = "agent", "the agent hung up"
            await session.close()
            session = None
            writer.emit("connector", "call_ended", {"ended_by": ended_by, "reason": reason}, turn=turn)
            ended = True

            if scenario["style"] == "simulated" and not self.config.offline:
                validator = LLMValidator(self.llm, model=self.config.validator_model, metadata=meta)
                res = await validator.validate(CallTrace(writer.record, writer.events), scenario)
                validator_off_script = not res.on_script
                writer.emit("validator", "validator_result",
                            {"on_script": res.on_script, "problems": list(res.problems)})

            for probe_id in self._probes_needed(scenario):
                result = SqlProbe(self.profile, self.config.root).run(
                    probe_id, ctx, (writer.record["started_at"], utc_now()))
                data = {"probe": result.probe, "ok": result.ok}
                if result.rows is not None:
                    data["rows"] = [dict(r) for r in result.rows]
                if result.value is not None and not isinstance(result.value, dict):
                    data["value"] = result.value
                if result.error:
                    data["error"] = result.error
                writer.emit("probe", "probe_result", data)
        except Exception as e:
            code = e.code if isinstance(e, HarnessError) else _error_code(e)
            writer.emit("harness", "harness_event", {"level": "error", "code": code,
                                                     "message": f"{type(e).__name__}: {e}"})
            if not ended:
                writer.emit("connector", "call_ended", {"ended_by": "harness", "reason": code})
            if session is not None:
                try:
                    await session.close()
                except Exception:
                    pass  # the call is already recorded as a harness error
            status = "harness_error"
            self.log(f"    harness error ({code}): {e}")

        writer.lock(status, cost_usd=self._spent() - cost_before)
        trace = self.store.read_call(run_id, ctx.test_id)
        verdict = self.evaluate(trace, scenario, validator_off_script)
        self.store.write_verdict(run_id, ctx.test_id, verdict)
        return verdict

    def _caller(self, scenario: Mapping[str, Any], meta: Mapping[str, str]):
        if scenario["style"] == "scripted":
            return ScriptedCaller([t["caller"] for t in scenario["turns"]])
        if self.config.offline:
            return RuleCaller(scenario)
        if self.llm is None:
            raise HarnessError("no_llm", "simulated scenarios need an LLM (set ANTHROPIC_API_KEY or use --offline)")
        persona = self.project.personas[scenario["caller"]["persona"]]
        return LLMCaller(scenario, persona, self.llm, model=self.config.caller_model,
                         agent_name=self.profile.get("name", "the agent"), metadata=meta)

    def _probes_needed(self, scenario: Mapping[str, Any]) -> list[str]:
        if self.profile.get("backend", {}).get("access") != "sql":
            return []
        ids = []
        if scenario.get("outcome", {}).get("record"):
            ids.append(scenario["outcome"]["record"]["probe"])
        checks = list(scenario.get("checks", [])) + [c for t in scenario.get("turns", []) for c in t.get("expect", [])]
        ids += [c["probe"] for c in checks if c.get("type") == "probe"]
        if any(c.get("type") == "entity_fidelity" for c in checks):
            ids += [p["id"] for p in self.profile["backend"].get("probes", [])]
        return list(dict.fromkeys(ids))

    def _spent(self) -> float:
        budget = getattr(self.llm, "_budget", None)
        return budget.spent_usd if budget else 0.0

    # ---- evaluation -----------------------------------------------------
    def evaluate(self, trace: CallTrace, scenario: Mapping[str, Any],
                 validator_off_script: bool | None) -> dict[str, Any]:
        facts = scenario.get("caller", {}).get("facts", {})
        salt = trace.record["test_id"]
        events = list(trace.events)
        results: list[CheckResult] = scenario_checks(scenario, events, facts, salt, "text")
        outcome = evaluate_outcome(scenario, self.profile, events, facts, salt)
        block = None
        if outcome:
            block, outcome_check = outcome
            results.insert(0, outcome_check)
        validator_events = [e for e in events if e.type == "validator_result"]
        if validator_events:
            e = validator_events[-1]
            off = validator_off_script if validator_off_script is not None else not e.data["on_script"]
            results.append(CheckResult(
                check_id="simulator-on-script", origin="validator",
                result=Result.ERROR if off else Result.PASS, severity="major", section="accuracy",
                message=("The simulated caller went off script: " + "; ".join(e.data.get("problems", []))
                         if off else "The simulated caller kept to its goal and facts."),
                evidence=(EvidenceRef(e.seq),), evidence_level=EvidenceLevel.AI_OPINION))
        return LocalVerdictBuilder().build(trace, results, block)


def _error_code(e: Exception) -> str:
    name = type(e).__name__
    if name == "BudgetExceeded":
        return "budget_exceeded"
    if name in ("LLMError", "ValueError"):
        return "llm_error"
    return "harness_crash"
