"""Shared signed-HTTP transport for the REST and RESTlet clients.

Both surfaces authenticate identically (RFC 5849 ``Authorization`` header, fresh per request) and
share the same retry policy, so that lives here once: ``429`` honours ``Retry-After`` when present
else exponential backoff with jitter; transient ``5xx`` uses backoff; ``401``/``403`` surface as
:class:`UserException`. SOAP is intentionally separate (it uses zeep + a TokenPassport header).
"""

import logging
import random
import time
from typing import Any

import requests
from keboola.component.exceptions import UserException

from client.auth import Signer

# Transient HTTP statuses worth retrying (429 is handled separately for Retry-After).
_RETRYABLE = {500, 502, 503, 504}


class SignedHttpClient:
    """Base client that signs every request and retries transient failures."""

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

    def _signed_request(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | list[Any] | None = None,
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
