"""Handyman CLI — the pipeline conductor.

    handyman generate <openapi-url-or-path>            # spec-first ingest
    handyman generate <docs-url> --docs --base-url ... # docs-page ingest
    handyman demo <server-name> [--task "..."]         # use the generated tools

One generate run is: ingest -> design -> generate -> verify, with verify
failures fed back into design for a bounded number of revision attempts. The
loop is the product decision at the heart of the project: a generated server
is not "done" when it renders — it is done when a fresh model, shown only the
served tools, uses them correctly. If the loop cannot converge, the server is
still emitted but loudly marked, with the failing cases written next to the
code.
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from handyman.agent import run_agent
from handyman.design import design_toolplan
from handyman.generate import GenerationError, generate_artifacts
from handyman.ingest import spec_from_docs, spec_from_openapi
from handyman.ir import APISpec, ToolPlan
from handyman.verify import author_eval_suite, boot_check, run_eval_suite, static_check


def main() -> None:
    load_dotenv()  # ANTHROPIC_API_KEY, NWS_USER_AGENT, HOME_LAT/LON from ./.env
    args = _parse_args()
    if args.command == "generate":
        run_pipeline(args)
    elif args.command == "demo":
        run_demo(args)


def run_demo(args: argparse.Namespace) -> None:
    server_path = Path(args.server)
    if server_path.suffix != ".py":
        server_path = Path("generated") / args.server / "server.py"
    if not server_path.exists():
        sys.exit(f"no server at {server_path} — run `handyman generate` first")
    task = args.task or _default_task(server_path.parent.name)

    _stage("DEMO", str(server_path))
    _say(f"task: {task}")
    answer = asyncio.run(run_agent(server_path, task))
    print(f"\n{answer}\n")


def _default_task(server_name: str) -> str:
    """Canned demo tasks for the two servers this take-home ships; anything
    else needs an explicit --task."""
    home = f"latitude {os.environ.get('HOME_LAT', '47.6062')}, " \
           f"longitude {os.environ.get('HOME_LON', '-122.3321')}"
    defaults = {
        "nws": (
            f"I'm planning a bike ride on Saturday morning near home ({home}). "
            "Will the weather cooperate, and are there any active weather alerts "
            "I should worry about?"
        ),
        "hackernews": (
            "Give me a morning digest of Hacker News right now: pick the 3-4 "
            "stories most worth my time as an AI infrastructure engineer, with "
            "one line each on why, including points and comment counts."
        ),
    }
    if server_name not in defaults:
        sys.exit(f"no default task for '{server_name}'; pass --task")
    return defaults[server_name]


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
    parser = argparse.ArgumentParser(prog="handyman", description=__doc__)
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

    demo = commands.add_parser("demo", help="Run the consumer agent against a generated server")
    demo.add_argument("server", help="Server name under generated/, or a path to a server.py")
    demo.add_argument("--task", help="What to ask (defaults exist for nws and hackernews)")
    return parser.parse_args()


def _stage(name: str, detail: str = "") -> None:
    print(f"\n=== {name} ═ {detail}" if detail else f"\n=== {name}")


def _say(text: str) -> None:
    print(f"    {text}")


if __name__ == "__main__":
    main()
