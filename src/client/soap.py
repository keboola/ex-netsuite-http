"""NetSuite SOAP (SuiteTalk Web Services) client, built on ``zeep``.

Used for saved-search execution and record search/get operations that the REST surface does not
cover. Authentication is a SOAP ``TokenPassport`` header (a different signature shape than REST —
see :mod:`client.auth`), injected into every operation via zeep ``_soapheaders``. The WSDL is
pinned to a NetSuite endpoint version and cached to ``/tmp``.

SOAP behaviour is exercised by VCR in a later phase; this module keeps the operations thin and
focuses on correct auth-header construction (the part unit-testable without a live envelope).
"""

import logging
from functools import cached_property
from typing import Any

import lxml.etree as etree

from client.auth import Signer

# NetSuite platform namespaces are version-suffixed; keep the version pinned in one place.
_DEFAULT_VERSION = "2023_2"
_CORE_NS = "urn:core_{v}.platform.webservices.netsuite.com"
_MESSAGES_NS = "urn:messages_{v}.platform.webservices.netsuite.com"


class SoapClient:
    """zeep-based SuiteTalk SOAP client with TBA TokenPassport auth."""

    def __init__(self, signer: Signer, version: str = _DEFAULT_VERSION, wsdl_cache_dir: str = "/tmp"):
        self.signer = signer
        self.version = version
        self.wsdl_cache_dir = wsdl_cache_dir

    @property
    def wsdl_url(self) -> str:
        # e.g. https://<host>/wsdl/v2023_2_0/netsuite.wsdl
        return f"{self.signer.rest_base_url}/wsdl/v{self.version}_0/netsuite.wsdl"

    @cached_property
    def _client(self):
        # Imported lazily so unit tests (and non-SOAP modes) never pay the WSDL-load cost.
        from zeep import Client
        from zeep.cache import SqliteCache
        from zeep.transports import Transport

        cache = SqliteCache(path=f"{self.wsdl_cache_dir}/netsuite_wsdl_cache.db")
        transport = Transport(cache=cache)
        logging.info("Loading NetSuite WSDL from %s", self.wsdl_url)
        return Client(self.wsdl_url, transport=transport)

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
        return self._client.service.search(searchRecord=search_record, _soapheaders=self._soapheaders() + [prefs])

    def search_more_with_id(self, search_id: str, page_index: int) -> Any:
        """Fetch a subsequent page of a running search (``searchMoreWithId``)."""
        return self._client.service.searchMoreWithId(
            searchId=search_id, pageIndex=page_index, _soapheaders=self._soapheaders()
        )

    def get(self, record_ref: Any) -> Any:
        """Fetch a single record by reference."""
        return self._client.service.get(record=record_ref, _soapheaders=self._soapheaders())

    def get_list(self, record_refs: list[Any]) -> Any:
        """Fetch multiple records by reference."""
        return self._client.service.getList(record=record_refs, _soapheaders=self._soapheaders())

    def get_saved_search(self, search_type: str) -> Any:
        """List saved searches of a given record type (powers the listSavedSearches sync action)."""
        record_type = self._client.get_type(f"{{{_CORE_NS.format(v=self.version)}}}GetSavedSearchRecord")
        return self._client.service.getSavedSearch(
            record=record_type(searchType=search_type), _soapheaders=self._soapheaders()
        )

    def _search_preferences(self, page_size: int) -> etree._Element:
        messages = _MESSAGES_NS.format(v=self.version)
        prefs = etree.Element(f"{{{messages}}}searchPreferences")
        page = etree.SubElement(prefs, f"{{{messages}}}pageSize")
        page.text = str(page_size)
        return prefs
