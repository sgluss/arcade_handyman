"""Verify-stage tests that don't need an LLM: static checks, boot check, the
design-name -> served-name mapping the eval runner depends on, and the
execution-smoke stage run end-to-end against a local stand-in API."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from handyman.generate import generate_artifacts
from handyman.ir import EvalCase, EvalSuite, ExpectedArg
from handyman.verify.evals import _ToolCacheAdapter, map_served_names, typed_value
from handyman.verify.smoke import smoke_check, smoke_feedback
from handyman.verify.static import boot_check, static_check


@pytest.fixture()
def server_path(fixture_plan, fixture_spec, tmp_path):
    return generate_artifacts(fixture_plan, fixture_spec, tmp_path, source="tests/fixture")


def test_generated_server_passes_static_checks(server_path):
    assert static_check(server_path) == []


def test_static_check_catches_broken_code(tmp_path):
    broken = tmp_path / "server.py"
    broken.write_text("def tool(:\n    pass\n")
    problems = static_check(broken)
    assert problems and "compile" in problems[0]


def test_boot_check_reports_served_names(server_path):
    assert set(boot_check(server_path)) == {"Fixture_GetStationData", "Fixture_GetDataForPoint"}


def test_design_names_map_to_served_names(fixture_plan):
    served = ["Fixture_GetStationData", "Fixture_GetDataForPoint"]
    mapping = map_served_names(fixture_plan, served)
    assert mapping == {
        "get_station_data": "Fixture_GetStationData",
        "get_data_for_point": "Fixture_GetDataForPoint",
    }


def test_ambiguous_served_names_fail_loudly(fixture_plan):
    with pytest.raises(RuntimeError, match="cannot map"):
        map_served_names(fixture_plan, ["Fixture_GetStationData"])


def test_gate_adapter_marks_only_the_last_tool_cacheable():
    class FakeMessages:
        kwargs = None

        async def create(self, **kwargs):
            self.kwargs = kwargs
            return "response"

    class FakeClient:
        def __init__(self):
            self.messages = FakeMessages()

    fake = FakeClient()
    adapter = _ToolCacheAdapter(fake)
    result = asyncio.run(
        adapter.messages.create(model="m", tools=[{"name": "a"}, {"name": "b"}], messages=[])
    )
    assert result == "response"
    sent = fake.messages.kwargs["tools"]
    assert sent[-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in sent[0]


def test_expected_values_retype_to_declared_arg_types():
    assert typed_value("-74.0060", "float") == -74.006
    assert typed_value("3", "int") == 3
    assert typed_value("true", "bool") is True
    assert typed_value("Seattle, WA", "str") == "Seattle, WA"
    # unparseable values fall back to the string rather than crashing the gate
    assert typed_value("not-a-number", "float") == "not-a-number"


# ---------------------------------------------------------------------------
# Execution smoke, end-to-end against a local stand-in for the fixture API
# ---------------------------------------------------------------------------


@pytest.fixture()
def local_api():
    """A live HTTP stand-in serving the fixture API's two endpoints, so smoke
    tests exercise a real server subprocess making real requests — without
    leaving the machine."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/lookup/0.0,0.0":  # a point whose station 404s downstream
                payload = {"properties": {"stationId": "MISSING", "name": "Ghost Station"}}
            elif path.startswith("/lookup/"):
                payload = {"properties": {"stationId": "KTEST", "name": "Test Station"}}
            elif path.startswith("/stations/MISSING/"):
                self.send_error(404)
                return
            elif path.startswith("/stations/LOCKED/"):
                self.send_error(401)
                return
            elif path.startswith("/stations/BROKEN/"):  # 200 with a non-JSON body
                body = b"<html>maintenance page</html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            elif path.startswith("/stations/"):
                payload = {"properties": {"data": [{"reading": 1}]}}
            else:
                self.send_error(404)
                return
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):  # keep pytest output clean
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture()
def smoke_server(fixture_plan, fixture_spec, tmp_path, local_api, monkeypatch):
    """The fixture server, regenerated against the local API, with its
    required secret present."""
    monkeypatch.setenv("FIXTURE_API_KEY", "test-key")
    spec = fixture_spec.model_copy(update={"base_url": local_api})
    return generate_artifacts(fixture_plan, spec, tmp_path, source="tests/fixture")


def _smoke_suite() -> EvalSuite:
    return EvalSuite(
        cases=[
            EvalCase(
                user_message="What are the latest readings from station KTEST?",
                expected_tool="get_station_data",
                expected_args=[ExpectedArg(name="station", value="KTEST", match="exact")],
            ),
            EvalCase(
                user_message="What are conditions at 47.6, -122.33?",
                expected_tool="get_data_for_point",
                expected_args=[
                    ExpectedArg(name="latitude", value="47.6", match="exact"),
                    ExpectedArg(name="longitude", value="-122.33", match="exact"),
                ],
            ),
        ]
    )


def test_smoke_executes_every_tool_once(fixture_plan, smoke_server):
    outcomes = smoke_check(_smoke_suite(), fixture_plan, smoke_server)
    assert [o.tool for o in outcomes] == ["get_station_data", "get_data_for_point"]
    assert all(o.ok for o in outcomes), [o.detail for o in outcomes]
    # case values reach the call retyped to the declared schema (the chained
    # tool passing proves the two-call wiring end-to-end)
    assert outcomes[1].args == {"latitude": 47.6, "longitude": -122.33}


def test_smoke_surfaces_per_call_errors(fixture_plan, fixture_spec, tmp_path, local_api,
                                        monkeypatch):
    # without its required secret, every call errors (arcade-mcp reports it
    # per call, not as a boot failure) — smoke must catch exactly that
    monkeypatch.delenv("FIXTURE_API_KEY", raising=False)
    spec = fixture_spec.model_copy(update={"base_url": local_api})
    server = generate_artifacts(fixture_plan, spec, tmp_path, source="tests/fixture")
    outcomes = smoke_check(_smoke_suite(), fixture_plan, server)
    assert all(not o.ok and o.detail for o in outcomes)
    assert "get_station_data" in smoke_feedback(outcomes)


def test_smoke_tolerates_upstream_errors_for_single_call_lookups(fixture_plan, smoke_server):
    """The examiner fabricates identifiers it cannot know; a single-call
    lookup that draws a clean HTTP error answer is wired, and smoke must say
    so instead of failing the gate."""
    suite = EvalSuite(
        cases=[
            EvalCase(
                user_message="Show me the readings from station MISSING.",
                expected_tool="get_station_data",
                expected_args=[ExpectedArg(name="station", value="MISSING", match="exact")],
            ),
            _smoke_suite().cases[1],
        ]
    )
    outcomes = smoke_check(suite, fixture_plan, smoke_server)
    lookup = {o.tool: o for o in outcomes}["get_station_data"]
    assert lookup.ok
    assert "tolerated UPSTREAM_RUNTIME_NOT_FOUND" in lookup.detail


def test_smoke_never_tolerates_auth_errors(fixture_plan, smoke_server):
    """With real credentials loaded, a 401 indicts the credential plumbing —
    the fabricated-input tolerance must not paper over it."""
    suite = EvalSuite(
        cases=[
            EvalCase(
                user_message="Show me the readings from station LOCKED.",
                expected_tool="get_station_data",
                expected_args=[ExpectedArg(name="station", value="LOCKED", match="exact")],
            ),
            _smoke_suite().cases[1],
        ]
    )
    outcomes = smoke_check(suite, fixture_plan, smoke_server)
    locked = {o.tool: o for o in outcomes}["get_station_data"]
    assert not locked.ok
    assert "401" in locked.detail


def test_smoke_never_tolerates_unparseable_success_bodies(fixture_plan, smoke_server):
    """A success status with a non-JSON body maps to the unmapped upstream
    kind; that indicts the request shape, not the examiner's fabricated
    input, so it must fail the gate."""
    suite = EvalSuite(
        cases=[
            EvalCase(
                user_message="Show me the readings from station BROKEN.",
                expected_tool="get_station_data",
                expected_args=[ExpectedArg(name="station", value="BROKEN", match="exact")],
            ),
            _smoke_suite().cases[1],
        ]
    )
    outcomes = smoke_check(suite, fixture_plan, smoke_server)
    broken = {o.tool: o for o in outcomes}["get_station_data"]
    assert not broken.ok
    assert "non-JSON" in broken.detail


def test_smoke_never_tolerates_upstream_errors_mid_chain(fixture_plan, smoke_server):
    """A chain feeds itself live data after step one, so an HTTP error there
    is a wiring failure, not an unanswerable input."""
    suite = EvalSuite(
        cases=[
            _smoke_suite().cases[0],
            EvalCase(
                user_message="What are conditions at 0.0, 0.0?",
                expected_tool="get_data_for_point",
                expected_args=[
                    ExpectedArg(name="latitude", value="0.0", match="exact"),
                    ExpectedArg(name="longitude", value="0.0", match="exact"),
                ],
            ),
        ]
    )
    outcomes = smoke_check(suite, fixture_plan, smoke_server)
    chain = {o.tool: o for o in outcomes}["get_data_for_point"]
    assert not chain.ok
    assert "404" in chain.detail


def test_smoke_fails_tools_no_case_covers(fixture_plan, smoke_server):
    suite = EvalSuite(cases=[_smoke_suite().cases[0]])
    outcomes = smoke_check(suite, fixture_plan, smoke_server)
    by_tool = {o.tool: o for o in outcomes}
    assert by_tool["get_station_data"].ok
    assert not by_tool["get_data_for_point"].ok
    assert "no eval case" in by_tool["get_data_for_point"].detail
