"""Unit tests for the record extractor (mocked REST client)."""

import json
from unittest import mock

from configuration import RecordRow
from extractor.record import RecordExtractor


def _row(**kw):
    base = {"mode": "record", "record_type": "customer", "output_table_name": "customer"}
    base.update(kw)
    return RecordRow(**base)


def _extractor(row, records, since=None):
    client = mock.Mock()
    client.iter_record_collection.return_value = iter(records)
    ext = RecordExtractor(
        row=row,
        rest_client=client,
        since=since,
        server_time_provider=lambda: "2024-05-01T00:00:00Z",
    )
    return ext, client


def test_incremental_filter_added_to_q():
    row = _row(load_type="incremental_load", incremental_field="lastModifiedDate")
    ext, client = _extractor(row, [{"id": "1"}], since="2024-01-01T00:00:00Z")
    ext.extract()
    _, kwargs = client.iter_record_collection.call_args
    assert 'lastModifiedDate ON_OR_AFTER "2024-01-01T00:00:00Z"' in kwargs["q"]


def test_user_query_filter_and_incremental_combined():
    row = _row(load_type="incremental_load", incremental_field="lastModifiedDate", query_filter='email CONTAINS "x"')
    ext, client = _extractor(row, [{"id": "1"}], since="2024-01-01T00:00:00Z")
    ext.extract()
    _, kwargs = client.iter_record_collection.call_args
    assert 'email CONTAINS "x"' in kwargs["q"]
    assert "ON_OR_AFTER" in kwargs["q"]


def test_full_load_has_no_incremental_filter():
    row = _row(load_type="full_load")
    ext, client = _extractor(row, [{"id": "1"}])
    result = ext.extract()
    _, kwargs = client.iter_record_collection.call_args
    assert kwargs["q"] is None
    assert result.tables[0].incremental is False


def test_flatten_serializes_sublist_into_parent_column():
    row = _row(sublist_handling="flatten")
    records = [{"id": "1", "entityid": "ACME", "item": {"items": [{"line": 1}, {"line": 2}]}}]
    ext, _ = _extractor(row, records)
    result = ext.extract()
    assert len(result.tables) == 1
    rows = list(result.tables[0].rows)
    assert len(rows) == 1
    assert json.loads(rows[0]["item"]) == [{"line": 1}, {"line": 2}]


def test_child_table_splits_sublist_keyed_to_parent():
    row = _row(sublist_handling="child_table", output_table_name="invoice")
    records = [{"id": "10", "tranid": "INV10", "item": {"items": [{"line": 1}, {"line": 2}]}}]
    ext, _ = _extractor(row, records)
    result = ext.extract()
    tables = {t.name: t for t in result.tables}
    assert set(tables) == {"invoice", "invoice_item"}
    parent_rows = list(tables["invoice"].rows)
    assert "item" not in parent_rows[0]
    child_rows = list(tables["invoice_item"].rows)
    assert len(child_rows) == 2
    assert all(r["_parent_id"] == "10" for r in child_rows)


def test_state_captured_from_server_time_before_fetch():
    calls = []
    client = mock.Mock()

    def fake_iter(*a, **k):
        calls.append("fetch")
        return iter([{"id": "1"}])

    client.iter_record_collection.side_effect = fake_iter

    def provider():
        calls.append("server_time")
        return "2024-05-01T00:00:00Z"

    row = _row(load_type="incremental_load")
    ext = RecordExtractor(row=row, rest_client=client, since=None, server_time_provider=provider)
    result = ext.extract()
    # watermark must be read before the fetch begins
    assert calls == ["server_time", "fetch"]
    assert result.state == {"last_run": "2024-05-01T00:00:00Z"}
