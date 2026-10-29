"""Small helpers shared by the evaluators: value matching and PII masking."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

MASK_PREFIX = "[masked:"


TITLES = re.compile(r"^(dr|doctor|mr|mrs|ms|miss|mx|prof)\s+")


def norm(value: Any) -> str:
    """Loose comparison form: case, dots, extra spaces and a leading title do not matter
    ("Dr. Patel" == "dr patel" == "Patel")."""
    text = re.sub(r"\s+", " ", str(value).replace(".", " ")).strip().lower()
    return TITLES.sub("", text)


def mask_token(value: Any, salt: str) -> str:
    """Replace a PII value with a short salted hash.

    Two masked copies of the same value still compare equal, and an evaluator
    that knows the expected value (from the caller's fact sheet) can check it
    without the evidence ever storing the value itself.
    """
    digest = hashlib.sha256(f"{salt}|{norm(value)}".encode("utf-8")).hexdigest()[:12]
    return f"{MASK_PREFIX}{digest}]"


def same_value(observed: Any, expected: Any, salt: str) -> bool:
    if isinstance(observed, str) and observed.startswith(MASK_PREFIX):
        return observed == mask_token(expected, salt)
    return norm(observed) == norm(expected)


def resolve(value: Any, facts: Mapping[str, Any]) -> Any:
    """"$doctor" -> the caller fact named doctor. Anything else is returned unchanged."""
    if isinstance(value, str) and value.startswith("$"):
        name = value[1:]
        if name not in facts:
            raise KeyError(f"{value} is not a caller fact")
        return facts[name]
    return value


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class MissingSecret(Exception):
    """A ${NAME} in the profile has no value in the shell or the .env file."""


def expand_env(value: Any) -> Any:
    """"Bearer ${ACME_TOKEN}" -> the value from the environment (the .env file is read at start-up).
    Profiles hold secret names only, never the secrets. Works through dicts and lists."""
    import os

    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    if not isinstance(value, str):
        return value
    missing = [n for n in ENV_REF.findall(value) if n not in os.environ]
    if missing:
        raise MissingSecret(f"set {', '.join(missing)} in the .env file (or the shell): the profile uses "
                            + ", ".join(f"${{{n}}}" for n in missing))
    return ENV_REF.sub(lambda m: os.environ[m.group(1)], value)
