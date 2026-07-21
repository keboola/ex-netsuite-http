"""Functional (VCR) tests for the NetSuite HTTP extractor.

Each case under ``tests/functional/`` is a recorded cassette replayed with no network access
(``keboola.datadirtest``). Cassettes are recorded with ``scratchpad/record_cassettes.py`` against
the verified sandbox and sanitized by the ``VCR_SANITIZERS`` declared in ``src/component.py``.
"""

from pathlib import Path

import pytest
from keboola.datadirtest.vcr import VCRDataDirTester, get_test_cases

FUNCTIONAL_DIR = str(Path(__file__).parent / "functional")
COMPONENT_SCRIPT = str(Path(__file__).parent.parent / "src" / "component.py")


@pytest.mark.parametrize("test_name", get_test_cases(FUNCTIONAL_DIR))
def test_functional(test_name):
    """Run a single VCR functional test case."""
    tester = VCRDataDirTester(
        data_dir=FUNCTIONAL_DIR,
        component_script=COMPONENT_SCRIPT,
        selected_tests=[test_name],
    )
    tester.run()
