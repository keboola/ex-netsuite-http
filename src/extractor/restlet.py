"""``restlet`` mode extractor — customer-deployed RESTlets.

Calls the RESTlet, extracts rows from the customer-described ``record_path``, follows the
customer-named cursor field for pagination, and maps rows to an output table. When incremental, the
configured watermark column is forwarded to the RESTlet as a query param (so a cursor-aware RESTlet
can filter), and the new watermark is captured from NetSuite server time for the next run.
"""

import logging
from collections.abc import Callable

from client.restlet import RestletClient
from configuration import RestletRow
from extractor.base import ExtractionResult, Extractor, OutputTable

_STATE_LAST_RUN = "last_run"


class RestletExtractor(Extractor):
    def __init__(
        self,
        row: RestletRow,
        restlet_client: RestletClient,
        since: str | None = None,
        server_time_provider: Callable[[], str] | None = None,
    ):
        self.row = row
        self.restlet_client = restlet_client
        self.since = since
        self.server_time_provider = server_time_provider

    def extract(self) -> ExtractionResult:
        # Capture the watermark from NetSuite's clock BEFORE fetching (spec §2).
        new_watermark = self._capture_watermark()

        query_params = dict(self.row.query_params)
        if self.row.incremental and self.since:
            query_params[self.row.incremental_field] = self.since

        logging.info("Calling RESTlet script=%s deploy=%s", self.row.script_id, self.row.deploy_id)
        rows = self.restlet_client.iter_records(
            self.row.script_id,
            self.row.deploy_id,
            method=self.row.method.value,
            query_params=query_params,
            body=self.row.request_body,
            record_path=self.row.record_path,
            cursor_field=self.row.pagination_cursor_field,
        )

        table = OutputTable(
            name=self.row.output_table_name or f"restlet_{self.row.script_id}",
            rows=rows,
            primary_key=self.row.primary_key,
            incremental=self.row.incremental,
        )
        state = {_STATE_LAST_RUN: new_watermark} if new_watermark else {}
        return ExtractionResult(tables=[table], state=state)

    def _capture_watermark(self) -> str | None:
        if not self.row.incremental or self.server_time_provider is None:
            return None
        return self.server_time_provider()
