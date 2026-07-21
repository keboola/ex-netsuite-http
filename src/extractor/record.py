"""``record`` mode extractor — REST Record API first.

Fetches a Record collection with an optional ``fields`` projection and ``q`` filter (the incremental
``lastModifiedDate ON_OR_AFTER`` clause is folded in for incremental runs), then handles sublists
either by flattening them into a JSON column on the parent row or by splitting them into a child
table keyed to the parent id.

Deferred variant (spec §4): a transparent REST→SOAP fallback when a record type is not REST-exposed
or a join is required. The seam is present (:meth:`_soap_fallback`) but the automatic trigger is not
wired pending sandbox coverage and user sign-off.
"""

import json
import logging
from collections.abc import Callable
from typing import Any

from keboola.component.exceptions import UserException

from client.rest import RestClient
from configuration import RecordRow, SublistHandling
from extractor.base import ExtractionResult, Extractor, OutputTable

_STATE_LAST_RUN = "last_run"


class RecordExtractor(Extractor):
    def __init__(
        self,
        row: RecordRow,
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

        q = self._build_q()
        logging.info("Fetching record collection '%s' (q=%s)", self.row.record_type, q)
        records = self.rest_client.iter_record_collection(
            self.row.record_type,
            q=q,
            fields=self.row.fields or None,
            limit=self.row.page_limit,
        )

        table_name = self.row.output_table_name or self.row.record_type
        if self.row.sublist_handling == SublistHandling.child_table:
            tables = self._split_child_tables(records, table_name)
        else:
            tables = [self._flatten_table(records, table_name)]

        state = {_STATE_LAST_RUN: new_watermark} if new_watermark else {}
        return ExtractionResult(tables=tables, state=state)

    # ---- watermark / filter ---------------------------------------------

    def _capture_watermark(self) -> str | None:
        if not self.row.incremental or self.server_time_provider is None:
            return None
        return self.server_time_provider()

    def _build_q(self) -> str | None:
        clauses: list[str] = []
        if self.row.query_filter:
            clauses.append(self.row.query_filter)
        if self.row.incremental and self.since:
            clauses.append(f'{self.row.incremental_field} ON_OR_AFTER "{self.since}"')
        if not clauses:
            return None
        return " AND ".join(f"({c})" for c in clauses)

    # ---- sublist handling ------------------------------------------------

    def _flatten_table(self, records: Any, table_name: str) -> OutputTable:
        return OutputTable(
            name=table_name,
            rows=self._flatten_rows(records),
            primary_key=self.row.primary_key,
            incremental=self.row.incremental,
        )

    def _flatten_rows(self, records: Any):
        for record in records:
            row = {}
            for key, value in record.items():
                if self._is_sublist(value):
                    row[key] = json.dumps(self._sublist_items(value))
                else:
                    row[key] = value
            yield row

    def _split_child_tables(self, records: Any, table_name: str) -> list[OutputTable]:
        # Child tables are streamed together, so materialize once and fan out.
        parent_rows: list[dict[str, Any]] = []
        child_rows: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            parent = {}
            record_id = record.get("id")
            for key, value in record.items():
                if self._is_sublist(value):
                    for item in self._sublist_items(value):
                        child_rows.setdefault(key, []).append({"_parent_id": record_id, **item})
                else:
                    parent[key] = value
            parent_rows.append(parent)

        tables = [
            OutputTable(
                name=table_name,
                rows=parent_rows,
                primary_key=self.row.primary_key,
                incremental=self.row.incremental,
            )
        ]
        for sublist_name, rows in child_rows.items():
            tables.append(
                OutputTable(
                    name=f"{table_name}_{sublist_name}",
                    rows=rows,
                    primary_key=[],
                    incremental=False,
                )
            )
        return tables

    @staticmethod
    def _is_sublist(value: Any) -> bool:
        if isinstance(value, dict) and isinstance(value.get("items"), list):
            return True
        return isinstance(value, list) and bool(value) and all(isinstance(v, dict) for v in value)

    @staticmethod
    def _sublist_items(value: Any) -> list[dict[str, Any]]:
        if isinstance(value, dict):
            return value.get("items", [])
        return value

    # ---- deferred SOAP fallback seam -------------------------------------

    def _soap_fallback(self) -> ExtractionResult:
        """Extension seam for record types not exposed over REST (deferred variant, spec §4)."""
        raise UserException(
            f"Record type '{self.row.record_type}' is not available over REST and the SOAP fallback "
            "is not enabled in this version."
        )
