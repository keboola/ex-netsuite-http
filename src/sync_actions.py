"""Sync actions and the client factories shared with ``run()``.

The UI-facing sync actions (testConnection, listRecordTypes, listFields, getColumns,
listSavedSearches, validateSuiteQL, previewRestlet) each call a real client and return a JSON result
the UI understands. Failures raise :class:`UserException`, which the ``@sync_action`` wrapper renders
to the UI.

The client factories (`build_signer`, `_rest_client`, …) live here too so both the sync actions and
``run()`` construct clients the same way. Mixed into :class:`component.Component`, which supplies
``self.params``.
"""

import json
import logging

from keboola.component.base import sync_action
from keboola.component.exceptions import UserException
from keboola.component.sync_actions import MessageType, SelectElement, ValidationResult

from client.auth import TBASigner
from client.rest import RestClient
from client.restlet import RestletClient
from client.soap import SoapClient
from configuration import Configuration, RecordRow, RestletRow, SuiteQLRow
from extractor.suiteql import substitute_probe_dates


class SyncActionsMixin:
    """Sync actions + client factories mixed into the component."""

    # Provided by Component.__init__ — declared here for type checkers.
    params: Configuration

    # ---- client factories (shared with run()) ----------------------------

    def build_signer(self) -> TBASigner:
        conn = self.params.connection
        if not all([conn.account_id, conn.consumer_key, conn.consumer_secret, conn.token_id, conn.token_secret]):
            raise UserException("account_id and all four TBA credentials are required.")
        return TBASigner(conn.account_id, conn.consumer_key, conn.consumer_secret, conn.token_id, conn.token_secret)

    def _rest_client(self) -> RestClient:
        return RestClient(self.build_signer())

    def _soap_client(self) -> SoapClient:
        return SoapClient(self.build_signer())

    def _restlet_client(self) -> RestletClient:
        return RestletClient(self.build_signer())

    # ---- sync actions ----------------------------------------------------

    @sync_action("testConnection")
    def test_connection(self) -> ValidationResult:
        """Ping the metadata catalog to validate the TBA credentials and host reachability."""
        self._rest_client().get_metadata_catalog()
        return ValidationResult("Connection successful.", MessageType.SUCCESS)

    @sync_action("listRecordTypes")
    def list_record_types(self) -> list[SelectElement]:
        """Populate the record_type dropdown from the metadata catalog."""
        catalog = self._rest_client().get_metadata_catalog()
        elements = []
        for item in catalog.get("items", []) or []:
            name = item.get("name") or self._href_tail(item)
            if name:
                elements.append(SelectElement(value=name, label=name))
        return elements

    @sync_action("listFields")
    def list_fields(self) -> list[SelectElement]:
        """Populate the fields dropdown from the metadata catalog for the chosen record_type."""
        row = self.params.row
        if not isinstance(row, RecordRow) or not row.record_type:
            raise UserException("Select a record type first.")
        record_type = row.record_type
        schema = self._rest_client().get_metadata_catalog(record_type)
        properties = schema.get("properties", {}) or {}
        return [SelectElement(value=name, label=name) for name in properties]

    @sync_action("getColumns")
    def get_columns(self) -> list[SelectElement]:
        """Suggest primary-key column options for the creatable PK picker, per mode.

        record  -> the chosen record type's metadata fields.
        suiteql -> the columns the query returns, probed by fetching a single row.
        saved_search / restlet -> not knowable ahead of a run, so return nothing (the picker is
        creatable, so the user types the key column names manually).
        """
        row = self.params.row
        if isinstance(row, RecordRow) and row.record_type:
            schema = self._rest_client().get_metadata_catalog(row.record_type)
            properties = schema.get("properties", {}) or {}
            return [SelectElement(value=name, label=name) for name in properties]
        if isinstance(row, SuiteQLRow) and row.query:
            return [SelectElement(value=name, label=name) for name in self._probe_suiteql_columns(row)]
        return []

    @sync_action("listSavedSearches")
    def list_saved_searches(self) -> list[SelectElement]:
        """Populate the saved_search_id dropdown via SOAP."""
        searches = self._soap_client().list_saved_searches()
        elements = []
        for search in searches:
            value = search.get("scriptId") or search.get("internalId") or search.get("name")
            if value:
                elements.append(SelectElement(value=str(value), label=str(search.get("name") or value)))
        return elements

    @sync_action("validateSuiteQL")
    def validate_suiteql(self) -> ValidationResult:
        """Cheaply validate SuiteQL syntax by fetching a single row (dry run)."""
        row = self.params.row
        if not isinstance(row, SuiteQLRow) or not row.query:
            raise UserException("Enter a SuiteQL query first.")
        self._rest_client().suiteql_page(row.query, limit=1)
        return ValidationResult("SuiteQL query is valid.", MessageType.SUCCESS)

    @sync_action("previewRestlet")
    def preview_restlet(self) -> ValidationResult:
        """Call the RESTlet once and return a sample of the response."""
        row = self.params.row
        if not isinstance(row, RestletRow) or not row.script_id or not row.deploy_id:
            raise UserException("Enter the RESTlet script id and deploy id first.")
        payload = self._restlet_client().call(
            row.script_id,
            row.deploy_id,
            method=row.method.value,
            query_params=row.parsed_query_params(),
            body=row.parsed_request_body(),
        )
        sample = json.dumps(payload)[:2000]
        return ValidationResult(f"RESTlet responded:\n{sample}", MessageType.INFO)

    def _probe_suiteql_columns(self, row: SuiteQLRow) -> list[str]:
        """Best-effort column list for a SuiteQL query: fetch one row and read its keys.

        The SuiteQL response carries no schema, so columns are only knowable from a returned row. The
        ``:date_from`` / ``:date_to`` placeholders are substituted with a dummy literal first so a
        date-filtered query still parses. Any failure (bad SQL, zero rows) yields no suggestions
        rather than an error — the picker is creatable, so the user can always type the columns.
        """
        try:
            payload = self._rest_client().suiteql_page(substitute_probe_dates(row.query), limit=1)
        except Exception as exc:  # noqa: BLE001 — suggestion helper must never hard-fail the picker
            logging.info("getColumns SuiteQL probe failed (%s); returning no suggestions.", type(exc).__name__)
            return []
        columns: list[str] = []
        for item in payload.get("items") or []:
            for key in item:
                if key not in columns:
                    columns.append(key)
        return columns

    @staticmethod
    def _href_tail(item: dict) -> str:
        for link in item.get("links", []) or []:
            href = link.get("href", "")
            if href:
                return href.rstrip("/").rsplit("/", 1)[-1]
        return ""
