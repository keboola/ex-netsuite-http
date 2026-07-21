"""NetSuite REST client — Record API, SuiteQL and the metadata catalog.

All requests are signed per-call with a fresh OAuth nonce/timestamp (required by NetSuite paging,
where each page is an independent signed request). Transient failures are retried: ``429`` honours
``Retry-After`` when present else exponential backoff with jitter; transient ``5xx`` uses backoff;
``401``/``403`` surface as :class:`UserException` (they are config/permission errors, not transient).
"""

from collections.abc import Iterator
from typing import Any

from client.http_base import SignedHttpClient

_RECORD_PATH = "/services/rest/record/v1"
_SUITEQL_PATH = "/services/rest/query/v1/suiteql"
_METADATA_PATH = "/services/rest/record/v1/metadata-catalog"


class RestClient(SignedHttpClient):
    """Signed REST client for the NetSuite SuiteTalk REST surfaces."""

    # ---- Record API ------------------------------------------------------

    def iter_record_collection(
        self,
        record_type: str,
        q: str | None = None,
        fields: list[str] | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> Iterator[dict[str, Any]]:
        """Yield records from a Record collection, following ``links.next`` until exhausted."""
        url = f"{self.signer_base}{_RECORD_PATH}/{record_type}"
        params: dict[str, Any] | None = {"limit": min(limit, 1000), "offset": offset}
        if q:
            params["q"] = q
        if fields:
            params["fields"] = ",".join(fields)
        while url:
            response = self._signed_request("GET", url, params=params)
            payload = response.json()
            yield from payload.get("items", [])
            url = self._next_link(payload)
            params = None  # the next link already carries limit/offset/q

    def get_record(self, record_type: str, record_id: str, expand_sub_resources: bool = True) -> dict[str, Any]:
        """GET a single record by internal id, optionally expanding sub-resources (sublists)."""
        url = f"{self.signer_base}{_RECORD_PATH}/{record_type}/{record_id}"
        params = {"expandSubResources": "true"} if expand_sub_resources else None
        return self._signed_request("GET", url, params=params).json()

    @staticmethod
    def _next_link(payload: dict[str, Any]) -> str | None:
        for link in payload.get("links", []) or []:
            if link.get("rel") == "next":
                return link.get("href")
        return None

    # ---- SuiteQL ---------------------------------------------------------

    def suiteql_page(self, query: str, limit: int = 1000, offset: int = 0) -> dict[str, Any]:
        """Run a single SuiteQL page and return the raw payload (used for windowing/validation)."""
        url = f"{self.signer_base}{_SUITEQL_PATH}"
        return self._signed_request(
            "POST",
            url,
            params={"limit": min(limit, 1000), "offset": offset},
            json_body={"q": query},
            extra_headers={"Prefer": "transient"},
        ).json()

    def iter_suiteql(self, query: str, limit: int = 1000) -> Iterator[dict[str, Any]]:
        """Yield SuiteQL result rows, paging on ``hasMore`` with a fresh signature per page."""
        offset = 0
        page_size = min(limit, 1000)
        while True:
            payload = self.suiteql_page(query, limit=page_size, offset=offset)
            yield from payload.get("items", [])
            if not payload.get("hasMore"):
                break
            offset += page_size

    # ---- metadata catalog ------------------------------------------------

    def get_metadata_catalog(self, record_type: str | None = None) -> dict[str, Any]:
        """Fetch the metadata catalog (all record types) or the schema for one record type."""
        url = f"{self.signer_base}{_METADATA_PATH}"
        if record_type:
            url = f"{url}/{record_type}"
        return self._signed_request("GET", url, extra_headers={"Accept": "application/schema+json"}).json()

    # ---- helpers ---------------------------------------------------------

    @property
    def signer_base(self) -> str:
        # Host derivation lives on the Signer strategy, so the client stays agnostic to it.
        return self.signer.rest_base_url
