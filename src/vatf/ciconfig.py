"""A ready CI file for one agent (Phase 6): GitHub Actions, GitLab CI or Jenkins.

Every file runs the same thing a person runs on their computer, through `uv run python -m vatf`
(never the vatf.exe launcher, which Windows Application Control can block), and:
  - fails the build on NO-GO (exit 1) and on not enough evidence (exit 2),
  - keeps the JUnit report, the Markdown summary and the run folder as build artifacts,
  - compares with the agent's committed baseline (baselines/<profile>.json) when there is one,
  - passes only the secrets the profile names as ${NAME}, plus the Claude key.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any, Mapping

from .util import ENV_REF

PROVIDERS = {"github": "GitHub Actions", "gitlab": "GitLab CI", "jenkins": "Jenkins"}


def secret_names(profile: Mapping[str, Any], offline: bool) -> list[str]:
    """${NAME} values the profile uses, and the Claude key unless the run is offline."""
    names: list[str] = [] if offline else ["ANTHROPIC_API_KEY"]

    def walk(v: Any) -> None:
        if isinstance(v, Mapping):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, str):
            for n in ENV_REF.findall(v):
                if n not in names:
                    names.append(n)
    walk(profile)
    return names


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "agent"


def command(project_path: str, profile_file: str, *, health: bool = False, offline: bool = False,
            start_agent: bool = False, conditions: str = "as_written", safety: bool = False) -> str:
    parts = ["uv run python -m vatf", "health" if health else "run", project_path]
    if profile_file != "profile.yaml":
        parts += ["--profile", profile_file]
    parts += ["--ci", "--quiet", "--junit reports/vocto.xml", "--summary reports/vocto.md"]
    if offline:
        parts.append("--offline")
    if start_agent:
        parts.append("--start-agent")
    if conditions != "as_written" and not health:
        parts += ["--conditions", conditions]
    if safety and not health:
        parts.append("--safety")
    return " ".join(parts)


def ci_file(provider: str, profile: Mapping[str, Any], project_path: str, profile_file: str, *,
            mode: str = "text", offline: bool = False, start_agent: bool = False, conditions: str = "as_written",
            safety: bool = False, branch: str = "main", schedule: bool = True) -> dict[str, str]:
    """{'path': where to save it in the repo, 'text': the file}."""
    if provider not in PROVIDERS:
        raise ValueError(f"unknown CI provider {provider!r} (use {', '.join(PROVIDERS)})")
    project_path = PurePosixPath(project_path).as_posix()
    name = profile.get("name") or profile.get("id", "agent")
    slug = _slug(profile.get("id") or name)
    secrets = secret_names(profile, offline)
    run = command(project_path, profile_file, offline=offline, start_agent=start_agent,
                  conditions=conditions if mode == "voice" else "as_written", safety=safety)
    health = command(project_path, profile_file, health=True, offline=offline, start_agent=start_agent)
    voice = mode == "voice"
    if provider == "github":
        env = "\n".join(f"          {n}: ${{{{ secrets.{n} }}}}" for n in secrets)
        lines = [
            f"# Vocto release check for {name}.",
            f"# Save as .github/workflows/vocto-{slug}.yml. Add these repository secrets first",
            "# (Settings > Secrets and variables > Actions): " + (", ".join(secrets) or "none needed") + ".",
            "# NO-GO (exit 1) or not enough evidence (exit 2) fails the job; the summary shows on the run's page.",
            f"name: Vocto - {name}",
            "on:",
            "  pull_request:",
            "  push:",
            f"    branches: [{branch}]",
        ]
        if schedule:
            lines += ["  schedule:", "    - cron: \"0 6 * * *\"   # health check every day at 06:00 UTC"]
        lines += [
            "  workflow_dispatch:",
            "jobs:",
            "  vocto:",
            "    runs-on: ubuntu-latest",
            "    timeout-minutes: 90",
            "    steps:",
            "      - uses: actions/checkout@v4",
            "      - uses: astral-sh/setup-uv@v6",
            "      - run: uv sync",
        ]
        if voice:
            lines += [
                "      - name: Speech models cache (Piper voices, Whisper)",
                "        uses: actions/cache@v4",
                "        with:",
                "          path: .models",
                "          key: vocto-speech-models-v1",
            ]
        cond = "github.event_name != 'schedule'"
        lines += [
            "      - name: Release check",
            *([f"        if: {cond}"] if schedule else []),
            *(["        env:", env] if secrets else []),
            f"        run: {run}",
        ]
        if schedule:
            lines += [
                "      - name: Health check",
                "        if: github.event_name == 'schedule'",
                *(["        env:", env] if secrets else []),
                f"        run: {health}",
            ]
        lines += [
            "      - uses: actions/upload-artifact@v4",
            "        if: always()",
            "        with:",
            f"          name: vocto-{slug}",
            "          path: |",
            "            reports/",
            "            runs/",
        ]
        return {"path": f".github/workflows/vocto-{slug}.yml", "text": "\n".join(lines) + "\n"}
    if provider == "gitlab":
        lines = [
            f"# Vocto release check for {name}. Add to .gitlab-ci.yml.",
            "# Add these as masked CI/CD variables (Settings > CI/CD > Variables): "
            + (", ".join(secrets) or "none needed") + ".",
            "# NO-GO (exit 1) or not enough evidence (exit 2) fails the job; GitLab shows the JUnit tests on the MR.",
            f"vocto-{slug}:",
            "  image: ghcr.io/astral-sh/uv:python3.12-bookworm",
            "  timeout: 90m",
        ]
        if voice:
            lines += ["  cache:", "    key: vocto-speech-models-v1", "    paths: [.models/]"]
        lines += [
            "  script:",
            "    - uv sync",
            "    - |",
            "      if [ \"$CI_PIPELINE_SOURCE\" = \"schedule\" ]; then",
            f"        {health}",
            "      else",
            f"        {run}",
            "      fi",
            "  artifacts:",
            "    when: always",
            "    paths: [reports/, runs/]",
            "    reports:",
            "      junit: reports/vocto.xml",
        ]
        return {"path": ".gitlab-ci.yml", "text": "\n".join(lines) + "\n"}
    creds = "\n".join(f"        {n} = credentials('{_slug(n)}')" for n in secrets)
    lines = [
        f"// Vocto release check for {name}. Save as Jenkinsfile (or add the stage to yours).",
        "// Add these as Secret text credentials (ids in lower case): " + (", ".join(secrets) or "none needed") + ".",
        "// NO-GO (exit 1) or not enough evidence (exit 2) fails the build. Needs uv on the agent machine.",
        "pipeline {",
        "    agent any",
        "    options { timeout(time: 90, unit: 'MINUTES') }",
        *([f"    triggers {{ cron('H 6 * * *') }}   // daily health check"] if schedule else []),
        *(["    environment {", creds, "    }"] if secrets else []),
        "    stages {",
        "        stage('Vocto') {",
        "            steps {",
        "                sh 'uv sync'",
    ]
    if schedule:
        lines += [
            "                script {",
            "                    def timer = currentBuild.getBuildCauses('hudson.triggers.TimerTrigger$TimerTriggerCause')",
            f"                    sh(timer ? '{health}' : '{run}')",
            "                }",
        ]
    else:
        lines.append(f"                sh '{run}'")
    lines += [
        "            }",
        "        }",
        "    }",
        "    post {",
        "        always {",
        "            junit allowEmptyResults: true, testResults: 'reports/vocto.xml'",
        "            archiveArtifacts artifacts: 'reports/**, runs/**', allowEmptyArchive: true",
        "        }",
        "    }",
        "}",
    ]
    return {"path": "Jenkinsfile", "text": "\n".join(lines) + "\n"}
