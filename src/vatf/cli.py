"""Command line: vatf validate | run | exit-check.

Exit codes for CI: 0 GO, 1 NO_GO, 2 NO_GO_INSUFFICIENT_EVIDENCE, 3 the framework itself failed.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import traceback
from pathlib import Path

from .llm import DEFAULT_MODEL, Budget, ClaudeClient
from .loader import ProjectError, load_project
from .orchestrator import Orchestrator, RunConfig

# Which scenario each clinic bug switch must be caught by (the Phase 1 exit criterion).
EXIT_CHECK = {
    "lie_on_failure": "clinic.book-checkup.tool-timeout",
    "skip_confirmation": "clinic.confirm-before-booking",
    "double_booking": "clinic.book-checkup",
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


def _config(args) -> RunConfig:
    return RunConfig(root=Path(args.root).resolve(), runs_dir=Path(args.runs_dir), offline=args.offline,
                     caller_model=args.model, validator_model=args.model, repeats=args.repeats,
                     scenario_ids=tuple(args.scenario or ()))


def _llm(args, project):
    if args.offline:
        return None
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        print("warning: ANTHROPIC_API_KEY is not set (in the shell or in a .env file); simulated calls will fail. "
              "Use --offline for a free dry run.")
    budget = Budget(project.profile.get("limits", {}).get("budget_usd", 5))
    return ClaudeClient(budget, cache_dir=Path(args.root) / args.runs_dir / ".llm-cache")


def cmd_validate(args) -> int:
    project = load_project(args.project)
    print(f"OK: profile {project.profile['id']}, {len(project.personas)} persona(s), "
          f"{len(project.scenarios)} scenario(s)")
    return 0


def cmd_run(args) -> int:
    project = load_project(args.project).with_agent_options(_agent_options(args.agent_opt or []))
    orch = Orchestrator(project, _config(args), llm=_llm(args, project))
    decision = asyncio.run(orch.run())
    print()
    print(f"DECISION: {decision['decision']}")
    for r in decision["reasons"]:
        print(f"  - {r['message']}")
    print(f"report: {decision['_report']}")
    return decision["exit_code"]


def cmd_exit_check(args) -> int:
    """Runs the clean agent and each bug switch, and checks the Phase 1 exit criterion:
    every bug caught in every repeat, and no false alarms on the clean agent."""
    base = load_project(args.project).with_agent_options(_agent_options(args.agent_opt or []))
    config = _config(args)
    llm = _llm(args, base)
    rows, all_ok = [], True
    for bug in [None, *EXIT_CHECK]:
        label = bug or "clean"
        print(f"\n=== {label} ===")
        project = base.with_agent_options({"bugs": bug or ""})
        decision = asyncio.run(Orchestrator(project, config, llm=llm).run())
        rel = {r["scenario_id"]: r for r in decision["reliability"]}
        if bug is None:
            failed = [r["scenario_id"] for r in decision["reliability"] if r["failed"]]
            ok = decision["decision"] == "GO"
            detail = "GO, no false alarms" if ok else f"{decision['decision']}; failing: {', '.join(failed) or '-'}"
        else:
            r = rel.get(EXIT_CHECK[bug])
            ok = bool(r) and r["failed"] == r["k"] and decision["decision"] == "NO_GO"
            detail = (f"caught in {r['failed']} of {r['k']} repeats of {EXIT_CHECK[bug]}; {decision['decision']}"
                      if r else f"scenario {EXIT_CHECK[bug]} was not run")
        all_ok &= ok
        rows.append((label, "OK  " if ok else "MISS", detail, decision["_report"]))
    print("\nPhase 1 exit check")
    for label, mark, detail, report in rows:
        print(f"  [{mark}] {label:<18} {detail}")
        print(f"         {report}")
    print("\nRESULT:", "PASSED" if all_ok else "NOT PASSED")
    return 0 if all_ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vatf", description="Voice Agent Testing Framework")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("validate", help="check a project's files against the schemas")
    p.add_argument("project", help="project folder, e.g. examples/clinic")
    p.set_defaults(func=cmd_validate)

    for name, func, text in [("run", cmd_run, "run the scenarios and give a GO / NO-GO decision"),
                             ("exit-check", cmd_exit_check, "Phase 1 exit check: clean agent + each bug switch")]:
        p = sub.add_parser(name, help=text)
        p.add_argument("project", help="project folder, e.g. examples/clinic")
        p.add_argument("--scenario", action="append", help="run only this scenario id (repeatable)")
        p.add_argument("--repeats", type=int, help="override the profile's repeats")
        p.add_argument("--agent-opt", action="append", metavar="KEY=VALUE",
                       help="override connection.options, e.g. bugs=lie_on_failure or brain=rules")
        p.add_argument("--offline", action="store_true",
                       help="no API calls: rule-based caller and no validator (smoke test only)")
        p.add_argument("--model", default=DEFAULT_MODEL, help=f"caller and validator model (default {DEFAULT_MODEL})")
        p.add_argument("--root", default=".", help="project root that relative paths resolve from (default: here)")
        p.add_argument("--runs-dir", default="runs", help="where runs are written (default: runs)")
        p.set_defaults(func=func)

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
