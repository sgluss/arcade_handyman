"""Design stage: APISpec -> ToolPlan.

This is the judgment step, and the reason an LLM is in the pipeline at all.
Good MCP tools are not 1:1 endpoint wrappers: a tool surface is *designed* for
the agent that will call it — a handful of well-named capabilities, indirection
hidden behind chained calls, responses pruned to what an agent can use, and
descriptions written as contracts for a language model rather than for humans.

The model's entire output is a `ToolPlan` (schema-validated); everything
mechanical downstream — code generation, secret wiring, eval scoring — runs
deterministically off that plan.
"""

from handyman.ir import APISpec, Endpoint, ToolPlan
from handyman.llm import parse

DESIGN_SYSTEM = """\
You are an expert MCP tool designer. Given an inventory of an HTTP API, you
produce a ToolPlan for an MCP server whose consumers are LLM agents.

Design principles, in priority order:

1. Select, don't wrap. Choose the smallest set of tools (typically 3-6) that
   covers what agents realistically ask for. Reject endpoints that are
   administrative, redundant with a better endpoint, or too niche to earn a
   place in an agent's context window. Record representative rejections with
   one-clause reasons.

2. Hide the API's plumbing. If reaching useful data requires an intermediate
   lookup (an ID resolution, a two-step indirection), chain the two calls
   inside one tool via `steps` + `extracts` so the agent never learns the
   API's internal topology. Never expose a tool whose only purpose is to feed
   another tool.

3. Design arguments for agents. Expose only parameters an agent can plausibly
   supply; use scalar types; give optional arguments sensible defaults. For
   API parameters that accept multiple values, take a comma-separated string
   and say so in the description. Omit API parameters that agents don't need
   — an unbound parameter is simply never sent.

4. Write descriptions as contracts. Each tool description states what it does,
   when to use it (and when not to), and what it returns. Argument
   descriptions state format and units. A fresh LLM seeing only these strings
   must reliably pick the right tool with the right arguments.

5. Prune responses. APIs return envelopes; agents need payloads. Set
   `response_extract` to the dot path holding the useful part whenever the
   response hint makes it clear.

6. Wire auth honestly. Map each auth scheme the server needs to a SecretSpec
   with an env var named {SERVER}_{PURPOSE}. Identification-only schemes
   (e.g. a required User-Agent) are required=false with a sensible default so
   the server runs with zero configuration; real credentials are required=true
   with no default. OAuth schemes are out of scope: skip them and note the
   affected endpoints as rejections.

Binding template rules: a binding's `value` is a Python str.format template
over tool argument names and extract names from earlier steps, e.g.
"{latitude},{longitude}" or "{grid_id}". Extract paths are dot paths into the
step's JSON response. Path parameters must all be bound; bindings that
reference an optional argument are dropped at runtime when the argument is
not provided.
"""

REVISION_PREAMBLE = """\
A previous version of this plan failed its verification gate; the failure
detail is below. Structural failures (plan validation, static checks, live
execution smoke) name an exact defect — correct that defect. Selection
failures mean an evaluator LLM, seeing only your tool names/descriptions/
arguments, chose the wrong tool or wrong arguments — sharpen names,
disambiguate descriptions, adjust arguments so a fresh model chooses
correctly. Keep the overall tool surface unless a failure demands otherwise;
argument-value or format failures are description problems, so fix the
wording rather than removing the tool.

"""


def design_toolplan(
    spec: APISpec, guidance: str | None = None, feedback: str | None = None
) -> ToolPlan:
    """Produce a ToolPlan for `spec`; `feedback` carries eval failures on revision."""
    prompt = _inventory(spec)
    if guidance:
        prompt += f"\nOperator guidance (weigh heavily when selecting tools):\n{guidance}\n"
    if feedback:
        prompt = REVISION_PREAMBLE + feedback + "\n\n---\n\n" + prompt
    return parse(prompt, ToolPlan, system=DESIGN_SYSTEM)


def _inventory(spec: APISpec) -> str:
    """Compact, complete rendering of the APISpec — the only facts the model gets."""
    lines = [
        f"API: {spec.name}",
        f"Base URL: {spec.base_url}",
        f"About: {spec.description or '(none)'}",
        "",
        "Auth schemes:",
    ]
    if not spec.auth:
        lines.append("- none")
    for auth in spec.auth:
        lines.append(f"- {auth.kind} via {auth.location} '{auth.name}': {auth.description[:300]}")

    lines += ["", f"Endpoints ({len(spec.endpoints)}):"]
    for endpoint in spec.endpoints:
        lines.append(_endpoint_line(endpoint))
    return "\n".join(lines)


def _endpoint_line(e: Endpoint) -> str:
    params = ", ".join(
        f"{p.name}({p.location},{p.type}{'*' if p.required else ''}"
        + (f",enum:{'|'.join(p.enum[:6])}" if p.enum else "")
        + (f" — {p.description[:90]}" if p.description else "")
        + ")"
        for p in e.params
    )
    line = f"- {e.id}: {e.method} {e.path} — {e.description[:160]}"
    if params:
        line += f"\n    params: {params}"
    if e.response_hint:
        line += f"\n    response: {e.response_hint}"
    return line
