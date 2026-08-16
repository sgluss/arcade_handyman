"""Verify stage: the gate between "generated" and "shipped".

Three checks, cheapest first, each attributing failures to the right place:

    static.py   the file is sound Python and boots as an MCP server
    evals.py    a fresh LLM, shown only the served tools, picks the right tool
                with the right arguments for realistic asks (Arcade's evals
                framework, run against the live generated server)

Failures come back as prose written for the design stage, which gets a
bounded number of attempts to revise the plan. A server that cannot pass its
own evals is not published silently — the pipeline either converges or emits
with a loud warning and the failing cases attached.
"""

from handyman.verify.evals import EvalReport, author_eval_suite, run_eval_suite
from handyman.verify.static import boot_check, static_check

__all__ = ["EvalReport", "author_eval_suite", "boot_check", "run_eval_suite", "static_check"]
