"""Generator tests: the rendered server must be valid Python, contain the
plumbing the plan calls for, and actually boot as an MCP server."""

import ast
import asyncio
import os
import sys

import pytest

from handyman.generate import GenerationError, generate_artifacts, render_server
from handyman.ir import CallStep, ParamBinding


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


def test_unset_optional_secrets_are_omitted_from_headers(
    fixture_plan, fixture_spec, tmp_path, monkeypatch
):
    """An optional secret with no default must vanish from request headers
    until its variable is set — a None header value crashes httpx at call
    time (found live: NWS_API_KEY took down every tool in the demo)."""
    monkeypatch.delenv("FIXTURE_TENANT", raising=False)
    monkeypatch.delenv("FIXTURE_USER_AGENT", raising=False)
    source = render_server(fixture_plan, fixture_spec, source="tests/fixture")
    # arcade's tool decorator inspects source and module name at registration,
    # so the code needs a real file and a __name__ before it can execute.
    server_file = tmp_path / "server.py"
    server_file.write_text(source)
    namespace: dict = {"__name__": "generated_server_under_test"}
    exec(compile(source, str(server_file), "exec"), namespace)  # noqa: S102
    assert namespace["_DEFAULT_HEADERS"] == {"User-Agent": "fixture-tests (dev@example.com)"}


def test_docs_regen_command_keeps_its_flags(fixture_plan, fixture_spec):
    """The header's regenerate command must be runnable as printed — docs
    ingests need their --docs --base-url flags back."""
    source = render_server(
        fixture_plan, fixture_spec, source="https://docs.example/readme",
        source_arg="https://docs.example/readme --docs --base-url https://api.example.com",
    )
    assert ("Regenerate with: handyman generate https://docs.example/readme "
            "--docs --base-url https://api.example.com") in source
    assert "Source API: https://docs.example/readme\n" in source


def test_dot_paths_index_lists(fixture_plan, fixture_spec, tmp_path):
    """Chained extracts routinely need the first element of a list (found
    live: NWS station discovery is features[0] deep, and _dig treating '0'
    as a dict key broke the chain against the real API)."""
    from arcade_mcp_server.exceptions import FatalToolError

    source = render_server(fixture_plan, fixture_spec, source="tests/fixture")
    server_file = tmp_path / "server.py"
    server_file.write_text(source)
    namespace: dict = {"__name__": "generated_server_under_test"}
    exec(compile(source, str(server_file), "exec"), namespace)  # noqa: S102

    payload = {"features": [{"properties": {"stationIdentifier": "KNYC"}}]}
    assert namespace["_dig"](payload, "features.0.properties.stationIdentifier") == "KNYC"
    assert namespace["_prune"](payload, "features.0.properties") == {"stationIdentifier": "KNYC"}
    # out of range: _dig fails loudly with the walked path, _prune stays lenient
    with pytest.raises(FatalToolError, match="response missing"):
        namespace["_dig"](payload, "features.9.properties")
    assert namespace["_prune"](payload, "features.9") == payload


def test_no_secret_plans_render_without_unused_imports(fixture_plan, fixture_spec):
    """A server with no secrets must not import os — the static gate flags
    unused imports (found live: the keyless Hacker News server)."""
    fixture_plan.secrets = []
    source = render_server(fixture_plan, fixture_spec, source="tests/fixture")
    compile(source, "server.py", "exec")
    assert "import os" not in source


@pytest.mark.parametrize(
    "mutate, message_fragment",
    [
        (lambda plan: plan.tools[0].steps.append(
            CallStep(endpoint_id="does_not_exist")), "unknown endpoint"),
        (lambda plan: plan.tools[1].steps[0].bindings.pop(0), "is not bound"),
        (lambda plan: plan.tools[0].steps[0].bindings.append(
            ParamBinding(param="units", value="{nonsense}")), "unknown name"),
        # a stray quote in a binding — benign typo or crafted splice — must be
        # rejected before it can reach an f-string in generated code
        (lambda plan: plan.tools[0].steps[0].bindings.append(
            ParamBinding(param="units", value='say "{units}"')), "safely embedded"),
        (lambda plan: plan.tools[0].steps[0].bindings.append(
            ParamBinding(param="units", value='{units}" + evil() + "')), "safely embedded"),
        (lambda plan: setattr(plan.tools[0].args[0], "name", "from"), "keyword"),
    ],
)
def test_plan_defects_fail_fast(fixture_plan, fixture_spec, mutate, message_fragment):
    mutate(fixture_plan)
    with pytest.raises(GenerationError, match=message_fragment):
        render_server(fixture_plan, fixture_spec, source="tests/fixture")


def test_constant_bindings_render_as_string_literals(fixture_plan, fixture_spec):
    """A placeholder-free binding is a constant the design chose; it must
    become a quoted literal, never code (and never crash the run)."""
    fixture_plan.tools[0].steps[0].bindings.append(ParamBinding(param="units", value="si"))
    source = render_server(fixture_plan, fixture_spec, source="tests/fixture")
    compile(source, "server.py", "exec")
    assert "params['units'] = 'si'" in source


def test_hostile_module_docstring_text_stays_data(fixture_plan, fixture_spec):
    """Server-level instructions are LLM text placed inside the module
    docstring; a triple quote in them must not escape into module-level code."""
    fixture_plan.instructions = 'Do this. """\nimport os; os.system("boom")\n_ = """'
    source = render_server(fixture_plan, fixture_spec, source="tests/fixture")
    compile(source, "server.py", "exec")
    assert "import os" in ast.get_docstring(ast.parse(source))  # trapped as text


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
    # the mcp SDK has renamed this field across versions; accept either spelling
    tool = by_name["Fixture_GetStationData"]
    schema = getattr(tool, "inputSchema", None) or tool.input_schema
    assert schema["required"] == ["station"]
    assert schema["properties"]["units"]["description"] == "'us' or 'si'"
    # The context parameter is server plumbing, invisible to consuming agents.
    assert "context" not in schema["properties"]
