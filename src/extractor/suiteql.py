"""``suiteql`` mode extractor — arbitrary SuiteQL over REST.

Runs the user's query with ``hasMore`` pagination. When the query contains the ``:date_from`` /
``:date_to`` placeholders, the configured date range (parsed with Keboola's dateparser — relative
strings like "5 days ago" or absolute dates) is substituted into the query before it runs. A single
range is issued verbatim (no auto sub-chunking); a range that would exceed NetSuite's ~100k SuiteQL
row ceiling should be narrowed. Typed columns are inferred for the native-types manifest.
"""

import logging
from datetime import datetime
from typing import Any, cast

from keboola.component.exceptions import UserException
from keboola.utils import parse_datetime_interval

from client.rest import RestClient
from configuration import SuiteQLRow
from extractor.base import ExtractionResult, Extractor, OutputTable, infer_base_type

_DATE_FROM = ":date_from"
_DATE_TO = ":date_to"

# NetSuite SuiteQL rejects a bare quoted ISO literal in a date/timestamp comparison (400); the
# substituted date must be wrapped in TO_TIMESTAMP with a matching mask.
_TS_MASK = 'YYYY-MM-DD"T"HH24:MI:SS"Z"'


def _ts(dt: datetime) -> str:
    """Render a datetime as a SuiteQL TO_TIMESTAMP literal NetSuite accepts."""
    return f"TO_TIMESTAMP('{dt.strftime('%Y-%m-%dT%H:%M:%SZ')}', '{_TS_MASK}')"


class SuiteQLExtractor(Extractor):
    def __init__(self, row: SuiteQLRow, rest_client: RestClient):
        self.row = row
        self.rest_client = rest_client

    def extract(self) -> ExtractionResult:
        rows = self._fetch_rows()
        table = OutputTable(
            name=self.row.output_table_name or "suiteql_result",
            rows=rows,
            primary_key=self.row.primary_key,
            incremental=self.row.incremental,
            columns=self._columns(rows),
            column_types=self._infer_types(rows),
        )
        return ExtractionResult(tables=[table])

    # ---- fetch -----------------------------------------------------------

    def _fetch_rows(self) -> list[dict[str, Any]]:
        query = self._bind_dates(self.row.query)
        logging.info("Running SuiteQL query")
        return list(self.rest_client.iter_suiteql(query, limit=self.row.page_limit))

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

    # ---- typing ----------------------------------------------------------

    @staticmethod
    def _columns(rows: list[dict[str, Any]]) -> list[str] | None:
        if not rows:
            return None
        columns: list[str] = []
        for row in rows:
            for key in row:
                if key not in columns:
                    columns.append(key)
        return columns

    @staticmethod
    def _infer_types(rows: list[dict[str, Any]]) -> dict[str, str] | None:
        if not rows:
            return None
        types: dict[str, str] = {}
        for row in rows:
            for key, value in row.items():
                if key not in types and value is not None:
                    types[key] = infer_base_type(value)
        return types
