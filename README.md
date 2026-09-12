**V**erified, **E**fficient **R**efinement via **G**uided **E**volution — a framework that combines
[metalift](metalift/) (program synthesis / verified lifting) with
[OpenEvolve](openevolve/) (LLM-driven grammar evolution) to automatically
discover and verify high-level specifications for low-level kernels.

> **Key insight:** Writing the correct loop invariant grammar for metalift is the hard part.
> VERGE lets a user write a rough "weak seed" grammar and delegates the hard
> grammar-engineering to an LLM+verifier loop that iterates until synthesis succeeds.

```
verge/
├── metalift/                    # Program synthesis & verified lifting framework
├── openevolve/                  # LLM-driven grammar evolution (OpenEvolve integration)
│   ├── benchmark.py             # Unified solver-agnostic benchmark interface
│   ├── new_benchmark.py         # Scaffold generator — add a new benchmark in one command
│   ├── runner_<name>.py         # Subprocess runner per benchmark
│   ├── evaluator_<name>.py      # OpenEvolve evaluator per benchmark
│   └── config_<name>.yaml       # LLM + evolution config per benchmark
├── demo/                        # End-to-end demos (one per metalift tutorial)
│   ├── run.py                   # Demo runner CLI
│   └── <name>/
│       ├── initial_program.py   # Seed grammar (strong — expert-written)
│       └── initial_program_weak.py  # Weak seed (naive — for VERGE comparison demo)
├── deps/                        # Local dependency installs (Racket, LLVM 11, bitwuzla, …)
├── install_deps.sh              # One-shot environment setup script
└── README.md                    # This file
```

---

## Quick start

### 1. Install dependencies

```bash
bash install_deps.sh
```

Sets up Racket + Rosette, bitwuzla, LLVM, and all Python packages via Poetry.

### 2. Activate the environment

All Python dependencies are managed by Poetry into `metalift/.venv` (Python 3.10):

```bash
source metalift/.venv/bin/activate
```

### 3. Run the demo

```bash
# Synthesis only — all 5 benchmarks with their seed grammars:
python demo/run.py

# Single benchmark:
python demo/run.py --bench list_abs_sum

# With OpenEvolve evolution (requires API key — see below):
eval "$(python openevolve/get_jedai_token.py)"
python demo/run.py --bench list_abs_sum --evolve

# Full metalift-alone vs VERGE comparison demo:
eval "$(python openevolve/get_jedai_token.py)"
python demo/run.py --compare
```

See [`demo/README.md`](demo/README.md) for per-benchmark details.

---

## Benchmarks

| # | Name | Source | Solver | Loop | Answer |
|---|------|--------|--------|------|--------|
| 1 | `fma_dsl` | scalar C | Rosette | ✗ | `ret == fma(base+base2, arg1, arg2) * 2` |
| 2 | `no_loop_matmul` | 2×2 matmul C++ | Rosette | ✗ | `ret == l1_norm(mat_mul(a, b, x))` |
| 3 | `list_abs_sum` | abs-sum loop C++ | Rosette | ✓ | `sum == list_abs_sum(lst)` |
| 4 | `count` | counting loop C++ | Rosette | ✓ | `count == reduce(map(data, lm), lr)` |
| 5 | `cblas_sgemv` | nested-loop BLAS | CVC5 | ✓✓ | `ret == cblas_sgemv(alpha, a, x, beta, y)` |

---

## Architecture

VERGE runs a closed-loop evolution pipeline:

```
  User writes a weak seed grammar
          │
          ▼
    OpenEvolve (LLM)
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
| **OpenEvolve** | LLM proposes grammar mutations; island-based population search |
| **metalift** | Analyses LLVM IR, builds verification conditions |
| **SMT solver** | Rosette (Z3) or CVC5 — formally verifies each candidate |
| **Score** | `0.5×synthesized + 0.3×speed + 0.2×tightness` — drives next LLM iteration |

---

## Adding a new benchmark

Use the scaffold generator — it creates all 5 required files from one command:

```bash
python openevolve/new_benchmark.py \
    --name my_bench \
    --llvm tests/llvm/my_bench.ll \
    --loops tests/llvm/my_bench.loops \
    --solver rosette \
    --has-loop \
    --dsl-fn my_fn \
    --answer "ret_val == my_fn(lst)"
```

Then fill in two `TODO` stubs:
1. `build_driver()` in `openevolve/runner_my_bench.py` — add input variables
2. `target_lang()` in `demo/my_bench/initial_program.py` — define the DSL

Use `--dry-run` to preview without writing files.

---

## Solver interface

Two solvers are supported, configured per benchmark in `BenchmarkConfig`:

| Solver | Flag | When to use |
|--------|------|-------------|
| `rosette` | *(default)* | Scalar / list benchmarks; uses Racket + Z3 |
| `cvc5` | `--solver cvc5` | Nested loops; recursive axioms; undecidable base logic |

The `benchmark.py` module handles all env setup (`RACKET_BIN`, `PLTADDONDIR`, `Z3_PATH`, `SYNTH_CVC5`) so runner scripts don't need to repeat it.

---

## Environment variables

After running `install_deps.sh`, add the printed snippet to `~/.bashrc`.

| Variable | Purpose |
|---|---|
| `PATH` | Python 3.10, Poetry, CVC5, Racket |
| `BITWUZLA_PATH` | Path to bitwuzla binary |
| `POETRY_CACHE_DIR` | Redirected away from `~/.cache` |
| `JEDAI_API_KEY` | LLM API key — get via `eval "$(python openevolve/get_jedai_token.py)"` |
