"""Run orchestrator (contracts/orchestrator.py): plan, run each call, rerun harness errors only,
evaluate, decide and write the report."""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .attacks import is_safety
from .calibration import calibration
from .caller import LLMCaller, LLMValidator, RuleCaller, ScriptedCaller
from .checks import scenario_checks
from .connectors import ConnectError, build_connector
from .contracts.models import CallContext, CallTrace, CheckResult, EvidenceLevel, EvidenceRef, Result
from .contracts.orchestrator import PlannedCall
from .decision import LocalDecisionEngine
from .evidence import LocalEvidenceStore, utc_now
from .llm import DEFAULT_MODEL
from .gateway import GatewayError, ToolGateway, gateway_address, uses_gateway
from .judges import JudgePanel, judge_settings, skipped
from .loader import Project, ProjectError
from .logs import LogCollector
from .outcome import evaluate_outcome
from .probe import BackendProbe, ProbeSetupError, load_fixtures
from .proxy import InProcessToolProxy
from .report import write_report
from .rubrics import items_for_call
from .speech import SpeechConfig
from .speech.local import blocked_hint
from .story import call_story_text, load_events, run_story_text
from .util import MissingSecret, expand_env, write_json
from .verdict import LocalVerdictBuilder, merge_results


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

    @property
    def mode(self) -> str:
        """voice when the profile connects over audio, otherwise text."""
        return "voice" if self.profile["connection"]["type"] == "websocket_audio" else "text"

    # ---- planning -------------------------------------------------------
    def scenarios(self) -> list[Mapping[str, Any]]:
        # A scenario runs in the modes it lists (default: both); e.g. barge-in needs voice.
        chosen = [s for s in self.project.scenarios if self.mode in s.get("modes", ["text", "voice"])]
        safety = [s for s in chosen if is_safety(s)
                  # Adaptive attackers need the Claude caller: not in a free dry run.
                  and not (self.config.offline and "adaptive" in s.get("tags", []))]
        chosen = [s for s in chosen if not is_safety(s)]
        if self.config.scenario_ids:
            known = {s["id"] for s in chosen} | {s["id"] for s in safety}
            unknown = set(self.config.scenario_ids) - known
            if unknown:
                raise KeyError(f"unknown scenario(s): {', '.join(sorted(unknown))}")
            picked = [s for s in chosen + safety if s["id"] in self.config.scenario_ids]
            # The safety tests come in whole with --safety, even when other scenarios are picked.
            return picked + [s for s in safety if self.config.safety and s not in picked]
        return chosen + (safety if self.config.safety else [])

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
        self.preflight()
        run_dir = self.store.runs_dir / run_id
        info = self._run_info(run_id, plan)
        run_dir.mkdir(parents=True, exist_ok=True)
        write_json(run_dir / "run.json", info)
        self.log(f"run {run_id}: {len(plan)} planned call(s)")
        try:
            if uses_gateway(self.profile):
                host, port = gateway_address(self.profile)
                try:
                    self._gateway = ToolGateway(host, port).start()
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

    def preflight(self) -> None:
        """Problems that would make every call fail, found before the first call: secrets named as
        ${NAME} in the profile must be set (in the .env file or the shell)."""
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
            "scenario_ids": list(cfg.scenario_ids), "safety": cfg.safety, "agent_options": dict(self.profile["connection"].get("options", {})),
            "caller_model": None if cfg.offline else cfg.caller_model, "pii_masked": cfg.mask_pii,
            "judges": ({"enabled": False, "why": self._judges_off()} if self._judges_off() else
                       {k: v for k, v in judge_settings(self.profile).items() if k != "policy"}),
            "plan": [{"scenario_id": pc.scenario_id, "persona_id": pc.persona_id, "repeat": pc.repeat} for pc in plan],
        }
        if self.mode == "voice":
            info["speech"] = {"engine": cfg.speech.engine, "ears_model": cfg.speech.ears_model,
                              "check_model": cfg.speech.check_model}
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
        write_json(run_dir / "decision.json", decision)
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

        voices = set()
        for pc in plan:
            persona = self.project.personas.get(pc.persona_id or "", {})
            voices.add(persona.get("speech", {}).get("tts_voice", self.config.speech.caller_voice))
        try:
            tts, stt = make_tts(self.config.speech, self.config.root), make_stt(self.config.speech, self.config.root)
            await asyncio.to_thread(lambda: ([tts.warm_up(v) for v in sorted(voices)], stt.warm_up()))
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
            try:
                load_fixtures(self.profile, scenario, self.config.root)
            except ProbeSetupError as e:
                raise HarnessError("fixtures_failed", str(e)) from e
            proxy = InProcessToolProxy(self.profile, scenario.get("setup", {}).get("faults", []),
                                       real_delays=self.mode == "voice")
            await proxy.attach(ctx, writer)
            if self._gateway:
                self._gateway.attach(proxy)
            logs = LogCollector(self.profile, self.config.root)
            logs.begin()
            writer.emit("connector", "call_started", {"test_id": ctx.test_id, "mode": self.mode})
            caller = self._caller(scenario, meta)
            if self.mode == "voice":
                ended_by, reason, turn = await self._converse_voice(ctx, scenario, caller, proxy, writer, live)
            else:
                ended_by, reason, turn = await self._converse_text(ctx, scenario, caller, proxy, writer, live)
            writer.emit("connector", "call_ended", {"ended_by": ended_by, "reason": reason,
                                                    **live.pop("recording", {})}, turn=turn)
            ended = True
            if self._gateway:
                self._gateway.detach()
            if self.mode == "voice":
                await self._listen_again(scenario, writer, live)
            if logs.enabled:
                await self._collect_logs(logs, ctx, writer)

            if scenario["style"] == "simulated" and not self.config.offline:
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
                self._gateway.detach()

        writer.lock(status, cost_usd=self._spent() - cost_before)
        trace = self.store.read_call(run_id, ctx.test_id)
        judge_before = self._spent()
        judged, notes = await self._judge(trace, scenario, status, meta)
        verdict = self.evaluate(trace, scenario, validator_off_script, judged)
        if notes:
            verdict["judge_notes"] = notes
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
        live.pop("session", None)
        return ended_by, reason, turn

    async def _converse_voice(self, ctx, scenario, caller, proxy, writer, live) -> tuple[str, str, int]:
        from .audio.noise import load_noise
        from .audio.recorder import Line, Recorder
        from .audio.tap import LiveTap
        from .speech import make_stt, make_tts
        from .voice_call import VoiceConversation, VoiceSettings

        connector = build_connector(self.profile, self.config.root, proxy)
        sr = connector.sample_rate
        rec = Recorder(sr, writer.elapsed)
        limits = self.profile.get("limits", {})
        audio = scenario.get("audio", {})
        persona = self.project.personas.get(scenario.get("caller", {}).get("persona", ""), {})
        voice_cfg = persona.get("speech", {})
        caller_voice = voice_cfg.get("tts_voice", self.config.speech.caller_voice)
        try:
            tts, stt = make_tts(self.config.speech, self.config.root), make_stt(self.config.speech, self.config.root)
            # Models load (and download, the first time) here, before the agent is dialled.
            await asyncio.to_thread(lambda: (tts.warm_up(caller_voice), stt.warm_up()))
            noise = load_noise(audio, limits.get("max_call_seconds", 300) + 60, sr, ctx.test_id, self.config.root)
        except Exception as e:
            raise HarnessError("speech_setup_failed", f"{type(e).__name__}: {e}{blocked_hint(e)}") from e
        try:
            session = await connector.open(ctx, rec)
        except ConnectError as e:
            raise HarnessError("connect_failed", str(e)) from e
        answered = rec.now_sample()   # the agent can only be heard from here on
        line = Line(rec, session.send_frame, noise)
        live.update(session=session, line=line, recorder=rec)
        line.start()
        live["tap"] = LiveTap.from_env(rec, f"{ctx.test_id} {scenario['id']}")   # dashboard's "Listen live"

        behaviour = persona.get("behaviour", {})
        settings = VoiceSettings(
            end_of_turn_ms=limits.get("end_of_turn_ms", 1000),
            no_reply_ms=behaviour.get("hangs_up_after_silence_ms", limits.get("no_reply_ms", 15000)),
            barge_in=list(audio.get("barge_in", [])))
        conv = VoiceConversation(
            session=session, recorder=rec, line=line, tts=tts, stt=stt,
            voice=caller_voice,
            speaking_rate=voice_cfg.get("speaking_rate", 1.0), settings=settings, writer=writer, proxy=proxy)

        heard = await conv.listen(since=answered, caller_end=None, turn=1)
        conv.emit_agent_turn(heard, 1, None)
        turn, caller_turns, silent = 1, 0, 0
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
            if not action.say:
                ended_by = "caller"
                reason = "the script ended" if scenario["style"] == "scripted" else "the caller hung up"
                break
            turn += 1
            caller_turns += 1
            start, end = await conv.say(action.say, turn)
            if action.hang_up:
                await line.wait_until(end + sr // 5)
                ended_by, reason = "caller", "the caller hung up"
                break
            heard = await conv.listen(since=start, caller_end=end, turn=turn)
            conv.emit_agent_turn(heard, turn, end)
            if heard.interrupted:
                b_start, b_end, words = heard.barge
                turn += 1
                caller_turns += 1
                conv.emit_caller_turn(words, b_start, b_end, turn, barge_in=True)
                note = getattr(caller, "note_barge_in", None)
                if note:
                    note(heard.text, words)
                heard = await conv.listen(since=heard.end + 1, caller_end=b_end, turn=turn)
                conv.emit_agent_turn(heard, turn, b_end)
            silent = silent + 1 if heard.start is None else 0
        await self._hang_up(live, writer)
        return ended_by, reason, turn

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
            stt = make_stt(self.config.speech, self.config.root, careful=True)
            for e in turns:
                a = max(0, e.data["audio"]["start_ms"] * sr // 1000 - sr // 20)
                b = e.data["audio"]["end_ms"] * sr // 1000 + sr // 20
                clip = AudioClip(to_pcm16(agent[a:b]), sr)
                text = (await asyncio.to_thread(stt.transcribe, clip)).text.strip()
                writer.emit("recorder", "stt_text", {"agent_turn": e.seq, "text": text,
                                                     "model": self.config.speech.check_model
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
        for line in result.lines:
            writer.emit("platform", "platform_event", {"kind": "log", "text": line})

    def _caller(self, scenario: Mapping[str, Any], meta: Mapping[str, str]):
        if scenario["style"] == "scripted":
            return ScriptedCaller([t["caller"] for t in scenario["turns"]])
        if self.config.offline:
            return RuleCaller(scenario, spoken=self.mode == "voice")
        if self.llm is None:
            raise HarnessError("no_llm", "simulated scenarios need an LLM (set ANTHROPIC_API_KEY or use --offline)")
        persona = self.project.personas[scenario["caller"]["persona"]]
        return LLMCaller(scenario, persona, self.llm, model=self.config.caller_model,
                         agent_name=self.profile.get("name", "the agent"), metadata=meta,
                         voice=self.mode == "voice")

    def _probes_needed(self, scenario: Mapping[str, Any]) -> list[str]:
        if self.profile.get("backend", {}).get("access") not in ("sql", "rest"):
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
                 validator_off_script: bool | None, judged: Sequence[CheckResult] = ()) -> dict[str, Any]:
        facts = scenario.get("caller", {}).get("facts", {})
        salt = trace.record["test_id"]
        events = list(trace.events)
        results: list[CheckResult] = scenario_checks(scenario, events, facts, salt, self.mode)
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
        return LocalVerdictBuilder().build(trace, merge_results(results, judged), block)


def _error_code(e: Exception) -> str:
    name = type(e).__name__
    if name == "BudgetExceeded":
        return "budget_exceeded"
    if name in ("LLMError", "ValueError"):
        return "llm_error"
    return "harness_crash"
