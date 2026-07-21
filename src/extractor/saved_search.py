"""``saved_search`` mode extractor — SOAP SuiteTalk.

Executes a saved search by id, pages the result set with ``searchMoreWithId`` and maps the SOAP
records to flat dict rows. The watermark is NetSuite server time captured before the fetch. Full
SOAP behaviour is verified by VCR in a later phase; the paging/mapping/state logic here is
unit-tested with a mocked SOAP client.

Deferred variants (spec §4): async execution for very large searches (:meth:`_run_async`) and
layered extra filters / date-parameterised criteria are extension seams, not wired in this version.
"""

import logging
from collections.abc import Callable
from typing import Any

from client.soap import SoapClient
from configuration import SavedSearchRow
from extractor.base import ExtractionResult, Extractor, OutputTable

_STATE_LAST_RUN = "last_run"


class SavedSearchExtractor(Extractor):
    def __init__(
        self,
        row: SavedSearchRow,
        soap_client: SoapClient,
        since: str | None = None,
        server_time_provider: Callable[[], str] | None = None,
    ):
        self.row = row
        self.soap_client = soap_client
        self.since = since
        self.server_time_provider = server_time_provider

    def extract(self) -> ExtractionResult:
        # Capture the watermark from NetSuite's clock BEFORE fetching (spec §2).
        new_watermark = self._capture_watermark()

        logging.info("Executing saved search '%s' via SOAP", self.row.saved_search_id)
        rows = self._fetch_rows()

        table = OutputTable(
            name=self.row.output_table_name or self.row.saved_search_id or "saved_search",
            rows=rows,
            primary_key=self.row.primary_key,
            incremental=self.row.incremental,
        )
        state = {_STATE_LAST_RUN: new_watermark} if new_watermark else {}
        return ExtractionResult(tables=[table], state=state)

    # ---- fetch + paging --------------------------------------------------

    def _fetch_rows(self) -> list[dict[str, Any]]:
        # Layer the incremental watermark (server-side lastModifiedDate filter) and any configured
        # extra filters onto the saved search so the server filters, not the client.
        since = self.since if self.row.incremental else None
        raw = self.soap_client.run_saved_search(
            self.row.saved_search_id,
            search_record_type=self.row.search_record_type,
            page_size=self.row.page_size,
            since=since,
            extra_filters=self.row.extra_filters or None,
        )
        result = self._search_result(raw)
        rows = [self._to_dict(r) for r in self._records(result)]

        total_pages = int(getattr(result, "totalPages", 1) or 1)
        search_id = getattr(result, "searchId", None)
        page_index = int(getattr(result, "pageIndex", 1) or 1)
        while search_id and page_index < total_pages:
            page_index += 1
            more = self._search_result(self.soap_client.search_more_with_id(search_id, page_index))
            rows.extend(self._to_dict(r) for r in self._records(more))
        return rows

    # ---- watermark -------------------------------------------------------

    def _capture_watermark(self) -> str | None:
        # Persist a watermark on every successful run (incl. full loads) so a later full->incremental
        # switch resumes from this run instead of re-pulling all history (spec §2).
        if self.server_time_provider is None:
            return None
        return self.server_time_provider()

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
