"""Execution smoke: every tool must survive one real call before it ships.

The eval gate scores tool *selection* — whether the descriptions steer a
fresh model to the right call — and deliberately never executes anything.
That separation once let a server pass its gate 14/14 and still crash on its
first live demo call. This stage closes the gap with the cheapest honest
execution test: call each tool exactly once against the live API, with
arguments taken from the examiner's own cases and retyped to the declared
schema exactly as the eval runner types them. A tool that cannot survive one
real call has no business being scored on its prose.

One call per tool proves the wiring — bindings, chains, headers, default
literals — not payload correctness, which stays a declared scope cut.

Tolerance rule: for single-call tools, an HTTP error *answer* (the generated
server raises Arcade's UPSTREAM_* error kinds, surfaced as typed metadata on
the wire) still counts as wired. The examiner is blind to the API by design,
so identifier arguments in its cases are fabricated, and a live API can only
answer them with 404/400/500 — an answer that already proves the request
formed, sent, and parsed. Chains get no tolerance: their later inputs come
from the live API's own responses, so an upstream error mid-chain indicts
the wiring (a chain's first step can also hit this for fabricated inputs; we
accept the occasional extra revision rather than guess which step answered).
Auth errors always fail — with real credentials loaded, a 401 indicts the
credential plumbing, not the fabricated input.
"""

import asyncio
import os
import sys
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from handyman.ir import EvalSuite, ToolPlan, ToolSpec
from handyman.verify.evals import map_served_names, typed_value

# The generated server allows each HTTP call 30 seconds; a two-call chain
# must land well inside this before the gate declares the tool hung.
_CALL_TIMEOUT = timedelta(seconds=90)


@dataclass
class SmokeOutcome:
    tool: str
    args: dict[str, Any]
    ok: bool
    detail: str = ""


def smoke_check(suite: EvalSuite, plan: ToolPlan, server_path: Path) -> list[SmokeOutcome]:
    """Call every planned tool once over stdio, one outcome per tool.

    A tool no eval case covers is a failure too: it could be neither executed
    here nor selection-scored later, so it would ship completely unverified.
    """
    return asyncio.run(_smoke(suite, plan, server_path))


def smoke_feedback(outcomes: list[SmokeOutcome]) -> str:
    """Failure prose for the design stage's revision attempt."""
    return "\n\n".join(
        f"Tool: {outcome.tool}\ncalled live with {outcome.args}\nproblem: {outcome.detail}"
        for outcome in outcomes
        if not outcome.ok
    )


async def _smoke(suite: EvalSuite, plan: ToolPlan, server_path: Path) -> list[SmokeOutcome]:
    outcomes = []
    params = StdioServerParameters(
        command=sys.executable, args=[str(server_path), "stdio"], env=dict(os.environ)
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        listed = [tool.name for tool in (await session.list_tools()).tools]
        served = map_served_names(plan, listed)
        for tool in plan.tools:
            case = next((c for c in suite.cases if c.expected_tool == tool.name), None)
            if case is None:
                outcomes.append(
                    SmokeOutcome(
                        tool.name, {}, ok=False,
                        detail="no eval case covers this tool, so it was never executed",
                    )
                )
                continue
            types = {arg.name: arg.py_type for arg in tool.args}
            args = {a.name: typed_value(a.value, types.get(a.name)) for a in case.expected_args}
            outcomes.append(await _call(session, served[tool.name], tool, args))
    return outcomes


async def _call(
    session: ClientSession, served_name: str, tool: ToolSpec, args: dict[str, Any]
) -> SmokeOutcome:
    try:
        result = await session.call_tool(served_name, args, read_timeout_seconds=_CALL_TIMEOUT)
    except Exception as error:  # noqa: BLE001 — a crashed call is a finding, not a gate crash
        return SmokeOutcome(tool.name, args, ok=False, detail=f"call failed: {error}")
    if result.isError:
        text = " ".join(
            block.text for block in result.content if getattr(block, "text", "")
        ).strip()
        kind = ((result.meta or {}).get("arcade") or {}).get("errorKind", "")
        if (
            len(tool.steps) == 1
            and kind.startswith("UPSTREAM_")
            and kind != "UPSTREAM_RUNTIME_AUTH_ERROR"
        ):
            return SmokeOutcome(
                tool.name, args, ok=True,
                detail=f"tolerated {kind} — single-call lookup, wiring proven by the API's "
                f"answer to fabricated case input: {text[:200]}",
            )
        return SmokeOutcome(tool.name, args, ok=False, detail=text[:500] or "tool returned an error")
    return SmokeOutcome(tool.name, args, ok=True)
