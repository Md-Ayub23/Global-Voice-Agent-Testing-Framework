"""`python -m vatf ...`: the same as the `vatf` command.

For machines where Windows App Control (Smart App Control / WDAC) blocks the small vatf.exe launcher
that uv creates in .venv\\Scripts: `uv run python -m vatf ui` runs the same code through python.exe.
"""

from vatf.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
