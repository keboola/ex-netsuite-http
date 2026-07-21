"""NetSuite SOAP (SuiteTalk Web Services) client, built on ``zeep``.

Used for saved-search execution and record search/get operations that the REST surface does not
cover. Authentication is a SOAP ``TokenPassport`` header (a different signature shape than REST —
see :mod:`client.auth`), injected into every operation via zeep ``_soapheaders``. The WSDL is
pinned to a NetSuite endpoint version and cached to ``/tmp``.

SOAP behaviour is exercised by VCR in a later phase; this module keeps the operations thin and
focuses on correct auth-header construction (the part unit-testable without a live envelope).
"""

import logging
from collections.abc import Callable
from functools import cached_property
from pathlib import Path
from typing import Any

import lxml.etree as etree
import requests
from keboola.component.exceptions import UserException

from client.auth import Signer

# NetSuite platform namespaces are version-suffixed; keep the version pinned in one place.
_DEFAULT_VERSION = "2023_2"
_CORE_NS = "urn:core_{v}.platform.webservices.netsuite.com"
_MESSAGES_NS = "urn:messages_{v}.platform.webservices.netsuite.com"

# The version-pinned WSDL + its imported XSD tree are bundled in the repo (see scratchpad/bundle_wsdl
# .py). The WSDL is account-agnostic — it carries no account id/host, only the generic service address
# which we override per account at runtime — so loading it from disk is safe and, crucially, lets SOAP
# cassettes REPLAY offline in CI without fetching the WSDL over the network.
_BUNDLED_WSDL_DIR = Path(__file__).parent / "wsdl"


class SoapClient:
    """zeep-based SuiteTalk SOAP client with TBA TokenPassport auth."""

    def __init__(
        self,
        signer: Signer,
        version: str = _DEFAULT_VERSION,
        wsdl_cache_dir: str = "/tmp",
        timeout: int = 120,
        operation_timeout: int | None = None,
    ):
        self.signer = signer
        self.version = version
        self.wsdl_cache_dir = wsdl_cache_dir
        self.timeout = timeout
        # Time the SOAP call itself, not just the socket connect/read; defaults to the same budget.
        self.operation_timeout = operation_timeout if operation_timeout is not None else timeout

    @property
    def wsdl_url(self) -> str:
        """Locate the WSDL: prefer the repo-bundled copy (offline — works in CI / VCR replay with no
        network), else fall back to the account's live WSDL endpoint."""
        local = _BUNDLED_WSDL_DIR / "wsdl" / f"v{self.version}_0" / "netsuite.wsdl"
        if local.exists():
            return local.as_uri()
        # e.g. https://<host>/wsdl/v2023_2_0/netsuite.wsdl
        return f"{self.signer.rest_base_url}/wsdl/v{self.version}_0/netsuite.wsdl"

    @cached_property
    def _client(self):
        # Imported lazily so unit tests (and non-SOAP modes) never pay the WSDL-load cost.
        from zeep import Client
        from zeep.cache import SqliteCache
        from zeep.exceptions import Error as ZeepError
        from zeep.transports import Transport

        cache = SqliteCache(path=f"{self.wsdl_cache_dir}/netsuite_wsdl_cache.db")
        # Explicit transport timeouts so a hung endpoint fails fast instead of blocking the job.
        transport = Transport(cache=cache, timeout=self.timeout, operation_timeout=self.operation_timeout)
        logging.info("Loading NetSuite WSDL from %s", self.wsdl_url)
        try:
            return Client(self.wsdl_url, transport=transport)
        except (requests.exceptions.RequestException, ZeepError) as exc:
            raise UserException(
                f"Could not load the NetSuite SOAP WSDL from {self.wsdl_url} "
                f"({type(exc).__name__}). Check the account id and network reachability."
            ) from exc

    @cached_property
    def _service(self):
        """Service proxy rebound to the account-specific SuiteTalk endpoint.

        The pinned WSDL advertises NetSuite's legacy generic host
        (``webservices.netsuite.com``), which accounts now reject with *"you must use
        account-specific domains with this SOAP web services endpoint."* Rebind the port to the
        account-specific host derived from the account id (same host as the REST surface) so every
        SOAP operation targets a domain the account accepts.
        """
        client = self._client
        binding_name = None
        for service in client.wsdl.services.values():
            for port in service.ports.values():
                binding_name = port.binding.name
        if binding_name is None:
            raise UserException("NetSuite SOAP WSDL exposes no service binding; cannot bind endpoint.")
        address = f"{self.signer.rest_base_url}/services/NetSuitePort_{self.version}"
        return client.create_service(binding_name, address)

    # ---- error translation ----------------------------------------------

    @staticmethod
    def _invoke(operation: str, call: Callable[[], Any]) -> Any:
        """Run a zeep call, translating SOAP faults and transport errors into UserException.

        A ``zeep.exceptions.Fault`` is a server-side rejection (bad credentials, an invalid or
        permission-denied saved search, a malformed request) — a user-fixable config error. Transport
        errors (timeouts, connection resets) are transient infrastructure failures.
        """
        from zeep.exceptions import Fault, TransportError

        try:
            return call()
        except Fault as exc:
            raise UserException(
                f"NetSuite SOAP {operation} was rejected: {exc.message or exc}. This usually means "
                "invalid TBA credentials, a missing permission, or an invalid saved search id."
            ) from exc
        except (TransportError, requests.exceptions.RequestException) as exc:
            raise UserException(
                f"NetSuite SOAP {operation} failed to reach the server ({type(exc).__name__}). "
                "This is usually transient; re-run the job."
            ) from exc

    # ---- auth header -----------------------------------------------------

    def token_passport_element(self, nonce: str | None = None, timestamp: str | None = None) -> etree._Element:
        """Build the SOAP ``TokenPassport`` header element from the signer's passport values."""
        passport = self.signer.token_passport(nonce=nonce, timestamp=timestamp)
        core = _CORE_NS.format(v=self.version)
        messages = _MESSAGES_NS.format(v=self.version)

        root = etree.Element(f"{{{messages}}}tokenPassport")
        for tag in ("account", "consumerKey", "token", "nonce", "timestamp"):
            child = etree.SubElement(root, f"{{{core}}}{tag}")
            child.text = passport[tag]
        signature = etree.SubElement(root, f"{{{core}}}signature")
        signature.set("algorithm", passport["algorithm"])
        signature.text = passport["signature"]
        return root

    def _soapheaders(self) -> list[etree._Element]:
        return [self.token_passport_element()]

    # ---- operations (thin; VCR-verified later) ---------------------------

    def search(self, search_record: Any, page_size: int = 1000) -> Any:
        """Run a SOAP ``search`` with a fresh TokenPassport and the given page size."""
        prefs = self._search_preferences(page_size)
        return self._invoke(
            "search",
            lambda: self._service.search(searchRecord=search_record, _soapheaders=self._soapheaders() + [prefs]),
        )

    def search_more_with_id(self, search_id: str, page_index: int) -> Any:
        """Fetch a subsequent page of a running search (``searchMoreWithId``)."""
        return self._invoke(
            "searchMoreWithId",
            lambda: self._service.searchMoreWithId(
                searchId=search_id, pageIndex=page_index, _soapheaders=self._soapheaders()
            ),
        )

    def get(self, record_ref: Any) -> Any:
        """Fetch a single record by reference."""
        return self._invoke("get", lambda: self._service.get(record=record_ref, _soapheaders=self._soapheaders()))

    def get_list(self, record_refs: list[Any]) -> Any:
        """Fetch multiple records by reference."""
        return self._invoke(
            "getList",
            lambda: self._service.getList(record=record_refs, _soapheaders=self._soapheaders()),
        )

    def run_saved_search(
        self,
        saved_search_id: str,
        page_size: int = 1000,
        since: str | None = None,
        extra_filters: list[dict[str, Any]] | None = None,
    ) -> Any:
        """Execute a saved search by id and return the first page's raw SOAP result.

        When ``since`` is supplied, an incremental ``lastModifiedDate onOrAfter`` criterion is layered
        onto the search so the server filters (spec §4); ``extra_filters`` are additional criteria
        layered the same way. Building the typed advanced-search record for an arbitrary saved search
        is record-type specific, so the exact criteria typing is confirmed against the sandbox in the
        VCR phase; here we attach the criteria the extractor computed. The paging loop and result
        mapping live in the extractor.
        """
        search_type = self._client.get_type(f"{{{_MESSAGES_NS.format(v=self.version)}}}SearchRequest")
        search_record = search_type(savedSearchId=saved_search_id)
        criteria = self._build_search_criteria(since, extra_filters)
        if criteria:
            # Layered filters (incremental watermark + extra_filters) — surfaced on the request so the
            # server filters instead of the client. Exact typed criteria are VCR-verified.
            search_record.criteria = criteria
        prefs = self._search_preferences(page_size)
        return self._invoke(
            "search",
            lambda: self._service.search(searchRecord=search_record, _soapheaders=self._soapheaders() + [prefs]),
        )

    @staticmethod
    def _build_search_criteria(since: str | None, extra_filters: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        """Assemble the layered search criteria (incremental watermark + configured extra filters)."""
        criteria: list[dict[str, Any]] = []
        if since:
            criteria.append({"field": "lastModifiedDate", "operator": "onOrAfter", "value": since})
        if extra_filters:
            criteria.extend(extra_filters)
        return criteria

    def get_saved_search(self, search_type: str) -> Any:
        """List saved searches of a given record type (powers the listSavedSearches sync action)."""
        record_type = self._client.get_type(f"{{{_CORE_NS.format(v=self.version)}}}GetSavedSearchRecord")
        return self._invoke(
            "getSavedSearch",
            lambda: self._service.getSavedSearch(
                record=record_type(searchType=search_type), _soapheaders=self._soapheaders()
            ),
        )

    def list_saved_searches(self, search_type: str = "transaction") -> list[dict[str, Any]]:
        """Return saved searches as ``{"internalId", "scriptId", "name"}`` dicts for the UI dropdown."""
        from zeep.helpers import serialize_object

        raw = self.get_saved_search(search_type)
        result = getattr(raw, "recordRefList", None) or getattr(raw, "recordList", None)
        records = getattr(result, "recordRef", None) or getattr(result, "record", None) or []
        searches: list[dict[str, Any]] = []
        for record in records:
            serialized = serialize_object(record, dict)
            if isinstance(serialized, dict):
                searches.append(serialized)
        return searches

    def _search_preferences(self, page_size: int) -> etree._Element:
        messages = _MESSAGES_NS.format(v=self.version)
        prefs = etree.Element(f"{{{messages}}}searchPreferences")
        page = etree.SubElement(prefs, f"{{{messages}}}pageSize")
        page.text = str(page_size)
        return prefs
