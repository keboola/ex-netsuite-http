"""Unit tests for the metadata extractor (mocked REST client).

The ``metadata`` mode dumps NetSuite's schema catalog into two tables:
``record_types`` (every record type from the metadata catalog) and ``fields`` (the field
definitions of every record type, one metadata-catalog call per type). Fixture responses mirror the
shape of NetSuite's ``application/schema+json`` metadata catalog (catalog ``items`` carry name+links;
a per-type schema carries ``properties``).
"""

import json
import logging
from unittest import mock

import pytest
from keboola.component.exceptions import UserException

from client.http_base import NetSuiteResourceError
from configuration import MetadataRow
from extractor.metadata import MetadataExtractor

CATALOG = {
    "items": [
        {"name": "customer", "links": [{"rel": "self", "href": "https://x/metadata-catalog/customer"}]},
        {"name": "invoice", "links": [{"rel": "self", "href": "https://x/metadata-catalog/invoice"}]},
    ]
}

CUSTOMER_SCHEMA = {
    "properties": {
        "id": {"type": "string", "title": "Internal ID"},
        "companyName": {"type": "string", "title": "Company Name", "nullable": True},
        "balance": {"type": ["number", "null"], "title": "Balance", "format": "double"},
        "category": {"type": "string", "enum": ["A", "B"]},
    }
}
INVOICE_SCHEMA = {
    "properties": {
        "id": {"type": "string"},
        "total": {"type": "number", "format": "double"},
    }
}


def _row(**kw):
    base = {"mode": "metadata"}
    base.update(kw)
    return MetadataRow(**base)


def _client(catalog, schemas, failing=()):
    """A mocked RestClient whose get_metadata_catalog() returns the catalog, and
    get_metadata_catalog(record_type) returns that type's schema (or raises for a failing type)."""
    client = mock.Mock()

    def fake_catalog(record_type=None):
        if record_type is None:
            return catalog
        if record_type in failing:
            # A scoped per-type failure (403 not permitted / 404 not found): the extractor skips it.
            raise NetSuiteResourceError(f"boom for {record_type}", status_code=404)
        return schemas[record_type]

    client.get_metadata_catalog.side_effect = fake_catalog
    return client


def _table(result, name):
    for table in result.tables:
        if table.name == name:
            return table
    raise AssertionError(f"no table named {name!r} in {[t.name for t in result.tables]}")


def _find(rows, **kw):
    for row in rows:
        if all(row.get(k) == v for k, v in kw.items()):
            return row
    raise AssertionError(f"no row matching {kw}")


def test_record_types_table_lists_all_types():
    client = _client(CATALOG, {"customer": CUSTOMER_SCHEMA, "invoice": INVOICE_SCHEMA})
    result = MetadataExtractor(row=_row(), rest_client=client).extract()
    rt = _table(result, "record_types")
    assert rt.primary_key == ["name"]
    assert rt.columns == ["name", "href"]
    rows = list(rt.rows)
    assert {r["name"] for r in rows} == {"customer", "invoice"}
    assert _find(rows, name="customer")["href"].endswith("/metadata-catalog/customer")


def test_fields_table_from_per_type_schemas():
    client = _client(CATALOG, {"customer": CUSTOMER_SCHEMA, "invoice": INVOICE_SCHEMA})
    result = MetadataExtractor(row=_row(), rest_client=client).extract()
    ft = _table(result, "fields")
    assert ft.primary_key == ["record_type", "field_name"]
    rows = list(ft.rows)
    assert len(rows) == 6  # 4 customer fields + 2 invoice fields
    id_row = _find(rows, record_type="customer", field_name="id")
    assert id_row["type"] == "string"
    assert id_row["title"] == "Internal ID"


def test_fields_table_columns_and_types():
    client = _client(CATALOG, {"customer": CUSTOMER_SCHEMA, "invoice": INVOICE_SCHEMA})
    ft = _table(MetadataExtractor(row=_row(), rest_client=client).extract(), "fields")
    assert ft.columns == ["record_type", "field_name", "type", "title", "format", "nullable", "enum", "definition"]
    assert ft.column_types["nullable"] == "boolean"
    assert ft.column_types["record_type"] == "string"


def test_nullable_normalized_from_flag_and_type_list():
    client = _client(CATALOG, {"customer": CUSTOMER_SCHEMA, "invoice": INVOICE_SCHEMA})
    rows = list(_table(MetadataExtractor(row=_row(), rest_client=client).extract(), "fields").rows)
    assert _find(rows, record_type="customer", field_name="companyName")["nullable"] is True  # explicit flag
    balance = _find(rows, record_type="customer", field_name="balance")
    assert balance["type"] == "number"  # "null" stripped from the type list
    assert balance["nullable"] is True
    assert _find(rows, record_type="customer", field_name="id")["nullable"] is None  # neither flag nor null


def test_enum_and_raw_definition_serialized_as_json():
    client = _client(CATALOG, {"customer": CUSTOMER_SCHEMA, "invoice": INVOICE_SCHEMA})
    rows = list(_table(MetadataExtractor(row=_row(), rest_client=client).extract(), "fields").rows)
    category = _find(rows, record_type="customer", field_name="category")
    assert json.loads(category["enum"]) == ["A", "B"]
    assert json.loads(category["definition"]) == CUSTOMER_SCHEMA["properties"]["category"]
    assert _find(rows, record_type="customer", field_name="id")["enum"] is None  # no enum -> empty


def test_per_type_failure_is_skipped_and_warned(caplog):
    client = _client(CATALOG, {"customer": CUSTOMER_SCHEMA}, failing=("invoice",))
    with caplog.at_level(logging.WARNING):
        result = MetadataExtractor(row=_row(), rest_client=client).extract()
        rows = list(_table(result, "fields").rows)  # per-type calls fire while the stream is consumed
    assert {r["record_type"] for r in rows} == {"customer"}  # invoice skipped, not fatal
    assert any("invoice" in rec.message for rec in caplog.records)
    # the record_types table still lists both — it comes from the catalog, not the per-type calls
    assert {r["name"] for r in _table(result, "record_types").rows} == {"customer", "invoice"}


def test_per_type_systemic_failure_aborts_run():
    # A systemic failure mid-walk (revoked credentials, outage, exhausted retries) is NOT a
    # NetSuiteResourceError, so it must propagate and abort the run — unlike a per-type 403/404, which
    # is skipped. Otherwise the fields table would come out near-empty while the job reports success.
    client = mock.Mock()

    def fake_catalog(record_type=None):
        if record_type is None:
            return CATALOG
        raise UserException("NetSuite authentication failed (401).")

    client.get_metadata_catalog.side_effect = fake_catalog
    result = MetadataExtractor(row=_row(), rest_client=client).extract()
    with pytest.raises(UserException, match="401"):
        list(_table(result, "fields").rows)  # the systemic failure fires as the stream is consumed


def test_snapshot_is_full_load_on_both_tables():
    # Metadata is a schema snapshot -> a full rewrite each run (an upsert would leave stale field rows
    # behind when a record type or field disappears). load_type defaults to full_load for this mode.
    client = _client(CATALOG, {"customer": CUSTOMER_SCHEMA, "invoice": INVOICE_SCHEMA})
    result = MetadataExtractor(row=_row(), rest_client=client).extract()
    assert all(t.incremental is False for t in result.tables)


def test_blank_name_record_type_excluded_from_both_tables():
    # A catalog item with no resolvable name would otherwise emit an empty-string primary key in
    # record_types (and two such items would collide). Such items are dropped from the table and never
    # fetched for fields, keeping the two tables consistent.
    catalog = {
        "items": [
            {"name": "customer", "links": [{"href": "https://x/metadata-catalog/customer"}]},
            {"links": []},  # no name and no href -> derived name is ""
        ]
    }
    client = _client(catalog, {"customer": CUSTOMER_SCHEMA})
    result = MetadataExtractor(row=_row(), rest_client=client).extract()
    assert [r["name"] for r in _table(result, "record_types").rows] == ["customer"]
    assert {r["record_type"] for r in _table(result, "fields").rows} == {"customer"}


def test_catalog_fetch_error_propagates():
    # The initial catalog call is the auth/connection gate: unlike a per-type failure it must NOT be
    # swallowed, or the run would produce empty tables on bad credentials.
    client = mock.Mock()
    client.get_metadata_catalog.side_effect = UserException("401 Unauthorized")
    with pytest.raises(UserException, match="401"):
        MetadataExtractor(row=_row(), rest_client=client).extract()
