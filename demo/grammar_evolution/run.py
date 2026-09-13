#!/usr/bin/env python3
"""VERGE demo: evolve the loop-invariant *grammar* metalift searches over —
the literal "weak seed grammar" premise from the top-level README, not the
LLM-prompt framing used by demo/prompt_evolution/.

The benchmark is metalift's own `list_abs_sum` tutorial (tests/python/,
pure Rosette/CVC5 search, no LLM on metalift's side at all). The DSL
(`target_lang`) and the postcondition (`ps_grammar`) are held fixed; only
`inv_grammar` — the `choose(...)`-based search space for the loop
invariant — is evolved.

This uses `levi.evolve_code` (not `evolve_prompts`, as in the sibling
demo): the evolved artifact is real Python source using choose() and
metalift IR operators, which is exactly the code-shaped task evolve_code
is suited for -- unlike free-text prompts, which is why the sibling demo
had to switch away from it.

The seed here (WEAK_INV_GRAMMAR) is deliberately missing one required
literal from its `choose(...)` set: `Int(0)`, the slice offset that
`inv_grammar` needs to express "the sum of the unprocessed tail of the
list equals the total". Verified independently: metalift *alone* with
this grammar raises SynthesisFailed -- deterministically, no LLM
involved, so there's no ambiguity about whether the baseline "fails".

Requires:
- `WANDB_API_KEY` in the environment (from https://wandb.ai/authorize).
  Unlike the prompt-evolution demo, metalift's own side needs no LLM at
  all here -- Rosette/CVC5 do the verification -- so this key is only
  spent on LEVI's proposer.
- `bash install_deps.sh` already run successfully.

Usage:
    cd levi
    uv run python ../demo/grammar_evolution/run.py

Much cheaper and faster than the prompt-evolution demo: each evaluation
is a local Rosette/CVC5 search (seconds, no network call on metalift's
side), not a synthesis LLM round-trip.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# LEVI's own harness does `exec(code, namespace)` in its own Python 3.11
# process just to *define* the evolved function (even though it never
# calls it) -- so the module-level `from metalift.ir import ...` in every
# candidate has to succeed there too, despite metalift not being installed
# in that environment. A permissive stub package makes those imports
# harmless; the function body is only ever actually executed for real in
# metalift's own Python 3.10 subprocess (verge_grammar_bridge.py). Set via
# both sys.path (this process) and PYTHONPATH (in case LEVI's eval workers
# are separate spawned processes that wouldn't inherit an in-memory
# sys.path change).
_STUB_DIR = str(Path(__file__).resolve().parent / "_metalift_stub")
sys.path.insert(0, _STUB_DIR)
os.environ["PYTHONPATH"] = _STUB_DIR + os.pathsep + os.environ.get("PYTHONPATH", "")

import levi
import weave

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _verge_bridge import METALIFT_DIR, METALIFT_PYTHON, bridge_env  # noqa: E402

BRIDGE_SCRIPT = Path(__file__).resolve().parent / "verge_grammar_bridge.py"

# Routed through W&B Inference via litellm's wandb/ prefix; reads
# WANDB_API_KEY automatically. GLM-5.3-Flash was tried and rejected: ~4min
# per generation and LEVI's extract_code() couldn't find a code block in
# its response at all -- almost certainly a truncated generation (verbose
# reasoning/prose eating the token budget before the closing ``` fence),
# not an extraction-logic gap (extract_code handles <think> tags and both
# fenced/unfenced code already). gpt-oss-20b is fast, cheap, and its
# harmony output format is well-tested against this exact extraction path.
# Swap for any other model at wandb.ai/site/inference -- click through to
# each model's page for its exact API slug, which often doesn't match the
# display name.
MODEL = "wandb/openai/gpt-oss-20b"
BUDGET_DOLLARS = 0.50
WEAVE_PROJECT = "verge-grammar-evolution"

# LEVI's ResilientProcessPool runs every score_fn call in a brand-new `spawn`
# subprocess (levi/utils/resilient_pool.py) that's killed (proc.terminate(),
# then proc.kill() if needed) the instant it reads a result off the queue --
# so a subprocess's own weave client, whose uploads happen on a background
# thread, can easily get killed mid-upload before a trace ever reaches the
# server. _ensure_weave_initialized() is called both in main() (so LEVI's
# own proposer/litellm calls, which run in-process and outlive the process,
# get traced normally) and in score() (so each spawned eval subprocess
# initializes its own client); score() then calls _weave_client.flush()
# after the traced call, blocking until that one trace is actually uploaded
# -- otherwise the parent's near-immediate proc.terminate() races it.
_weave_initialized_in_process = False
_weave_client = None


def _ensure_weave_initialized() -> None:
    global _weave_initialized_in_process, _weave_client
    if not _weave_initialized_in_process:
        _weave_client = weave.init(WEAVE_PROJECT)
        _weave_initialized_in_process = True
# Correct grammars verify in ~9s and the weak seed fails in ~7s (both
# measured); 20s gives ~2x headroom over the known-good case while cutting
# the worst-case wait for an over-broad candidate from 90s to 20s, freeing
# that worker slot for another attempt sooner.
EVAL_TIMEOUT_SECONDS = 20.0
# This machine has 8 cores; LEVI's defaults (4/4) leave half of them idle.
# LLM generation is network-bound (safe to run well above the core count),
# and each eval spawns one Rosette/CVC5 subprocess (CPU-bound, so capped at
# the core count to avoid contention that would slow each one down instead
# of increasing throughput). Neither knob touches BUDGET_DOLLARS -- this is
# purely "try more candidates per unit wall-clock," not "spend more."
N_LLM_WORKERS = 8
N_EVAL_PROCESSES = 8

description = """
## Task
metalift is trying to verify that a Python while-loop implementing
`list_abs_sum` (sum of absolute values of a list) is correctly summarized
by a recursive DSL function. It searches for a loop invariant within the
search space defined by `inv_grammar` below, using Rosette/CVC5.

Your job is to evolve `inv_grammar`. The DSL (`list_abs_sum`) and the
postcondition are fixed -- only the invariant's search space can change.
A better grammar lets metalift actually find and verify a correct
invariant, ideally in fewer synthesis rounds and less wall-clock time.

## Background on the benchmark
The loop is:
    i = 0; sum = 0
    while i < len(lst):
        curr_el = lst[i]
        sum += abs(curr_el)
        i += 1
    return sum

`reads` is `[curr_el, i, lst, sum]`, `writes` is `[curr_el, i, sum]`.
The DSL provides `list_abs_sum(lst)` (recursive: sum of abs values).

## Constraints
- Keep the exact signature:
  `def inv_grammar(writes, reads, in_scope, relaxed) -> Expr:`
- The invariant must be expressible using metalift's `choose(...)`
  combinator (which asks the solver to pick among the listed
  alternatives) and its `Object`/IR arithmetic and comparison operators
  (`+`, `-`, `==`, `>=`, `<=`, `>`, `<`, list slicing `lst[a:b]`,
  `.len()`, function `call(...)`).
- Do not hardcode a solution using concrete literals tied only to this
  exact benchmark's expected answer -- the grammar should still represent
  a genuine search space, not a single fixed formula (that would trivially
  score well here but demonstrate nothing about evolving a search space).
- Import whatever you need from `metalift.ir` and `metalift.vc_util`
  (`and_objects` is commonly useful) at the top of the file.
"""

signature = (
    "def inv_grammar(writes: List[Object], reads: List[Object], "
    "in_scope: List[Object], relaxed: bool) -> Expr:\n    ...\n"
)

# Deliberately missing Int(0) from int_lit's choices -- the offset needed
# to express "sum of the unprocessed tail equals the total". Verified
# independently (not via this demo) that metalift alone raises
# SynthesisFailed with this exact grammar.
WEAK_INV_GRAMMAR = '''from typing import List

from metalift.ir import Bool, Expr, Int, Object, call, choose
from metalift.vc_util import and_objects

LIST_ABS_SUM_FN_NAME = "list_abs_sum"


def inv_grammar(
    writes: List[Object], reads: List[Object], in_scope: List[Object], relaxed: bool
) -> Expr:
    lst = reads[2]
    choose_write = choose(*writes)

    int_lit = choose(Int(0) - Int(1), Int(1), Int(2))
    lst_length = lst.len()

    lst_sum = call(LIST_ABS_SUM_FN_NAME, Int, lst)
    lst_tail_sum = call(LIST_ABS_SUM_FN_NAME, Int, lst[choose_write + int_lit :])

    index_lower_bound = choose(
        choose_write >= int_lit,
        choose_write > int_lit,
    )
    index_upper_bound = choose(
        choose_write <= lst_length,
        choose_write < lst_length,
    )
    return and_objects(
        index_lower_bound,
        index_upper_bound,
        choose(choose_write + lst_tail_sum == lst_sum, choose_write == lst_tail_sum),
    )
'''


@weave.op()
def _traced_score(fn_source: str) -> dict:
    """Actually run metalift synthesis with the evolved grammar and see if it verifies.

    Takes the grammar's source as a plain string (rather than the live
    function object score() receives) so Weave can log/serialize the
    candidate directly instead of an opaque, non-JSON-able function.
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, dir=tempfile.gettempdir()
    ) as f:
        f.write(fn_source)
        evolved_path = f.name

    # Every branch below scores 0.0 with feedback rather than returning an
    # "error" key -- LEVI discards any result carrying one outright instead
    # of recording it in the archive, which was silently swallowing every
    # timeout and infrastructure hiccup until this was caught mid-run.
    try:
        proc = subprocess.run(
            [str(METALIFT_PYTHON), str(BRIDGE_SCRIPT), evolved_path],
            cwd=METALIFT_DIR,
            env=bridge_env(),
            capture_output=True,
            text=True,
            timeout=EVAL_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return {
            "score": 0.0,
            "verified": False,
            "feedback_per_example": [
                f"FAILED: synthesis exceeded the {EVAL_TIMEOUT_SECONDS:.0f}s timeout "
                f"without finishing. This almost always means the grammar's "
                f"choose(...) search space is too large for Rosette/CVC5 to explore "
                f"quickly -- prefer fewer, more targeted choose() branches over "
                f"broad ones that happen to also be correct."
            ],
            "per_example_scores": [0.0],
        }
    finally:
        Path(evolved_path).unlink(missing_ok=True)

    result_line = next(
        (line for line in proc.stdout.splitlines() if line.startswith("VERGE_RESULT: ")),
        None,
    )
    if result_line is None:
        return {
            "score": 0.0,
            "verified": False,
            "feedback_per_example": [
                f"FAILED: the bridge process crashed before reporting a result "
                f"(stderr: {proc.stderr[-500:] or '(empty)'}). Likely an error in "
                f"the code outside inv_grammar itself, or an import that doesn't "
                f"exist in metalift's real modules."
            ],
            "per_example_scores": [0.0],
        }
    return json.loads(result_line[len("VERGE_RESULT: ") :])


def score(inv_grammar_fn, _inputs=None) -> dict:
    """LEVI's actual entry point -- extracts source from the live function
    object it hands us, then delegates to the @weave.op()-traced worker.

    weave.init() must run here, BEFORE _traced_score() is invoked, not
    inside it: @weave.op()'s wrapper checks for an active client at call
    entry, so a client set up from inside the decorated function itself is
    one call too late and that call is never traced. Likewise the flush()
    after the call: this whole subprocess gets killed right after this
    function returns, so the trace has to be fully uploaded before then.
    """
    _ensure_weave_initialized()
    fn_source = inv_grammar_fn.__globals__.get("__source_code__")
    if fn_source is None:
        return {
            "score": 0.0,
            "verified": False,
            "feedback_per_example": ["FAILED: could not read the evolved function's source code."],
            "per_example_scores": [0.0],
        }
    result = _traced_score(fn_source)
    if _weave_client is not None:
        _weave_client.flush()
    return result


# Every verified candidate scores in [0.5, 1.0] (see score()); every failure
# scores exactly 0.0. metalift alone can't reach even 0.5 with this grammar
# -- it can't verify anything at all. So target_score=0.5 means: stop the
# instant ANY candidate verifies, rather than continuing to spend budget
# chasing a tighter grammar once the actual comparison is already won.
# Raise this (toward the 0.9+ range) only if you want LEVI to keep
# searching for a materially *better* grammar after the first success --
# that trades directly against "fast and cheap."
TARGET_SCORE = 0.5


def main() -> None:
    # No team prefix -- logs to your own default W&B entity. Initializes
    # weave in THIS (main) process so LEVI's own proposer/litellm calls,
    # which run in-process, get traced; each spawned eval subprocess
    # separately initializes its own client (see _ensure_weave_initialized).
    _ensure_weave_initialized()
    result = levi.evolve_code(
        description,
        function_signature=signature,
        seed_program=WEAK_INV_GRAMMAR,
        score_fn=score,
        model=MODEL,
        budget_dollars=BUDGET_DOLLARS,
        target_score=TARGET_SCORE,
        pipeline=levi.PipelineConfig(
            eval_timeout=EVAL_TIMEOUT_SECONDS + 30.0,
            n_llm_workers=N_LLM_WORKERS,
            n_eval_processes=N_EVAL_PROCESSES,
        ),
    )

    succeeded = result.best_score >= TARGET_SCORE
    print(f"\n{'='*70}")
    print(f"VERGE {'SUCCEEDED' if succeeded else 'DID NOT REACH TARGET'}")
    print(f"{'='*70}")
    print(f"metalift alone (this exact weak grammar): SynthesisFailed, always -- verified independently, no LLM involved")
    print(f"VERGE (LEVI + metalift): best score {result.best_score:.4f}")
    print(f"  Evaluations: {result.total_evaluations}")
    print(f"  Cost:        ${result.total_cost:.4f}")
    print(f"  Wall clock:  {result.runtime_seconds:.1f}s")
    print(f"{'='*70}\n")
    print(result.best_program)


if __name__ == "__main__":
    main()
