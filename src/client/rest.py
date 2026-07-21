"""NetSuite REST client — Record API, SuiteQL and the metadata catalog.

All requests are signed per-call with a fresh OAuth nonce/timestamp (required by NetSuite paging,
where each page is an independent signed request). Transient failures are retried: ``429`` honours
``Retry-After`` when present else exponential backoff with jitter; transient ``5xx`` uses backoff;
``401``/``403`` surface as :class:`UserException` (they are config/permission errors, not transient).
"""

import logging
import random
import time
from collections.abc import Iterator
from typing import Any

import requests
from keboola.component.exceptions import UserException

from client.auth import Signer

_RECORD_PATH = "/services/rest/record/v1"
_SUITEQL_PATH = "/services/rest/query/v1/suiteql"
_METADATA_PATH = "/services/rest/record/v1/metadata-catalog"

# Transient HTTP statuses worth retrying (429 handled separately for Retry-After).
_RETRYABLE = {500, 502, 503, 504}


class RestClient:
    """Signed REST client for the NetSuite SuiteTalk REST surfaces."""

    def __init__(
        self,
        signer: Signer,
        timeout: int = 120,
        max_retries: int = 5,
        backoff_base: float = 1.0,
    ):
        self.signer = signer
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.session = requests.Session()

    # ---- low-level signed request with retry -----------------------------

    def _signed_request(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> requests.Response:
        params = {k: str(v) for k, v in (params or {}).items()}
        attempt = 0
        while True:
            headers = {"Authorization": self.signer.authorization_header(method, url, query_params=params)}
            if extra_headers:
                headers.update(extra_headers)
            response = self.session.request(
                method,
                url,
                params=params or None,
                json=json_body,
                headers=headers,
                timeout=self.timeout,
            )
            if response.status_code in (401, 403):
                raise UserException(
                    f"NetSuite authentication/permission failed ({response.status_code}). "
                    f"Check the account id, TBA credentials and role permissions. Response: {response.text[:500]}"
                )
            if response.status_code == 429 or response.status_code in _RETRYABLE:
                attempt += 1
                if attempt > self.max_retries:
                    raise UserException(
                        f"NetSuite request to {url} failed after {self.max_retries} retries "
                        f"(last status {response.status_code})."
                    )
                self._sleep_before_retry(response, attempt)
                continue
            if not response.ok:
                raise UserException(f"NetSuite request to {url} failed ({response.status_code}): {response.text[:500]}")
            return response

    def _sleep_before_retry(self, response: requests.Response, attempt: int) -> None:
        retry_after = response.headers.get("Retry-After") if response.status_code == 429 else None
        if retry_after is not None:
            delay = float(retry_after)
        else:
            delay = self.backoff_base * (2 ** (attempt - 1)) + random.uniform(0, self.backoff_base)
        logging.warning("NetSuite returned %s; retrying in %.1fs (attempt %s).", response.status_code, delay, attempt)
        time.sleep(delay)

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
