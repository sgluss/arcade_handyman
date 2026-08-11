"""Shared fixture: a small fake API and a hand-written ToolPlan that together
exercise every code-generation feature — a simple tool, a two-call chain with
extracts, optional arguments (guarded bindings), a required API-key secret, an
optional identification header, enum params, and response pruning.

This fixture also stands in for the auth mode the live demos don't need:
both demo APIs (NWS, Hacker News) are keyless, so the required-secret path is
proven here instead.
"""

import pytest

from toolsmith.ir import (
    APIParam,
    APISpec,
    AuthScheme,
    CallStep,
    Endpoint,
    Extract,
    ParamBinding,
    SecretSpec,
    ToolArg,
    ToolPlan,
    ToolSpec,
)


@pytest.fixture()
def fixture_spec() -> APISpec:
    return APISpec(
        name="Fixture Observations API",
        base_url="https://api.example.com",
        description="A small fake API for tests.",
        auth=[
            AuthScheme(kind="api_key", name="X-Api-Key", location="header"),
            AuthScheme(kind="api_key", name="User-Agent", location="header"),
        ],
        endpoints=[
            Endpoint(
                id="lookup_point",
                method="GET",
                path="/lookup/{latitude},{longitude}",
                description="Resolve a coordinate to its observing station.",
                params=[
                    APIParam(name="latitude", location="path", type="number", required=True),
                    APIParam(name="longitude", location="path", type="number", required=True),
                ],
                response_hint="{properties: {stationId, name}}",
            ),
            Endpoint(
                id="station_data",
                method="GET",
                path="/stations/{station}/data",
                description="Data for one station.",
                params=[
                    APIParam(name="station", location="path", type="string", required=True),
                    APIParam(name="units", location="query", type="string", enum=["us", "si"]),
                    APIParam(name="tags", location="query", type="array",
                             description="Filter tags"),
                ],
                response_hint="{properties: {data: [{...}]}}",
            ),
        ],
    )


@pytest.fixture()
def fixture_plan() -> ToolPlan:
    return ToolPlan(
        server_name="fixture",
        display_name="Fixture Observations",
        instructions="Observation data for tests.",
        secrets=[
            SecretSpec(
                env_var="FIXTURE_API_KEY",
                param_name="X-Api-Key",
                required=True,
                description="API key for the fixture service.",
            ),
            SecretSpec(
                env_var="FIXTURE_USER_AGENT",
                param_name="User-Agent",
                required=False,
                default="fixture-tests (dev@example.com)",
                description="Identify this client to the service.",
            ),
        ],
        tools=[
            ToolSpec(
                name="get_station_data",
                description="Get observation data for a known station ID. Use when the "
                "station is already known; otherwise use get_data_for_point.",
                args=[
                    ToolArg(name="station", py_type="str",
                            description="Station identifier, e.g. KSEA"),
                    ToolArg(name="units", py_type="str", description="'us' or 'si'",
                            required=False, default='"us"'),
                    ToolArg(name="tags", py_type="str",
                            description="Comma-separated filter tags", required=False),
                ],
                steps=[
                    CallStep(
                        endpoint_id="station_data",
                        bindings=[
                            ParamBinding(param="station", value="{station}"),
                            ParamBinding(param="units", value="{units}"),
                            ParamBinding(param="tags", value="{tags}"),
                        ],
                    )
                ],
                response_extract="properties.data",
                returns="A list of observation records",
            ),
            ToolSpec(
                name="get_data_for_point",
                description="Get observation data for a coordinate. Resolves the "
                "coordinate to its station internally.",
                args=[
                    ToolArg(name="latitude", py_type="float", description="Decimal degrees"),
                    ToolArg(name="longitude", py_type="float", description="Decimal degrees"),
                ],
                steps=[
                    CallStep(
                        endpoint_id="lookup_point",
                        bindings=[
                            ParamBinding(param="latitude", value="{latitude}"),
                            ParamBinding(param="longitude", value="{longitude}"),
                        ],
                        extracts=[Extract(name="station_id", path="properties.stationId")],
                    ),
                    CallStep(
                        endpoint_id="station_data",
                        bindings=[ParamBinding(param="station", value="{station_id}")],
                    ),
                ],
                response_extract="properties.data",
                returns="A list of observation records",
            ),
        ],
    )
