"""Unit tests for the SuiteQL extractor (mocked REST client)."""

from unittest import mock

from configuration import SuiteQLRow
from extractor.suiteql import SuiteQLExtractor


def _row(**kw):
    base = {"mode": "suiteql", "output_table_name": "result", "query": "SELECT id FROM customer"}
    base.update(kw)
    return SuiteQLRow(**base)


def _extractor(row, pages, since=None, server_now="2024-05-01T00:00:00Z"):
    client = mock.Mock()
    # iter_suiteql returns a fresh iterator per call (per window)
    client.iter_suiteql.side_effect = [iter(p) for p in pages]
    ext = SuiteQLExtractor(
        row=row,
        rest_client=client,
        since=since,
        server_time_provider=lambda: server_now,
    )
    return ext, client


def test_incremental_binds_state_placeholder():
    row = _row(query="SELECT id FROM customer WHERE lastmodifieddate > :state", load_type="incremental_load")
    ext, client = _extractor(row, [[{"id": "1"}]], since="2024-01-01T00:00:00Z")
    ext.extract()
    query = client.iter_suiteql.call_args[0][0]
    assert ":state" not in query
    assert "2024-01-01T00:00:00Z" in query


def test_full_load_runs_query_verbatim():
    row = _row(query="SELECT id FROM customer", load_type="full_load")
    ext, client = _extractor(row, [[{"id": "1"}]])
    result = ext.extract()
    assert client.iter_suiteql.call_args[0][0] == "SELECT id FROM customer"
    assert result.tables[0].incremental is False


def test_windowing_splits_large_range():
    row = _row(
        query="SELECT id FROM tx WHERE trandate BETWEEN :window_start AND :window_end",
        window_size=30,
        load_type="incremental_load",
    )
    ext, client = _extractor(
        row,
        [[{"id": "1"}], [{"id": "2"}], [{"id": "3"}]],
        since="2024-01-01T00:00:00Z",
        server_now="2024-03-02T00:00:00Z",
    )
    result = ext.extract()
    # 2024-01-01 -> 2024-03-02 in 30-day windows == 3 windows
    assert client.iter_suiteql.call_count == 3
    queries = [c[0][0] for c in client.iter_suiteql.call_args_list]
    assert "2024-01-01T00:00:00Z" in queries[0]
    assert "2024-03-02T00:00:00Z" in queries[-1]
    # rows from every window are concatenated into one table
    assert [r["id"] for r in result.tables[0].rows] == ["1", "2", "3"]


def test_watermark_captured_before_fetch():
    calls = []
    client = mock.Mock()

    def fake_iter(*a, **k):
        calls.append("fetch")
        return iter([{"id": "1"}])

    client.iter_suiteql.side_effect = fake_iter

    def provider():
        calls.append("server_time")
        return "2024-05-01T00:00:00Z"

    row = _row(load_type="incremental_load")
    ext = SuiteQLExtractor(row=row, rest_client=client, since=None, server_time_provider=provider)
    result = ext.extract()
    assert calls[0] == "server_time"
    assert "fetch" in calls
    assert result.state == {"last_run": "2024-05-01T00:00:00Z"}


def test_typed_columns_inferred():
    row = _row(load_type="full_load")
    ext, _ = _extractor(row, [[{"id": 1, "name": "ACME", "balance": 10.5, "active": True}]])
    result = ext.extract()
    table = result.tables[0]
    assert table.column_types["id"] == "integer"
    assert table.column_types["name"] == "string"
    assert table.column_types["balance"] == "numeric"
    assert table.column_types["active"] == "boolean"
