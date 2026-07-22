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
    collect_columns,
    infer_column_types,
)


class RestletExtractor(Extractor):
    def __init__(self, row: RestletRow, restlet_client: RestletClient):
        self.row = row
        self.restlet_client = restlet_client

    def extract(self) -> ExtractionResult:
        query_params = self.row.parsed_query_params()
        body = self.row.parsed_request_body()

        logging.info("Calling RESTlet script=%s deploy=%s", self.row.script_id, self.row.deploy_id)
        # Materialize the paged RESTlet rows so the output manifest carries native column types
        # (inferred from the fetched rows, as the SuiteQL extractor does). RESTlet result sets are
        # bounded by what the customer's script returns per configured pagination.
        rows = list(
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
            rows=rows,
            primary_key=self.row.primary_key,
            incremental=self.row.incremental,
            columns=collect_columns(rows) or None,
            column_types=infer_column_types(rows) or None,
        )
        return ExtractionResult(tables=[table])
