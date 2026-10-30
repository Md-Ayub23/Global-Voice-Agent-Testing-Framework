"""Command line: vatf validate | run | exit-check | calibrate | ui.

Exit codes for CI: 0 GO, 1 NO_GO, 2 NO_GO_INSUFFICIENT_EVIDENCE, 3 the framework itself failed.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
import traceback
from dataclasses import replace
from pathlib import Path

from .agent_process import AgentProcess, AgentProcessError
from .connectors import is_voice
from .llm import DEFAULT_MODEL, Budget, ClaudeClient
from .loader import ProjectError, load_project
from .orchestrator import Orchestrator, RunConfig
from .speech import ENGINES, SpeechConfig
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
                     judges=not args.no_judges, safety=args.safety)


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
    try:
        decision = _run(project, _config(args), llm, args.planted_bug, args.start_agent)
    except AgentProcessError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    print()
    if args.quiet:
        print(f"DECISION: {decision['decision']}")
        for r in decision["reasons"]:
            print(f"  - {r['message']}")
    else:
        # Every call in plain English (conversation, tool calls, database, judgement), then the decision.
        print(Path(decision["_transcript"]).read_text(encoding="utf-8"))
    if llm is not None:
        print(f"framework LLM cost (caller, validator and judges): ${llm.spent_usd:.4f}")
    print(f"report: {decision['_report']}")
    print(f"transcript: {decision['_transcript']}")
    return decision["exit_code"]


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
                             ("exit-check", cmd_exit_check, "exit check: clean agent + each bug switch")]:
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
        p.add_argument("--show-pii", action="store_true",
                       help="keep names, dates of birth and ids readable in the saved run (for checking; don't share)")
        if name == "run":
            p.add_argument("--quiet", action="store_true",
                           help="don't print each call's conversation (it is still saved in transcript.txt)")
            p.add_argument("--start-agent", action="store_true",
                           help="start the agent first and stop it after the run (the profile's agent_process)")
            p.add_argument("--planted-bug", metavar="ID",
                           help="with --start-agent: start it with one of the profile's planted_bugs switched on")
        else:
            p.add_argument("--summary", metavar="FILE", help="also write the exit check's result table as JSON")
        p.set_defaults(func=func)

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
