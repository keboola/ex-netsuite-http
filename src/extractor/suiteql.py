"""``suiteql`` mode extractor — arbitrary SuiteQL over REST.

Runs the user's query with ``hasMore`` pagination. Incremental runs bind the stored watermark into
a ``:state`` placeholder (``WHERE lastmodifieddate > :state``). To stay under NetSuite's ~100k-row
result ceiling, a query written with ``:window_start`` / ``:window_end`` placeholders is executed
once per date window (``window_size`` days) across the incremental range, concatenating the results.
Typed columns are inferred for the native-types manifest. The watermark is NetSuite server time
captured before the fetch begins (spec §2).
"""

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from client.rest import RestClient
from configuration import SuiteQLRow
from extractor.base import ExtractionResult, Extractor, OutputTable, infer_base_type

_STATE_LAST_RUN = "last_run"
_STATE_PLACEHOLDER = ":state"
_WINDOW_START = ":window_start"
_WINDOW_END = ":window_end"


class SuiteQLExtractor(Extractor):
    def __init__(
        self,
        row: SuiteQLRow,
        rest_client: RestClient,
        since: str | None = None,
        server_time_provider: Callable[[], str] | None = None,
    ):
        self.row = row
        self.rest_client = rest_client
        self.since = since
        self.server_time_provider = server_time_provider

    def extract(self) -> ExtractionResult:
        # Capture the watermark from NetSuite's clock BEFORE fetching (spec §2).
        new_watermark = self._capture_watermark()

        rows = self._fetch_rows(new_watermark)
        table = OutputTable(
            name=self.row.output_table_name or "suiteql_result",
            rows=rows,
            primary_key=self.row.primary_key,
            incremental=self.row.incremental,
            columns=self._columns(rows),
            column_types=self._infer_types(rows),
        )
        state = {_STATE_LAST_RUN: new_watermark} if new_watermark else {}
        return ExtractionResult(tables=[table], state=state)

    # ---- fetch -----------------------------------------------------------

    def _fetch_rows(self, watermark: str | None) -> list[dict[str, Any]]:
        if self._is_windowed():
            return self._fetch_windowed(watermark)
        query = self._bind_state(self.row.query)
        logging.info("Running SuiteQL query")
        return list(self.rest_client.iter_suiteql(query, limit=self.row.page_limit))

    def _fetch_windowed(self, watermark: str | None) -> list[dict[str, Any]]:
        end = watermark or (self.server_time_provider() if self.server_time_provider else None)
        windows = self._windows(self.since, end)
        rows: list[dict[str, Any]] = []
        for start, stop in windows:
            query = self.row.query.replace(_WINDOW_START, f"'{start}'").replace(_WINDOW_END, f"'{stop}'")
            logging.info("Running SuiteQL window %s -> %s", start, stop)
            rows.extend(self.rest_client.iter_suiteql(query, limit=self.row.page_limit))
        return rows

    def _is_windowed(self) -> bool:
        return self.row.window_size > 0 and _WINDOW_START in self.row.query and self.since is not None

    def _bind_state(self, query: str) -> str:
        if self.row.incremental and _STATE_PLACEHOLDER in query:
            lower = self.since or "1970-01-01T00:00:00Z"
            return query.replace(_STATE_PLACEHOLDER, f"'{lower}'")
        return query

    def _windows(self, since: str | None, end: str | None) -> list[tuple[str, str]]:
        if not since or not end:
            return [(since or "1970-01-01T00:00:00Z", end or "")]
        start_dt = _parse(since)
        end_dt = _parse(end)
        step = timedelta(days=self.row.window_size)
        windows: list[tuple[str, str]] = []
        cursor = start_dt
        while cursor < end_dt:
            stop = min(cursor + step, end_dt)
            windows.append((_iso(cursor), _iso(stop)))
            cursor = stop
        return windows or [(_iso(start_dt), _iso(end_dt))]

    # ---- watermark / typing ---------------------------------------------

    def _capture_watermark(self) -> str | None:
        if not self.row.incremental or self.server_time_provider is None:
            return None
        return self.server_time_provider()

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


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")
