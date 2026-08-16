"""Eval gate: score the generated tools with Arcade's evals framework.

Two separated roles, deliberately:

The *examiner* (an LLM) writes evaluation cases while seeing only what a
consuming agent would see — tool names, descriptions, and argument docs. It
never sees the design rationale or the API inventory, so it cannot write
softball cases that only match the designer's private phrasing.

The *runner* is Arcade's `EvalSuite` pointed at the live generated server
over stdio: for each case a fresh model is given the served tools and the
user message, and its tool selection and arguments are scored by critics
(exact fields by `BinaryCritic`, free-text fields by `SimilarityCritic`)
against the examiner's expectations. This measures the thing that actually
determines tool quality in production — whether the descriptions steer a
model correctly — not whether the code runs.
"""

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from anthropic import AsyncAnthropic, AsyncAnthropicBedrock
from arcade_evals import BinaryCritic, ExpectedMCPToolCall, SimilarityCritic
from arcade_evals import EvalSuite as ArcadeEvalSuite
from openai import AsyncOpenAI

from handyman.ir import EvalSuite, ToolPlan
from handyman.llm import model_name, parse

EXAMINER_SYSTEM = """\
You are an evaluation author for MCP tool servers. You see only the consumer
surface of a server — tool names, descriptions, and argument documentation —
exactly as a calling agent would. Write selection-accuracy cases for it.

Rules for good cases:

- Write user messages a real person would send. Never mention tool or argument
  names; the message must justify every expected argument value (coordinates,
  IDs, counts must appear in or follow from the message).
- Cover every tool at least once, and include at least one case whose phrasing
  sits near the boundary between two tools, so weak descriptions fail.
- Expect only arguments a model must supply; leave defaulted arguments out
  unless the message overrides them.
- Mark an expected argument `exact` when only one value is correct
  (identifiers, enums, numbers). Mark it `similar` when phrasing may vary
  (free-text queries).
"""


def author_eval_suite(plan: ToolPlan, cases_per_tool: int = 2) -> EvalSuite:
    """Have the examiner write cases from the plan's consumer-visible surface."""
    return parse(
        f"Write about {cases_per_tool} cases per tool for this MCP server.\n\n"
        + _consumer_surface(plan),
        EvalSuite,
        system=EXAMINER_SYSTEM,
    )


def _consumer_surface(plan: ToolPlan) -> str:
    """Exactly what a consuming agent sees; nothing about how tools work inside."""
    lines = [f"Server: {plan.display_name} — {plan.instructions}", ""]
    for tool in plan.tools:
        lines.append(f"## {tool.name}")
        lines.append(tool.description)
        for arg in tool.args:
            optionality = "required" if arg.required else f"optional, default {arg.default}"
            lines.append(f"- {arg.name} ({arg.py_type}, {optionality}): {arg.description}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Running a suite against the live server
# ---------------------------------------------------------------------------


@dataclass
class CaseOutcome:
    name: str
    passed: bool
    warning: bool
    score: float
    detail: str = ""


@dataclass
class EvalReport:
    outcomes: list[CaseOutcome] = field(default_factory=list)

    @property
    def failures(self) -> list[CaseOutcome]:
        return [outcome for outcome in self.outcomes if not (outcome.passed or outcome.warning)]

    @property
    def summary(self) -> str:
        passed = sum(1 for o in self.outcomes if o.passed)
        warned = sum(1 for o in self.outcomes if o.warning)
        return f"{passed}/{len(self.outcomes)} passed, {warned} warnings, {len(self.failures)} failed"

    def feedback(self) -> str:
        """Failure prose for the design stage's revision attempt."""
        return "\n\n".join(f"Case: {o.name}\n{o.detail}" for o in self.failures)


async def run_eval_suite(suite: EvalSuite, plan: ToolPlan, server_path: Path) -> EvalReport:
    """Score the suite against the live server with Arcade's framework."""
    arcade_suite = ArcadeEvalSuite(
        name=plan.display_name,
        system_message=plan.instructions,
        max_concurrent=4,
    )
    await arcade_suite.add_mcp_stdio_server([sys.executable, str(server_path), "stdio"])
    served = _map_served_names(plan, arcade_suite.list_tool_names())

    for case in suite.cases:
        exact = {a.name: a.value for a in case.expected_args if a.match == "exact"}
        similar = {a.name: a.value for a in case.expected_args if a.match == "similar"}
        weight = 1.0 / max(len(exact) + len(similar), 1)
        arcade_suite.add_case(
            name=case.user_message[:60],
            user_message=case.user_message,
            expected_tool_calls=[
                ExpectedMCPToolCall(served[case.expected_tool], args={**exact, **similar})
            ],
            critics=[BinaryCritic(critic_field=name, weight=weight) for name in exact]
            + [SimilarityCritic(critic_field=name, weight=weight) for name in similar],
        )

    client, model_id, provider = _gate_runner()
    results = await arcade_suite.run(client, model=model_id, provider=provider)
    return _report(results)


def _gate_runner() -> tuple[Any, str, str]:
    """Map the pipeline's model string onto arcade_evals' runner interface.

    arcade_evals drives a provider SDK client itself (openai or anthropic
    Messages API), so it cannot take a pydantic-ai model. Bedrock-hosted
    Claude runs through the anthropic SDK's Bedrock client — the same
    Messages API over AWS transport — which arcade_evals accepts unchanged
    because it only duck-types `client.messages.create`.
    """
    name = model_name()
    provider, _, model_id = name.partition(":")
    if provider == "bedrock" and "anthropic" in model_id:
        return AsyncAnthropicBedrock(), model_id, "anthropic"
    if provider == "anthropic":
        return AsyncAnthropic(), model_id, "anthropic"
    if provider == "openai":
        return AsyncOpenAI(), model_id, "openai"
    raise RuntimeError(
        f"the eval gate cannot run on {name!r}; it needs a bedrock-hosted Claude, "
        "anthropic, or openai model"
    )


def _map_served_names(plan: ToolPlan, served_names: list[str]) -> dict[str, str]:
    """Match design-time names to arcade-mcp's namespaced served names
    (get_forecast -> Nws_GetForecast) without hardcoding the convention."""
    mapping = {}
    for tool in plan.tools:
        wanted = _normalize(tool.name)
        matches = [name for name in served_names if _normalize(name).endswith(wanted)]
        if len(matches) != 1:
            raise RuntimeError(
                f"cannot map tool '{tool.name}' to a served tool: candidates {matches} "
                f"from {served_names}"
            )
        mapping[tool.name] = matches[0]
    return mapping


def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _report(results: dict) -> EvalReport:
    report = EvalReport()
    for case in results.get("cases", []):
        evaluation = case["evaluation"]
        detail = ""
        if not evaluation.passed:
            expected = case.get("expected_tool_calls")
            predicted = case.get("predicted_tool_calls")
            detail = (
                f"user message: {case.get('input')}\n"
                f"expected: {expected}\npredicted: {predicted}\n"
                f"reason: {evaluation.failure_reason or 'score below threshold'}"
            )
        report.outcomes.append(
            CaseOutcome(
                name=case.get("name", "?"),
                passed=evaluation.passed,
                warning=evaluation.warning,
                score=evaluation.score,
                detail=detail,
            )
        )
    return report
