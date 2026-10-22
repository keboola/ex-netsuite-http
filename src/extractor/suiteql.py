"""``suiteql`` mode extractor — arbitrary SuiteQL over REST.

Runs the user's query with ``hasMore`` pagination. When the query contains the ``:date_from`` /
``:date_to`` placeholders, the configured date range (parsed with Keboola's dateparser — relative
strings like "5 days ago" or absolute dates) is substituted into the query before it runs. A single
range is issued verbatim (no auto sub-chunking); a range that would exceed NetSuite's ~100k SuiteQL
row ceiling should be narrowed. NetSuite returns every SuiteQL value as text (dates in the user's
date format, booleans as ``T``/``F``), so the inferred column types come out as string. The row's
Column Types set the Storage type of named columns, and the extractor converts their values to match.
"""

import logging
import re
from collections.abc import Iterator
from datetime import date, datetime
from typing import Any, cast

from keboola.component.exceptions import UserException
from keboola.utils import parse_datetime_interval

from client.rest import RestClient
from configuration import SuiteQLRow
from extractor.base import ExtractionResult, Extractor, OutputTable, resolve_stream_schema

_DATE_FROM = ":date_from"
_DATE_TO = ":date_to"

# NetSuite SuiteQL rejects a bare quoted ISO literal in a date/timestamp comparison (400); the
# substituted date must be wrapped in TO_TIMESTAMP with a matching mask.
_TS_MASK = 'YYYY-MM-DD"T"HH24:MI:SS"Z"'


def _ts(dt: datetime) -> str:
    """Render a datetime as a SuiteQL TO_TIMESTAMP literal NetSuite accepts."""
    return f"TO_TIMESTAMP('{dt.strftime('%Y-%m-%dT%H:%M:%SZ')}', '{_TS_MASK}')"


def substitute_probe_dates(query: str) -> str:
    """Replace the ``:date_from`` / ``:date_to`` placeholders with a fixed dummy literal.

    Used by the ``getColumns`` column probe, which only needs the query to parse and return one row
    so it can read the column names — the actual date values are irrelevant. Without this a
    date-filtered query would 400 on the unbound placeholders and yield no column suggestions.
    """
    dummy = _ts(datetime(1970, 1, 1))
    return query.replace(_DATE_FROM, dummy).replace(_DATE_TO, dummy)


# NetSuite renders a DATE in the date format of the user's preferences. Rendering a known date tells
# the extractor which format that is.
_PROBE_DATE = date(2001, 12, 31)
_DATE_PROBE_QUERY = "SELECT TO_DATE('2001-12-31', 'YYYY-MM-DD') AS probe FROM DUAL"
_PROBE_DATE_PARTS = {"december": "%B", "2001": "%Y", "dec": "%b", "31": "%d", "12": "%m"}
_PROBE_DATE_PARTS_RE = re.compile("|".join(_PROBE_DATE_PARTS), re.IGNORECASE)
_ISO_START_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_NETSUITE_BOOLEANS = {"T": "true", "F": "false"}
_TO_CHAR_DATE = "TO_CHAR(<column>, 'YYYY-MM-DD') AS <column>"
_TO_CHAR_TIMESTAMP = "TO_CHAR(<column>, 'YYYY-MM-DD\"T\"HH24:MI:SS') AS <column>"


def date_format_from_probe(rendered: str) -> str:
    """Return the ``strptime`` format of a date that NetSuite rendered from ``_PROBE_DATE``."""
    pattern = _PROBE_DATE_PARTS_RE.sub(lambda m: _PROBE_DATE_PARTS[m.group().lower()], rendered.replace("%", "%%"))
    try:
        parsed = datetime.strptime(rendered, pattern).date()
    except ValueError:
        parsed = None
    if parsed != _PROBE_DATE:
        raise UserException(
            f"NetSuite returns dates in a format the extractor cannot convert (31 December 2001 is "
            f"'{rendered}'). Convert the date columns in the query, e.g. {_TO_CHAR_DATE}."
        )
    return pattern


class ColumnTypeConverter:
    """Convert the values of the columns with a set type to what Storage accepts for that type."""

    def __init__(self, column_types: dict[str, str], rest_client: RestClient):
        self.column_types = column_types
        self.rest_client = rest_client
        self._date_format: str | None = None

    def convert(self, rows: Iterator[dict[str, Any]]) -> Iterator[dict[str, Any]]:
        for row in rows:
            for column, column_type in self.column_types.items():
                value = row.get(column)
                if value not in (None, ""):
                    row[column] = self._convert(column, column_type, value)
            yield row

    def _convert(self, column: str, column_type: str, value: Any) -> Any:
        if column_type == "boolean":
            return _NETSUITE_BOOLEANS.get(value, value)
        if column_type == "date":
            return self._to_iso_date(column, value)
        if column_type == "timestamp":
            return self._check_iso_timestamp(column, value)
        return value

    def _to_iso_date(self, column: str, value: str) -> str:
        if _ISO_START_RE.match(value):
            return value[:10]
        try:
            return datetime.strptime(value, self._netsuite_date_format()).date().isoformat()
        except ValueError:
            raise UserException(
                f"Column '{column}' has the type Date, but the value '{value}' is not a date. "
                f"Convert it in the query, e.g. {_TO_CHAR_DATE}."
            ) from None

    @staticmethod
    def _check_iso_timestamp(column: str, value: str) -> str:
        # NetSuite renders a date with time as the date only, so only TO_CHAR in the query keeps the time.
        if _ISO_START_RE.match(value):
            try:
                datetime.fromisoformat(value)
                return value
            except ValueError:
                pass
        raise UserException(
            f"Column '{column}' has the type Timestamp, but NetSuite returned '{value}'. NetSuite leaves "
            f"out the time unless the query converts the column, e.g. {_TO_CHAR_TIMESTAMP}."
        )

    def _netsuite_date_format(self) -> str:
        if self._date_format is None:
            probe_row = next(self.rest_client.iter_suiteql(_DATE_PROBE_QUERY, limit=1))
            self._date_format = date_format_from_probe(probe_row["probe"])
        return self._date_format


class SuiteQLExtractor(Extractor):
    def __init__(self, row: SuiteQLRow, rest_client: RestClient):
        self.row = row
        self.rest_client = rest_client

    def extract(self) -> ExtractionResult:
        query = self._bind_dates(self.row.query)
        logging.info("Running SuiteQL query")
        # Stream the paged result straight to the writer instead of materializing it (a ~100k-row
        # pull would OOM); the column set + native types are resolved from a bounded head of rows.
        stream, columns, column_types = resolve_stream_schema(
            self.rest_client.iter_suiteql(query, limit=self.row.page_limit)
        )
        set_column_types = {setting.column: setting.type.value for setting in self.row.column_types}
        if set_column_types:
            stream = ColumnTypeConverter(set_column_types, self.rest_client).convert(stream)
            unknown = [col for col in set_column_types if columns is not None and col not in columns]
            if unknown:
                logging.warning(
                    "Column Types name columns that are not in the first rows of the result (a typo, or "
                    "the column is null in all of them): %s",
                    unknown,
                )
        table = OutputTable(
            name=self.row.output_table_name or "suiteql_result",
            rows=stream,
            primary_key=self.row.primary_key,
            incremental=self.row.incremental,
            columns=columns,
            column_types=column_types,
            set_column_types=set_column_types,
        )
        return ExtractionResult(tables=[table])

    def _bind_dates(self, query: str) -> str:
        """Substitute the parsed date range into the ':date_from' / ':date_to' placeholders.

        No-op when neither placeholder is present. When present, ``date_from`` is guaranteed set by
        the run-start validator; ``date_to`` defaults to "now". Both are parsed with Keboola's
        dateparser and rendered as TO_TIMESTAMP literals.
        """
        if _DATE_FROM not in query and _DATE_TO not in query:
            return query
        try:
            # Called without strformat, so the helper returns datetimes (its return type is a union
            # only because of the optional strformat overload).
            start, end = cast(tuple[datetime, datetime], parse_datetime_interval(self.row.date_from, self.row.date_to))
        except (ValueError, TypeError) as exc:
            raise UserException(
                f"Could not parse the date range (Start Date '{self.row.date_from}', End Date "
                f"'{self.row.date_to}'): {exc}. Use an absolute date (2024-01-01) or a relative "
                "string dateparser understands (e.g. '5 days ago', 'now')."
            ) from exc
        return query.replace(_DATE_FROM, _ts(start)).replace(_DATE_TO, _ts(end))
