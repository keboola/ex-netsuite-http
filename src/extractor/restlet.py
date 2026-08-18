"""``restlet`` mode extractor — customer-deployed RESTlets.

Calls the RESTlet with the user-authored query params and JSON body (both parsed from free-text JSON
at run time), extracts rows from the customer-described ``record_path``, follows the customer-named
cursor field for pagination, and maps rows to an output table.
"""

import logging

from client.restlet import RestletClient
from configuration import RestletRow
from extractor.base import (
    ExtractionResult,
    Extractor,
    OutputTable,
    resolve_stream_schema,
)


class RestletExtractor(Extractor):
    def __init__(self, row: RestletRow, restlet_client: RestletClient):
        self.row = row
        self.restlet_client = restlet_client

    def extract(self) -> ExtractionResult:
        query_params = self.row.parsed_query_params()
        body = self.row.parsed_request_body()

        logging.info("Calling RESTlet script=%s deploy=%s", self.row.script_id, self.row.deploy_id)
        # Stream the paged RESTlet rows straight to the writer instead of materializing them; the
        # output manifest still carries native column types, resolved from the first row.
        stream, columns, column_types = resolve_stream_schema(
            self.restlet_client.iter_records(
                self.row.script_id,
                self.row.deploy_id,
                method=self.row.method.value,
                query_params=query_params,
                body=body,
                record_path=self.row.record_path,
                cursor_field=self.row.pagination_cursor_field,
            )
        )

        table = OutputTable(
            name=self.row.output_table_name or f"restlet_{self.row.script_id}",
            rows=stream,
            primary_key=self.row.primary_key,
            incremental=self.row.incremental,
            columns=columns,
            column_types=column_types,
        )
        return ExtractionResult(tables=[table])
