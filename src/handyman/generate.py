"""Generate stage: ToolPlan + APISpec -> a runnable arcade-mcp server.

Deliberately deterministic. The LLM's judgment is already captured in the
ToolPlan; letting a model free-write server code would trade away the three
properties that matter for generated artifacts — syntactic validity by
construction, a uniform reviewable style, and diffable regeneration. So the
skeleton lives in a Jinja template, each tool body is assembled here from the
plan, and every LLM-authored string is inserted as a validated identifier, a
Python literal, or an escaped docstring — never spliced in as code.

Plan mistakes that templates can catch (an unknown endpoint, a binding that
references a nonexistent argument) raise `GenerationError` with a message
written to be fed back to the design stage.
"""

import ast
import re
import textwrap
from pathlib import Path

from jinja2 import Environment, PackageLoader

from handyman.ir import APISpec, CallStep, Endpoint, ToolPlan, ToolSpec

_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


class GenerationError(Exception):
    """A ToolPlan defect that deterministic checks caught before runtime."""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def generate_artifacts(plan: ToolPlan, spec: APISpec, out_dir: Path, source: str) -> Path:
    """Write server.py, plan.json, and a provenance README into `out_dir`.

    Returns the server path. plan.json is the design stage's full decision,
    kept next to the code it produced so the artifact is self-explaining.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    server_path = out_dir / "server.py"
    server_path.write_text(render_server(plan, spec, source))
    (out_dir / "plan.json").write_text(plan.model_dump_json(indent=2) + "\n")
    (out_dir / "README.md").write_text(_readme(plan, source))
    return server_path


def render_server(plan: ToolPlan, spec: APISpec, source: str) -> str:
    endpoints = {endpoint.id: endpoint for endpoint in spec.endpoints}
    required = [s for s in plan.secrets if s.required]
    optional = [s for s in plan.secrets if not s.required]

    env = Environment(loader=PackageLoader("handyman", "templates"), keep_trailing_newline=True)
    env.filters["py"] = repr
    template = env.get_template("server.py.j2")
    return template.render(
        plan=plan,
        source=source,
        source_arg=source,
        base_url=spec.base_url,
        required_secrets=required,
        optional_secrets=optional,
        tool_blocks=[_tool_block(tool, plan, endpoints) for tool in plan.tools],
    )


# ---------------------------------------------------------------------------
# Per-tool code assembly
# ---------------------------------------------------------------------------


def _tool_block(tool: ToolSpec, plan: ToolPlan, endpoints: dict[str, Endpoint]) -> str:
    name = _ident(tool.name, context="tool name")
    required_secrets = [s for s in plan.secrets if s.required]

    decorator = "@app.tool"
    if required_secrets:
        env_vars = ", ".join(repr(s.env_var) for s in required_secrets)
        decorator = f"@app.tool(requires_secrets=[{env_vars}])"

    lines = [decorator, f"def {name}("]
    if required_secrets:
        lines.append("    context: Context,")
    lines += [f"    {arg_line}" for arg_line in _signature_args(tool)]
    lines.append(f") -> Annotated[dict, {tool.returns!r}]:")
    lines.append(_docstring(tool.description))
    lines += _body(tool, required_secrets, endpoints)
    return "\n".join(lines)


def _signature_args(tool: ToolSpec) -> list[str]:
    """Render arguments, required first (Python requires defaults last)."""
    rendered = []
    for arg in sorted(tool.args, key=lambda a: not a.required):
        arg_name = _ident(arg.name, context=f"argument of {tool.name}")
        if arg.required:
            rendered.append(f"{arg_name}: Annotated[{arg.py_type}, {arg.description!r}],")
        else:
            default = _default_literal(arg.default)
            annotation = arg.py_type if default != "None" else f"{arg.py_type} | None"
            rendered.append(
                f"{arg_name}: Annotated[{annotation}, {arg.description!r}] = {default},"
            )
    return rendered


def _default_literal(default: str | None) -> str:
    """Admit LLM-proposed defaults only as pure Python literals; anything else
    is demoted to a string literal rather than trusted as code."""
    if default is None:
        return "None"
    try:
        ast.literal_eval(default)
        return default
    except (ValueError, SyntaxError):
        return repr(default)


def _body(tool: ToolSpec, required_secrets: list, endpoints: dict[str, Endpoint]) -> list[str]:
    lines: list[str] = []
    scope = _initial_scope(tool)

    if required_secrets:
        pairs = ", ".join(
            f"{s.param_name!r}: context.get_secret({s.env_var!r})" for s in required_secrets
        )
        lines.append(f"    secret_headers = {{{pairs}}}")

    multi_step = len(tool.steps) > 1
    for index, step in enumerate(tool.steps, start=1):
        endpoint = endpoints.get(step.endpoint_id)
        if endpoint is None:
            raise GenerationError(
                f"tool '{tool.name}' step {index} references unknown endpoint "
                f"'{step.endpoint_id}'"
            )
        suffix = f"_{index}" if multi_step else ""
        lines += _step_lines(tool, step, endpoint, suffix, scope, bool(required_secrets))
        for extract in step.extracts:
            extract_name = _ident(extract.name, context=f"extract in {tool.name}")
            lines.append(f"    {extract_name} = _dig(response{suffix}, {extract.path!r})")
            scope[extract_name] = "extract"

    result = f"response{'_' + str(len(tool.steps)) if multi_step else ''}"
    if tool.response_extract:
        result = f"_prune({result}, {tool.response_extract!r})"
    lines.append(f"    return _as_object({result})")
    return lines


def _step_lines(
    tool: ToolSpec,
    step: CallStep,
    endpoint: Endpoint,
    suffix: str,
    scope: dict[str, str],
    has_secrets: bool,
) -> list[str]:
    """Render one HTTP call: bind path params into an f-string path, then
    query/header params into dicts (optional-argument bindings are guarded)."""
    lines = []
    locations = {param.name: param.location for param in endpoint.params}
    bindings = {binding.param: binding.value for binding in step.bindings}

    # Path: replace each {param} in the endpoint path with its binding template,
    # yielding an f-string over tool arguments and extracts.
    path_template = endpoint.path
    for param_name in _PLACEHOLDER.findall(endpoint.path):
        if param_name not in bindings:
            raise GenerationError(
                f"tool '{tool.name}': path parameter '{param_name}' of "
                f"'{endpoint.id}' is not bound"
            )
        template = bindings.pop(param_name)
        _check_refs(template, scope, tool.name)
        path_template = path_template.replace("{" + param_name + "}", template)
    path_expr = f'f"{path_template}"' if _PLACEHOLDER.search(path_template) else repr(path_template)

    # Query and header parameters.
    dict_lines: dict[str, list[str]] = {"params": [], "headers": []}
    for param_name, template in bindings.items():
        target = "headers" if locations.get(param_name) == "header" else "params"
        refs = _check_refs(template, scope, tool.name)
        optional_refs = [ref for ref in refs if scope[ref] == "optional"]
        assignment = f"{target}{suffix}[{param_name!r}] = {_value_expr(template, refs)}"
        if optional_refs:
            guard = " and ".join(f"{ref} is not None" for ref in optional_refs)
            dict_lines[target] += [f"    if {guard}:", f"        {assignment}"]
        else:
            dict_lines[target].append(f"    {assignment}")

    call_args = [f'"{endpoint.method}"', path_expr]
    for target in ("params", "headers"):
        if dict_lines[target]:
            lines.append(f"    {target}{suffix}: dict[str, Any] = {{}}")
            lines += dict_lines[target]
            call_args.append(f"{target}={target}{suffix}")
    if has_secrets:
        if dict_lines["headers"]:
            lines.append(f"    headers{suffix}.update(secret_headers)")
        else:
            call_args.append("headers=secret_headers")

    lines.append(f"    response{suffix} = _request({', '.join(call_args)})")
    return lines


def _value_expr(template: str, refs: list[str]) -> str:
    """A binding that is exactly one placeholder passes the argument through
    with its type intact; anything else becomes an f-string."""
    if template == "{" + refs[0] + "}" and len(refs) == 1:
        return refs[0]
    return f'f"{template}"'


# ---------------------------------------------------------------------------
# Validation and text helpers
# ---------------------------------------------------------------------------


def _initial_scope(tool: ToolSpec) -> dict[str, str]:
    """Names a binding template may reference, mapped to their optionality.
    Only arguments that can actually be None need runtime guards."""
    return {
        _ident(arg.name, context=f"argument of {tool.name}"):
            "optional" if (not arg.required and _default_literal(arg.default) == "None")
            else "required"
        for arg in tool.args
    }


def _check_refs(template: str, scope: dict[str, str], tool_name: str) -> list[str]:
    refs = _PLACEHOLDER.findall(template)
    unknown = [ref for ref in refs if ref not in scope]
    if unknown:
        raise GenerationError(
            f"tool '{tool_name}': binding '{template}' references unknown name(s) "
            f"{unknown}; known names are {sorted(scope)}"
        )
    return refs


def _ident(name: str, context: str) -> str:
    candidate = re.sub(r"[^a-z0-9_]+", "_", name.strip().lower()).strip("_")
    if not candidate or not _IDENT.match(candidate):
        raise GenerationError(f"cannot derive a Python identifier for {context}: {name!r}")
    return candidate


def _docstring(text: str, indent: str = "    ") -> str:
    body = "\n\n".join(
        textwrap.fill(paragraph, width=88, initial_indent=indent, subsequent_indent=indent)
        for paragraph in text.replace('"""', "'''").split("\n\n")
    )
    return f'{indent}"""\n{body}\n{indent}"""'


def _readme(plan: ToolPlan, source: str) -> str:
    tools = "\n".join(f"| `{t.name}` | {t.returns} |" for t in plan.tools)
    secrets = "\n".join(
        f"| `{s.env_var}` | {'required' if s.required else f'optional (default: `{s.default}`)'} "
        f"| {s.description} |"
        for s in plan.secrets
    ) or "| — | — | no configuration needed |"
    rejected = "\n".join(f"- `{r.endpoint_id}`: {r.reason}" for r in plan.rejected)
    return f"""# {plan.display_name}

Generated by [Handyman](../../README.md) from `{source}`. Do not edit by hand —
regenerate instead. The full design decision, including everything below, is in
[plan.json](plan.json).

Run locally:

```bash
uv run generated/{plan.server_name}/server.py stdio   # or: http
```

## Tools

| Tool | Returns |
|---|---|
{tools}

## Configuration

| Env var | Required | Purpose |
|---|---|---|
{secrets}

## Endpoints deliberately not exposed

{rejected}
"""
