**V**erified, **E**fficient **R**efinement via **G**uided **E**volution — a framework that combines
[metalift](metalift/) (program synthesis / verified lifting) with
[LEVI](levi/) (LLM-guided evolution for code & prompts) to automatically
discover and verify high-level specifications for low-level kernels.

> **Key insight:** Writing the correct loop invariant grammar for metalift is the hard part.
> VERGE lets a user write a rough "weak seed" grammar and delegates the hard
> grammar-engineering to an LLM+verifier loop that iterates until synthesis succeeds.

```
verge/
├── metalift/                    # Program synthesis & verified lifting framework (git submodule)
├── levi/                        # LLM-guided evolutionary framework for code & prompts (git submodule)
│   └── examples/quickstart/     # Single-file starters: API key, Claude/Codex CLI, or prompt tuning
├── demo/                        # End-to-end demos (one per metalift tutorial) — not yet built
├── deps/                        # Local dependency installs (Racket, LLVM 15, bitwuzla, cvc5, Poetry, …)
├── install_deps.sh              # One-shot environment setup script
└── README.md                    # This file
```

> **Status:** `metalift` and `levi` are vendored as git submodules; the glue code that drives
> LEVI's evolution loop against metalift's verifier (the `demo/` folder and a VERGE-specific
> `evolve_code()` integration) hasn't been written yet. Until then, use each project's own
> examples directly — see [Try the projects directly](#try-the-projects-directly) below.

---

## Quick start

### 0. Clone with submodules

`metalift` and `levi` are git submodules, so clone (or update) accordingly:

```bash
git clone --recurse-submodules <this-repo-url>
# or, if you already cloned without that flag:
git submodule update --init --recursive
```

### 1. Install dependencies

```bash
bash install_deps.sh
```

Sets up Racket + Rosette, bitwuzla, cvc5, CMake, LLVM 15, and Python (via Poetry, into
`metalift/.venv`) — all resolved locally (Homebrew on macOS, apt/dnf on Linux) rather than
from a shared cluster mount. See `bash install_deps.sh --help` for skip flags.

### 2. Activate the environment

All Python dependencies for metalift are managed by Poetry into `metalift/.venv` (Python 3.10):

```bash
source metalift/.venv/bin/activate
```

### 3. Try the projects directly

The VERGE-specific pipeline (`demo/run.py`, the OpenEvolve-era `--evolve`/`--compare` flags)
described further down is aspirational and not implemented yet. For now, run each project's
own examples:

**metalift** — LLM-based synthesis over its bundled benchmarks (needs `OPENAI_API_KEY`,
`CLAUDE_API_KEY`, or `GEMINI_API_KEY` in a `.env` file inside `metalift/`):

```bash
cd metalift
python3 benchmarks/blend/driver/<benchmark_name>_driver.py
# or: python3 benchmarks/llama/driver/<benchmark_name>_driver.py
```

See [`metalift/benchmarks/README.md`](metalift/benchmarks/README.md) for the full list.

**levi** — LLM-guided code/prompt evolution (pick the quickstart matching what you have):

```bash
cd levi
uv run python examples/quickstart/quickstart_claude.py   # Claude Code CLI subscription, $0
uv run python examples/quickstart/quickstart_codex.py    # Codex CLI subscription, $0
uv run python examples/quickstart/quickstart_api.py       # API key, ~$0.05–0.10
uv run python examples/quickstart/quickstart_prompts.py   # API key, prompt tuning, ~$0.05–0.10
```

The API-based scripts need `OPENAI_API_KEY` set (or edit `MODEL` at the top of the file for
another [litellm provider](https://docs.litellm.ai/docs/providers)).

---

## Benchmarks

*(VERGE-specific benchmarks — pending the `demo/` integration described in Status above.)*

| # | Name | Source | Solver | Loop | Answer |
|---|------|--------|--------|------|--------|
| 1 | `fma_dsl` | scalar C | Rosette | ✗ | `ret == fma(base+base2, arg1, arg2) * 2` |
| 2 | `no_loop_matmul` | 2×2 matmul C++ | Rosette | ✗ | `ret == l1_norm(mat_mul(a, b, x))` |
| 3 | `list_abs_sum` | abs-sum loop C++ | Rosette | ✓ | `sum == list_abs_sum(lst)` |
| 4 | `count` | counting loop C++ | Rosette | ✓ | `count == reduce(map(data, lm), lr)` |
| 5 | `cblas_sgemv` | nested-loop BLAS | CVC5 | ✓✓ | `ret == cblas_sgemv(alpha, a, x, beta, y)` |

---

## Architecture

VERGE's intended closed-loop evolution pipeline (not yet implemented — see Status above):

```
  User writes a weak seed grammar
          │
          ▼
       LEVI (LLM)
    mutates EVOLVE-BLOCK  ◄──────────────────────────────┐
          │                                               │
          ▼                                               │
  Candidate grammar                                       │
          │                                               │
          ▼                                        score feeds back
  metalift + SMT solver                                   │
  (Rosette / CVC5)                                        │
          │                                               │
    ┌─────┴──────┐                                        │
    │ synthesized │──── score = 1.0 ────────────── ✅ done │
    │  failed     │──── score = 0.0 ────────────────────── ┘
    └────────────┘
```

| Stage | Role |
|---|---|
| **Weak seed** | User writes a rough grammar — just needs to know the DSL function exists |
| **LEVI** | LLM proposes grammar mutations, matching model capacity to task demand |
| **metalift** | Analyses LLVM IR, builds verification conditions |
| **SMT solver** | Rosette (Z3) or CVC5 — formally verifies each candidate |
| **Score** | `0.5×synthesized + 0.3×speed + 0.2×tightness` — drives next LLM iteration |

---

## Adding a new benchmark

*Pending:* the old `openevolve/new_benchmark.py` scaffold generator was specific to the
OpenEvolve integration and doesn't apply to LEVI's `evolve_code()`/`evolve_prompts()` API.
A VERGE-specific scaffold for LEVI hasn't been written yet — for now, adapt one of
[`levi/examples/quickstart/`](levi/examples/quickstart/) directly, using metalift to build
your `score_fn`.

---

## Solver interface

Two solvers are supported by metalift, configured per benchmark in `BenchmarkConfig`:

| Solver | Flag | When to use |
|--------|------|-------------|
| `rosette` | *(default)* | Scalar / list benchmarks; uses Racket + Z3 |
| `cvc5` | `--solver cvc5` | Nested loops; recursive axioms; undecidable base logic |

The `benchmark.py` module (pending, see Status above) is intended to handle all env setup
(`RACKET_BIN`, `PLTADDONDIR`, `Z3_PATH`, `SYNTH_CVC5`) so runner scripts don't need to repeat it.

---

## Environment variables

After running `install_deps.sh`, add the printed snippet to `~/.zshrc` (macOS) or
`~/.bashrc` (Linux).

| Variable | Purpose |
|---|---|
| `PATH` | Python 3.10, Poetry, CVC5, CMake, LLVM 15, Racket |
| `BITWUZLA_PATH` | Path to bitwuzla binary |
| `POETRY_CACHE_DIR` / `POETRY_DATA_DIR` / `POETRY_CONFIG_DIR` | Redirected away from `~/.cache` / `~/.local` |
| `OPENAI_API_KEY` / `CLAUDE_API_KEY` / `GEMINI_API_KEY` | LLM keys for metalift's benchmark drivers (`metalift/.env`) |
| `OPENAI_API_KEY` (or another [litellm provider](https://docs.litellm.ai/docs/providers) key) | LLM key for LEVI's API-based quickstarts |
