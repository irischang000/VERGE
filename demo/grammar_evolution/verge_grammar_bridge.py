"""VERGE bridge: run one metalift synthesis attempt using an evolved
`inv_grammar` implementation for the list_abs_sum benchmark, and report
whether it verified.

Invoked as a subprocess (via `poetry run python verge_grammar_bridge.py
<evolved_inv_grammar.py>`) from LEVI's score_fn, which runs in a separate
Python 3.11 environment without metalift's own dependencies installed.
Communication happens over a single "VERGE_RESULT: {json}" line on stdout.

Unlike the prompt-evolution demo, this evolves actual Python source (the
grammar function itself, using choose() combinators) rather than free text,
so it uses levi.evolve_code on the LEVI side, not evolve_prompts.

target_lang and ps_grammar are held fixed -- only inv_grammar is evolved,
matching the README's "weak seed grammar" premise: the DSL and the
postcondition are known, only the loop-invariant search space is unknown.
"""

import io
import json
import runpy
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path
from typing import List

from metalift.frontend.python import Driver
from metalift.ir import Bool, Int, List as mlList, Object, call, fn_decl_recursive, ite
from metalift.synthesis_common import SynthesisFailed
from tests.python.utils.utils import codegen

LIST_ABS_SUM_FN_NAME = "list_abs_sum"


def target_lang():
    lst = mlList(Int, "lst")
    list_abs_sum = fn_decl_recursive(
        LIST_ABS_SUM_FN_NAME,
        Int,
        ite(
            lst.len() >= 1,
            ite(lst[0] < 0, Int(0) - lst[0], lst[0])
            + call(LIST_ABS_SUM_FN_NAME, Int, lst[1:]),
            Int(0),
        ),
        lst,
    )
    return [list_abs_sum]


def ps_grammar(writes: List[Object], reads: List[Object], in_scope: List[Object], relaxed: bool) -> Bool:
    ret_val = writes[0]
    lst = reads[0]
    lst_sum = call(LIST_ABS_SUM_FN_NAME, Int, lst)
    return ret_val == lst_sum


def _run_with_grammar(evolved_inv_grammar) -> None:
    driver = Driver()
    test = driver.analyze(
        filepath="tests/python/list_abs_sum.py",
        fn_name="test",
        target_lang_fn=target_lang,
        inv_grammar=evolved_inv_grammar,
        ps_grammar=ps_grammar,
    )

    lst = mlList(Int, "lst")
    driver.add_var_object(lst)
    test(lst)
    driver.synthesize()


def main() -> None:
    if len(sys.argv) < 2:
        print(json.dumps({"score": 0.0, "error": "usage: verge_grammar_bridge.py <evolved_inv_grammar.py>"}))
        sys.exit(1)

    evolved_path = sys.argv[1]
    captured = io.StringIO()
    start = time.perf_counter()
    error = None
    synthesis_failed = False
    evolved_inv_grammar = None
    try:
        # LEVI validates each candidate by exec()'ing it against a
        # permissive stub metalift package (see run.py's _STUB_DIR) that
        # accepts any imported name -- it never actually calls the function
        # there. A candidate that imports a name which exists in the stub
        # but not in *this*, the real metalift.ir/vc_util, only surfaces
        # that here, at module-load time, before the function is even
        # extracted. Loading has to be inside the same try/except as the
        # synthesis attempt, or exactly this class of error crashes with no
        # VERGE_RESULT line at all -- silently discarded by LEVI rather
        # than scored, exactly like the bug already fixed once in
        # demo/prompt_evolution's score_fn.
        evolved_ns = runpy.run_path(evolved_path)
        evolved_inv_grammar = evolved_ns.get("inv_grammar")
        if evolved_inv_grammar is None:
            error = "evolved file has no inv_grammar function"
        else:
            with redirect_stdout(captured):
                _run_with_grammar(evolved_inv_grammar)
    except SynthesisFailed:
        # The grammar's search space contains no candidate that satisfies
        # the verification condition -- a legitimate 0.0 for a weak
        # grammar, not an infrastructure error.
        synthesis_failed = True
    except Exception as e:  # noqa: BLE001
        error = f"{type(e).__name__}: {e}".strip().rstrip(":").strip() or repr(e)

    elapsed = time.perf_counter() - start
    log = captured.getvalue()
    print(log)

    verified = "Verification Output: unsat" in log
    round_count = log.count("====== verification of round")

    feedback_parts = []
    if error is not None:
        # A real error (bad import, undefined name, wrong signature, ...) --
        # score it like any other failure rather than routing it through
        # LEVI's "error" key, which discards the result outright instead of
        # recording it in the archive. This is metalift's real Python 3.10
        # environment, so an error here (e.g. "cannot import name X from
        # metalift.ir") means the candidate referenced something that
        # doesn't actually exist, even if it passed LEVI's own permissive
        # stub-backed validation.
        result = {
            "score": 0.0,
            "verified": False,
            "seconds": elapsed,
            "feedback_per_example": [
                f"FAILED with a real error, not just an unverified grammar: {error}. "
                f"This means the code referenced a name, import, or signature that "
                f"doesn't exist in metalift's actual ir/vc_util modules, or crashed "
                f"for some other reason unrelated to the grammar's correctness -- "
                f"fix the code itself, not just the logic."
            ],
            "per_example_scores": [0.0],
        }
    elif synthesis_failed or not verified:
        feedback_parts.append(
            "FAILED: no candidate in this grammar's search space satisfied the "
            "verification condition after exhausting the synthesis rounds. The "
            "grammar needs to express: an index bound on the loop variable, and "
            "an inductive relationship between the accumulated `sum` and "
            "`list_abs_sum` of the remaining (unprocessed) slice of `lst`. Check "
            "whether the `choose(...)` options for the slice offset and the "
            "comparison operators actually cover the exact relationship needed -- "
            "a grammar that's missing even one required literal or operator will "
            "fail exactly like this, with no partial credit."
        )
        result = {
            "score": 0.0,
            "verified": False,
            "seconds": elapsed,
            "rounds": round_count,
            "feedback_per_example": [" ".join(feedback_parts)],
            "per_example_scores": [0.0],
        }
    else:
        # Fewer synthesis rounds and less wall-clock time both indicate a
        # tighter, more targeted grammar (less irrelevant search space for
        # Rosette to explore) rather than a lucky broad one.
        round_penalty = min(0.4, 0.1 * max(0, round_count - 1))
        time_penalty = min(0.1, elapsed / 300.0)
        score = max(0.5, 1.0 - round_penalty - time_penalty)
        feedback_parts.append(
            f"VERIFIED in {round_count} round(s), {elapsed:.1f}s. Fewer rounds and "
            f"less time scores higher -- prefer a tighter, more targeted grammar "
            f"over a broader one that happens to also work."
        )
        result = {
            "score": score,
            "verified": True,
            "seconds": elapsed,
            "rounds": round_count,
            "feedback_per_example": [" ".join(feedback_parts)],
            "per_example_scores": [score],
        }

    print("VERGE_RESULT: " + json.dumps(result))


if __name__ == "__main__":
    main()
