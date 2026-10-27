"""Small helpers shared by the evaluators: value matching and PII masking."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

MASK_PREFIX = "[masked:"


def norm(value: Any) -> str:
    """Loose comparison form: case, dots and extra spaces do not matter ("Dr. Patel" == "dr patel")."""
    return re.sub(r"\s+", " ", str(value).replace(".", " ")).strip().lower()


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
