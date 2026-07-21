"""Sync actions and the client factories shared with ``run()``.

The six UI-facing sync actions (testConnection, listRecordTypes, listFields, listSavedSearches,
validateSuiteQL, previewRestlet) each call a real client and return a JSON result the UI understands.
Failures raise :class:`UserException`, which the ``@sync_action`` wrapper renders to the UI.

The client factories (`build_signer`, `_rest_client`, …) live here too so both the sync actions and
``run()`` construct clients the same way. Mixed into :class:`component.Component`, which supplies
``self.params``.
"""

import json

from keboola.component.base import sync_action
from keboola.component.exceptions import UserException
from keboola.component.sync_actions import MessageType, SelectElement, ValidationResult

from client.auth import TBASigner
from client.rest import RestClient
from client.restlet import RestletClient
from client.soap import SoapClient
from configuration import Configuration, RestletRow, SuiteQLRow


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
        record_type = getattr(self.params.row, "record_type", "")
        if not record_type:
            raise UserException("Select a record type first.")
        schema = self._rest_client().get_metadata_catalog(record_type)
        properties = schema.get("properties", {}) or {}
        return [SelectElement(value=name, label=name) for name in properties]

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
            query_params=row.query_params,
            body=row.request_body,
        )
        sample = json.dumps(payload)[:2000]
        return ValidationResult(f"RESTlet responded:\n{sample}", MessageType.INFO)

    @staticmethod
    def _href_tail(item: dict) -> str:
        for link in item.get("links", []) or []:
            href = link.get("href", "")
            if href:
                return href.rstrip("/").rsplit("/", 1)[-1]
        return ""
