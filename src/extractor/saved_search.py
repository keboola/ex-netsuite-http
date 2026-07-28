"""``saved_search`` mode extractor — SuiteTalk SOAP.

Executes a saved search by id, pages the result set with ``searchMoreWithId`` and maps the records
to flat dict rows. Full SOAP behaviour is verified by VCR in a later phase; the paging/mapping logic
here is unit-tested with a mocked SOAP client.

Filtering is defined inside the saved search itself (there is no server-side extra-filter layering
and no date watermark). Deferred variant (spec §4): async execution for very large searches
(:meth:`_run_async`) is an extension seam, not wired in this version.
"""

import logging
from collections.abc import Iterator
from typing import Any

from client.soap import SoapClient
from configuration import SavedSearchRow
from extractor.base import (
    ExtractionResult,
    Extractor,
    OutputTable,
    resolve_stream_schema,
)


class SavedSearchExtractor(Extractor):
    def __init__(self, row: SavedSearchRow, soap_client: SoapClient):
        self.row = row
        self.soap_client = soap_client

    def extract(self) -> ExtractionResult:
        logging.info("Executing saved search '%s'", self.row.saved_search_id)
        # Stream the SOAP result page by page (one page in memory at a time) instead of accumulating
        # every record into a list; columns + native types are resolved from the first row.
        stream, columns, column_types = resolve_stream_schema(self._iter_rows())

        table = OutputTable(
            name=self.row.output_table_name or self.row.saved_search_id or "saved_search",
            rows=stream,
            primary_key=self.row.primary_key,
            incremental=self.row.incremental,
            columns=columns,
            column_types=column_types,
        )
        return ExtractionResult(tables=[table])

    # ---- fetch + paging --------------------------------------------------

    def _iter_rows(self) -> Iterator[dict[str, Any]]:
        raw = self.soap_client.run_saved_search(
            self.row.saved_search_id,
            search_record_type=self.row.search_record_type,
            page_size=self.row.page_size,
        )
        result = self._search_result(raw)
        yield from (self._to_dict(r) for r in self._records(result))

        total_pages = int(getattr(result, "totalPages", 1) or 1)
        search_id = getattr(result, "searchId", None)
        page_index = int(getattr(result, "pageIndex", 1) or 1)
        while search_id and page_index < total_pages:
            page_index += 1
            more = self._search_result(self.soap_client.search_more_with_id(search_id, page_index))
            yield from (self._to_dict(r) for r in self._records(more))

    # ---- SOAP result mapping --------------------------------------------

    @staticmethod
    def _search_result(raw: Any) -> Any:
        return getattr(raw, "searchResult", raw)

    @staticmethod
    def _records(result: Any) -> list[Any]:
        record_list = getattr(result, "recordList", None)
        if record_list is None:
            return []
        return getattr(record_list, "record", None) or []

    @staticmethod
    def _to_dict(record: Any) -> dict[str, Any]:
        if isinstance(record, dict):
            return record
        from zeep.helpers import serialize_object

        serialized = serialize_object(record, dict)
        return serialized if isinstance(serialized, dict) else {"value": serialized}

    # ---- deferred async seam ---------------------------------------------

    def _run_async(self) -> ExtractionResult:
        """Extension seam for async execution of very large saved searches (deferred, spec §4)."""
        raise NotImplementedError("Async saved-search execution is a deferred variant.")
