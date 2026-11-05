"""Command line: vatf validate | run | load | exit-check | health | baseline | compare | calibrate | plan | ui.

On a computer that blocks the vatf.exe launcher (Windows Application Control), use
`uv run python -m vatf <command>`: same program, no launcher.

Exit codes for CI: 0 GO, 1 NO_GO, 2 NO_GO_INSUFFICIENT_EVIDENCE, 3 the framework itself failed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import time
import traceback
from dataclasses import replace
from pathlib import Path

from .agent_process import AgentProcess, AgentProcessError
from .conditions import PRESETS
from .connectors import is_voice
from .llm import DEFAULT_MODEL, Budget, ClaudeClient
from .loader import ProjectError, load_project
from .orchestrator import Orchestrator, RunConfig
from .speech import ENGINES, SpeechConfig
from .alerts import send_alerts
from .attacks import is_safety
from .baseline import BaselineError, baseline_path, clear_baseline, load_baseline, run_snapshot, set_baseline
from .cireport import write_ci_reports
from .compare import compare
from .health import scenario_ids as health_scenarios, settings as health_settings
from .modes import CHOICES as MODE_CHOICES
from .util import read_json, write_json

# Which scenario each clinic bug switch must be caught by (the Phase 1 exit criterion).
EXIT_CHECK = {
    "lie_on_failure": "clinic.book-checkup.tool-timeout",
    "skip_confirmation": "clinic.confirm-before-booking",
    "double_booking": "clinic.book-checkup",
}
# Voice adds the speech-layer bug (Phase 2 exit criterion): only audibly wrong, so text mode can't see it.
EXIT_CHECK_VOICE = {**EXIT_CHECK, "misread_reference": "clinic.book-checkup"}
# Soft bugs only an AI judge can see (Phase 3 exit criterion): scenario and the judge item that must flag it.
EXIT_CHECK_JUDGES = {
    "rude_tone": ("clinic.book-checkup", "polite-professional-tone"),
    "made_up_facts": ("clinic.book-checkup", "grounded-facts"),
    "refuses_human": ("clinic.asks-for-person", "offers-human-when-asked"),
}
# Safety bugs (with --safety): the built-in attack call that must fail in every repeat.
EXIT_CHECK_SAFETY = {
    "answers_off_topic": "safety.off-topic.pythagoras",
    "leaks_secrets": "safety.secrets.persistent",
    "obeys_injection": "safety.injection.code-word",
}


def load_dotenv(path: Path) -> list[str]:
    """Reads KEY=VALUE lines from a .env file into the environment.

    Variables already set in the shell win over the file. Returns the names it set.
    """
    if not path.is_file():
        return []
    loaded = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


def _agent_options(pairs: list[str]) -> dict:
    options = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            raise SystemExit(f"--agent-opt needs key=value, got {pair!r}")
        options[key.strip()] = value.strip()
    return options


def _project(args):
    """The project with its agent options: offline_options first (with --offline), then --agent-opt."""
    project = load_project(args.project, getattr(args, "profile", "profile.yaml"))
    if args.offline:
        project = project.with_agent_options(project.profile["connection"].get("offline_options", {}))
    return project.with_agent_options(_agent_options(args.agent_opt or []))


def _config(args) -> RunConfig:
    return RunConfig(root=Path(args.root).resolve(), runs_dir=Path(args.runs_dir), offline=args.offline,
                     caller_model=args.model, validator_model=args.model, repeats=args.repeats,
                     scenario_ids=tuple(args.scenario or ()), mask_pii=not args.show_pii,
                     speech=SpeechConfig(engine=args.speech, ears_model=args.stt_model,
                                         check_model=args.stt_check_model),
                     judges=not args.no_judges, safety=args.safety,
                     conditions=getattr(args, "conditions", None) or "as_written",
                     # The exit check proves Vocto catches each bug on its own, so it never compares.
                     baseline=getattr(args, "baseline", None) or "none",
                     trigger="ci" if getattr(args, "ci", False) else "manual",
                     test_mode=getattr(args, "test_mode", None))


def _llm(args, project):
    if args.offline:
        return None
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        print("warning: ANTHROPIC_API_KEY is not set (in the shell or in a .env file); simulated calls will fail. "
              "Use --offline for a free dry run.")
    budget = Budget(project.profile.get("limits", {}).get("budget_usd", 5))
    return ClaudeClient(budget, cache_dir=Path(args.root) / args.runs_dir / ".llm-cache")


def cmd_validate(args) -> int:
    project = load_project(args.project, args.profile)
    print(f"OK: profile {project.profile['id']} ({args.profile}, {project.profile['connection']['type']}), "
          f"{len(project.personas)} persona(s), "
          f"{len(project.scenarios)} scenario(s)")
    return 0


def _run(project, config: RunConfig, llm, planted_bug: str | None = None, start_agent: bool = False):
    """One run; with start_agent, vatf starts the agent first (agent_process) and stops it after."""
    if planted_bug:
        project = project.with_agent_options({"bugs": planted_bug})   # recorded with the run, like a bug switch
    if not (start_agent or planted_bug):
        return asyncio.run(Orchestrator(project, config, llm=llm).run())
    agent = AgentProcess(project.profile, config.root, config.runs_dir, planted_bug or None)
    print(f"agent output: {agent.log_path}", flush=True)
    with agent:
        return asyncio.run(Orchestrator(project, config, llm=llm).run())


def cmd_run(args) -> int:
    project = _project(args)
    llm = _llm(args, project)
    if args.planted_bug and not args.start_agent:
        raise SystemExit("--planted-bug needs --start-agent: vatf restarts the agent with the bug planted")
    known = {b["id"] for b in project.profile.get("planted_bugs", [])}
    if args.planted_bug and args.planted_bug not in known:
        raise SystemExit(f"unknown planted bug {args.planted_bug!r}; the profile lists: {', '.join(sorted(known)) or 'none'}")
    config = _config(args)
    if config.test_mode:
        from . import modes
        if modes.mode(config.test_mode)["voice_only"] and not is_voice(project.profile):
            print(f"error: the {config.test_mode} tests need a voice connection; this profile is tested by text",
                  file=sys.stderr)
            return 3
        # A mode's own tests: the safety mode is the attack calls, so they are switched on with it.
        config = replace(config, safety=config.safety or config.test_mode == "safety")
        try:
            picked = Orchestrator(project, config).scenarios()
        except KeyError as e:
            print(f"error: {e.args[0]}", file=sys.stderr)
            return 3
        if not picked:
            print(f"error: nothing to run in the {config.test_mode} tests: {modes.why_empty(config.test_mode)}",
                  file=sys.stderr)
            return 3
    try:
        decision = _run(project, config, llm, args.planted_bug, args.start_agent)
    except AgentProcessError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    return _finish(args, project, decision, llm)


def cmd_load(args) -> int:
    """A load test: many test calls at once, in the profile's load shape (load.py), with a GO / NO-GO for that load."""
    from .load import LoadSettingsError, settings as load_settings, unreachable
    from .loadrun import LoadOrchestrator

    project = _project(args)
    try:
        s = load_settings(project.profile, shape=args.shape, calls_at_once=args.calls, minutes=args.minutes,
                          step_minutes=args.step_minutes, max_calls=args.max_calls, caller=args.caller,
                          scenarios=list(args.scenario) if args.scenario else None)
    except LoadSettingsError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    judges = bool(s["judges"]) and not args.no_judges
    needs_claude = not args.offline and (s["caller"] == "claude" or judges)
    llm = _llm(args, project) if needs_claude else None
    voice = is_voice(project.profile)
    config = replace(_config(args), judges=judges, repeats=None, scenario_ids=(),
                     conditions=(args.conditions if args.conditions != "as_written" else s["conditions"]) if voice else "as_written",
                     baseline=args.baseline or "auto")
    try:
        orch = LoadOrchestrator(project, config, s, llm=llm)
    except LoadSettingsError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    start_agent = args.start_agent or bool(s["start_agent"])

    def run_load():
        # One quick check that the agent is there, so a load test against nothing stops with one clear
        # message instead of hundreds of "couldn't connect" calls.
        missing = unreachable(project.profile)
        if missing:
            raise AgentProcessError(missing)
        return asyncio.run(orch.run())

    try:
        if start_agent:
            agent = AgentProcess(project.profile, config.root, config.runs_dir, None)
            print(f"agent output: {agent.log_path}", flush=True)
            with agent:
                decision = run_load()
        else:
            decision = run_load()
    except AgentProcessError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    return _finish(args, project, decision, llm)


def load_text(load: dict) -> str:
    """The load test's stages as a table for the terminal."""
    lines = [f"LOAD TEST: {load['shape']} up to {load['calls_at_once']} call(s) at once, {load['calls']} call(s), "
             f"capacity {load['capacity']} at once within the limits"]
    lines.append(f"  {'stage':<22} {'at once':>7} {'calls':>6} {'failed':>7} {'p50 ms':>7} {'p95 ms':>7} {'pass':>6}  result")
    for st in load["stages"]:
        res = "met" if st["meets_limits"] else ("not judged" if not st["trusted"] else "BROKE A LIMIT")
        fmt = lambda v, unit="": "-" if v is None else f"{v:g}{unit}"  # noqa: E731
        lines.append(f"  {st['name']:<22} {st['level']:>7} {st['finished']:>6} {fmt(st['failed_pct'], '%'):>7} "
                     f"{fmt(st['p50_ms']):>7} {fmt(st['p95_ms']):>7} {fmt(st['pass_rate'], '%'):>6}  {res}")
    return "\n".join(lines)


def _finish(args, project, decision, llm) -> int:
    """After a run: the decision, the comparison with the baseline, CI reports and alerts."""
    print()
    if decision.get("load"):
        print(load_text(decision["load"]))
        print(f"\nDECISION: {decision['decision']}")
        for r in decision["reasons"]:
            print(f"  - {r['message']}")
    elif args.quiet:
        print(f"DECISION: {decision['decision']}")
        for r in decision["reasons"]:
            print(f"  - {r['message']}")
    else:
        # Every call in plain English (conversation, tool calls, database, judgement), then the decision.
        print(Path(decision["_transcript"]).read_text(encoding="utf-8"))
    if llm is not None:
        print(f"framework LLM cost (caller, validator and judges): ${llm.spent_usd:.4f}")
    comparison = decision.get("_comparison")
    if comparison:
        print(compare_text(comparison))
    run_dir = Path(args.root) / args.runs_dir / decision["run_id"]
    info = read_json(run_dir / "run.json")
    try:  # the release readiness in plain words (DECISION above keeps the code CI scripts read)
        from .readiness import build as readiness_report
        v = readiness_report(run_dir)["verdict"]
        print(f"RESULT: {v['headline']}. {v['line']}")
    except Exception:  # noqa: BLE001 - the plain words never stop a run from finishing
        pass
    for path in write_ci_reports(decision, decision.get("_verdicts", []), info, comparison,
                                 getattr(args, "junit", None), getattr(args, "summary", None)):
        print(f"CI report: {path}")
    for a in send_alerts(project.profile, decision, info, Path(args.root) / args.runs_dir):
        print(f"alert {a['name']}: " + ("sent" if a["sent"] else f"NOT sent ({a.get('error') or a.get('status')})"))
    print(f"report: {decision['_report']}")
    print(f"transcript: {decision['_transcript']}")
    return decision["exit_code"]


def compare_text(comparison) -> str:
    """The comparison with the baseline (or another run) in plain lines, worst first."""
    b, c = comparison["baseline"], comparison["counts"]
    lines = [f"VS BASELINE {b['run_id']} ({b.get('decision')}): {c['regression']} new failure(s), {c['worse']} worse, "
             f"{c['better']} better, {c['new']} new, {c['removed']} not run"]
    if not comparison["like_for_like"]:
        lines.append("  not like-for-like (" + ", ".join(f"{d['title']} {d['before']} -> {d['after']}"
                                                      for d in comparison["differences"]) + "): nothing here blocks")
    for ch in comparison["changes"]:
        if ch["direction"] == "same":
            continue
        tag = "BLOCKS" if ch["blocks"] else ch["direction"]
        lines.append(f"  [{tag}] {ch['message']}")
    return "\n".join(lines)


def cmd_health(args) -> int:
    """One health check now: the profile's health_checks tests, 1 repeat, judges off unless switched on."""
    project = _project(args)
    hc = health_settings(project.profile)
    mode = "voice" if is_voice(project.profile) else "text"
    in_mode = [s for s in project.scenarios if mode in s.get("modes", ["text", "voice"]) and not is_safety(s)]
    ids = health_scenarios(project.profile, in_mode)
    if not ids:
        raise SystemExit("no tests to run in this profile's mode")
    llm = _llm(args, project)
    config = replace(_config(args), scenario_ids=tuple(args.scenario or ids), repeats=args.repeats or hc["repeats"],
                     judges=bool(hc["judges"]) and not args.no_judges,
                     conditions=("as_written" if mode != "voice" else
                                 args.conditions if args.conditions != "as_written" else hc["conditions"]),
                     trigger="health")
    print(f"health check: {len(config.scenario_ids)} test(s), {config.repeats} repeat(s)")
    try:
        decision = _run(project, config, llm, None, args.start_agent)
    except AgentProcessError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    return _finish(args, project, decision, llm)


def cmd_baseline(args) -> int:
    runs = Path(args.root) / args.runs_dir
    if args.action == "set":
        run_dir = runs / args.target
        if not (run_dir / "run.json").is_file():
            raise SystemExit(f"no run {args.target} in {runs}")
        info = read_json(run_dir / "run.json")
        project = load_project(Path(args.root) / info["project"], info.get("profile_file", "profile.yaml"))
        titles = {sc["id"]: sc.get("title", sc["id"]) for sc in project.scenarios}
        try:
            doc = set_baseline(project.folder, run_dir, titles, note=args.note or "")
        except BaselineError as e:
            print(f"error: {e}", file=sys.stderr)
            return 3
        kind = "load" if (doc.get("how") or {}).get("kind") == "load" else "run"
        print(f"{'load ' if kind == 'load' else ''}baseline for {info.get('profile_name') or doc['profile_id']} "
              f"({doc['profile_file']}) is now run {doc['run_id']} ({doc['decision']}): "
              f"{baseline_path(project.folder, doc['profile_file'], kind)}")
        for w in doc["warnings"]:
            print(f"warning: {w}")
        return 0
    folder = Path(args.target)
    kind = "load" if args.load else "run"
    if args.action == "clear":
        print("baseline removed" if clear_baseline(folder, args.profile, kind) else "there was no baseline")
        return 0
    try:
        doc = load_baseline(folder, args.profile, kind)
    except BaselineError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    if not doc:
        print(f"no baseline for {folder} ({args.profile}); set one with: python -m vatf baseline set <run_id>")
        return 0
    print(f"baseline: run {doc['run_id']} ({doc['decision']}), set {doc.get('set_at')}, "
          f"{len(doc['tests'])} test(s), file {baseline_path(folder, args.profile, kind)}")
    return 0


def cmd_compare(args) -> int:
    runs = Path(args.root) / args.runs_dir
    try:
        a, b = run_snapshot(runs / args.run_a), run_snapshot(runs / args.run_b)
    except (BaselineError, OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    info = read_json(runs / args.run_b / "run.json")
    try:
        profile = load_project(Path(args.root) / info["project"], info.get("profile_file", "profile.yaml")).profile
    except (ProjectError, KeyError, OSError):
        profile = {}
    comparison = compare(a, b, profile, args.run_a)
    if args.json:
        print(json.dumps(comparison, indent=2))
    else:
        print(compare_text(comparison))
    return 1 if comparison["blocking"] else 0


def _judge_results(config: RunConfig, decision, scenario_id: str, item: str) -> list[str]:
    """The judge item's result in each final call of one scenario of a run."""
    out = []
    for c in decision["calls"]:
        if c["scenario_id"] != scenario_id:
            continue
        v = read_json(config.root / config.runs_dir / decision["run_id"] / "calls" / c["test_id"] / "verdict.json")
        out += [x["result"] for x in v["checks"] if x["check_id"] == item and x["origin"] == "judge"]
    return out


def cmd_exit_check(args) -> int:
    """Runs the clean agent and each bug switch, and checks the exit criteria: every bug caught in every
    repeat, and no false alarms on the clean agent. With the AI judges on (a live run), the soft bugs
    must be flagged by their judge item in every repeat (FAIL, or NEEDS_REVIEW: sent to a person).
    A profile with planted_bugs (a real agent vatf starts itself) uses those instead of the zoo's switches."""
    base = _project(args)
    if base.profile.get("planted_bugs"):
        return _planted_exit_check(args, base)
    config = _config(args)
    llm = _llm(args, base)
    rows, all_ok, summary = [], True, []
    voice = is_voice(base.profile)
    expected = dict(EXIT_CHECK_VOICE if voice else EXIT_CHECK)
    judged = {} if args.offline or args.no_judges or not base.profile.get("judges", {}).get("enabled", True) \
        else {b: v for b, v in EXIT_CHECK_JUDGES.items() if b in _bug_names(base, config.root)}
    expected.update({b: sc for b, (sc, _) in judged.items()})
    ids = {s["id"] for s in Orchestrator(base, config).scenarios()}
    safety = {b: sc for b, sc in EXIT_CHECK_SAFETY.items() if args.safety and b in _bug_names(base, config.root) and sc in ids}
    expected.update(safety)
    planned = ["clean", *expected]
    if args.summary:  # the dashboard shows the steps before the first one finishes
        write_json(Path(args.summary), {"kind": "exit_check", "mode": "voice" if voice else "text",
                                        "passed": False, "finished": False, "rows": [], "planned": planned})
    for bug in [None, *expected]:
        label = bug or "clean"
        print(f"\n=== {label} ===")
        project = base.with_agent_options({"bugs": bug or ""})
        decision = asyncio.run(Orchestrator(project, config, llm=llm).run())
        rel = {r["scenario_id"]: r for r in decision["reliability"]}
        if bug is None:
            failed = [r["scenario_id"] for r in decision["reliability"] if r["failed"]]
            false_alarms = [f"{item} ({r})" for sc, item in set(judged.values())
                            for r in _judge_results(config, decision, sc, item) if r in ("FAIL", "NEEDS_REVIEW")]
            false_alarms += [f"safety test {sc}" for sc in failed if sc.startswith("safety.")]
            ok = decision["decision"] == "GO" and not false_alarms
            detail = "GO, no false alarms" if ok else (
                f"{decision['decision']}; failing: {', '.join(failed) or '-'}"
                + (f"; judges flagged: {', '.join(false_alarms)}" if false_alarms else ""))
        elif bug in judged:
            sc, item = judged[bug]
            results = _judge_results(config, decision, sc, item)
            flagged = [r for r in results if r in ("FAIL", "NEEDS_REVIEW")]
            ok = bool(results) and len(flagged) == len(results)
            detail = (f"judge item {item} flagged {len(flagged)} of {len(results)} repeats of {sc} "
                      f"({', '.join(results) or 'not judged'}); {decision['decision']}")
        else:
            r = rel.get(expected[bug])
            # A safety bug fails its attack call every time; only a blocker one also means NO-GO.
            ok = bool(r) and r["failed"] == r["k"] and (bug in safety or decision["decision"] == "NO_GO")
            detail = (f"caught in {r['failed']} of {r['k']} repeats of {expected[bug]}; {decision['decision']}"
                      if r else f"scenario {expected[bug]} was not run")
        all_ok &= ok
        rows.append((label, "OK  " if ok else "MISS", detail, decision["_transcript"]))
        summary.append({"label": label, "bug": bug, "scenario_id": expected.get(bug) if bug else None,
                        "ok": ok, "detail": detail, "run_id": decision["run_id"],
                        "decision": decision["decision"]})
        if args.summary:  # rewritten after every run, so a stopped exit check still shows what finished
            write_json(Path(args.summary), {"kind": "exit_check", "mode": "voice" if voice else "text",
                                            "passed": all_ok, "finished": False, "rows": summary, "planned": planned})
    print(f"\n{'Phase 2 (voice)' if voice else 'Phase 1 (text)'} exit check" + (" + Phase 3 (judges)" if judged else "")
          + (" + safety tests" if safety else ""))
    for label, mark, detail, report in rows:
        print(f"  [{mark}] {label:<18} {detail}")
        print(f"         {report}")
    print("\nRESULT:", "PASSED" if all_ok else "NOT PASSED")
    if args.summary:
        write_json(Path(args.summary), {"kind": "exit_check", "mode": "voice" if voice else "text",
                                        "passed": all_ok, "finished": True, "rows": summary, "planned": planned})
    return 0 if all_ok else 1


def _planted_exit_check(args, base) -> int:
    """The exit check for a real agent: vatf restarts it clean and then once with each planted bug.

    Clean: every scenario (and the safety tests with --safety) must give GO, and the judge items that
    planted bugs rely on must not flag the clean agent. Each bug: only the scenario that should catch it
    runs, and it must fail in every repeat (for a judge bug: the judge item flags every repeat)."""
    config = _config(args)
    llm = _llm(args, base)
    voice = is_voice(base.profile)
    judges_on = not (args.offline or args.no_judges) and base.profile.get("judges", {}).get("enabled", True)
    ids = {s["id"] for s in Orchestrator(base, replace(config, safety=True)).scenarios()}
    bugs, skipped = [], []
    for bug in base.profile["planted_bugs"]:
        caught = bug["caught_by"]
        if caught["scenario"] not in ids:
            skipped.append((bug["id"], f"its scenario {caught['scenario']} does not run in this profile"))
        elif caught.get("judge") and not judges_on:
            skipped.append((bug["id"], "only an AI judge can see it: turn the judges on (not a free dry run)"))
        elif caught["scenario"].startswith("safety.") and not args.safety:
            skipped.append((bug["id"], "it is caught by a safety test: add --safety"))
        else:
            bugs.append(bug)
    judged = {(b["caught_by"]["scenario"], b["caught_by"]["judge"]) for b in bugs if b["caught_by"].get("judge")}
    rows, all_ok, summary = [], True, []
    planned = ["clean", *(b["id"] for b in bugs)]
    if args.summary:
        write_json(Path(args.summary), {"kind": "exit_check", "mode": "voice" if voice else "text", "planted": True,
                                        "passed": False, "finished": False, "rows": [], "planned": planned,
                                        "skipped": [{"bug": b, "why": w} for b, w in skipped]})
    for bug in [None, *bugs]:
        label = bug["id"] if bug else "clean"
        print(f"\n=== {label} ===", flush=True)
        run_config = config if bug is None else replace(config, scenario_ids=(bug["caught_by"]["scenario"],), safety=False)
        try:
            decision = _run(base, run_config, llm, planted_bug=bug["id"] if bug else None, start_agent=True)
        except AgentProcessError as e:
            print(f"error: {e}", file=sys.stderr)
            if args.summary:
                write_json(Path(args.summary), {"kind": "exit_check", "mode": "voice" if voice else "text",
                                                "passed": False, "finished": True, "rows": summary, "planned": planned,
                                                "error": str(e)})
            return 3
        rel = {r["scenario_id"]: r for r in decision["reliability"]}
        if bug is None:
            failed = [r["scenario_id"] for r in decision["reliability"] if r["failed"]]
            false_alarms = [f"{item} ({r})" for sc, item in judged
                            for r in _judge_results(config, decision, sc, item) if r in ("FAIL", "NEEDS_REVIEW")]
            ok = decision["decision"] == "GO" and not false_alarms
            detail = "GO, no false alarms" if ok else (
                f"{decision['decision']}; failing: {', '.join(failed) or '-'}"
                + (f"; judges flagged: {', '.join(false_alarms)}" if false_alarms else ""))
        elif bug["caught_by"].get("judge"):
            sc, item = bug["caught_by"]["scenario"], bug["caught_by"]["judge"]
            results = _judge_results(config, decision, sc, item)
            flagged = [r for r in results if r in ("FAIL", "NEEDS_REVIEW")]
            ok = bool(results) and len(flagged) == len(results)
            detail = (f"judge item {item} flagged {len(flagged)} of {len(results)} repeats of {sc} "
                      f"({', '.join(results) or 'not judged'}); {decision['decision']}")
        else:
            sc = bug["caught_by"]["scenario"]
            r = rel.get(sc)
            ok = bool(r) and r["failed"] == r["k"]
            detail = (f"caught in {r['failed']} of {r['k']} repeats of {sc}; {decision['decision']}"
                      if r else f"scenario {sc} was not run")
        all_ok &= ok
        rows.append((label, "OK  " if ok else "MISS", detail, decision["_transcript"]))
        summary.append({"label": label, "bug": bug["id"] if bug else None,
                        "title": bug.get("title") if bug else "The agent as released, nothing planted",
                        "scenario_id": bug["caught_by"]["scenario"] if bug else None,
                        "ok": ok, "detail": detail, "run_id": decision["run_id"], "decision": decision["decision"]})
        if args.summary:
            write_json(Path(args.summary), {"kind": "exit_check", "mode": "voice" if voice else "text", "planted": True,
                                            "passed": all_ok, "finished": False, "rows": summary, "planned": planned,
                                            "skipped": [{"bug": b, "why": w} for b, w in skipped]})
    print(f"\nExit check with planted bugs ({'voice' if voice else 'text'}): {base.profile['name']}")
    for label, mark, detail, report in rows:
        print(f"  [{mark}] {label:<22} {detail}")
        print(f"         {report}")
    for bug_id, why in skipped:
        print(f"  [SKIP] {bug_id:<22} {why}")
    print("\nRESULT:", "PASSED" if all_ok else "NOT PASSED")
    if args.summary:
        write_json(Path(args.summary), {"kind": "exit_check", "mode": "voice" if voice else "text", "planted": True,
                                        "passed": all_ok, "finished": True, "rows": summary, "planned": planned,
                                        "skipped": [{"bug": b, "why": w} for b, w in skipped]})
    return 0 if all_ok else 1


def _bug_names(project, root: Path) -> set[str]:
    """The bug switches the agent offers (zoo agents list them in BUG_HELP). The agent's module is
    found from the project root, the same way a run launches it."""
    import importlib

    conn = project.profile["connection"]
    target = conn.get("launch") or conn.get("target") or ""
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        return set(getattr(importlib.import_module(target.split(":")[0]), "BUGS", ()))
    except Exception:  # noqa: BLE001 - not an importable agent (an outside service): no switches known here
        return set()


def cmd_calibrate(args) -> int:
    """How well each judge item agrees with the labels a person gave (Cohen's kappa), and whether it may block."""
    from .calibration import calibration
    from .rubrics import items_for_call

    project = load_project(args.project, args.profile)
    items = {i.id: i for sc in project.scenarios for i in items_for_call(project.profile, sc)}
    status = calibration(project.folder, project.profile, list(items.values()))
    print(f"{'item':<30} {'labels':>6} {'kappa 1st':>9} {'kappa 2nd':>9}  status")
    for item_id, st in status.items():
        k1 = "-" if st["kappa_primary"] is None else f"{st['kappa_primary']:.2f}"
        k2 = "-" if st["kappa_secondary"] is None else f"{st['kappa_secondary']:.2f}"
        print(f"{item_id:<30} {st['labelled_calls']:>6} {k1:>9} {k2:>9}  "
              f"{'CAN BLOCK' if st['can_block'] else 'advisory'}: {st['status']}")
    print(f"\n{sum(s['can_block'] for s in status.values())} of {len(status)} item(s) calibrated. "
          "Label calls on the dashboard's Labels page (vatf ui).")
    return 0


def cmd_plan(args) -> int:
    """Writes a draft test plan from the agent's instructions; --add saves the drafts that passed the checks."""
    from . import planner

    project = load_project(args.project, args.profile)
    budget = Budget(project.profile.get("limits", {}).get("budget_usd", 5))
    llm = ClaudeClient(budget, effort="medium")
    try:
        plan = asyncio.run(planner.write_plan(project, llm, count=args.count, focus=args.focus or "", model=args.model,
                                              log_dir=project.folder / "plans" / "llm"))
    except planner.PlanError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    path = planner.save_plan(project.folder, plan)
    a = plan["analysis"]
    print(f"{a['summary']}\n{len(a['intents'])} job(s), {len(a['rules'])} rule(s), {len(a['unclear'])} open question(s).\n")
    for t in plan["tests"]:
        mark = "x" if t["selected"] else " "
        flag = " DUPLICATE of " + t["duplicate_of"] if t["duplicate_of"] else ""
        flag += " PROBLEMS: " + "; ".join(t["problems"]) if t["problems"] else ""
        rules = ",".join(r["id"] for r in t["rules"]) or "-"
        print(f"[{mark}] {planner.CATEGORY_TITLES[t['category']]:<24} {t['criticality']:<11} rules {rules:<8} {t['title']}{flag}")
    print(f"\nplan saved: {path}\nmodel cost: ${llm.spent_usd:.4f}")
    if args.add:
        out = planner.accept(project.folder, plan, [t["id"] for t in plan["tests"] if t["selected"]], profile_file=args.profile)
        print(f"added {len(out['added'])} test(s); personas added: {', '.join(out['personas']) or 'none'}")
    else:
        print("Review it in the dashboard (Set up > Test plan), or run again with --add to save the ticked ones.")
    return 0


def cmd_monitor(args) -> int:
    """Phase 8: real calls. import FILES, listen (webhook), judge (the waiting calls), check (alerts and
    retention, for a scheduler), status."""
    from .monitor.intake import IntakeError
    from .monitor.service import Monitor

    root = Path(args.root).resolve()
    m = Monitor(root, root / args.runs_dir, Path(args.project), args.profile)
    if args.action == "import":
        if not args.files:
            print("error: give the call files to import", file=sys.stderr)
            return 3
        added = dupes = 0
        for f in args.files:
            try:
                out = m.ingest(Path(f).read_bytes(), str(f), via="cli", check_alerts=False)
            except (OSError, IntakeError) as e:
                print(f"{f}: not imported: {e}")
                continue
            added += len(out["added"])
            dupes += len(out["duplicates"])
            print(f"{f}: {len(out['added'])} call(s) added, {len(out['duplicates'])} already imported"
                  + "".join(f"\n  problem: {p}" for p in out["problems"]))
        if not args.no_judges:
            j = m.judge_pending()
            print(f"AI judges: {len(j['judged'])} call(s) judged" + "".join(f"\n  {p}" for p in j.get("problems", [])))
        _print_alerts(m.check_alerts())
        print(f"{added} new real call(s). Personal data was removed before saving.")
        return 0
    if args.action == "judge":
        j = m.judge_pending(limit=args.limit)
        print(f"AI judges: {len(j['judged'])} call(s) judged" + "".join(f"\n  {p}" for p in j.get("problems", [])))
        _print_alerts(m.check_alerts())
        return 0
    if args.action == "check":
        out = m.maintain()
        if out["deleted_old"]:
            print(f"{out['deleted_old']} call(s) older than the retention period deleted")
        _print_alerts(out["alerts"])
        return 1 if out["alerts"]["active"] else 0
    if args.action == "listen":
        from .monitor.listener import Listener, token_of

        if not token_of(m):
            print("error: set monitor.intake.token in the profile (as ${NAME}) and the value in the .env file first",
                  file=sys.stderr)
            return 3
        lst = Listener(lambda key: m, args.host, args.port, judge=not args.no_judges, log=print).start()
        print(f"Receiving real calls for {m.agent} at http://{args.host}:{lst.port}/ingest  (Ctrl+C stops)")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            lst.stop()
        return 0
    o = m.overview()
    w, t = o["window"], o["totals"]
    print(f"{m.agent}: {t['calls']} real call(s) kept; last {m.settings['alerts']['window_hours']} h: {w['calls']} call(s), "
          f"pass rate {w['pass_rate'] if w['pass_rate'] is not None else '-'}%, flagged {w['flagged']}, "
          f"p90 {w['p90_ms'] if w['p90_ms'] is not None else '-'} ms, {o['pending']} waiting for the judges")
    _print_alerts({"active": o["alerts"]["active"], "raised": [], "cleared": [], "sent": []})
    return 1 if o["alerts"]["active"] else 0


def _print_alerts(a) -> None:
    if not a["active"]:
        print("Alerts: none")
    for k, v in a["active"].items():
        print(f"ALERT {v['title']}: {v['message']}")
    for r in a.get("sent", []):
        print(f"  sent to {r['name']}: {'ok' if r.get('sent') else r.get('error') or r.get('status')}")


def cmd_ui(args) -> int:
    from .web.server import serve

    return serve(Path(args.root).resolve(), Path(args.runs_dir), port=args.port, open_browser=not args.no_browser)


def main(argv: list[str] | None = None) -> int:
    if os.environ.pop("VATF_UI_CHILD", None):
        # Started by `vatf ui`: its Stop button sends Ctrl+C (Ctrl+Break on Windows). Make sure both
        # stop the run the way Ctrl+C in a terminal does, even from a shell that ignores Ctrl+C.
        signal.signal(signal.SIGINT, signal.default_int_handler)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, signal.default_int_handler)
    parser = argparse.ArgumentParser(prog="vatf", description="Voice Agent Testing Framework")
    sub = parser.add_subparsers(dest="command", required=True)

    def profile_arg(p):
        p.add_argument("--profile", default="profile.yaml",
                       help="profile file in the project folder (default profile.yaml; "
                            "the clinic's voice profile is profile-voice.yaml)")

    p = sub.add_parser("validate", help="check a project's files against the schemas")
    p.add_argument("project", help="project folder, e.g. examples/clinic")
    profile_arg(p)
    p.set_defaults(func=cmd_validate)

    for name, func, text in [("run", cmd_run, "run the scenarios and give a GO / NO-GO decision"),
                             ("exit-check", cmd_exit_check, "exit check: clean agent + each bug switch"),
                             ("health", cmd_health, "one health check now: the profile's health_checks tests")]:
        p = sub.add_parser(name, help=text)
        p.add_argument("project", help="project folder, e.g. examples/clinic")
        profile_arg(p)
        p.add_argument("--speech", default="local", choices=ENGINES,
                       help="voice mode: the caller's speech engines. local = Piper + faster-whisper (default); "
                            "codec = test tones, only for vatf's own tests")
        p.add_argument("--stt-model", default="base.en", help="voice: whisper model the caller hears with")
        p.add_argument("--stt-check-model", default="small.en",
                       help="voice: whisper model for the careful second listen behind read-back checks")
        p.add_argument("--scenario", action="append", help="run only this scenario id (repeatable)")
        p.add_argument("--repeats", type=int, help="override the profile's repeats")
        p.add_argument("--agent-opt", action="append", metavar="KEY=VALUE",
                       help="override connection.options, e.g. bugs=lie_on_failure or brain=rules")
        p.add_argument("--offline", action="store_true",
                       help="no API calls: rule-based caller and no validator (smoke test only)")
        p.add_argument("--model", default=DEFAULT_MODEL, help=f"caller and validator model (default {DEFAULT_MODEL})")
        p.add_argument("--root", default=".", help="project root that relative paths resolve from (default: here)")
        p.add_argument("--runs-dir", default="runs", help="where runs are written (default: runs)")
        p.add_argument("--no-judges", action="store_true",
                       help="don't run the AI judges (they score tone, made-up facts and the like; see the Judges page)")
        p.add_argument("--safety", action="store_true",
                       help="add the built-in safety tests: attack calls written for this agent (profile safety section)")
        if name == "run":
            p.add_argument("--test-mode", choices=list(MODE_CHOICES),
                           help="run one kind of testing: functional (your test cases), timing, speech (voice only), "
                                "safety (the attack calls), resilience (test cases that break a tool), regression "
                                "(everything, compared with the baseline) or full (test cases + safety)")
        p.add_argument("--conditions", choices=list(PRESETS), default="as_written",
                       help="voice: caller conditions laid over every test: phone (phone line, lost packets), "
                            "noisy (cafe noise) or hard (street noise, phone line, and callers who cut in)")
        p.add_argument("--show-pii", action="store_true",
                       help="keep names, dates of birth and ids readable in the saved run (for checking; don't share)")
        if name in ("run", "health"):
            p.add_argument("--quiet", action="store_true",
                           help="don't print each call's conversation (it is still saved in transcript.txt)")
            p.add_argument("--baseline", default="auto",
                           help="compare with: auto = the agent's saved baseline (default), none, or a baseline file")
            p.add_argument("--junit", metavar="FILE", help="also write a JUnit XML report (for CI)")
            p.add_argument("--summary", metavar="FILE",
                           help="also write a Markdown summary (on GitHub Actions it also goes to the job summary)")
            p.add_argument("--ci", action="store_true", help="mark the run as started by CI (shown in Results and alerts)")
            p.add_argument("--start-agent", action="store_true",
                           help="start the agent first and stop it after the run (the profile's agent_process)")
            p.add_argument("--planted-bug", metavar="ID",
                           help="with --start-agent: start it with one of the profile's planted_bugs switched on")
        elif name == "health":
            p.add_argument("--start-agent", action="store_true",
                           help="start the agent first and stop it after the check (the profile's agent_process)")
        else:
            p.add_argument("--summary", metavar="FILE", help="also write the exit check's result table as JSON")
        p.set_defaults(func=func)

    p = sub.add_parser("load", help="load test: many test calls at once (ramp, spike, soak or steady), GO / NO-GO for that load")
    p.add_argument("project", help="project folder, e.g. examples/clinic")
    profile_arg(p)
    p.add_argument("--shape", choices=["ramp", "spike", "soak", "steady"], help="override the profile's load shape")
    p.add_argument("--calls", type=int, help="calls at once at the top of the load (override load.calls_at_once)")
    p.add_argument("--minutes", type=float, help="steady / soak: how long to hold; spike: how long the spike lasts")
    p.add_argument("--step-minutes", type=float, help="ramp: how long each level runs")
    p.add_argument("--max-calls", type=int, help="never start more calls than this (override load.max_calls)")
    p.add_argument("--caller", choices=["free", "claude"], help="free: scripted lines and the rule-based caller; claude: Claude callers")
    p.add_argument("--scenario", action="append", help="use only this test (repeatable; overrides load.scenarios)")
    p.add_argument("--speech", default="local", choices=ENGINES, help="voice mode: the caller's speech engines")
    p.add_argument("--stt-model", default="base.en", help="voice: whisper model the caller hears with")
    p.add_argument("--stt-check-model", default="small.en", help="voice: whisper model for the careful second listen")
    p.add_argument("--agent-opt", action="append", metavar="KEY=VALUE", help="override connection.options")
    p.add_argument("--offline", action="store_true", help="no API calls: the agent's offline options and the rule-based caller")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"caller model with --caller claude (default {DEFAULT_MODEL})")
    p.add_argument("--root", default=".", help="project root that relative paths resolve from (default: here)")
    p.add_argument("--runs-dir", default="runs", help="where runs are written (default: runs)")
    p.add_argument("--no-judges", action="store_true", help="don't run the AI judges even if the load settings switch them on")
    p.add_argument("--conditions", choices=list(PRESETS), default="as_written", help="voice: caller conditions over every call")
    p.add_argument("--show-pii", action="store_true", help="keep names and dates readable in the saved run")
    p.add_argument("--quiet", action="store_true", help="(load runs never print every call; transcript.txt has them)")
    p.add_argument("--baseline", default="auto", help="compare with: auto = the agent's load baseline (default), none, or a baseline file")
    p.add_argument("--junit", metavar="FILE", help="also write a JUnit XML report (one case per stage)")
    p.add_argument("--summary", metavar="FILE", help="also write a Markdown summary")
    p.add_argument("--ci", action="store_true", help="mark the run as started by CI")
    p.add_argument("--start-agent", action="store_true", help="start the agent first and stop it after (agent_process)")
    p.set_defaults(func=cmd_load, repeats=None, safety=False)
    p = sub.add_parser("baseline", help="the agent's baseline: the known-good run every run is compared with")
    p.add_argument("action", choices=["set", "show", "clear"])
    p.add_argument("target", help="set: a run id (r-...); show and clear: the project folder")
    profile_arg(p)
    p.add_argument("--note", help="set: why this run is the baseline")
    p.add_argument("--load", action="store_true", help="show and clear: the load test's baseline (set picks it from the run)")
    p.add_argument("--root", default=".", help="project root (default: here)")
    p.add_argument("--runs-dir", default="runs", help="where runs are written (default: runs)")
    p.set_defaults(func=cmd_baseline)

    p = sub.add_parser("compare", help="what changed between two runs (worst first); exit code 1 if anything blocks")
    p.add_argument("run_a", help="the earlier run (r-...)")
    p.add_argument("run_b", help="the later run (r-...)")
    p.add_argument("--json", action="store_true", help="print the full comparison as JSON")
    p.add_argument("--root", default=".", help="project root (default: here)")
    p.add_argument("--runs-dir", default="runs", help="where runs are written (default: runs)")
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("calibrate", help="show how well each AI judge item agrees with your labels")
    p.add_argument("project", help="project folder, e.g. examples/clinic")
    profile_arg(p)
    p.set_defaults(func=cmd_calibrate)

    p = sub.add_parser("plan", help="write a draft test plan from the agent's instructions (needs Claude)")
    p.add_argument("project", help="project folder, e.g. examples/pipecat-food")
    profile_arg(p)
    p.add_argument("--count", type=int, default=20, help="how many tests to write (default 20, at most 60)")
    p.add_argument("--focus", help="what to concentrate on, in plain words")
    p.add_argument("--model", default="claude-opus-5-5", help="model that writes the plan (default claude-opus-5-5)")
    p.add_argument("--add", action="store_true", help="save the drafts that passed the checks as scenario files")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("monitor", help="real production calls: import, listen for them, judge, check alerts, status")
    p.add_argument("action", choices=["import", "listen", "judge", "check", "status"])
    p.add_argument("project", help="project folder, e.g. examples/clinic")
    p.add_argument("files", nargs="*", help="import: call files (Vocto JSON, Vapi, Retell, transcripts; JSON lists or JSONL)")
    profile_arg(p)
    p.add_argument("--no-judges", action="store_true", help="import / listen: don't run the AI judges now")
    p.add_argument("--limit", type=int, help="judge: at most this many calls")
    p.add_argument("--host", default="127.0.0.1", help="listen: address to listen on (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=8791, help="listen: port (default 8791)")
    p.add_argument("--root", default=".", help="project root (default: here)")
    p.add_argument("--runs-dir", default="runs", help="where runs and real calls are kept (default: runs)")
    p.set_defaults(func=cmd_monitor)

    p = sub.add_parser("ui", help="open the vatf dashboard in your browser (set up, run and read tests)")
    p.add_argument("--port", type=int, default=8765, help="port on this computer (default 8765)")
    p.add_argument("--root", default=".", help="project root (default: here)")
    p.add_argument("--runs-dir", default="runs", help="where runs are written (default: runs)")
    p.add_argument("--no-browser", action="store_true", help="don't open a browser window")
    p.set_defaults(func=cmd_ui)

    args = parser.parse_args(argv)
    load_dotenv(Path(getattr(args, "root", ".")) / ".env")
    try:
        return args.func(args)
    except ProjectError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        return 3
    except Exception:
        traceback.print_exc()
        print("error: the framework itself failed (exit code 3)", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
