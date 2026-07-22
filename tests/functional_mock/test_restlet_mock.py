"""SYNTHETIC / mock-based functional tests for the `restlet` mode.

NOT live recordings. No RESTlet was deployed in the sandbox and the credentials could not deploy one,
so these tests stand in for live VCR cassettes. They drive the REAL ``RestletClient`` +
``RestletExtractor`` end-to-end (request construction, cursor-pagination loop, record_path
extraction, error surfacing, output mapping, state) against synthetic HTTP responses served by the
``responses`` library from the JSON fixtures in ``fixtures/restlet_*.json``. Only the network is
faked; all component code runs for real.

To record REAL cassettes later: deploy a RESTlet in the sandbox, then run
``scripts/record_cassettes.py`` with a restlet config.
"""

import json
from pathlib import Path
from typing import Any

import pytest
import responses
from keboola.component.exceptions import UserException

from client.auth import TBASigner
from client.restlet import RestletClient
from configuration import RestletRow
from extractor.restlet import RestletExtractor

_FIXTURES = Path(__file__).parent / "fixtures"
_RESTLET_URL = "https://1234567-sb1.restlets.api.netsuite.com/app/site/hosting/restlet.nl"


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((_FIXTURES / name).read_text())


def _extractor(**kw) -> RestletExtractor:
    params: dict[str, Any] = {
        "mode": "restlet",
        "script_id": "123",
        "deploy_id": "1",
        "output_table_name": "restlet_out",
        **kw,
    }
    row = RestletRow.model_validate(params)
    client = RestletClient(TBASigner("1234567_SB1", "ck", "cs", "ti", "ts"))
    return RestletExtractor(row=row, restlet_client=client, server_time_provider=lambda: "2024-05-01T00:00:00Z")


@responses.activate
def test_get_basic_maps_rows_from_record_path():
    responses.add(responses.GET, _RESTLET_URL, json=_fixture("restlet_get.json"), status=200)
    ext = _extractor(method="GET", record_path="data.results", load_type="full_load", primary_key=["id"])
    result = ext.extract()
    rows = list(result.tables[0].rows)
    assert [r["id"] for r in rows] == ["1", "2"]
    assert rows[0]["name"] == "Alpha"
    assert "script=123" in (responses.calls[0].request.url or "")


@responses.activate
def test_post_with_body_sends_body_and_maps_rows():
    responses.add(responses.POST, _RESTLET_URL, json=_fixture("restlet_post.json"), status=200)
    ext = _extractor(
        method="POST", request_body={"filter": "active"}, record_path="data.results", load_type="full_load"
    )
    rows = list(ext.extract().tables[0].rows)
    assert [r["id"] for r in rows] == ["10"]
    sent = responses.calls[0].request
    assert sent.method == "POST"
    assert b"filter" in (sent.body or b"")


@responses.activate
def test_marker_pagination_two_pages():
    responses.add(responses.GET, _RESTLET_URL, json=_fixture("restlet_page1.json"), status=200)
    responses.add(responses.GET, _RESTLET_URL, json=_fixture("restlet_page2.json"), status=200)
    ext = _extractor(method="GET", record_path="rows", pagination_cursor_field="next_cursor", load_type="full_load")
    rows = list(ext.extract().tables[0].rows)
    assert [r["id"] for r in rows] == ["1", "2"]
    assert len(responses.calls) == 2
    assert "cursor=CURSOR_PAGE_2" in (responses.calls[1].request.url or "")


@responses.activate
def test_error_response_surfaced_with_status_and_body():
    responses.add(responses.GET, _RESTLET_URL, json=_fixture("restlet_error.json"), status=400)
    ext = _extractor(method="GET", record_path="data.results", load_type="full_load")
    with pytest.raises(UserException) as exc:
        list(ext.extract().tables[0].rows)
    assert "400" in str(exc.value)
    assert "unknown script parameter" in str(exc.value)
