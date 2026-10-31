"""Unit tests for the SuiteQL extractor (mocked REST client)."""

from unittest import mock

import pytest
from keboola.component.exceptions import UserException

from configuration import SuiteQLRow
from extractor.base import SCHEMA_SAMPLE_SIZE
from extractor.suiteql import SuiteQLExtractor, date_format_from_probe


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
    # F1: extract() must NOT drain the client generator (a ~100k-row pull would OOM). It samples a
    # bounded head (SCHEMA_SAMPLE_SIZE rows) to resolve the schema and streams the rest lazily. The
    # infinite generator proves it never drains to exhaustion.
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
    # Only the bounded head was pulled to resolve columns; the generator is not drained to exhaustion.
    assert len(consumed) == SCHEMA_SAMPLE_SIZE
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


# ---- Column Types ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rendered", "expected"),
    [
        ("12/31/2001", "%m/%d/%Y"),
        ("31/12/2001", "%d/%m/%Y"),
        ("31.12.2001", "%d.%m.%Y"),
        ("2001/12/31", "%Y/%m/%d"),
        ("31-Dec-2001", "%d-%b-%Y"),
        ("31 December, 2001", "%d %B, %Y"),
    ],
)
def test_date_format_from_probe(rendered, expected):
    assert date_format_from_probe(rendered) == expected


def test_date_format_from_probe_rejects_two_digit_year():
    with pytest.raises(UserException, match="12/31/01"):
        date_format_from_probe("12/31/01")


def _typed_rows(column_types, rows, probe="12/31/2001"):
    """Run the extractor with Column Types; the second iter_suiteql call is the date-format probe."""
    row = _row(
        load_type="full_load",
        column_types=[{"column": col, "type": typ} for col, typ in column_types.items()],
    )
    ext, client = _extractor(row, [rows, [{"links": [], "probe": probe}]])
    table = ext.extract().tables[0]
    return list(table.rows), table, client


def test_set_column_types_reach_the_table():
    _, table, _ = _typed_rows({"amount": "numeric", "trandate": "date"}, [{"amount": "1.5", "trandate": "4/30/2025"}])
    assert table.set_column_types == {"amount": "numeric", "trandate": "date"}


@pytest.mark.parametrize(
    ("column_type", "value", "expected"),
    [
        ("date", "4/30/2025", "2025-04-30"),
        ("date", "2025-04-30", "2025-04-30"),
        ("date", "2025-04-30T10:11:12", "2025-04-30"),
        ("timestamp", "2025-04-30T10:11:12", "2025-04-30T10:11:12"),
        ("boolean", "T", "true"),
        ("boolean", "F", "false"),
        ("integer", "42", "42"),
        ("numeric", "1.5", "1.5"),
    ],
)
def test_values_converted_to_the_set_type(column_type, value, expected):
    rows, _, _ = _typed_rows({"col": column_type}, [{"id": "1", "col": value}])
    assert rows[0]["col"] == expected


def test_date_in_the_netsuite_user_format_d_m_y():
    rows, _, _ = _typed_rows({"col": "date"}, [{"col": "30/4/2025"}], probe="31/12/2001")
    assert rows[0]["col"] == "2025-04-30"


def test_date_format_probed_once_and_only_when_needed():
    rows, _, client = _typed_rows({"d": "date"}, [{"d": "2025-04-30"}, {"d": "2025-05-01"}])
    assert [r["d"] for r in rows] == ["2025-04-30", "2025-05-01"]
    assert client.iter_suiteql.call_count == 1

    rows, _, client = _typed_rows({"d": "date"}, [{"d": "4/30/2025"}, {"d": "5/1/2025"}])
    assert [r["d"] for r in rows] == ["2025-04-30", "2025-05-01"]
    assert client.iter_suiteql.call_count == 2


def test_null_values_left_alone():
    rows, _, client = _typed_rows({"d": "date", "b": "boolean"}, [{"id": "1"}, {"id": "2", "d": ""}])
    assert rows == [{"id": "1"}, {"id": "2", "d": ""}]
    assert client.iter_suiteql.call_count == 1


def test_value_that_is_not_a_date_raises_user_exception():
    with pytest.raises(UserException, match="'col' has the type Date.*TO_CHAR"):
        _typed_rows({"col": "date"}, [{"col": "not a date"}])


def test_timestamp_without_time_raises_user_exception():
    # NetSuite renders a date with time as the date only; the time is gone, so do not invent midnight.
    with pytest.raises(UserException, match="'col' has the type Timestamp.*TO_CHAR"):
        _typed_rows({"col": "timestamp"}, [{"col": "4/30/2025"}])


def test_unknown_set_column_is_logged(caplog):
    _typed_rows({"tpyo": "date"}, [{"id": "1"}])
    assert "tpyo" in caplog.text


# ---- Deleted Records Query ---------------------------------------------------------------------


def _deleted_run(main_rows, deleted_rows, **kw):
    row = _row(
        load_type="incremental_load", primary_key=["id"], deleted_query="SELECT recordid AS id FROM deletedrecord", **kw
    )
    ext, client = _extractor(row, [main_rows, deleted_rows])
    table = ext.extract().tables[0]
    return list(table.rows), table, client


def test_deleted_records_flagged_after_main_rows():
    rows, table, client = _deleted_run([{"id": "1", "name": "A"}], [{"id": "7", "deleteddate": "x"}])
    assert rows == [{"id": "1", "name": "A", "is_deleted": "0"}, {"id": "7", "is_deleted": "1"}]
    assert table.columns == ["id", "name", "is_deleted"]
    assert client.iter_suiteql.call_args_list[1][0][0] == "SELECT recordid AS id FROM deletedrecord"


def test_deleted_records_with_empty_main_result():
    rows, table, _ = _deleted_run([], [{"id": "7"}])
    assert rows == [{"id": "7", "is_deleted": "1"}]
    assert table.columns is None


def test_deleted_row_without_primary_key_raises_user_exception():
    with pytest.raises(UserException, match="without the primary key column\\(s\\) \\['id'\\].*recordid AS id"):
        _deleted_run([{"id": "1"}], [{"recordid": "7"}])


def test_main_query_with_is_deleted_column_raises_user_exception():
    with pytest.raises(UserException, match="column named 'is_deleted'"):
        _deleted_run([{"id": "1", "is_deleted": "F"}], [])


def test_date_placeholders_substituted_in_deleted_query():
    row = _row(
        load_type="incremental_load",
        primary_key=["id"],
        deleted_query="SELECT recordid AS id FROM deletedrecord WHERE deleteddate >= :date_from",
        date_from="2026-09-29",
    )
    ext, client = _extractor(row, [[{"id": "1"}], [{"id": "7"}]])
    list(ext.extract().tables[0].rows)
    deleted_query = client.iter_suiteql.call_args_list[1][0][0]
    assert ":date_from" not in deleted_query
    assert "TO_TIMESTAMP('2026-09-29" in deleted_query


def test_no_deleted_query_no_is_deleted_column():
    row = _row(load_type="full_load")
    ext, _ = _extractor(row, [[{"id": "1"}]])
    table = ext.extract().tables[0]
    assert list(table.rows) == [{"id": "1"}]
    assert table.columns == ["id"]
