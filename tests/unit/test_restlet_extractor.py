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


def _extractor(row, rows):
    client = mock.Mock()
    client.iter_records.return_value = iter(rows)
    return RestletExtractor(row=row, restlet_client=client), client


def test_rows_mapped_and_record_path_forwarded():
    row = _row(pagination_cursor_field="next", load_type="full_load")
    ext, client = _extractor(row, [{"id": "1"}, {"id": "2"}])
    result = ext.extract()
    rows = list(result.tables[0].rows)
    assert [r["id"] for r in rows] == ["1", "2"]
    _, kwargs = client.iter_records.call_args
    assert kwargs["record_path"] == "data.results"
    assert kwargs["cursor_field"] == "next"


def test_json_query_params_and_body_parsed_and_forwarded():
    # §7: query_params / request_body are authored as JSON strings and parsed before the call.
    row = _row(load_type="full_load", query_params='{"since": "2024-01-01"}', request_body='{"filter": "active"}')
    ext, client = _extractor(row, [{"id": "1"}])
    ext.extract()
    _, kwargs = client.iter_records.call_args
    assert kwargs["query_params"] == {"since": "2024-01-01"}
    assert kwargs["body"] == {"filter": "active"}


def test_empty_json_fields_default_to_empty_and_none():
    row = _row(load_type="full_load")
    ext, client = _extractor(row, [{"id": "1"}])
    ext.extract()
    _, kwargs = client.iter_records.call_args
    assert kwargs["query_params"] == {}
    assert kwargs["body"] is None


def test_incremental_flag_reflected_on_table():
    row = _row(load_type="incremental_load", primary_key=["id"])
    ext, _ = _extractor(row, [{"id": "1"}])
    result = ext.extract()
    assert result.tables[0].incremental is True


def test_no_state_produced():
    row = _row(load_type="full_load")
    ext, _ = _extractor(row, [{"id": "1"}])
    result = ext.extract()
    assert not hasattr(result, "state")


def test_native_column_types_inferred_from_rows():
    # T2: restlet output must carry native column types inferred from the fetched rows, not STRING
    # for everything.
    row = _row(load_type="full_load")
    ext, _ = _extractor(row, [{"id": "1", "amount": 12.5, "qty": 3, "active": True}])
    table = ext.extract().tables[0]
    assert table.columns == ["id", "amount", "qty", "active"]
    assert table.column_types == {"id": "string", "amount": "numeric", "qty": "integer", "active": "boolean"}
