"""Shared signed-HTTP transport for the REST and RESTlet clients.

Both surfaces authenticate identically (RFC 5849 ``Authorization`` header, fresh per request) and
share the same retry policy, so that lives here once: ``429`` honours ``Retry-After`` when present
else exponential backoff with jitter; transient ``5xx`` uses backoff; ``401``/``403`` surface as
:class:`UserException`. SOAP is intentionally separate (it uses zeep + a TokenPassport header).
"""

import logging
import random
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

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
        surface_body: bool = False,
    ) -> requests.Response:
        params = {k: str(v) for k, v in (params or {}).items()}
        attempt = 0
        while True:
            headers = {"Authorization": self.signer.authorization_header(method, url, query_params=params)}
            if extra_headers:
                headers.update(extra_headers)
            try:
                response = self.session.request(
                    method,
                    url,
                    params=params or None,
                    json=json_body,
                    headers=headers,
                    timeout=self.timeout,
                )
            except requests.exceptions.RequestException as exc:
                # Transient network failure (connection reset, read timeout, DNS blip): fold into the
                # same retry/backoff path as 5xx instead of escaping as an unhandled exit-2 crash.
                attempt += 1
                if attempt > self.max_retries:
                    raise UserException(
                        f"NetSuite request to {urlsplit(url).path} failed after {self.max_retries} retries "
                        f"(network error: {type(exc).__name__})."
                    ) from exc
                self._sleep_on_network_error(exc, attempt)
                continue
            if response.status_code in (401, 403):
                logging.debug("Auth/permission failure body: %s", response.text[:500])
                raise UserException(
                    f"NetSuite authentication/permission failed ({response.status_code}). "
                    "Check the account id, TBA credentials and role permissions."
                )
            if response.status_code == 429 or response.status_code in _RETRYABLE:
                attempt += 1
                if attempt > self.max_retries:
                    raise UserException(
                        f"NetSuite request to {urlsplit(url).path} failed after {self.max_retries} retries "
                        f"(last status {response.status_code})."
                    )
                self._sleep_before_retry(response, attempt)
                continue
            if not response.ok:
                logging.debug("Failed response body (%s): %s", response.status_code, response.text[:500])
                message = f"NetSuite request to {urlsplit(url).path} failed ({response.status_code})."
                # RESTlet errors are surfaced with body (spec §4); REST/SuiteQL keep the message plain.
                if surface_body:
                    message = f"{message} Response: {response.text[:500]}"
                raise UserException(message)
            return response

    def _backoff_delay(self, attempt: int) -> float:
        return self.backoff_base * (2 ** (attempt - 1)) + random.uniform(0, self.backoff_base)

    def _sleep_before_retry(self, response: requests.Response, attempt: int) -> None:
        retry_after = response.headers.get("Retry-After") if response.status_code == 429 else None
        delay = self._parse_retry_after(retry_after)
        if delay is None:
            delay = self._backoff_delay(attempt)
        logging.warning("NetSuite returned %s; retrying in %.1fs (attempt %s).", response.status_code, delay, attempt)
        time.sleep(delay)

    def _sleep_on_network_error(self, exc: Exception, attempt: int) -> None:
        delay = self._backoff_delay(attempt)
        logging.warning(
            "NetSuite request network error (%s); retrying in %.1fs (attempt %s).",
            type(exc).__name__,
            delay,
            attempt,
        )
        time.sleep(delay)

    @staticmethod
    def _parse_retry_after(retry_after: str | None) -> float | None:
        """Parse a ``Retry-After`` header: delta-seconds, or an RFC 7231 HTTP-date, else None."""
        if not retry_after:
            return None
        try:
            return float(retry_after)
        except ValueError:
            pass
        try:
            when = parsedate_to_datetime(retry_after)
        except TypeError, ValueError:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return max(0.0, (when - datetime.now(UTC)).total_seconds())
