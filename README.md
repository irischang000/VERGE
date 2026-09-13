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
├── demo/                        # Working VERGE loops (LEVI + metalift together)
│   ├── _verge_bridge.py         # Shared: Poetry path + subprocess env for both demos below
│   ├── prompt_evolution/        # LEVI evolves metalift's synthesis-LLM prompt (evolve_prompts)
│   └── grammar_evolution/       # LEVI evolves metalift's loop-invariant grammar (evolve_code)
├── deps/                        # Local dependency installs (Racket, LLVM 15, bitwuzla, cvc5, Poetry, …)
├── install_deps.sh              # One-shot environment setup script
└── README.md                    # This file
```

> **Status:** `metalift` and `levi` are vendored as git submodules, with two working VERGE
> loops: [`demo/prompt_evolution/`](#run-the-prompt-evolution-demo) evolves the *prompt*
> metalift's synthesis LLM uses (`levi.evolve_prompts`, text), and
> [`demo/grammar_evolution/`](#run-the-grammar-evolution-demo) evolves the actual loop-invariant
> *grammar* metalift searches over (`levi.evolve_code`, real Python) — the literal "weak seed
> grammar" premise below. The prompt demo had to use `evolve_prompts` rather than `evolve_code`
> because cheap models produced invalid Python on ~100% of candidates when asked to emit
> multi-line prompt text as Python source; the grammar demo doesn't hit that, since a
> `choose(...)`-based grammar is genuinely code-shaped. Everything else in the
> Architecture/Benchmarks sections further down (the other 4 example benchmarks) is still
> aspirational.

---

## Quick start

### 0. Clone with submodules

`metalift` and `levi` are git submodules, so clone (or update) accordingly:

```bash
git clone --recurse-submodules <this-repo-url>
# or, if you already cloned without that flag:
git submodule update --init --recursive
```

> Note: `git status` run *inside* `metalift/` or `levi/` reports on that submodule's own
> repository, not this one — run it from the repo root to see your actual changes. Expect
> `metalift/` to show modified content: that's the local patches in `patches/`, plus
> `llmlift_scripts/dsl.py`, which metalift regenerates on every synthesis run
> (`llmlift_scripts/parser.py` rewrites it). That file's churn is harmless — discard it.

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

**metalift's classic Rosette tutorials** — pure grammar search, no LLM at all on metalift's
side (only `tests/llvm/`, not `benchmarks/`; these match the original weak-seed-grammar
premise directly, since `ps_grammar`/`inv_grammar` in the driver *are* the grammar):

```bash
cd metalift/tests/llvm
bash compile-add-blocks.sh list_abs_sum.cc   # generates .ll/.loops (gitignored, not checked in)
cd ../..
poetry run python tests/llvm/list_abs_sum_driver.py
```

This needs no API key and no network access — Rosette solves it locally via Z3/bitwuzla in
a few seconds. `compile-add-blocks.sh` and `gen_loops_file.py` were both patched to work on
LLVM 15 (the tutorials were written against LLVM 11); see
`patches/metalift-llvm15-compile-script-fix.patch` for the details of what changed and why.

**metalift's Python-source tutorials** (`tests/python/`) — same idea, but working directly
from Python source instead of compiled LLVM IR, so there's no clang/opt toolchain involved
at all. All 16 pass:

```bash
poetry run bash tests/python/run_all_tests.sh
```

(`run_all_tests.sh` stops at the first failure; loop over `tests/python/*_driver.py`
yourself with a per-file `timeout` if you want to see every result instead of just the
first one.) This uncovered a real bug in `metalift/frontend/python.py` that blocked all
16 drivers uniformly — `gen_Synth()` never passed the `relaxed` argument its own grammar
functions require — plus one driver-specific bug in `tuples1_driver.py` (`make_tuple` was
imported but never called; `x_tuple`/`y_tuple` were referenced but never defined). Both
fixed in `patches/metalift-python-frontend-relaxed-arg.patch` and
`patches/metalift-tuples1-driver-fix.patch`.

### Run the prompt-evolution demo

This is the first real VERGE loop — LEVI evolving something metalift actually verifies.
Runs entirely on `WANDB_API_KEY` (from [wandb.ai/authorize](https://wandb.ai/authorize)) —
both LEVI's proposer model and metalift's own internal synthesis calls route through W&B
Inference (see `patches/metalift-wandb-inference-routing.patch`, applied automatically by
`install_deps.sh`). Set `OPENAI_API_KEY` too if you'd rather metalift's internal calls use
real GPT-4o instead of a W&B-hosted open-source model.

```bash
export WANDB_API_KEY=...
cd levi
uv run python ../demo/prompt_evolution/run.py
```

~5-15 minutes and a few dollars, since every LEVI evaluation runs a full metalift synthesis
+ Rosette verification attempt, not just a proposer call.

**Weak vs. strong seed.** By default the demo starts from a deliberately *weak* prompt —
it states the task but omits every constraint metalift's parser enforces (single return
statement, no loops, no intermediate variables, semantic equivalence). That's the README's
original premise, and it leaves real headroom for evolution to rediscover those rules.

Run `VERGE_SEED=strong` to start from metalift's own hand-tuned production prompt instead.
That one solves `normal_blend_8` on the first attempt with zero parser rejections — the
minimum possible work — scoring ~0.996 with only wall-clock noise left as headroom. It's a
useful comparison target, but evolution can only tie it, never beat it.

**Self-correction.** Each evaluation returns `feedback_per_example` /
`per_example_scores` alongside the score. LEVI samples these into its mutation prompt, so
the proposer sees *why* a parent failed — metalift's actual parser errors, whether the
rewrite parsed but failed verification, how many LLM round-trips it burned — and encodes
those lessons into the next candidate rather than mutating blind on a scalar.

**Scoring.** A candidate prompt scores 0.0 if metalift never verifies a rewrite with it.
Otherwise it scores on efficiency: total LLM round-trips (PS + invariant attempts) dominate,
parser rejections are penalised separately (output metalift can't parse is worse than output
that parses but doesn't verify), and wall-clock breaks ties — roughly 0.50 to 0.99 in
practice. The spread matters: an earlier version scored only on PS-attempt count, which
collapsed to four possible values with the seed already at the ceiling, leaving evolution
nothing to climb.

### Run the grammar-evolution demo

The literal "weak seed grammar" premise from the top of this README — LEVI evolves the
`inv_grammar` search space itself (real Python using `choose(...)`), not an LLM prompt.
Benchmark is metalift's own `list_abs_sum` tutorial: pure Rosette/CVC5 search, **no LLM at
all on metalift's side**, so this only spends `WANDB_API_KEY` on LEVI's proposer — much
cheaper and faster per-evaluation than the prompt-evolution demo (a local solver call,
seconds, versus a synthesis LLM round-trip).

```bash
export WANDB_API_KEY=...
cd levi
uv run python ../demo/grammar_evolution/run.py
```

The seed (`WEAK_INV_GRAMMAR`) is deliberately missing one literal metalift needs (the `0`
slice offset) — verified independently that metalift *alone* with this exact grammar raises
`SynthesisFailed`, deterministically, no LLM involved. That's the actual "metalift alone
fails" baseline the VERGE comparison needs.

This uses `levi.evolve_code` (not `evolve_prompts`, unlike the sibling demo) — a grammar
function is genuinely code-shaped (choose() calls, IR arithmetic), which is exactly what
evolve_code is suited for; free-text prompts are not, which is why the other demo had to
switch away from it.

**The tricky part:** LEVI's own harness does `exec(code, namespace)` in its own Python 3.11
process just to *define* the evolved function, even though it never calls it there — so
every candidate's module-level `from metalift.ir import ...` has to succeed in an
environment where metalift isn't installed. `demo/grammar_evolution/_metalift_stub/` is a
permissive stub package (every attribute access returns a no-op placeholder) that makes
those imports harmless; the function body only ever actually runs for real inside
`verge_grammar_bridge.py`'s metalift Python 3.10 subprocess, where the genuine package
exists. Wired in via both `sys.path` and `PYTHONPATH` (the latter in case LEVI's eval
workers are separate spawned processes rather than threads) — and `_verge_bridge.py`
explicitly strips `PYTHONPATH` back out before handing the environment to the bridge
subprocess, or the stub would shadow the real metalift package there instead.

**Every failure mode scores 0.0 with feedback — none get discarded.** LEVI drops any
result carrying an `"error"` key outright rather than recording it in the archive, so
every failure path routes through `feedback_per_example`/`per_example_scores` instead:
a synthesis timeout (the grammar's search space is too large for Rosette/CVC5 to explore
quickly), a crashed bridge subprocess, and — the subtle one — a candidate that imports a
name accepted by the permissive stub but that doesn't actually exist in the real
`metalift.ir`/`vc_util` (caught by wrapping the evolved file's own module-level load in
the same try/except as the synthesis attempt itself, not just the attempt).

**Model choice matters more here than for the prompt-evolution demo**, since the failure
modes are different: GLM-5.3-Flash was tried and rejected (~4 min per generation, and
LEVI's `extract_code()` couldn't find a code block at all — almost certainly a truncated
generation eating its token budget on reasoning/prose before ever closing its code fence).
`openai/gpt-oss-20b` (current default) is fast, cheap, and its output format is well-tested
against this exact extraction path.

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
