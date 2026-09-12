"""VERGE bridge: run one metalift synthesis attempt using an evolved prompt
template, and report whether it verified.

This is invoked as a subprocess (via `poetry run python verge_prompt_bridge.py
<evolved_prompt.txt> [benchmark_name]`) from LEVI's evaluator, which runs in a
separate Python 3.11 environment that doesn't have metalift's own
dependencies installed. Communication happens over a single JSON line on
stdout, prefixed with "VERGE_RESULT: " so it can be picked out from
metalift's own progress logging (which we let through unfiltered, for
debugging).

The evolved artifact is raw prompt *text* (via levi.evolve_prompts), not
Python source -- an earlier version evolved a `get_ps_prompt` function via
evolve_code, but cheap models failed to emit syntactically valid Python
containing multi-line prompt text roughly 100% of the time. The template
carries DSL_CODE_TOKEN / SOURCE_CODE_TOKEN markers which are substituted by
plain string replacement (not str.format, which would choke on the stray
braces that appear throughout C++ source).

Currently only the `normal_blend_8` benchmark is wired up.
"""

import io
import json
import sys
import time
import traceback
from contextlib import redirect_stdout
from pathlib import Path

DSL_CODE_TOKEN = "<<<DSL_CODE>>>"
SOURCE_CODE_TOKEN = "<<<SOURCE_CODE>>>"

# metalift raises Exception(this) when synthesis runs out of retries without
# verifying. It means "weak prompt", not "broken plumbing".
SYNTHESIS_FAILED_MESSAGE = "No correct solution found"

import llmlift_scripts.synthesis as synthesis_mod
from llmlift_scripts.synthesis import LLMModel, VerificationMethod
from llmlift_scripts.utils import SingleLoopInfo, get_inv_args, replace_args
from metalift.frontend.llvm import Driver, InvGrammar
from metalift.ir import Int, List
from metalift.utils.tenspiler.constants import (
    TENSPILER_FN_NAME_TO_AXIOMS,
    TENSPILER_FNS,
)

SUPPORTED_BENCHMARKS = ("normal_blend_8",)


def _run_normal_blend_8() -> None:
    driver = Driver()
    loop_info = SingleLoopInfo(
        loop_var=Int("i"),
        modified_vars=[List(Int, "out")],
        read_vars=[List(Int, "base"), List(Int, "active"), Int("opacity")],
    )
    output_var = List(Int, "out")

    inv_args = replace_args(
        args=get_inv_args(loop_info), replace_args={"out": "agg.result"}
    )
    normal_blend_8 = driver.analyze(
        "benchmarks/blend/cpp/normal_blend_8.ll",
        "benchmarks/blend/cpp/normal_blend_8.loops",
        "normal_blend_8",
        target_lang_fn=[],
        inv_grammars={"normal_blend_8_inv0": InvGrammar(None, [], inv_args)},
        ps_grammar=None,
    )

    base_var = List(Int, "base")
    active_var = List(Int, "active")
    opacity_var = Int("opacity")
    driver.add_var_objects([base_var, active_var, opacity_var])
    driver.add_precondition(base_var.len() == active_var.len())
    driver.add_precondition(base_var.len() > 0)

    normal_blend_8(base_var, active_var, opacity_var)

    input_code = Path("benchmarks/blend/cpp/normal_blend_8.cc").read_text()

    synthesis_mod.run_llm_synthesis_algorithm(
        driver=driver,
        loop_info=loop_info,
        output_var=output_var,
        source_code=input_code,
        benchmark_name="normal_blend_8",
        llm_model=LLMModel.GPT,
        dsl_fns=TENSPILER_FNS,
        dsl_fn_name_to_axioms=TENSPILER_FN_NAME_TO_AXIOMS,
        max_num_ps_sols=3,
        max_num_inv_sols=3,
        verification_method=VerificationMethod.ROSETTE,
    )


def _build_feedback(
    *,
    log: str,
    solved: bool,
    ps_iterations: int,
    inv_iterations: int,
    parser_rejections: int,
    benchmark_name: str = "",
    wrong_shape: bool = False,
) -> str:
    """Describe *why* this prompt did or didn't work, in terms the proposer can act on.

    LEVI samples these strings into its mutation prompt (see
    `_extract_failure_feedback` in levi/pipeline/producer.py), and its template
    tells the proposer to bake the lessons into the next candidate. Without
    this it mutates blind, knowing only a scalar score.
    """
    parts: list[str] = []

    # metalift prints "Failed to pass the parser <exception>" -- the exception
    # text is the single most actionable signal we have, since it says exactly
    # what shape of output the DSL parser refused.
    parser_errors: list[str] = []
    for line in log.splitlines():
        marker = "Failed to pass the parser"
        if marker in line:
            detail = line.split(marker, 1)[1].strip()
            if detail and detail not in parser_errors:
                parser_errors.append(detail)

    if solved:
        parts.append(
            f"VERIFIED. metalift synthesized and verified a rewrite using "
            f"{ps_iterations} rewrite attempt(s) and {inv_iterations} invariant attempt(s). "
            f"Fewer attempts scores higher, so make the instructions more precise and "
            f"unambiguous rather than longer."
        )
    elif wrong_shape:
        named = f"`{benchmark_name}`" if benchmark_name else "the C++ function"
        parts.append(
            f"FAILED: the generated rewrite parsed, but metalift could not find a "
            f"synthesized function matching the benchmark. The Python function must be "
            f"named exactly {named} -- mirroring the name and parameters of the C++ "
            f"function shown in the prompt -- and the prompt must ask for a complete "
            f"function definition (signature plus body), not a bare expression, lambda, "
            f"or a differently-named helper."
        )
    elif ps_iterations == 0:
        parts.append(
            "FAILED before any rewrite was attempted. The prompt likely omitted the "
            "DSL definitions or the C++ function under test."
        )
    else:
        parts.append(
            f"FAILED: after {ps_iterations} rewrite attempt(s) and {inv_iterations} "
            f"invariant attempt(s), no candidate passed Rosette verification."
        )

    if parser_errors:
        joined = " | ".join(parser_errors[:3])
        parts.append(
            f"metalift's parser rejected {parser_rejections} generated rewrite(s). "
            f"Parser errors: {joined}. The prompt must make the LLM emit a single "
            f"return statement built only from the provided DSL functions, with no "
            f"loops and no intermediate variables."
        )
    elif not solved and inv_iterations > 0:
        parts.append(
            "The rewrite parsed successfully but the loop invariant never verified. "
            "Emphasise that the rewrite must be semantically equivalent to the C++ "
            "function for every input, not merely plausible-looking."
        )

    return " ".join(parts)


def emit(result: dict) -> None:
    """Every exit path must emit this prefixed line -- it's the only thing the
    caller parses out of stdout."""
    print("VERGE_RESULT: " + json.dumps(result))


def main() -> None:
    if len(sys.argv) < 2:
        emit({"score": 0.0, "error": "usage: verge_prompt_bridge.py <evolved_prompt.txt> [benchmark]"})
        sys.exit(1)

    evolved_path = sys.argv[1]
    benchmark_name = sys.argv[2] if len(sys.argv) > 2 else "normal_blend_8"
    if benchmark_name not in SUPPORTED_BENCHMARKS:
        emit({"score": 0.0, "error": f"unsupported benchmark {benchmark_name!r}"})
        sys.exit(1)

    template = Path(evolved_path).read_text()
    missing = [
        token
        for token in (DSL_CODE_TOKEN, SOURCE_CODE_TOKEN)
        if token not in template
    ]
    if missing:
        emit(
            {
                "score": 0.0,
                "error": f"evolved prompt is missing required token(s): {', '.join(missing)}",
            }
        )
        sys.exit(0)

    def evolved_get_ps_prompt(*, dsl_code: str, source_code: str) -> str:
        return template.replace(DSL_CODE_TOKEN, dsl_code).replace(
            SOURCE_CODE_TOKEN, source_code
        )

    original_get_ps_prompt = synthesis_mod.get_ps_prompt
    synthesis_mod.get_ps_prompt = evolved_get_ps_prompt

    captured = io.StringIO()
    start = time.perf_counter()
    error = None
    wrong_shape = False
    try:
        with redirect_stdout(captured):
            _run_normal_blend_8()
    except StopIteration:
        # metalift does `next(fn for fn in ps_fn_decls if "ps" in fn.name())`
        # with no default. It only tags a function `<benchmark>_ps` when the
        # generated function is named exactly after the benchmark, so output
        # that parses but is named anything else blows up here. That's a
        # prompt-quality failure (score 0.0 with feedback), not broken
        # plumbing -- a weak prompt hits it routinely.
        wrong_shape = True
    except Exception as e:  # noqa: BLE001
        # metalift signals "synthesis exhausted its retries without verifying"
        # by raising this. That's a legitimate score of 0.0 for a weak prompt,
        # NOT an infrastructure failure -- it has to stay out of the "error"
        # channel, because LEVI discards any result carrying an "error" key
        # and the archive would never accumulate the negative examples that
        # drive evolution.
        if str(e).strip() != SYNTHESIS_FAILED_MESSAGE:
            # Anything else is a real error. Never emit an empty message --
            # some exceptions stringify to "", which surfaces as a blank
            # "Eval failed:" with no way to diagnose it.
            error = f"{type(e).__name__}: {e}".strip().rstrip(":").strip()
            if not str(e).strip():
                error = traceback.format_exc()[-2000:]
    finally:
        synthesis_mod.get_ps_prompt = original_get_ps_prompt

    elapsed = time.perf_counter() - start
    log = captured.getvalue()
    print(log)  # surface metalift's own progress output for debugging

    solved = "Solution verified" in log
    ps_iterations = log.count("===== Starting iteration")
    inv_iterations = log.count("----- Generating")
    parser_rejections = log.count("Failed to pass the parser")

    feedback = _build_feedback(
        log=log,
        solved=solved,
        ps_iterations=ps_iterations,
        inv_iterations=inv_iterations,
        parser_rejections=parser_rejections,
        benchmark_name=benchmark_name,
        wrong_shape=wrong_shape,
    )

    if error is not None:
        result = {"score": 0.0, "solved": False, "seconds": elapsed, "error": error}
    elif solved:
        # Scoring has to discriminate between prompts that all "work", or
        # evolution has nothing to climb. An earlier version scored purely on
        # PS-attempt count, which (with max_num_ps_sols=3) collapsed to just
        # {0.97, 0.94, 0.91, 0.0} -- and the seed already sat at the ceiling.
        #
        # LLM round-trips are the real cost driver, so total calls (PS +
        # invariant) is the primary signal, with parser rejections penalised
        # separately: a prompt whose output metalift can't even parse is worse
        # than one that parses but doesn't verify, even at equal call counts.
        # Wall-clock breaks remaining ties continuously.
        total_calls = ps_iterations + inv_iterations
        call_penalty = min(0.30, 0.03 * max(0, total_calls - 2))
        parser_penalty = min(0.15, 0.05 * parser_rejections)
        time_penalty = min(0.05, elapsed / 2000.0)
        score = max(0.5, 1.0 - call_penalty - parser_penalty - time_penalty)
        result = {
            "score": score,
            "solved": True,
            "seconds": elapsed,
            "ps_iterations": ps_iterations,
            "inv_iterations": inv_iterations,
            "parser_rejections": parser_rejections,
            # Key names are load-bearing: LEVI's _extract_failure_feedback
            # only picks up feedback for entries scoring < 1.0.
            "per_example_scores": [score],
            "feedback_per_example": [feedback],
        }
    else:
        result = {
            "score": 0.0,
            "solved": False,
            "seconds": elapsed,
            "ps_iterations": ps_iterations,
            "inv_iterations": inv_iterations,
            "parser_rejections": parser_rejections,
            "per_example_scores": [0.0],
            "feedback_per_example": [feedback],
        }

    emit(result)


if __name__ == "__main__":
    main()
