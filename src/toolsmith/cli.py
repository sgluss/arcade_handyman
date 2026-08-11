"""Toolsmith CLI — the pipeline conductor.

    toolsmith generate <openapi-url-or-path>            # spec-first ingest
    toolsmith generate <docs-url> --docs --base-url ... # docs-page ingest

One run is: ingest -> design -> generate -> verify, with verify failures fed
back into design for a bounded number of revision attempts. The loop is the
product decision at the heart of the project: a generated server is not
"done" when it renders — it is done when a fresh model, shown only the served
tools, uses them correctly. If the loop cannot converge, the server is still
emitted but loudly marked, with the failing cases written next to the code.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from toolsmith.design import design_toolplan
from toolsmith.generate import GenerationError, generate_artifacts
from toolsmith.ingest import spec_from_docs, spec_from_openapi
from toolsmith.ir import APISpec, ToolPlan
from toolsmith.verify import author_eval_suite, boot_check, run_eval_suite, static_check


def main() -> None:
    args = _parse_args()
    if args.command == "generate":
        run_pipeline(args)


def run_pipeline(args: argparse.Namespace) -> Path:
    _stage("INGEST", args.source)
    spec = _ingest(args)
    auth = ", ".join(f"{a.kind}:{a.name}" for a in spec.auth) or "none"
    _say(f"{spec.name} — {len(spec.endpoints)} endpoints, auth: {auth}")

    feedback: str | None = None
    for attempt in range(1, args.attempts + 1):
        _stage("DESIGN", f"attempt {attempt}/{args.attempts}" + (" (revision)" if feedback else ""))
        plan = design_toolplan(spec, guidance=args.guidance, feedback=feedback)
        _describe_plan(plan)

        _stage("GENERATE", plan.server_name)
        out_dir = Path(args.out) / plan.server_name
        try:
            server_path = generate_artifacts(plan, spec, out_dir, source=args.source)
        except GenerationError as error:
            feedback = f"The plan failed deterministic validation: {error}"
            _say(f"✗ {feedback}")
            continue

        problems = static_check(server_path)
        if problems:
            feedback = "The generated server failed static checks: " + "; ".join(problems)
            _say(f"✗ {feedback}")
            continue
        served = boot_check(server_path)
        _say(f"✓ {server_path} boots and serves {len(served)} tools")

        if args.skip_evals:
            _say("eval gate skipped (--skip-evals)")
            return server_path

        _stage("VERIFY", "authoring eval cases from the consumer surface")
        suite = author_eval_suite(plan)
        _say(f"{len(suite.cases)} cases; scoring tool selection against the live server")
        report = asyncio.run(run_eval_suite(suite, plan, server_path))
        _write_eval_artifacts(out_dir, suite, report)
        for outcome in report.outcomes:
            mark = "✓" if outcome.passed else ("~" if outcome.warning else "✗")
            _say(f"  {mark} [{outcome.score:.2f}] {outcome.name}")
        _say(report.summary)

        if not report.failures:
            _stage("DONE", f"{server_path} passed its eval gate")
            return server_path
        feedback = report.feedback()

    _stage("WARNING", "emitted WITHOUT a passing eval gate — see evals.json for failing cases")
    return server_path


def _ingest(args: argparse.Namespace) -> APISpec:
    if args.docs:
        if not args.base_url:
            sys.exit("--docs ingestion requires --base-url (docs pages rarely state it reliably)")
        return spec_from_docs(args.source, base_url=args.base_url)
    return spec_from_openapi(args.source)


def _describe_plan(plan: ToolPlan) -> None:
    _say(f"{plan.display_name}: {len(plan.tools)} tools designed")
    for tool in plan.tools:
        chained = f" ({len(tool.steps)}-call chain)" if len(tool.steps) > 1 else ""
        _say(f"  • {tool.name}{chained} — {tool.returns}")
    for secret in plan.secrets:
        kind = "required" if secret.required else f"optional, default {secret.default!r}"
        _say(f"  ○ secret {secret.env_var} -> {secret.param_name} ({kind})")
    if plan.rejected:
        examples = "; ".join(f"{r.endpoint_id}: {r.reason}" for r in plan.rejected[:3])
        _say(f"  ⊘ rejected {len(plan.rejected)} endpoints, e.g. {examples}")


def _write_eval_artifacts(out_dir: Path, suite, report) -> None:
    """Keep the gate's evidence next to the code it judged."""
    (out_dir / "evals.json").write_text(
        json.dumps(
            {
                "cases": suite.model_dump()["cases"],
                "outcomes": [vars(outcome) for outcome in report.outcomes],
                "summary": report.summary,
            },
            indent=2,
        )
        + "\n"
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="toolsmith", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    generate = commands.add_parser("generate", help="Generate an MCP server from an API")
    generate.add_argument("source", help="OpenAPI URL/path, or a docs-page URL with --docs")
    generate.add_argument("--docs", action="store_true",
                          help="Treat source as human-written docs (LLM extraction)")
    generate.add_argument("--base-url", help="API base URL (required with --docs)")
    generate.add_argument("--guidance", help="Operator hint for tool selection")
    generate.add_argument("--out", default="generated", help="Output root (default: generated/)")
    generate.add_argument("--attempts", type=int, default=3,
                          help="Design attempts before emitting with a warning (default: 3)")
    generate.add_argument("--skip-evals", action="store_true",
                          help="Skip the eval gate (static checks still run)")
    return parser.parse_args()


def _stage(name: str, detail: str = "") -> None:
    print(f"\n=== {name} ═ {detail}" if detail else f"\n=== {name}")


def _say(text: str) -> None:
    print(f"    {text}")


if __name__ == "__main__":
    main()
