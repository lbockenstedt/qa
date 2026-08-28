"""Cover TestScenario.matches(), which decides what a targeted QA run executes.

Getting this wrong is quiet and expensive: too strict and a module-targeted run
silently skips the connectivity/security scenarios that every run depends on;
too loose and an unrelated module's scenarios run against the wrong spoke.
"""
import pytest

from qa_engine import TestScenario


def _scenario(modules=None):
    return TestScenario("s", lambda: None, modules)


def test_no_filter_runs_everything():
    assert _scenario().matches() is True
    assert _scenario(["opnsense"]).matches() is True
    assert _scenario(["opnsense"]).matches(None) is True
    assert _scenario(["opnsense"]).matches("") is True


def test_untagged_scenario_runs_under_any_filter():
    """modules=None means 'part of the full suite regardless of filter'."""
    assert _scenario().matches("opnsense") is True
    assert _scenario([]).matches("netbox") is True


def test_tagged_scenario_runs_only_for_its_module():
    assert _scenario(["opnsense"]).matches("opnsense") is True
    assert _scenario(["netbox"]).matches("opnsense") is False


def test_filter_is_case_insensitive_both_ways():
    assert _scenario(["OPNsense"]).matches("opnsense") is True
    assert _scenario(["opnsense"]).matches("OPNSENSE") is True


def test_scenario_with_several_tags_matches_any_of_them():
    assert _scenario(["cs", "pxmx"]).matches("pxmx") is True
    assert _scenario(["cs", "pxmx"]).matches("netbox") is False


@pytest.mark.parametrize("flt", ["sense", "box"])
def test_filter_matches_on_substring(flt):
    """Documented as-is: the filter is a substring test, not an exact match.

    So `--module sense` selects the opnsense scenarios. Pinned deliberately --
    if this is ever tightened to an exact match, these are the cases to revisit.
    """
    assert _scenario(["opnsense", "netbox"]).matches(flt) is True
