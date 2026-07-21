"""Unit tests for the record extractor (mocked REST client)."""

import json
from unittest import mock

import pytest
from keboola.component.exceptions import UserException

from configuration import RecordRow
from extractor.record import RecordExtractor


def _row(**kw):
    base = {"mode": "record", "record_type": "customer", "output_table_name": "customer"}
    base.update(kw)
    return RecordRow(**base)


def _extractor(row, records, since=None):
    """Build an extractor whose collection is ID-only and whose get_record returns full records.

    This mirrors NetSuite: the REST record collection returns ids + links only (spec §9 risk 5), so
    record mode fetches each record with expandSubResources (I3).
    """
    client = mock.Mock()
    client.iter_record_collection.return_value = iter([{"id": r.get("id")} for r in records])
    by_id = {str(r.get("id")): r for r in records}
    client.get_record.side_effect = lambda rt, rid, expand_sub_resources=True: by_id[str(rid)]
    ext = RecordExtractor(
        row=row,
        rest_client=client,
        since=since,
        server_time_provider=lambda: "2024-05-01T00:00:00Z",
    )
    return ext, client


def test_incremental_filter_added_to_q():
    row = _row(load_type="incremental_load", incremental_field="lastModifiedDate", primary_key=["id"])
    ext, client = _extractor(row, [{"id": "1"}], since="2024-01-01T00:00:00Z")
    ext.extract()
    _, kwargs = client.iter_record_collection.call_args
    assert 'lastModifiedDate ON_OR_AFTER "1/1/2024"' in kwargs["q"]


def test_user_query_filter_and_incremental_combined():
    row = _row(
        load_type="incremental_load",
        incremental_field="lastModifiedDate",
        query_filter='email CONTAINS "x"',
        primary_key=["id"],
    )
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


def test_per_id_get_with_expand_sub_resources_called():
    # I3: record mode reads the ID-only collection then GETs each record with expandSubResources.
    row = _row(load_type="full_load", fields=["entityid"])
    ext, client = _extractor(row, [{"id": "1", "entityid": "ACME"}])
    result = ext.extract()
    client.get_record.assert_called_once_with("customer", "1", expand_sub_resources=True)
    assert list(result.tables[0].rows)[0]["entityid"] == "ACME"


def test_flatten_serializes_sublist_into_parent_column():
    row = _row(sublist_handling="flatten", load_type="full_load")
    records = [{"id": "1", "entityid": "ACME", "item": {"items": [{"line": 1}, {"line": 2}]}}]
    ext, _ = _extractor(row, records)
    result = ext.extract()
    assert len(result.tables) == 1
    rows = list(result.tables[0].rows)
    assert len(rows) == 1
    assert json.loads(rows[0]["item"]) == [{"line": 1}, {"line": 2}]


def test_child_table_splits_sublist_keyed_to_parent():
    row = _row(sublist_handling="child_table", output_table_name="invoice", load_type="full_load")
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


def test_child_table_incremental_gets_composite_pk_and_incremental():
    # B3: child tables must inherit incremental and a composite PK so incremental runs upsert lines
    # instead of truncating the child table to the current batch.
    row = _row(
        sublist_handling="child_table", output_table_name="invoice", load_type="incremental_load", primary_key=["id"]
    )
    records = [{"id": "10", "tranid": "INV10", "item": {"items": [{"line": 1}, {"line": 2}]}}]
    ext, _ = _extractor(row, records)
    result = ext.extract()
    child = {t.name: t for t in result.tables}["invoice_item"]
    assert child.incremental is True
    assert child.primary_key == ["_parent_id", "line"]


def test_child_table_incremental_rejected_without_derivable_key():
    # B3: if no sound per-line key exists, incremental child-table extraction is rejected (data loss).
    row = _row(
        sublist_handling="child_table", output_table_name="invoice", load_type="incremental_load", primary_key=["id"]
    )
    records = [{"id": "10", "item": {"items": [{"amount": 5.0}, {"amount": 7.0}]}}]
    ext, _ = _extractor(row, records)
    with pytest.raises(UserException, match="per-line key"):
        ext.extract()


def test_child_table_full_load_without_key_has_empty_pk():
    row = _row(sublist_handling="child_table", output_table_name="invoice", load_type="full_load")
    records = [{"id": "10", "item": {"items": [{"amount": 5.0}]}}]
    ext, _ = _extractor(row, records)
    result = ext.extract()
    child = {t.name: t for t in result.tables}["invoice_item"]
    assert child.primary_key == []
    assert child.incremental is False


def test_state_captured_from_server_time_before_fetch():
    calls = []
    client = mock.Mock()

    def fake_iter(*a, **k):
        calls.append("fetch")
        return iter([{"id": "1"}])

    client.iter_record_collection.side_effect = fake_iter
    client.get_record.side_effect = lambda *a, **k: {"id": "1"}

    def provider():
        calls.append("server_time")
        return "2024-05-01T00:00:00Z"

    row = _row(load_type="incremental_load", primary_key=["id"])
    ext = RecordExtractor(row=row, rest_client=client, since=None, server_time_provider=provider)
    result = ext.extract()
    # watermark must be read before the fetch begins
    assert calls == ["server_time", "fetch"]
    assert result.state == {"last_run": "2024-05-01T00:00:00Z"}


def test_full_load_also_persists_watermark():
    # NTH2: full loads persist the watermark so a later full->incremental switch resumes correctly.
    row = _row(load_type="full_load")
    ext, _ = _extractor(row, [{"id": "1"}])
    result = ext.extract()
    assert result.state == {"last_run": "2024-05-01T00:00:00Z"}
