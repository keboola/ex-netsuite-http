"""Unit tests for the SuiteQL extractor (mocked REST client)."""

from unittest import mock

import pytest
from keboola.component.exceptions import UserException

from configuration import SuiteQLRow
from extractor.suiteql import SuiteQLExtractor


def _row(**kw):
    base = {"mode": "suiteql", "output_table_name": "result", "query": "SELECT id FROM customer"}
    base.update(kw)
    return SuiteQLRow(**base)


def _extractor(row, pages):
    client = mock.Mock()
    client.iter_suiteql.side_effect = [iter(p) for p in pages]
    return SuiteQLExtractor(row=row, rest_client=client), client


def test_query_run_verbatim_without_placeholders():
    row = _row(query="SELECT id FROM customer", load_type="full_load")
    ext, client = _extractor(row, [[{"id": "1"}]])
    result = ext.extract()
    assert client.iter_suiteql.call_args[0][0] == "SELECT id FROM customer"
    assert result.tables[0].incremental is False


def test_incremental_flag_reflected_on_table():
    row = _row(load_type="incremental_load", primary_key=["id"])
    ext, _ = _extractor(row, [[{"id": "1"}]])
    result = ext.extract()
    assert result.tables[0].incremental is True


def test_date_placeholders_substituted():
    row = _row(
        query="SELECT id FROM tx WHERE trandate BETWEEN :date_from AND :date_to",
        load_type="full_load",
        date_from="2024-01-01",
        date_to="2024-03-01",
    )
    ext, client = _extractor(row, [[{"id": "1"}]])
    ext.extract()
    query = client.iter_suiteql.call_args[0][0]
    assert ":date_from" not in query
    assert ":date_to" not in query
    assert "2024-01-01" in query
    assert "2024-03-01" in query
    assert "TO_TIMESTAMP" in query


def test_no_substitution_when_no_placeholders():
    row = _row(query="SELECT id FROM customer", load_type="full_load")
    ext, client = _extractor(row, [[{"id": "1"}]])
    ext.extract()
    assert client.iter_suiteql.call_args[0][0] == "SELECT id FROM customer"


def test_unparseable_date_raises_user_exception():
    row = _row(
        query="SELECT id FROM tx WHERE trandate > :date_from",
        load_type="full_load",
        date_from="not a date at all zzz",
        date_to="now",
    )
    ext, _ = _extractor(row, [[{"id": "1"}]])
    with pytest.raises(UserException, match="date range"):
        ext.extract()


def test_no_state_produced():
    # §4: the extraction result no longer carries any state.
    row = _row(load_type="incremental_load", primary_key=["id"])
    ext, _ = _extractor(row, [[{"id": "1"}]])
    result = ext.extract()
    assert not hasattr(result, "state")


def test_rows_streamed_not_materialized():
    # F1: extract() must NOT drain the client generator (a ~100k-row pull would OOM). It peeks only
    # the first row to resolve the schema and streams the rest lazily to the writer.
    consumed: list[int] = []

    def infinite():
        i = 0
        while True:
            consumed.append(i)
            yield {"id": i}
            i += 1

    client = mock.Mock()
    client.iter_suiteql.return_value = infinite()
    ext = SuiteQLExtractor(row=_row(load_type="full_load"), rest_client=client)

    result = ext.extract()
    # Only the first row was pulled to resolve columns; the generator is not drained.
    assert consumed == [0]
    table = result.tables[0]
    assert table.columns == ["id"]

    stream = iter(table.rows)
    first_three = [next(stream) for _ in range(3)]
    assert [r["id"] for r in first_three] == [0, 1, 2]


def test_typed_columns_inferred():
    row = _row(load_type="full_load")
    ext, _ = _extractor(row, [[{"id": 1, "name": "ACME", "balance": 10.5, "active": True}]])
    result = ext.extract()
    table = result.tables[0]
    assert table.column_types["id"] == "integer"
    assert table.column_types["name"] == "string"
    assert table.column_types["balance"] == "numeric"
    assert table.column_types["active"] == "boolean"
