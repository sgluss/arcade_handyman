"""Typed contracts between pipeline stages.

Every stage of the pipeline consumes and produces one of these models:

    ingest  ->  APISpec      what the API offers (mechanical facts, no judgment)
    design  ->  ToolPlan     what the MCP server should be (judgment, via LLM)
    verify  ->  EvalSuite    how to check the plan reads well to a fresh LLM

Keeping the contracts in one small file is deliberate: LLM stages emit these
models via structured outputs, so the schemas below are simultaneously the
internal data model, the validation layer for model output, and the
documentation an interviewer reads first. Field descriptions are written for
the LLM that must fill them in.

Structured-output constraint: models the LLM emits avoid free-form dicts
(JSON Schema `additionalProperties`), so mappings are lists of named pairs.
"""

from typing import Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# APISpec — produced by ingest (openapi.py deterministically, docs.py via LLM)
# ---------------------------------------------------------------------------


class APIParam(BaseModel):
    """One parameter of one endpoint, as the API defines it."""

    name: str
    location: Literal["query", "path", "header"]
    type: Literal["string", "integer", "number", "boolean", "array"] = "string"
    required: bool = False
    description: str = ""
    enum: list[str] = Field(
        default_factory=list, description="Allowed values, when the API restricts them"
    )


class AuthScheme(BaseModel):
    """One way the API authenticates callers.

    OAuth flows are recorded but out of scope for generation: Arcade's managed
    OAuth (`requires_auth` on a tool) is the right mechanism for those, and
    wiring it is a product decision, not something to generate blind.
    """

    kind: Literal["api_key", "http_bearer", "oauth", "other"]
    name: str = Field(description="Header or query parameter that carries the credential")
    location: Literal["header", "query"] = "header"
    description: str = ""


class Endpoint(BaseModel):
    """One operation of the API, flattened to what tool design needs."""

    id: str = Field(description="operationId, or METHOD_path when the spec has none")
    method: str
    path: str = Field(description="Path template, e.g. /points/{latitude},{longitude}")
    description: str = ""
    params: list[APIParam] = Field(default_factory=list)
    response_hint: str = Field(
        default="", description="Shallow summary of the success-response shape"
    )


class APISpec(BaseModel):
    """Everything the design stage is allowed to know about an API.

    This is the mechanical inventory. It contains no opinions about which
    endpoints matter — that judgment belongs to the design stage.
    """

    name: str
    base_url: str
    description: str = ""
    auth: list[AuthScheme] = Field(default_factory=list)
    endpoints: list[Endpoint] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# ToolPlan — produced by the design stage (LLM, schema-validated)
# ---------------------------------------------------------------------------


class ToolArg(BaseModel):
    """One argument of a generated tool, as the *consuming agent* will see it."""

    name: str = Field(description="snake_case Python identifier")
    py_type: Literal["str", "int", "float", "bool"]
    description: str = Field(description="Written for the LLM that will call this tool")
    required: bool = True
    default: str | None = Field(
        default=None, description="Python literal source for the default, e.g. '\"us\"' or '7'"
    )


class ParamBinding(BaseModel):
    """Assigns a value to one API parameter of a call step.

    `value` is a template over tool arguments and earlier extracts, e.g.
    "{latitude},{longitude}" or "{point_data[gridId]}". Templates keep the
    plan declarative: the LLM chooses the wiring, deterministic code runs it.
    """

    param: str = Field(description="API parameter name from the endpoint definition")
    value: str


class Extract(BaseModel):
    """Names a value pulled from a step's JSON response for use in later steps."""

    name: str = Field(description="Variable name usable in later bindings")
    path: str = Field(description="Dot path into the response JSON, e.g. properties.gridId")


class CallStep(BaseModel):
    """One HTTP call in a tool's implementation."""

    endpoint_id: str = Field(description="Endpoint.id from the APISpec")
    bindings: list[ParamBinding] = Field(default_factory=list)
    extracts: list[Extract] = Field(
        default_factory=list, description="Only needed when a later step uses the value"
    )


class ToolSpec(BaseModel):
    """One tool of the generated MCP server.

    A tool is not an endpoint wrapper: it may chain up to two calls to hide an
    API's internal indirection (the NWS point -> gridpoint dance), prune a
    response to the part an agent can use, or decline to exist at all.
    """

    name: str = Field(description="snake_case, verb-first, e.g. get_forecast")
    description: str = Field(
        description="The contract shown to consuming agents: what it does, when to use it, "
        "what it returns. This text is what the eval gate scores."
    )
    args: list[ToolArg] = Field(default_factory=list)
    steps: list[CallStep] = Field(description="1 call, or 2 when the API forces indirection")
    response_extract: str | None = Field(
        default=None,
        description="Dot path pruning the final response to what agents need, "
        "e.g. properties.periods. None returns the full body.",
    )
    returns: str = Field(description="One line describing the returned payload")


class SecretSpec(BaseModel):
    """A credential or identifying value the generated server needs at runtime.

    Maps an environment variable to the request header/query parameter that
    carries it. `required=False` with a default covers courtesy schemes like
    NWS's User-Agent, which should not block a zero-config demo.
    """

    env_var: str = Field(description="UPPER_SNAKE name, prefixed with the server name")
    param_name: str = Field(description="Header or query parameter the value is sent as")
    location: Literal["header", "query"] = "header"
    required: bool = True
    default: str | None = Field(default=None, description="Fallback when not required")
    description: str = ""


class RejectedEndpoint(BaseModel):
    """Design-stage transparency: what was left out, and why."""

    endpoint_id: str
    reason: str = Field(description="One short clause")


class ToolPlan(BaseModel):
    """The design stage's complete decision, ready for deterministic codegen."""

    server_name: str = Field(description="snake_case package-style name, e.g. nws")
    display_name: str
    instructions: str = Field(description="Server-level guidance shown to MCP clients")
    secrets: list[SecretSpec] = Field(default_factory=list)
    tools: list[ToolSpec]
    rejected: list[RejectedEndpoint] = Field(
        default_factory=list,
        description="Representative rejections grouped by reason; not exhaustive",
    )


# ---------------------------------------------------------------------------
# EvalSuite — produced by the examiner (LLM), consumed by the eval gate
# ---------------------------------------------------------------------------


class ExpectedArg(BaseModel):
    """One argument the examiner expects in a correct tool call."""

    name: str
    value: str = Field(description="Expected value, as a string")
    match: Literal["exact", "similar"] = Field(
        description="exact for identifiers/enums; similar for free text a model may phrase "
        "differently (mirrors Arcade's BinaryCritic vs SimilarityCritic)"
    )


class EvalCase(BaseModel):
    """One tool-selection scenario: a realistic user ask and the correct call."""

    user_message: str = Field(description="What a real user would say, no tool names in it")
    expected_tool: str = Field(description="ToolSpec.name the model should choose")
    expected_args: list[ExpectedArg] = Field(default_factory=list)


class EvalSuite(BaseModel):
    """Selection-accuracy cases scored by the verify stage before a server ships."""

    cases: list[EvalCase]
