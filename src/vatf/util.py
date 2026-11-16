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


def write_text_atomic(path: Path, text: str, newline: str | None = None) -> None:
    """Writes the whole file or nothing: a reader (the dashboard polling a run, another process) never sees
    half a file. The text goes to a unique temporary file next to it, then replaces it in one step."""
    import os
    import tempfile
    import time

    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline=newline) as f:
            f.write(text)
        for attempt in range(20):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                # Windows: the target is open in another process (a reader) for a moment; try again.
                if attempt == 19:
                    raise
                time.sleep(0.02 * (attempt + 1))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_json(path: Path, data: Any) -> None:
    write_text_atomic(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


class file_lock:
    """A lock shared by every process on this computer (a lock file next to `path`), for read-modify-write
    of a shared file such as an index or the .env file. Also a lock between threads of one process."""

    _thread_locks: dict[str, Any] = {}

    def __init__(self, path: Path, timeout: float = 30.0):
        import os
        import threading

        self.lock_path = Path(str(path) + ".lock")
        self.timeout = timeout
        key = os.path.normcase(os.path.abspath(self.lock_path))
        self._tlock = file_lock._thread_locks.setdefault(key, threading.RLock())
        self._fh = None

    def __enter__(self) -> "file_lock":
        import os
        import time

        if not self._tlock.acquire(timeout=self.timeout):
            raise TimeoutError(f"{self.lock_path.name} is busy")
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(self.lock_path, "a+b")
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt

                        self._fh.seek(0)
                        msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return self
                except OSError:
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"{self.lock_path.name} is held by another process") from None
                    time.sleep(0.02)
        except BaseException:
            if self._fh:
                self._fh.close()
                self._fh = None
            self._tlock.release()
            raise

    def __exit__(self, *exc: Any) -> None:
        import os

        try:
            if self._fh:
                if os.name == "nt":
                    import msvcrt

                    self._fh.seek(0)
                    try:
                        msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
                else:
                    import fcntl

                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
                self._fh.close()
                self._fh = None
        finally:
            self._tlock.release()


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
