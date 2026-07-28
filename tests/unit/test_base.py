"""Unit tests for the extractor.base streaming schema helper."""

from extractor.base import SCHEMA_SAMPLE_SIZE, resolve_stream_schema


def test_resolve_stream_schema_unions_columns_across_ragged_head():
    """NetSuite omits null-valued keys per row, so the column union must be taken across the
    sampled head — not just the first row — or a column null in row 1 would be dropped."""
    rows = [
        {"id": 1, "name": "a"},  # 'email' omitted (null) here
        {"id": 2, "name": "b", "email": "b@example.com"},
    ]
    stream, columns, types = resolve_stream_schema(iter(rows))
    assert columns == ["id", "name", "email"]
    assert types == {"id": "integer", "name": "string", "email": "string"}
    # the stream still yields every original row, unchanged
    assert list(stream) == rows


def test_resolve_stream_schema_empty_yields_no_schema():
    stream, columns, types = resolve_stream_schema(iter([]))
    assert list(stream) == []
    assert columns is None
    assert types is None


def test_resolve_stream_schema_streams_beyond_the_sample():
    """Rows past the sampled head are still yielded (the tail is chained lazily, not dropped)."""
    rows = [{"id": i} for i in range(SCHEMA_SAMPLE_SIZE + 25)]
    stream, columns, _ = resolve_stream_schema(iter(rows))
    assert columns == ["id"]
    assert len(list(stream)) == SCHEMA_SAMPLE_SIZE + 25
