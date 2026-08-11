"""Generator tests: the rendered server must be valid Python, contain the
plumbing the plan calls for, and actually boot as an MCP server."""

import asyncio
import os
import sys

import pytest

from toolsmith.generate import GenerationError, generate_artifacts, render_server
from toolsmith.ir import CallStep, ParamBinding


def test_rendered_server_is_valid_python(fixture_plan, fixture_spec):
    source = render_server(fixture_plan, fixture_spec, source="tests/fixture")
    compile(source, "server.py", "exec")


def test_rendered_server_wires_the_plan(fixture_plan, fixture_spec):
    source = render_server(fixture_plan, fixture_spec, source="tests/fixture")

    # Required secret uses Arcade's native mechanism; optional one defaults from env.
    assert "requires_secrets=['FIXTURE_API_KEY']" in source
    assert "context.get_secret('FIXTURE_API_KEY')" in source
    assert "os.environ.get('FIXTURE_USER_AGENT', 'fixture-tests (dev@example.com)')" in source

    # Optional argument bindings are guarded, so unset args are never sent.
    assert "if tags is not None:" in source

    # The chain hides the lookup: strict extract feeds the second call's path.
    assert "_dig(response_1, 'properties.stationId')" in source
    assert 'f"/stations/{station_id}/data"' in source

    # Final payloads are pruned best-effort.
    assert "_prune(" in source


@pytest.mark.parametrize(
    "mutate, message_fragment",
    [
        (lambda plan: plan.tools[0].steps.append(
            CallStep(endpoint_id="does_not_exist")), "unknown endpoint"),
        (lambda plan: plan.tools[1].steps[0].bindings.pop(0), "is not bound"),
        (lambda plan: plan.tools[0].steps[0].bindings.append(
            ParamBinding(param="units", value="{nonsense}")), "unknown name"),
    ],
)
def test_plan_defects_fail_fast(fixture_plan, fixture_spec, mutate, message_fragment):
    mutate(fixture_plan)
    with pytest.raises(GenerationError, match=message_fragment):
        render_server(fixture_plan, fixture_spec, source="tests/fixture")


def test_generated_server_boots_and_lists_tools(fixture_plan, fixture_spec, tmp_path):
    """Boot the generated file as a real MCP server over stdio and confirm both
    tools are served with schemas derived from the annotated signatures."""
    server_path = generate_artifacts(fixture_plan, fixture_spec, tmp_path, source="tests/fixture")

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def list_tools():
        params = StdioServerParameters(
            command=sys.executable, args=[str(server_path), "stdio"], env=dict(os.environ)
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            return (await session.list_tools()).tools

    tools = asyncio.run(list_tools())
    by_name = {tool.name: tool for tool in tools}

    assert set(by_name) == {"Fixture_GetStationData", "Fixture_GetDataForPoint"}
    schema = by_name["Fixture_GetStationData"].input_schema
    assert schema["required"] == ["station"]
    assert schema["properties"]["units"]["description"] == "'us' or 'si'"
    # The context parameter is server plumbing, invisible to consuming agents.
    assert "context" not in schema["properties"]
