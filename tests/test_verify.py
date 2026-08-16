"""Verify-stage tests that don't need an LLM: static checks, boot check, and
the design-name -> served-name mapping the eval runner depends on."""

import pytest

from handyman.generate import generate_artifacts
from handyman.verify.evals import _map_served_names, _typed
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
    mapping = _map_served_names(fixture_plan, served)
    assert mapping == {
        "get_station_data": "Fixture_GetStationData",
        "get_data_for_point": "Fixture_GetDataForPoint",
    }


def test_ambiguous_served_names_fail_loudly(fixture_plan):
    with pytest.raises(RuntimeError, match="cannot map"):
        _map_served_names(fixture_plan, ["Fixture_GetStationData"])


def test_expected_values_retype_to_declared_arg_types():
    assert _typed("-74.0060", "float") == -74.006
    assert _typed("3", "int") == 3
    assert _typed("true", "bool") is True
    assert _typed("Seattle, WA", "str") == "Seattle, WA"
    # unparseable values fall back to the string rather than crashing the gate
    assert _typed("not-a-number", "float") == "not-a-number"
