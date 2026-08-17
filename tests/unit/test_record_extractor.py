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


def _extractor(row, records):
    """Build an extractor whose collection is ID-only and whose get_record returns full records.

    This mirrors NetSuite: the REST record collection returns ids + links only (spec §9 risk 5), so
    record mode fetches each record with expandSubResources (I3).
    """
    client = mock.Mock()
    client.iter_record_collection.return_value = iter([{"id": r.get("id")} for r in records])
    by_id = {str(r.get("id")): r for r in records}
    client.get_record.side_effect = lambda rt, rid, expand_sub_resources=True: by_id[str(rid)]
    return RecordExtractor(row=row, rest_client=client), client


def test_user_query_filter_forwarded_as_q():
    row = _row(load_type="full_load", query_filter='email CONTAINS "x"', primary_key=["id"])
    ext, client = _extractor(row, [{"id": "1"}])
    ext.extract()
    _, kwargs = client.iter_record_collection.call_args
    assert kwargs["q"] == 'email CONTAINS "x"'


def test_no_query_filter_means_q_none():
    row = _row(load_type="full_load")
    ext, client = _extractor(row, [{"id": "1"}])
    result = ext.extract()
    _, kwargs = client.iter_record_collection.call_args
    assert kwargs["q"] is None
    assert result.tables[0].incremental is False


def test_incremental_flag_reflected_on_table():
    row = _row(load_type="incremental_load", primary_key=["id"])
    ext, _ = _extractor(row, [{"id": "1"}])
    result = ext.extract()
    assert result.tables[0].incremental is True


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


def test_child_key_requires_candidate_on_every_row():
    # T4: a candidate key present on only SOME child rows must not be chosen as the composite PK —
    # rows missing it would collide/drop on upsert. Require it present AND non-null on EVERY row.
    row = _row(
        sublist_handling="child_table", output_table_name="invoice", load_type="incremental_load", primary_key=["id"]
    )
    # 'line' present on both rows; 'id' present on only one -> must pick 'line', not 'id'.
    records = [{"id": "10", "item": {"items": [{"line": 1, "id": "a"}, {"line": 2}]}}]
    ext, _ = _extractor(row, records)
    result = ext.extract()
    child = {t.name: t for t in result.tables}["invoice_item"]
    assert child.primary_key == ["_parent_id", "line"]


def test_child_key_rejects_candidate_null_on_some_rows():
    # T4: a candidate present on every row but NULL on some is not a sound key -> fall through.
    row = _row(
        sublist_handling="child_table", output_table_name="invoice", load_type="incremental_load", primary_key=["id"]
    )
    # 'line' is null on the second row; 'sequence' is complete -> must pick 'sequence'.
    records = [{"id": "10", "item": {"items": [{"line": 1, "sequence": 5}, {"line": None, "sequence": 6}]}}]
    ext, _ = _extractor(row, records)
    result = ext.extract()
    child = {t.name: t for t in result.tables}["invoice_item"]
    assert child.primary_key == ["_parent_id", "sequence"]


def test_child_key_incremental_rejected_when_no_candidate_on_every_row():
    # T4: when no candidate is present-and-non-null on EVERY row, incremental must be rejected.
    row = _row(
        sublist_handling="child_table", output_table_name="invoice", load_type="incremental_load", primary_key=["id"]
    )
    records = [{"id": "10", "item": {"items": [{"line": 1}, {"key": "x"}]}}]
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


def test_native_column_types_inferred_for_record_output():
    # T2: record output (flatten) must carry native column types inferred from the fetched records.
    row = _row(load_type="full_load", sublist_handling="flatten")
    records = [{"id": "1", "balance": 100.5, "isInactive": False, "daysOverdue": 4}]
    ext, _ = _extractor(row, records)
    table = ext.extract().tables[0]
    assert table.column_types["balance"] == "numeric"
    assert table.column_types["isInactive"] == "boolean"
    assert table.column_types["daysOverdue"] == "integer"
    assert table.column_types["id"] == "string"


def test_no_state_produced():
    # §4: no state watermark is produced anymore.
    row = _row(load_type="full_load")
    ext, _ = _extractor(row, [{"id": "1"}])
    result = ext.extract()
    assert not hasattr(result, "state")


# ---- Fields picker column shaping (finding #1) ------------------------------


def test_fields_projection_restricts_columns_and_retains_pk():
    # A selected Fields list must restrict emitted columns to that selection, in the selected
    # order, while always keeping the primary key so the writer's PK-subset-of-columns check holds.
    row = _row(load_type="full_load", fields=["entityid"], primary_key=["id"])
    records = [{"id": "1", "entityid": "ACME", "balance": 100.0}]
    ext, _ = _extractor(row, records)
    rows = list(ext.extract().tables[0].rows)
    assert list(rows[0].keys()) == ["entityid", "id"]
    assert rows[0] == {"entityid": "ACME", "id": "1"}


def test_empty_fields_emits_all_columns():
    # No Fields selection means pass every column through unchanged (current/legacy behavior).
    row = _row(load_type="full_load")
    records = [{"id": "1", "entityid": "ACME", "balance": 100.0}]
    ext, _ = _extractor(row, records)
    rows = list(ext.extract().tables[0].rows)
    assert rows[0] == {"id": "1", "entityid": "ACME", "balance": 100.0}


def test_child_table_fields_filter_only_splits_selected_sublist():
    # child_table + a Fields selection: only split a sublist whose key was selected; a sublist not
    # in the selection (and not itself selected) must not appear anywhere in the output.
    row = _row(
        sublist_handling="child_table",
        output_table_name="invoice",
        load_type="full_load",
        fields=["tranid", "item"],
    )
    records = [
        {
            "id": "10",
            "tranid": "INV10",
            "item": {"items": [{"line": 1}]},
            "expense": {"items": [{"line": 1, "amount": 5}]},
        }
    ]
    ext, _ = _extractor(row, records)
    result = ext.extract()
    assert {t.name for t in result.tables} == {"invoice", "invoice_item"}


# ---- HATEOAS 'links' is reserved, never emitted (finding #2) ----------------


def test_links_never_emitted_as_column_in_flatten():
    row = _row(sublist_handling="flatten", load_type="full_load")
    records = [{"id": "1", "entityid": "ACME", "links": [{"rel": "self", "href": "http://x"}]}]
    ext, _ = _extractor(row, records)
    rows = list(ext.extract().tables[0].rows)
    assert "links" not in rows[0]


def test_links_never_becomes_a_child_table():
    row = _row(sublist_handling="child_table", output_table_name="customer", load_type="full_load")
    records = [{"id": "1", "entityid": "ACME", "links": [{"rel": "self", "href": "http://x"}]}]
    ext, _ = _extractor(row, records)
    result = ext.extract()
    tables = {t.name: t for t in result.tables}
    assert set(tables) == {"customer"}
    assert "links" not in list(tables["customer"].rows)[0]


def test_nested_links_stripped_from_flatten_json_blob():
    # NetSuite attaches a 'links' array to every sublist item too — recursive strip must remove it
    # from inside the JSON-serialized sublist blob, not just the top-level record.
    row = _row(sublist_handling="flatten", load_type="full_load")
    records = [
        {
            "id": "1",
            "entityid": "ACME",
            "links": [{"rel": "self"}],
            "addressBook": {"items": [{"line": 1, "links": [{"rel": "self"}]}]},
        }
    ]
    ext, _ = _extractor(row, records)
    rows = list(ext.extract().tables[0].rows)
    assert "links" not in rows[0]
    assert json.loads(rows[0]["addressBook"]) == [{"line": 1}]


def test_child_table_name_sanitizes_unsafe_sublist_key():
    # The API-supplied sublist key is spliced into the child-table name; an unsafe key must be
    # sanitized so it can't escape data/out/tables (symmetry with the output_table_name slug guard).
    row = _row(sublist_handling="child_table", output_table_name="customer", load_type="full_load")
    records = [{"id": "1", "bad/name": {"items": [{"line": 1}]}}]
    ext, _ = _extractor(row, records)
    names = {t.name for t in ext.extract().tables}
    assert "customer_bad_name" in names
    assert not any("/" in n for n in names)


def test_record_type_fallback_table_name_sanitized():
    # With no output_table_name the table name falls back to record_type, which is unvalidated config
    # input, so it too must be sanitized.
    row = _row(record_type="weird/type", output_table_name="", load_type="full_load")
    records = [{"id": "1", "entityid": "ACME"}]
    ext, _ = _extractor(row, records)
    assert ext.extract().tables[0].name == "weird_type"


def test_nested_links_stripped_from_child_table_rows():
    row = _row(sublist_handling="child_table", output_table_name="customer", load_type="full_load")
    records = [
        {
            "id": "1",
            "entityid": "ACME",
            "addressBook": {"items": [{"line": 1, "links": [{"rel": "self"}]}]},
        }
    ]
    ext, _ = _extractor(row, records)
    tables = {t.name: t for t in ext.extract().tables}
    child_rows = list(tables["customer_addressBook"].rows)
    assert child_rows and all("links" not in r for r in child_rows)
