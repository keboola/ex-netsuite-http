"""Functional (VCR) tests for the NetSuite HTTP extractor.

Each case under ``tests/functional/`` is a recorded cassette replayed with no network access
(``keboola.datadirtest``). Cassettes are recorded with ``scratchpad/record_cassettes.py`` against
the verified sandbox and sanitized by the ``VCR_SANITIZERS`` declared in ``src/component.py``.
"""

from pathlib import Path
from unittest import mock

import pytest
from keboola.datadirtest.vcr import VCRDataDirTester, get_test_cases
from keboola.datadirtest.vcr.tester import VCRTestDataDir

FUNCTIONAL_DIR = str(Path(__file__).parent / "functional")
COMPONENT_SCRIPT = str(Path(__file__).parent.parent / "src" / "component.py")


@pytest.mark.parametrize("test_name", get_test_cases(FUNCTIONAL_DIR))
def test_functional(test_name):
    """Run a single VCR functional test case.

    NOTE: the generic runner always resets the input state to ``{}`` (see
    ``TestDataDir._override_input_state``), so every parametrized case runs as a *first* run (empty
    state). The incremental cases (13, 17) therefore exercise the empty-state path here; genuine
    resume-from-prior-state is exercised by ``test_suiteql_incremental_genuinely_resumes_from_seeded_state``
    below and by the unit tests in ``tests/test_component_run.py`` / ``tests/unit``.
    """
    tester = VCRDataDirTester(
        data_dir=FUNCTIONAL_DIR,
        component_script=COMPONENT_SCRIPT,
        selected_tests=[test_name],
    )
    tester.run()


def test_suiteql_incremental_genuinely_resumes_from_seeded_state():
    """T7: genuinely exercise incremental resume against the recorded SuiteQL cassette.

    Seed a non-trivial prior watermark via ``last_state_override`` and assert the run rebuilds the
    ``:state`` bind from that seeded timestamp (not the epoch default). This works against the
    recorded cassette because the SuiteQL query travels in the POST *body*, which the VCR matcher
    does not key on (it matches method/scheme/host/port/path/query), so the seeded run replays the
    same recorded response and the expected output is unchanged.
    """
    import extractor.suiteql as suiteql_mod

    captured: list[str] = []
    real_bind = suiteql_mod.SuiteQLExtractor._bind_state

    def capturing_bind(self, query):
        bound = real_bind(self, query)
        captured.append(bound)
        return bound

    test_dir = str(Path(__file__).parent / "functional" / "17_suiteql_incremental")
    tester = VCRTestDataDir(
        data_dir=test_dir,
        component_script=COMPONENT_SCRIPT,
        vcr_mode="replay",
        last_state_override={"last_run": "2024-03-15T00:00:00Z"},
    )
    with mock.patch.object(suiteql_mod.SuiteQLExtractor, "_bind_state", capturing_bind):
        tester.setUp()
        try:
            tester.compare_source_and_expected()
        finally:
            tester.tearDown()

    assert any("2024-03-15T00:00:00Z" in q for q in captured), captured
