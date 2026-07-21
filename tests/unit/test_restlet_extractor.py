"""Unit tests for the RESTlet extractor (mocked RESTlet client)."""

from typing import Any
from unittest import mock

from configuration import RestletRow
from extractor.restlet import RestletExtractor


def _row(**kw) -> RestletRow:
    params: dict[str, Any] = {
        "mode": "restlet",
        "script_id": "123",
        "deploy_id": "1",
        "output_table_name": "restlet_out",
        "record_path": "data.results",
        **kw,
    }
    return RestletRow.model_validate(params)


def _extractor(row, rows, since=None):
    client = mock.Mock()
    client.iter_records.return_value = iter(rows)
    ext = RestletExtractor(
        row=row,
        restlet_client=client,
        since=since,
        server_time_provider=lambda: "2024-05-01T00:00:00Z",
    )
    return ext, client


def test_rows_mapped_and_record_path_forwarded():
    row = _row(pagination_cursor_field="next", load_type="full_load")
    ext, client = _extractor(row, [{"id": "1"}, {"id": "2"}])
    result = ext.extract()
    rows = list(result.tables[0].rows)
    assert [r["id"] for r in rows] == ["1", "2"]
    _, kwargs = client.iter_records.call_args
    assert kwargs["record_path"] == "data.results"
    assert kwargs["cursor_field"] == "next"


def test_state_captured_from_server_time_when_incremental():
    row = _row(load_type="incremental_load")
    ext, _ = _extractor(row, [{"id": "1"}])
    result = ext.extract()
    assert result.state == {"last_run": "2024-05-01T00:00:00Z"}
    assert result.tables[0].incremental is True


def test_full_load_writes_no_state():
    row = _row(load_type="full_load")
    ext, _ = _extractor(row, [{"id": "1"}])
    result = ext.extract()
    assert result.state == {}


def test_incremental_since_passed_as_query_param():
    row = _row(load_type="incremental_load", incremental_field="modified_since")
    ext, client = _extractor(row, [{"id": "1"}], since="2024-01-01T00:00:00Z")
    ext.extract()
    _, kwargs = client.iter_records.call_args
    assert kwargs["query_params"]["modified_since"] == "2024-01-01T00:00:00Z"
