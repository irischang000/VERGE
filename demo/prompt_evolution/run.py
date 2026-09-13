#!/usr/bin/env python3
"""VERGE demo: evolve the *prompt* metalift uses to ask an LLM to rewrite a
C++ kernel into its tensor DSL — not the DSL itself.

Concretely, LEVI evolves the raw instruction text metalift sends to its
synthesis LLM for the `normal_blend_8` benchmark. Each candidate prompt is
scored by actually running metalift's synthesis + Rosette verification
against it (via `verge_prompt_bridge.py`, run as a subprocess in metalift's
own Python 3.10 / Poetry environment, since LEVI requires Python 3.11+ and
can't share that virtualenv).

This uses `levi.evolve_prompts` (evolving text) rather than
`levi.evolve_code` (evolving a `get_ps_prompt` function). The code-evolution
framing was tried first and failed: asked to emit a Python function whose
body is multi-line prompt text, cheaper models produced syntactically
invalid Python on essentially every candidate (unterminated string
literals), so nothing but the seed ever entered the archive.

Requires:
- `WANDB_API_KEY` in the environment (from https://wandb.ai/authorize). Used
  for both sides: LEVI's proposer model via litellm's `wandb/<model>`
  routing, AND metalift's own internal synthesis calls, which fall back to
  W&B Inference (patches/metalift-wandb-inference-routing.patch) when there's
  no OPENAI_API_KEY. Everything runs on your W&B Inference credits.
- `OPENAI_API_KEY` is optional -- set it instead/as well if you'd rather
  metalift's internal calls use real GPT-4o (W&B Inference only hosts
  open-source models).
- `bash install_deps.sh` already run successfully (Racket/Rosette, Poetry
  venv, the metalift submodule, and its local patches all in place)

By default this starts from a deliberately *weak* seed prompt (see
WEAK_SEED_PROMPT) so evolution has real headroom. Set VERGE_SEED=strong to
start from metalift's production prompt, which already sits at the practical
ceiling (~0.996) and leaves nothing to climb.

Usage (from the levi/ virtualenv, e.g. `uv run python ../demo/prompt_evolution/run.py`):

    cd levi
    uv run python ../demo/prompt_evolution/run.py

~5-15 min and a few dollars of OpenAI spend, since every evaluation runs a
real metalift synthesis attempt (not just a proposer call).
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import levi
import weave

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _verge_bridge import METALIFT_DIR, POETRY_BIN, bridge_env  # noqa: E402

# Bridge script lives alongside this file (not inside the metalift submodule,
# which is untracked/opaque to our own repo) but runs with cwd=METALIFT_DIR
# so its relative benchmark paths resolve.
BRIDGE_SCRIPT = Path(__file__).resolve().parent / "verge_prompt_bridge.py"
BENCHMARK_NAME = "normal_blend_8"

# Routed through W&B Inference (uses your hackathon W&B credits) via litellm's
# wandb/ prefix; reads WANDB_API_KEY automatically. Swap for any model listed
# at wandb.ai/site/inference.
MODEL = "wandb/Qwen/Qwen3-235B-A22B-Instruct-2507"
BUDGET_DOLLARS = 0.50
EVAL_TIMEOUT_SECONDS = 300.0
WEAVE_PROJECT = "verge-prompt-evolution"

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


# Substituted by plain string replacement in verge_prompt_bridge.py. Not
# str.format placeholders -- C++ source is full of braces that would break it.
DSL_CODE_TOKEN = "<<<DSL_CODE>>>"
SOURCE_CODE_TOKEN = "<<<SOURCE_CODE>>>"

# metalift's own hand-tuned production prompt. It solves normal_blend_8 on the
# first attempt with zero parser rejections -- i.e. the minimum possible work --
# so it sits at ~0.996 with only wall-clock noise left as headroom. Useful as a
# comparison target, but there is nothing for evolution to climb toward.
STRONG_SEED_PROMPT = """Your task is to rewrite the given `test` C++ Function. You need to use only the set of provided functions and constants to achieve this. The rewritten program should be semantically equivalent to the `test` function. Please generate the shortest possible solution.

#Instructions
# 1. Do not use for/while loops for rewriting the function.
# 2. The rewritten program should just be a single return statement of the form return provided_function(...)
# 3. Inline all the expressions. Do not use intermediate variables. Return the function signature as well as the function body in python.

#defined functions
```python
<<<DSL_CODE>>>
```

```cpp
//test function
<<<SOURCE_CODE>>>
```
"""

# The "weak seed" from the README's premise: state the task, but omit every
# constraint metalift's parser actually enforces (single return statement, no
# loops, no intermediate variables, inlined expressions, semantic equivalence).
# The downstream LLM will tend to violate those, drawing parser rejections and
# retries, so this scores low and leaves real room for evolution to rediscover
# the missing rules -- which is the behaviour the demo is meant to show.
WEAK_SEED_PROMPT = """Rewrite the C++ function below in Python, using the provided functions.

Provided functions:
```python
<<<DSL_CODE>>>
```

C++ function to rewrite:
```cpp
<<<SOURCE_CODE>>>
```
"""

# Set VERGE_SEED=strong to start from metalift's production prompt instead.
SEED_MODE = os.environ.get("VERGE_SEED", "weak").strip().lower()
seed_prompt = STRONG_SEED_PROMPT if SEED_MODE == "strong" else WEAK_SEED_PROMPT

description = f"""
## Task
metalift is trying to rewrite a C++ image-blending kernel ({BENCHMARK_NAME})
as a single expression over a fixed tensor DSL, then verify the rewrite with
Rosette. The rewrite is produced by an LLM given the prompt you are evolving.

Your job is to evolve that prompt text. The DSL and the benchmark are fixed --
only the wording, structure, ordering, and any examples or reasoning
scaffolding in the prompt can change. A better prompt makes the downstream LLM
more likely to produce a DSL rewrite that passes metalift's parser and Rosette
verification, and to do so in fewer retries.

## Constraints
- The prompt MUST contain the literal tokens {DSL_CODE_TOKEN} and
  {SOURCE_CODE_TOKEN}. They are replaced with the DSL definitions and the C++
  function under test before the prompt is sent. Without both, the downstream
  LLM cannot see the problem and the candidate scores 0.
- Do not hardcode a solution to {BENCHMARK_NAME} -- the prompt must stay
  general enough that it is still asking the LLM to do the rewriting.
- The downstream LLM must be told to return a single return statement using
  only the provided DSL functions, with no loops and no intermediate
  variables, or metalift's parser will reject it.
"""


@weave.op()
def _traced_score(prompt: str) -> dict:
    """Actually run metalift synthesis with the evolved prompt and see if it verifies."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, dir=tempfile.gettempdir()
    ) as f:
        f.write(prompt)
        evolved_path = f.name

    try:
        proc = subprocess.run(
            [str(POETRY_BIN), "run", "python", str(BRIDGE_SCRIPT), evolved_path, BENCHMARK_NAME],
            cwd=METALIFT_DIR,
            env=bridge_env(),
            capture_output=True,
            text=True,
            timeout=EVAL_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return {"score": 0.0, "error": f"synthesis attempt exceeded {EVAL_TIMEOUT_SECONDS}s"}
    finally:
        Path(evolved_path).unlink(missing_ok=True)

    result_line = next(
        (line for line in proc.stdout.splitlines() if line.startswith("VERGE_RESULT: ")),
        None,
    )
    if result_line is None:
        return {
            "score": 0.0,
            "error": "bridge produced no VERGE_RESULT line",
            "stdout_tail": proc.stdout[-2000:],
            "stderr_tail": proc.stderr[-2000:],
        }
    return json.loads(result_line[len("VERGE_RESULT: ") :])


def score(prompt: str, _inputs=None) -> dict:
    """LEVI's actual entry point -- delegates to the @weave.op()-traced worker.

    weave.init() must run here, BEFORE _traced_score() is invoked, not
    inside it: @weave.op()'s wrapper checks for an active client at call
    entry, so a client set up from inside the decorated function itself is
    one call too late and that call is never traced. Likewise the flush()
    after the call: this whole subprocess gets killed right after this
    function returns, so the trace has to be fully uploaded before then.
    """
    _ensure_weave_initialized()
    result = _traced_score(prompt)
    if _weave_client is not None:
        _weave_client.flush()
    return result


def main() -> None:
    # No team prefix -- logs to your own default W&B entity. Initializes
    # weave in THIS (main) process so LEVI's own proposer/litellm calls,
    # which run in-process, get traced; each spawned eval subprocess
    # separately initializes its own client (see _ensure_weave_initialized).
    _ensure_weave_initialized()
    print(f"Seed: {SEED_MODE} (VERGE_SEED=strong for metalift's production prompt)")
    print(f"Benchmark: {BENCHMARK_NAME}   Model: {MODEL}\n")
    result = levi.evolve_prompts(
        description,
        evaluator=score,
        seed_prompt=seed_prompt,
        model=MODEL,
        budget_dollars=BUDGET_DOLLARS,
        pipeline=levi.PipelineConfig(eval_timeout=EVAL_TIMEOUT_SECONDS + 30.0),
    )
    print(f"\nBest score: {result.best_score:.4f}")
    print(f"Evaluations: {result.total_evaluations}   Cost: ${result.total_cost:.3f}\n")
    print(result.best_prompt)


if __name__ == "__main__":
    main()
