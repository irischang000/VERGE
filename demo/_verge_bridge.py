"""Shared infrastructure for VERGE demos that run a metalift synthesis
subprocess from inside LEVI's Python 3.11 environment.

Every demo under demo/ that scores candidates by actually invoking
metalift (as opposed to scoring in-process) needs the same two things:
Poetry's absolute path, and a subprocess environment that resolves the
whole local toolchain (Python 3.10, CMake, LLVM 15, Racket, cvc5,
bitwuzla) independent of whatever shell launched the demo -- LEVI's own
eval workers won't have sourced install_deps.sh's printed PATH exports.
"""

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
METALIFT_DIR = REPO_ROOT / "metalift"
# Absolute path, not just "poetry" -- this runs inside LEVI's own uv-managed
# subprocess/eval workers, whose PATH won't have install_deps.sh's toolchain
# exports unless the caller's shell happened to have sourced them too.
POETRY_BIN = REPO_ROOT / "deps" / "poetry" / "bin" / "poetry"
# Prefer this over `poetry run python` for the actual per-evaluation bridge
# call -- `poetry run` adds ~0.7s of wrapper overhead per invocation
# (measured), which is pure waste once the venv already exists and doesn't
# need re-resolving. Poetry itself is still needed for one-time setup
# (install_deps.sh's `poetry install`, `poetry env use`), just not for every
# single evaluation.
METALIFT_PYTHON = METALIFT_DIR / ".venv" / "bin" / "python3.10"


def _brew_prefix(formula: str) -> str | None:
    try:
        result = subprocess.run(
            ["brew", "--prefix", formula], capture_output=True, text=True, check=True
        )
        return result.stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None


def bridge_env() -> dict:
    """Build a subprocess environment with the full toolchain on PATH,
    independent of whatever shell launched this script -- LEVI's own eval
    workers won't have sourced install_deps.sh's printed PATH exports, and
    metalift itself shells out to bare `racket`/`cvc5` internally."""
    env = os.environ.copy()
    # This script runs inside `uv run`, which sets VIRTUAL_ENV to LEVI's own
    # Python 3.11 venv. Poetry respects that convention and would otherwise
    # treat it as "already active", ignoring metalift's pinned 3.10 .venv.
    env.pop("VIRTUAL_ENV", None)
    # Demos that stub out `metalift` for LEVI's own in-process exec() (see
    # grammar_evolution/run.py) set PYTHONPATH to make that stub importable.
    # That must never reach this subprocess -- it runs in metalift's real
    # Python 3.10 venv, where the stub would shadow the genuine package.
    env.pop("PYTHONPATH", None)
    path_parts = []
    for formula in ("python@3.10", "cmake", "llvm@15"):
        prefix = _brew_prefix(formula)
        if prefix:
            path_parts.append(f"{prefix}/bin")
    path_parts.append(str(REPO_ROOT / "deps" / "racket" / "bin"))
    path_parts.append(str(REPO_ROOT / "deps" / "cvc5" / "bin"))
    env["PATH"] = ":".join(path_parts) + ":" + env.get("PATH", "")
    env["BITWUZLA_PATH"] = str(REPO_ROOT / "deps" / "bitwuzla" / "bin" / "bitwuzla")

    # Poetry's own venv-selection state (from `poetry env use` in
    # install_deps.sh) lives under these redirected config dirs -- without
    # them Poetry falls back to its default ~/.config/pypoetry, doesn't find
    # the pinned Python 3.10 venv, and picks whatever python3 it finds on
    # PATH instead (breaking metalift's `python = ">=3.9,<3.11"` constraint).
    env["PYTHON_KEYRING_BACKEND"] = "keyring.backends.null.Keyring"
    env["POETRY_CACHE_DIR"] = str(REPO_ROOT / "deps" / "poetry-cache")
    env["POETRY_DATA_DIR"] = str(REPO_ROOT / "deps" / "poetry-data")
    env["POETRY_CONFIG_DIR"] = str(REPO_ROOT / "deps" / "poetry-config")
    return env
