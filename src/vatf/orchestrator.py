"""Run orchestrator (contracts/orchestrator.py): plan, run each call, rerun harness errors only,
evaluate, decide and write the report."""

from __future__ import annotations

import asyncio
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import claimread, modes, recordings, statewatch
from .attacks import is_safety
from .calibration import calibration
from .caller import LLMCaller, LLMValidator, RuleCaller, ScriptedCaller, ScriptThenLLM, follow_up_scenario
from .checks import scenario_checks
from .connectors import ConnectError, build_connector, is_voice
from .contracts.models import CallContext, CallTrace, CheckResult, EvidenceLevel, EvidenceRef, Result
from .contracts.orchestrator import PlannedCall
from .decision import LocalDecisionEngine, apply_regression
from .evidence import LocalEvidenceStore, utc_now
from .llm import DEFAULT_MODEL
from .gateway import GatewayError, ToolGateway, gateway_address, uses_gateway
from .judges import JudgePanel, judge_settings, skipped
from .loader import Project, ProjectError
from .logs import LogCollector, heard_in_log
from .metrics import call_quality, with_bands
from .outcome import claim_question, evaluate_more, evaluate_outcome
from .probe import BackendProbe, ProbeSetupError, load_fixtures
from .proxy import InProcessToolProxy
from .report import write_report
from .rubrics import items_for_call
from .speech import SpeechConfig
from .speech.local import blocked_hint
from .story import call_story_text, load_events, run_story_text
from .baseline import BaselineError, load_baseline
from .compare import compare
from .snapshot import snapshot
from .util import MissingSecret, expand_env, read_json, write_json
from .verdict import LocalVerdictBuilder, merge_results

FOLLOW_UP_TURNS = 6   # caller turns Claude adds after a scripted call's fixed lines (limits.follow_up_turns)
MAX_WENT_ON = 3   # voice: times one agent turn may go on again before the caller answers anyway


class RunSetupError(ProjectError):
    """The run cannot start: a secret is missing, the tool gateway's port is taken... Nothing was called."""


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
    speech: SpeechConfig = field(default_factory=SpeechConfig)  # voice mode: the caller's TTS and STT
    judges: bool = True                           # False (--no-judges): skip the AI judges for this run
    safety: bool = False                          # True (--safety): add the built-in safety tests (attacks.py)
    conditions: str = "as_written"                # voice: caller conditions preset over every test (conditions.py)
    sweep: tuple[str, ...] = ()                   # voice, Speech mode: run every test once under each of these conditions
    drills: tuple[str, ...] = ()                  # Resilience: run every test again with its write tool failing (drills.py)
    ab: dict[str, Any] | None = None              # A/B test: {group, side A|B, label}, recorded in run.json
    baseline: str = "auto"                        # Phase 6: auto (the agent's saved baseline), none, or a snapshot file
    trigger: str = "manual"                       # manual | ci | health: how the run was started (shown and alerted)
    caller: str = "claude"                        # Phase 7 load tests: free = scripted lines / rule-based caller, no Claude
    fixtures_once: bool = False                   # Phase 7: test data loaded once before the calls, not before each call
    test_mode: str | None = None                  # modes.py: functional, timing, speech, safety, resilience, regression, full
    language: str | None = None                   # every caller speaks this language (languages.py); None = each caller's own


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
        self._gateway: ToolGateway | None = None
        self._calibration: dict[str, dict[str, Any]] | None = None
        self._judge_cost = 0.0
        self.concurrent = False   # True while a load test runs calls at the same time (loadrun.py)

    @property
    def mode(self) -> str:
        """voice when the profile connects over audio, otherwise text."""
        return "voice" if is_voice(self.profile) else "text"

    # ---- planning -------------------------------------------------------
    def scenarios(self) -> list[Mapping[str, Any]]:
        # A scenario runs in the modes it lists (default: both); e.g. barge-in needs voice.
        chosen = [s for s in self.project.scenarios if self.mode in s.get("modes", ["text", "voice"])]
        safety = [s for s in chosen if is_safety(s)
                  # Adaptive attackers need the Claude caller: not in a free dry run.
                  and not (self.config.offline and "adaptive" in s.get("tags", []))]
        chosen = [s for s in chosen if not is_safety(s)]
        if self.config.test_mode == "resilience" and self.config.drills:
            return self._drill_scenarios(chosen)
        if self.config.test_mode:
            # A test mode picks its own tests (modes.select); picked ids narrow them further.
            pool = modes.select(self.config.test_mode, chosen, safety, self.config.safety)
            if not self.config.scenario_ids:
                return pool
            unknown = set(self.config.scenario_ids) - {s["id"] for s in pool}
            if unknown:
                raise KeyError(f"not part of the {self.config.test_mode} tests: {', '.join(sorted(unknown))}")
            return [s for s in pool if s["id"] in self.config.scenario_ids]
        if self.config.scenario_ids:
            known = {s["id"] for s in chosen} | {s["id"] for s in safety}
            unknown = set(self.config.scenario_ids) - known
            if unknown:
                raise KeyError(f"unknown scenario(s): {', '.join(sorted(unknown))}")
            picked = [s for s in chosen + safety if s["id"] in self.config.scenario_ids]
            # The safety tests come in whole with --safety, even when other scenarios are picked.
            return picked + [s for s in safety if self.config.safety and s not in picked]
        return chosen + (safety if self.config.safety else [])

    def _drill_scenarios(self, own: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        """Resilience with failure drills: the picked test cases (all, if none picked) that break a tool
        themselves, then each drillable one again per failure. The drills join the project's scenarios
        so they are found by id like any test."""
        from . import drills

        bad = [d for d in self.config.drills if d not in drills.FAILURES]
        if bad:
            raise RunSetupError(f"unknown failure(s) {', '.join(bad)} (use {', '.join(drills.FAILURES)})")
        own = [s for s in own if drills.DRILL_TAG not in (s.get("tags") or [])]   # drills made by an earlier call
        picked = set(self.config.scenario_ids)
        unknown = picked - {s["id"] for s in own}
        if unknown:
            raise KeyError(f"unknown test case(s): {', '.join(sorted(unknown))}")
        # Resilience drills its own cases and the Functional ones (Timing and Speech cases test other things).
        base = [s for s in own if (not picked or s["id"] in picked) and modes.type_of(s) in ("resilience", "functional")]
        made = drills.expand(base, self.config.drills)
        have = {s["id"] for s in self.project.scenarios}
        self.project.scenarios = list(self.project.scenarios) + [d for d in made if d["id"] not in have]
        for d in made:
            sha = self.project.scenario_sha256.get(drills.split(d["id"])[0], "")
            self.project.scenario_sha256.setdefault(d["id"], sha)
        return [s for s in base if modes.type_of(s) == "resilience"] + made

    def repeats_for(self, scenario: Mapping[str, Any]) -> int:
        return self.config.repeats or scenario.get("repeats") or self.profile.get("thresholds", {}).get("repeats", 3)

    def plan(self) -> list[PlannedCall]:
        calls = []
        for sc in self.scenarios():
            persona = sc["caller"]["persona"] if sc["style"] == "simulated" else None
            # A Speech mode sweep runs each test under every chosen condition (repeats per condition).
            for cond in (self.config.sweep if self.mode == "voice" and self.config.sweep else (None,)):
                for r in range(1, self.repeats_for(sc) + 1):
                    calls.append(PlannedCall(sc["id"], persona, r, cond))
        return calls

    # ---- running --------------------------------------------------------
    async def run(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        run_id = f"r-{now:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
        self._run_date = f"{now:%Y%m%d}"
        plan = self.plan()
        self.preflight()
        run_dir = self.store.runs_dir / run_id
        info = self._run_info(run_id, plan)
        self._info = info
        run_dir.mkdir(parents=True, exist_ok=True)
        write_json(run_dir / "run.json", info)
        self.log(f"run {run_id}: {self._plan_text(plan)}")
        try:
            if uses_gateway(self.profile):
                host, port = gateway_address(self.profile)
                try:
                    header = (self.profile.get("test_id") or {}).get("header_name", "X-Test-Id")
                    self._gateway = ToolGateway(host, port, header=header, fallback=self._gateway_fallback()).start()
                except GatewayError as e:
                    raise RunSetupError(str(e)) from e
                self.log(f"tool gateway: the agent's tools are at http://{host}:{self._gateway.port}/tools/<name>")
            decision = await self._run_plan(run_id, plan, run_dir)
        except BaseException as e:
            stopped = isinstance(e, (KeyboardInterrupt, asyncio.CancelledError))
            info.update(status="stopped" if stopped else "failed", finished_at=utc_now(),
                        error="" if stopped else f"{type(e).__name__}: {e}")
            write_json(run_dir / "run.json", info)
            raise
        finally:
            if self._gateway:
                self._gateway.stop()
                self._gateway = None
        info.update(status="finished", finished_at=utc_now(), decision=decision["decision"],
                    exit_code=decision["exit_code"], judge_cost_usd=round(self._judge_cost, 6))
        write_json(run_dir / "run.json", info)
        return decision

    def _plan_text(self, plan: list[PlannedCall]) -> str:
        return f"{len(plan)} planned call(s)"   # the dashboard reads "run <id>: <n> planned call" from this line

    def _gateway_fallback(self) -> Any:
        """Where tool calls go that can't be matched to a call (only with several calls at once)."""
        return None

    def _compare(self, decision: dict[str, Any], run_dir: Path) -> dict[str, Any] | None:
        """Phase 6: this run against the agent's baseline (baseline.py), saved as comparison.json, with a
        copy of the baseline used (baseline.json) so relabelling later compares against the same one."""
        mode = self.config.baseline or "auto"
        if mode == "none":
            return None
        try:
            if mode == "auto":
                base = load_baseline(Path(self.project.folder), self.project.profile_file)
                source = "baseline"
            else:
                base, source = read_json(Path(mode)), Path(mode).name
                if not isinstance(base, dict) or base.get("kind") != "run_snapshot":
                    raise BaselineError(f"{mode} is not a Vocto baseline (a run snapshot)")
        except (BaselineError, OSError, ValueError) as e:
            self.log(f"warning: no comparison with the baseline: {e}")
            return None
        if base is None:
            return None
        return self._compare_with(decision, run_dir, base, source)

    def _compare_with(self, decision: dict[str, Any], run_dir: Path, base: Mapping[str, Any],
                      source: str) -> dict[str, Any]:
        titles = {sc["id"]: sc.get("title", sc["id"]) for sc in self.scenarios()}
        comparison = compare(base, snapshot(decision, self._info, titles), self.profile, source)
        write_json(run_dir / "baseline.json", base)
        write_json(run_dir / "comparison.json", comparison)
        c = comparison["counts"]
        self.log(f"vs baseline {base.get('run_id')}: {c['regression']} new failure(s), {c['worse']} worse, "
                 f"{c['better']} better" + ("" if comparison["like_for_like"] else " (not like-for-like)"))
        return comparison

    def preflight(self) -> None:
        """Problems that would make every call fail, found before the first call: secrets named as
        ${NAME} in the profile must be set (in the .env file or the shell)."""
        from .conditions import PRESETS, known

        for c in (self.config.conditions, *self.config.sweep):
            if not known(c):
                raise RunSetupError(f"unknown caller conditions {c!r} (use one of {', '.join(PRESETS)}, or accent:<id>)")
        if self.config.drills:
            from .drills import FAILURES

            bad = [d for d in self.config.drills if d not in FAILURES]
            if bad:
                raise RunSetupError(f"unknown failure(s) {', '.join(bad)} (use {', '.join(FAILURES)})")
            if self.config.test_mode != "resilience":
                raise RunSetupError("failure drills are part of the Resilience test mode (--test-mode resilience)")
        from .languages import LANGUAGES

        langs = {self._language(sc) for sc in self.project.scenarios}
        unknown = sorted(langs - set(LANGUAGES))
        if unknown:
            raise RunSetupError(f"Vocto's caller can't speak {', '.join(unknown)} yet "
                                f"(it speaks {', '.join(v['name'] for v in LANGUAGES.values())})")
        if self.mode == "voice":
            bad = recordings.problems(Path(self.project.folder), self.scenarios())
            if bad:
                raise RunSetupError("A recorded caller line can't be played: " + "; ".join(bad[:5])
                                    + (f" (and {len(bad) - 5} more)" if len(bad) > 5 else ""))
        p = self.profile
        named = [p["connection"].get("options", {}).get("headers"), p.get("backend", {}).get("connection"),
                 p.get("backend", {}).get("headers"), (p.get("logs") or {}).get("url"), (p.get("logs") or {}).get("headers"),
                 [t.get("headers") for t in p.get("tools", [])]]
        try:
            expand_env(named)
        except MissingSecret as e:
            raise RunSetupError(str(e)) from e

    def _run_info(self, run_id: str, plan: list[PlannedCall]) -> dict[str, Any]:
        """runs/<run_id>/run.json: how the run was started and what it will do (read by `vatf ui`)."""
        from . import __version__

        cfg = self.config
        info: dict[str, Any] = {
            "schema_version": "0.1", "kind": "run_info", "run_id": run_id, "status": "running",
            "started_at": utc_now(), "framework_version": __version__,
            "project": Path(self.project.folder).as_posix(), "profile_file": self.project.profile_file,
            "profile_id": self.profile["id"], "profile_name": self.profile.get("name", self.profile["id"]),
            "mode": self.mode, "offline": cfg.offline, "repeats": cfg.repeats,
            "scenario_ids": list(cfg.scenario_ids), "safety": cfg.safety, "conditions": cfg.conditions,
            **({"sweep": list(cfg.sweep)} if cfg.sweep and self.mode == "voice" else {}),
            **({"drills": list(cfg.drills)} if cfg.drills and cfg.test_mode == "resilience" else {}),
            **({"caller_language": cfg.language} if cfg.language else {}),
            **({"ab": dict(cfg.ab)} if cfg.ab else {}),
            "test_mode": cfg.test_mode or ("full" if cfg.safety else "functional"),
            "trigger": cfg.trigger, "baseline": cfg.baseline, "agent_options": dict(self.profile["connection"].get("options", {})),
            "caller_model": None if cfg.offline else cfg.caller_model, "pii_masked": cfg.mask_pii,
            "judges": ({"enabled": False, "why": self._judges_off()} if self._judges_off() else
                       {k: v for k, v in judge_settings(self.profile).items() if k != "policy"}),
            "plan": [{"scenario_id": pc.scenario_id, "persona_id": pc.persona_id, "repeat": pc.repeat,
                      **({"conditions": pc.conditions} if pc.conditions else {})} for pc in plan],
        }
        if self.mode == "voice":
            info["speech"] = {"engine": cfg.speech.engine, "ears_model": cfg.speech.ears_model,
                              "check_model": cfg.speech.check_model}
        from .languages import hearing_model

        langs = sorted({self._language(self.project.scenario(pc.scenario_id)) for pc in plan})
        if langs != ["en"]:
            info["languages"] = langs
            if self.mode == "voice" and cfg.speech.engine == "local":
                # A call in another language hears with the multilingual model: say which one was used.
                info["speech"]["by_language"] = {
                    lang: {"ears_model": hearing_model(cfg.speech.ears_model, lang),
                           "check_model": hearing_model(cfg.speech.check_model, lang, True)} for lang in langs}
        return info

    async def _run_plan(self, run_id: str, plan: list[PlannedCall], run_dir: Path) -> dict[str, Any]:
        retries = self.profile.get("thresholds", {}).get("max_harness_retries", 2)
        if self.mode == "voice":
            await self._warm_up_speech(plan)
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
        return self._close(run_id, decision, finals, error_attempts, run_dir)

    def _close(self, run_id: str, decision: dict[str, Any], finals: list, error_attempts: list,
               run_dir: Path) -> dict[str, Any]:
        """The decision against the baseline, then decision.json, the report and the transcripts."""
        comparison = self._compare(decision, run_dir)
        decision = apply_regression(decision, comparison)
        write_json(run_dir / "decision.json", decision)
        decision["_comparison"] = comparison
        decision["_verdicts"] = finals
        decision["_pii_masked"] = self.config.mask_pii
        decision["_mode"] = self.mode
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

    async def _warm_up_speech(self, plan) -> None:
        """Load (and the first time, download) the speech models once, before any call clock starts,
        so recordings don't open with the loading time. A failure here is reported by the call itself."""
        from .speech import make_stt, make_tts

        voices, languages = set(), set()
        for pc in plan:
            scenario = self.project.scenario(pc.scenario_id)
            cond = self._conditions(scenario, "", pc.conditions)
            voices.add(cond.voice)
            languages.add(cond.language)
        try:
            tts = make_tts(self.config.speech, self.config.root)
            ears = [make_stt(self.config.speech, self.config.root, language=lang) for lang in sorted(languages)]
            await asyncio.to_thread(lambda: ([tts.warm_up(v) for v in sorted(voices)], [s.warm_up() for s in ears]))
        except Exception:  # noqa: BLE001 - each call retries and records the error as a harness error
            pass

    async def run_call(self, run_id: str, pc: PlannedCall, attempt: int) -> dict[str, Any]:
        scenario = self.project.scenario(pc.scenario_id)
        ctx = CallContext(test_id=self._test_id(), run_id=run_id, profile=self.profile, scenario=scenario,
                          repeat=pc.repeat, attempt=attempt, mode=self.mode)
        writer = self.store.open_call(ctx, persona_id=pc.persona_id,
                                      profile_sha256=self.project.profile_sha256,
                                      scenario_sha256=self.project.scenario_sha256.get(scenario["id"], ""),
                                      mask=self.config.mask_pii)
        meta = {"test_id": ctx.test_id, "log_dir": str(writer.llm_dir)}
        cost_before = self._spent()
        status = "completed"
        ended = False
        validator_off_script = None
        live: dict[str, Any] = {}  # what must be closed if the call fails half way (session, line)
        try:
            if not self.config.fixtures_once:   # a load test loads them once, before its first call
                try:
                    load_fixtures(self.profile, scenario, self.config.root)
                except ProbeSetupError as e:
                    raise HarnessError("fixtures_failed", str(e)) from e
            watch = self._watched_tables()
            before, watch_error = None, None
            if watch:
                try:
                    before = statewatch.snapshot(self.profile, self.config.root, watch)
                except statewatch.WatchError as e:
                    watch_error = str(e)
            proxy = InProcessToolProxy(self.profile, scenario.get("setup", {}).get("faults", []),
                                       real_delays=self.mode == "voice")
            await proxy.attach(ctx, writer)
            if self._gateway:
                self._gateway.attach(proxy, ctx.test_id)
            logs = LogCollector(self.profile, self.config.root)
            logs.begin()
            cond = self._conditions(scenario, ctx.test_id, pc.conditions) if self.mode == "voice" else None
            writer.emit("connector", "call_started", {"test_id": ctx.test_id, "mode": self.mode,
                                                      **({"conditions": cond.summary()} if cond else {})})
            caller = self._caller(scenario, meta)
            if self.mode == "voice":
                ended_by, reason, turn = await self._converse_voice(ctx, scenario, caller, proxy, writer, live, cond)
            else:
                ended_by, reason, turn = await self._converse_text(ctx, scenario, caller, proxy, writer, live)
            writer.emit("connector", "call_ended", {"ended_by": ended_by, "reason": reason,
                                                    **live.pop("recording", {})}, turn=turn)
            ended = True
            if self._gateway:
                self._gateway.detach(ctx.test_id)
            if self.mode == "voice":
                await self._listen_again(scenario, writer, live)
            if logs.enabled:
                await self._collect_logs(logs, ctx, writer)

            if scenario["style"] == "simulated" and not self.config.offline and self.config.caller != "free":
                validator = LLMValidator(self.llm, model=self.config.validator_model, metadata=meta)
                res = await validator.validate(CallTrace(writer.record, writer.events), scenario)
                validator_off_script = not res.on_script
                writer.emit("validator", "validator_result",
                            {"on_script": res.on_script, "problems": list(res.problems)})

            for probe_id in self._probes_needed(scenario):
                result = BackendProbe(self.profile, self.config.root).run(
                    probe_id, ctx, (writer.record["started_at"], utc_now()))
                data = {"probe": result.probe, "ok": result.ok}
                if result.rows is not None:
                    data["rows"] = [dict(r) for r in result.rows]
                if result.value is not None and not isinstance(result.value, dict):
                    data["value"] = result.value
                if result.error:
                    data["error"] = result.error
                writer.emit("probe", "probe_result", data)
            if watch:
                if before is not None:
                    try:
                        diff = statewatch.diff(before, statewatch.snapshot(self.profile, self.config.root, watch),
                                               statewatch.ignored_columns(self.profile))
                    except statewatch.WatchError as e:
                        diff = {"error": str(e)}
                else:
                    diff = {"error": watch_error or "the tables could not be read before the call"}
                writer.emit("probe", "state_diff", {"watched": watch, **diff})
        except Exception as e:
            code = e.code if isinstance(e, HarnessError) else _error_code(e)
            writer.emit("harness", "harness_event", {"level": "error", "code": code,
                                                     "message": f"{type(e).__name__}: {e}"})
            await self._hang_up(live, writer)
            if not ended:
                writer.emit("connector", "call_ended", {"ended_by": "harness", "reason": code,
                                                        **live.pop("recording", {})})
            status = "harness_error"
            self.log(f"    harness error ({code}): {e}")
        finally:
            if self._gateway:
                self._gateway.detach(ctx.test_id)

        # With several calls at once (load tests) the spend can't be split per call: the run has the total.
        shared = self.concurrent
        writer.lock(status, cost_usd=0.0 if shared else self._spent() - cost_before)
        trace = self.store.read_call(run_id, ctx.test_id)
        judge_before = self._spent()
        judged, notes = await self._judge(trace, scenario, status, meta)
        readings = await self._read_claims(trace, scenario, status, meta)
        verdict = self.evaluate(trace, scenario, validator_off_script, judged, readings)
        if notes:
            verdict["judge_notes"] = notes
        if not shared:
            verdict["metrics"]["judge_cost_usd"] = round(self._spent() - judge_before, 6)
            self._judge_cost += self._spent() - judge_before
        self.store.write_verdict(run_id, ctx.test_id, verdict)
        return verdict

    # ---- AI judges ------------------------------------------------------
    def _judges_off(self) -> str | None:
        """Why the judges do not run for this run, or None when they do."""
        if not judge_settings(self.profile)["enabled"]:
            return "Judges are switched off for this agent."
        if not self.config.judges:
            return "Judges were switched off for this run."
        if self.config.offline:
            return "Judges do not run in offline runs (they need an AI model)."
        if self.llm is None:
            return "Judges need an AI model (set ANTHROPIC_API_KEY)."
        return None

    async def _read_claims(self, trace: CallTrace, scenario: Mapping[str, Any], status: str,
                           meta: Mapping[str, str]) -> dict[str, dict[str, Any]]:
        """Two AI models read the agent's words when the claim patterns couldn't (claimread.py)."""
        if status == "harness_error" or self.config.offline or self.llm is None:
            return {}
        questions = claim_question(scenario, list(trace.events))
        if not questions:
            return {}
        s = judge_settings(self.profile)
        try:
            return await claimread.read_claims(self.llm, questions, [s["primary_model"], s["secondary_model"]], meta)
        except Exception as e:  # noqa: BLE001 - the claim stays unclear (NOT_VERIFIED), never a pass
            if type(e).__name__ == "BudgetExceeded":
                raise
            self.log(f"    claim reader failed: {e}")
            return {}

    def calibration(self) -> dict[str, dict[str, Any]]:
        """Each judge item's calibration, read once per run from the project's labels."""
        if self._calibration is None:
            items = {i.id: i for sc in self.scenarios() for i in items_for_call(self.profile, sc)}
            self._calibration = calibration(self.project.folder, self.profile, list(items.values()))
        return self._calibration

    async def _judge(self, trace: CallTrace, scenario: Mapping[str, Any], status: str,
                     meta: Mapping[str, str]) -> tuple[list[CheckResult], list[str]]:
        items = items_for_call(self.profile, scenario)
        off = self._judges_off()
        if off or status == "harness_error":
            return skipped(items, off or "Not judged: the call did not complete."), []
        events = [{"seq": e.seq, "t_ms": e.t_ms, "source": e.source, "type": e.type, "data": e.data, "turn": e.turn}
                  for e in trace.events]
        panel = JudgePanel(self.llm, self.profile, self.calibration(), metadata=meta)
        try:
            return await panel.judge(events, scenario, items, self.mode)
        except Exception as e:  # noqa: BLE001 - budget used up, or the API failed: judges are not the agent's fault
            why = "the run's budget is used up" if type(e).__name__ == "BudgetExceeded" else f"{type(e).__name__}: {e}"
            self.log(f"    judges could not run: {why}")
            return skipped(items, f"Not judged: {why}."), [f"judges could not run: {why}"]

    # ---- one conversation, text or voice ------------------------------------
    def _limits(self, scenario: Mapping[str, Any]) -> tuple[int, float]:
        limits = self.profile.get("limits", {})
        max_turns = (len(scenario["turns"]) if scenario["style"] == "scripted"
                     else scenario["caller"].get("max_turns", limits.get("max_turns", 30)))
        return max_turns, time.monotonic() + limits.get("max_call_seconds", 300)

    async def _converse_text(self, ctx, scenario, caller, proxy, writer, live) -> tuple[str, str, int]:
        try:
            session = await build_connector(self.profile, self.config.root, proxy).open(ctx, writer.now_ms)
        except ConnectError as e:
            raise HarnessError("connect_failed", str(e)) from e
        live["session"] = session
        greeting = await session.receive()
        writer.emit("connector", "agent_turn", {"heard_text": greeting.text or ""}, turn=1)
        heard, turn = greeting.text or "", 1
        ended_by, reason = None, ""
        max_turns, deadline = self._limits(scenario)
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
                reason = ("the script ended" if scenario["style"] == "scripted" and not getattr(caller, "following_up", False)
                          else "the caller hung up")
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
        live.pop("session", None)
        return ended_by, reason, turn

    def _conditions(self, scenario: Mapping[str, Any], seed: str, preset: str | None = None):
        """The caller conditions of one voice call: persona, the test's audio and the run's preset
        (or, in a Speech mode sweep, this call's condition)."""
        from .conditions import resolve

        persona = self.project.personas.get((scenario.get("caller") or {}).get("persona", ""), {})
        return resolve(persona, scenario, preset=preset or self.config.conditions,
                       default_voice=self.config.speech.caller_voice, seed=seed,
                       language=self._language(scenario))

    def _language(self, scenario: Mapping[str, Any]) -> str:
        """The language the caller speaks on this call: the run's choice, else its persona's, else the agent's first."""
        from .languages import call_language

        if self.config.language:
            return self.config.language
        persona = self.project.personas.get((scenario.get("caller") or {}).get("persona", ""), {})
        return call_language(self.profile, persona)

    async def _converse_voice(self, ctx, scenario, caller, proxy, writer, live, cond=None) -> tuple[str, str, int]:
        from .audio.line import LineEffects, NetworkJitter
        from .audio.noise import load_noise
        from .audio.recorder import Line, Recorder
        from .conditions import noise_settings
        from .audio.tap import LiveTap
        from .speech import make_stt, make_tts
        from .voice_call import VoiceConversation, VoiceSettings

        connector = build_connector(self.profile, self.config.root, proxy)
        sr = connector.sample_rate
        rec = Recorder(sr, writer.elapsed)
        limits = self.profile.get("limits", {})
        audio = scenario.get("audio", {})
        persona = self.project.personas.get(scenario.get("caller", {}).get("persona", ""), {})
        cond = cond or self._conditions(scenario, ctx.test_id)
        try:
            tts = make_tts(self.config.speech, self.config.root)
            stt = make_stt(self.config.speech, self.config.root, language=cond.language)
            # Models load (and download, the first time) here, before the agent is dialled.
            await asyncio.to_thread(lambda: (tts.warm_up(cond.voice), stt.warm_up()))
            noise = load_noise(noise_settings(cond), limits.get("max_call_seconds", 300) + 60, sr, ctx.test_id,
                               self.config.root)
        except Exception as e:
            raise HarnessError("speech_setup_failed", f"{type(e).__name__}: {e}{blocked_hint(e)}") from e
        try:
            session = await connector.open(ctx, rec)
        except ConnectError as e:
            raise HarnessError("connect_failed", str(e)) from e
        answered = rec.now_sample()   # the agent can only be heard from here on
        line = Line(rec, session.send_frame, noise,
                    LineEffects(sr, phone=cond.phone, packet_loss_pct=cond.packet_loss_pct, seed=ctx.test_id),
                    NetworkJitter(sr, jitter_ms=cond.jitter_ms, reorder_pct=cond.reorder_pct, seed=ctx.test_id))
        live.update(session=session, line=line, recorder=rec)
        line.start()
        live["tap"] = LiveTap.from_env(rec, f"{ctx.test_id} {scenario['id']}")   # dashboard's "Listen live"

        behaviour = persona.get("behaviour", {})
        settings = VoiceSettings(
            end_of_turn_ms=limits.get("end_of_turn_ms", 1000),
            no_reply_ms=behaviour.get("hangs_up_after_silence_ms", limits.get("no_reply_ms", 15000)),
            barge_in=list(audio.get("barge_in", [])), conditions=cond)
        conv = VoiceConversation(
            session=session, recorder=rec, line=line, tts=tts, stt=stt,
            voice=cond.voice, speaking_rate=cond.speaking_rate, settings=settings, writer=writer, proxy=proxy,
            language=cond.language)
        await conv.prepare_sounds()

        heard = await conv.listen(since=answered, caller_end=None, turn=1)
        conv.emit_agent_turn(heard, 1, None, answered=answered)
        turn, caller_turns, silent = 1, 0, 0
        if heard.interrupted:          # a scenario cut-in on the greeting (audio.barge_in agent_turn 1)
            heard, turn, caller_turns = await self._after_cut_in(conv, caller, heard, turn, caller_turns)
        ended_by, reason = None, ""
        max_turns, deadline = self._limits(scenario)
        while ended_by is None:
            if conv._done():
                ended_by, reason = "agent", session.close_reason or "the agent hung up"
                break
            if time.monotonic() > deadline:
                ended_by, reason = "timeout", "max_call_seconds reached"
                break
            if caller_turns >= max_turns and scenario["style"] == "simulated":
                ended_by, reason = "max_turns", f"{max_turns} caller turns"
                break
            if silent >= 2:
                ended_by, reason = "caller", "the agent stopped answering"
                break
            action = await caller.next_turn(heard.text)
            clip = await self._clip(conv, action)
            if action.say and cond.quiet_turn == caller_turns + 1 and not conv._done():
                await self._go_quiet(conv, cond, heard, turn, writer)
            # The agent may go on talking while the caller thinks of an answer (a second sentence, or
            # what it says after a tool call). A person would stop and listen, then answer all of it.
            for _ in range(MAX_WENT_ON):
                if not conv.agent_went_on() or conv._done():
                    break
                heard = await conv.hear_rest(turn)
                conv.emit_agent_turn(heard, turn, None)
                redo = getattr(caller, "redo", None)       # a scripted caller keeps its next line
                if redo is not None:
                    action = await redo(heard.text)
                    clip = await self._clip(conv, action)
            if not action.say:
                ended_by = "caller"
                reason = ("the script ended" if scenario["style"] == "scripted" and not getattr(caller, "following_up", False)
                          else "the caller hung up")
                break
            turn += 1
            caller_turns += 1
            start, end = await conv.say(action.say, turn, clip=clip)
            if action.hang_up:
                await line.wait_until(end + sr // 5)
                ended_by, reason = "caller", "the caller hung up"
                break
            heard = await conv.listen(since=start, caller_end=end, turn=turn)
            conv.emit_agent_turn(heard, turn, end)
            if heard.interrupted:
                heard, turn, caller_turns = await self._after_cut_in(conv, caller, heard, turn, caller_turns)
            silent = silent + 1 if heard.start is None else 0
        await self._hang_up(live, writer)
        return ended_by, reason, turn

    async def _clip(self, conv, action):
        """The caller's next line, ready to say: from its recording when it has one, else synthesized."""
        if not action.say:
            return None
        if action.audio:
            try:
                return await conv.recording(recordings.resolve(self.project.folder, action.audio), action.audio)
            except (OSError, ValueError) as e:
                raise HarnessError("recording_failed", f"recording {action.audio}: {e}") from e
        return await conv.prepare(action.say)

    async def _after_cut_in(self, conv, caller, heard, turn: int, caller_turns: int):
        """The caller cut in on the agent: write down what it said, then hear the agent's answer to it."""
        b_start, b_end, words = heard.barge
        if not heard.auto:      # the caller's own cut-in keeps the turn, so scripted turn checks line up
            turn += 1
            caller_turns += 1
        conv.emit_caller_turn(words, b_start, b_end, turn, barge_in=True, auto=heard.auto)
        note = getattr(caller, "note_barge_in", None)
        if note:
            note(heard.text, words)
        heard = await conv.listen(since=heard.end + 1, caller_end=b_end, turn=turn)
        conv.emit_agent_turn(heard, turn, b_end)
        return heard, turn, caller_turns

    async def _go_quiet(self, conv, cond, heard, turn: int, writer) -> None:
        """The caller says nothing for quiet_ms after the agent's turn (once per call). A good agent checks
        in ("are you still there?"); the call loop then hears that and the caller answers it."""
        rec = conv.rec
        start = heard.end if heard.end is not None else rec.now_sample()
        deadline = start + cond.behaviour.quiet_ms * rec.sr // 1000
        while rec.now_sample() < deadline and not conv.agent_went_on() and not conv._done():
            if conv.line.error:
                raise conv.line.error
            await asyncio.sleep(0.03)
        spans = rec.agent_spans(conv._heard_to + 1, rec.now_sample())
        spoke = spans[0][0] if spans else None
        if spoke is None and rec.agent_playing():
            spoke = rec.now_sample()
        end = spoke if spoke is not None else max(deadline, start)
        data: dict[str, Any] = {"kind": "silence", "text": "", "quiet_ms": cond.behaviour.quiet_ms,
                                "agent_spoke_after_ms": rec.ms(spoke - start) if spoke is not None else None,
                                "audio": {"file": "audio/call.wav", "start_ms": rec.ms(start), "end_ms": rec.ms(end)}}
        writer.emit("caller", "caller_sound", data, turn=turn, t_ms=rec.ms(start))

    async def _hang_up(self, live: dict[str, Any], writer) -> None:
        """Close whatever is open; in voice mode save the recording (also after a harness error)."""
        line, session, rec = live.pop("line", None), live.pop("session", None), live.pop("recorder", None)
        tap = live.pop("tap", None)
        if line is not None:
            await line.stop()
        if tap is not None:
            await tap.stop()
        if session is not None:
            try:
                await session.close()
            except Exception:
                pass  # a failure here is recorded by the caller of _hang_up if it matters
        if rec is not None:
            from .audio.pcm import write_wav
            from .voice_call import RECORDING

            length = max(rec.caller.length, rec.agent.length)
            live["agent_track"] = (rec.agent.slice(0, length), rec.sr)
            write_wav(writer.folder / RECORDING, [rec.caller.slice(0, length), rec.agent.slice(0, length)], rec.sr)
            live["recording"] = {"recording": {"file": RECORDING, "sample_rate_hz": rec.sr,
                                               "channels": ["caller", "agent"],
                                               "duration_ms": rec.ms(length),
                                               "agent_audio_cleared": len(rec.clears)}}
            if getattr(session, "call_meta", None):        # a real phone call: its id, numbers, length, price
                live["recording"]["phone"] = dict(session.call_meta)
            for sample, text in getattr(session, "heard", None) or []:
                # What the agent reported understanding (protocol "heard"): compared with the caller's
                # words for the "caller words misheard" metric (metrics.caller_wer).
                writer.emit("connector", "agent_heard", {"text": text, "source": "agent"}, t_ms=rec.ms(sample))
            if getattr(session, "protocol_errors", None):
                writer.emit("harness", "harness_event", {
                    "level": "warning", "code": "protocol_error",
                    "message": "; ".join(session.protocol_errors[:5])})

    async def _listen_again(self, scenario: Mapping[str, Any], writer, live: dict[str, Any]) -> None:
        """Read-back checks and the outcome's "said" rest on what the agent said, so every agent turn
        after the tool result they look at is transcribed a second time with the careful model
        (stt_text events). One mishearing then never decides a check on its own."""
        from .audio.pcm import to_pcm16
        from .contracts.models import AudioClip
        from .speech import make_stt

        checks = list(scenario.get("checks", [])) + [c for t in scenario.get("turns", []) for c in t.get("expect", [])]
        tools = {c["tool"] for c in checks if c.get("type") == "readback"}
        action = scenario.get("outcome", {}).get("action", {}).get("tool")
        if not (tools or action) or "agent_track" not in live:
            return
        events = writer.events
        # From the first read-back tool result, or the first answer of the outcome's write tool
        # (the agent's claim about the result comes after it, success or failure).
        starts = [e.seq for e in events if e.type == "tool_response" and (
            (e.data.get("tool") in tools and e.data.get("ok")) or e.data.get("tool") == action)]
        if not starts:
            return
        first = min(starts)
        turns = [e for e in events if e.type == "agent_turn" and e.seq > first and e.data.get("audio")]
        if not turns:
            return
        agent, sr = live.pop("agent_track")
        try:
            from .languages import hearing_model

            language = self._language(scenario)
            stt = make_stt(self.config.speech, self.config.root, careful=True, language=language)
            for e in turns:
                a = max(0, e.data["audio"]["start_ms"] * sr // 1000 - sr // 20)
                b = e.data["audio"]["end_ms"] * sr // 1000 + sr // 20
                clip = AudioClip(to_pcm16(agent[a:b]), sr)
                text = (await asyncio.to_thread(stt.transcribe, clip, language)).text.strip()
                writer.emit("recorder", "stt_text", {"agent_turn": e.seq, "text": text,
                                                     "model": hearing_model(self.config.speech.check_model, language, True)
                                                     if self.config.speech.engine == "local" else self.config.speech.engine},
                                     turn=e.turn)
        except Exception as ex:
            writer.emit("harness", "harness_event", {"level": "warning", "code": "stt_failed",
                                                     "message": f"second listen failed: {type(ex).__name__}: {ex}"})

    async def _collect_logs(self, logs: LogCollector, ctx: CallContext, writer) -> None:
        result = await asyncio.to_thread(logs.collect, ctx.test_id, writer.record["started_at"], utc_now())
        summary = {"kind": "log_collected", "ok": result.ok, "from": result.source, "lines": len(result.lines)}
        if result.truncated:
            summary["truncated"] = True
        if result.error:
            summary["error"] = result.error
        writer.emit("platform", "platform_event", summary)
        pattern = (self.profile.get("logs") or {}).get("heard_pattern")
        rx = None
        if pattern:
            try:
                rx = re.compile(pattern)
            except re.error as e:
                writer.emit("harness", "harness_event", {"level": "warning", "code": "bad_heard_pattern",
                                                         "message": f"logs.heard_pattern is not a valid pattern: {e}"})
        for line in result.lines:
            writer.emit("platform", "platform_event", {"kind": "log", "text": line})
            text = heard_in_log(rx, line) if rx else None
            if text is not None:   # a log line saying what the agent's speech recognition heard from the caller
                writer.emit("platform", "agent_heard", {"text": text, "source": "log"})

    def _caller(self, scenario: Mapping[str, Any], meta: Mapping[str, str]):
        if scenario["style"] == "scripted":
            lines = [t["caller"] for t in scenario["turns"]]
            # Voice: a line with a recording is played from it (recordings.py); text sends the words.
            audio = [t.get("audio") if self.mode == "voice" else None for t in scenario["turns"]]
            turns = self._follow_up_turns(scenario)
            if not turns:
                return ScriptedCaller(lines, audio)
            brief = follow_up_scenario(scenario)
            persona_id = brief["caller"].get("persona") or next(iter(self.project.personas), None)
            claude = LLMCaller(brief, self.project.personas.get(persona_id, {}), self.llm,
                               model=self.config.caller_model, agent_name=self.profile.get("name", "the agent"),
                               metadata=meta, voice=self.mode == "voice", language=self._language(scenario))
            return ScriptThenLLM(lines, claude, turns, audio)
        if self.config.offline or self.config.caller == "free":
            return RuleCaller(scenario, spoken=self.mode == "voice")
        if self.llm is None:
            raise HarnessError("no_llm", "simulated scenarios need an LLM (set ANTHROPIC_API_KEY or use --offline)")
        persona = self.project.personas[scenario["caller"]["persona"]]
        return LLMCaller(scenario, persona, self.llm, model=self.config.caller_model,
                         agent_name=self.profile.get("name", "the agent"), metadata=meta,
                         voice=self.mode == "voice", language=self._language(scenario))

    def _follow_up_turns(self, scenario: Mapping[str, Any]) -> int:
        """Caller turns Claude may add after a scripted call's fixed lines (0: the call ends with them).
        Live runs only: a dry run has no Claude, and its calls stay exactly as scripted."""
        if (self.config.offline or self.llm is None or scenario.get("follow_up") is False
                or self.config.caller == "free"):
            return 0
        f = scenario.get("follow_up") if isinstance(scenario.get("follow_up"), dict) else {}
        return int(f.get("turns", self.profile.get("limits", {}).get("follow_up_turns", FOLLOW_UP_TURNS)))

    def _watched_tables(self) -> list[str]:
        """The backend tables copied before and after each call (statewatch.py). None for load tests,
        where many calls change the tables at once, and for runs that load test data only once."""
        if self.config.fixtures_once or self.concurrent:
            return []
        return statewatch.tables_of(self.profile)

    def _probes_needed(self, scenario: Mapping[str, Any]) -> list[str]:
        if self.profile.get("backend", {}).get("access") not in ("sql", "rest"):
            return []
        ids = []
        for o in ([scenario["outcome"]] if scenario.get("outcome") else []) + list(scenario.get("more_outcomes") or []):
            if o.get("record"):
                ids.append(o["record"]["probe"])
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
                 validator_off_script: bool | None, judged: Sequence[CheckResult] = (),
                 readings: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
        facts = scenario.get("caller", {}).get("facts", {})
        salt = trace.record["test_id"]
        events = list(trace.events)
        results: list[CheckResult] = scenario_checks(scenario, events, facts, salt, self.mode)
        outcome = evaluate_outcome(scenario, self.profile, events, facts, salt, readings)
        block = None
        if outcome:
            block, outcome_check = outcome
            results.insert(0, outcome_check)
        more = evaluate_more(scenario, self.profile, events, facts, salt, readings)
        for n, (_, step_check) in enumerate(more, start=1):
            results.insert(n, step_check)
        for extra in (statewatch.side_effects_check(scenario, self.profile, events),
                      statewatch.wrong_tool_check(scenario, self.profile, events)):
            if extra is not None:
                results.append(extra)
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
        verdict = LocalVerdictBuilder().build(trace, merge_results(results, judged), block)
        if more:
            verdict["more_outcomes"] = [b for b, _ in more]
        # Call quality (metrics.py): read off the timeline, banded with this agent's own values.
        verdict["metrics"]["quality"] = with_bands(call_quality(events, self.mode, scenario), self.profile)
        started = next((e for e in events if e.type == "call_started"), None)
        if started is not None and started.data.get("conditions"):
            verdict["conditions"] = dict(started.data["conditions"])
        return verdict


def _error_code(e: Exception) -> str:
    name = type(e).__name__
    if name == "BudgetExceeded":
        return "budget_exceeded"
    if name in ("LLMError", "ValueError"):
        return "llm_error"
    return "harness_crash"
