"""NetSuite RESTlet client — a generic caller for customer-deployed RESTlets.

Signs requests for the ``restlets.api`` host with the mandatory ``script``/``deploy`` params, sends
arbitrary query params and JSON body over GET/POST/PUT/DELETE, extracts rows from a customer-named
``record_path`` in the response, and follows a customer-named cursor field for pagination. Shares
the signed-request/retry transport with the REST client (see :mod:`client.http_base`).
"""

from collections.abc import Iterator
from typing import Any

from client.http_base import SignedHttpClient

_RESTLET_PATH = "/app/site/hosting/restlet.nl"


class RestletClient(SignedHttpClient):
    """Signed caller for deployed RESTlets."""

    @property
    def _url(self) -> str:
        return f"{self.signer.restlet_base_url}{_RESTLET_PATH}"

    def call(
        self,
        script_id: str,
        deploy_id: str,
        method: str = "GET",
        query_params: dict[str, Any] | None = None,
        body: dict[str, Any] | list[Any] | None = None,
    ) -> Any:
        """Invoke the RESTlet once and return the parsed JSON response."""
        params: dict[str, Any] = {"script": script_id, "deploy": deploy_id}
        if query_params:
            params.update(query_params)
        response = self._signed_request(method.upper(), self._url, params=params, json_body=body, surface_body=True)
        return response.json()

    def iter_records(
        self,
        script_id: str,
        deploy_id: str,
        method: str = "GET",
        query_params: dict[str, Any] | None = None,
        body: dict[str, Any] | list[Any] | None = None,
        record_path: str = "",
        cursor_field: str = "",
    ) -> Iterator[dict[str, Any]]:
        """Yield rows from a RESTlet, following an optional customer-defined cursor field."""
        params = dict(query_params or {})
        while True:
            payload = self.call(script_id, deploy_id, method=method, query_params=params, body=body)
            yield from self._extract_rows(payload, record_path)
            cursor = self._extract_value(payload, cursor_field) if cursor_field else None
            if not cursor:
                break
            params["cursor"] = cursor

    @staticmethod
    def _extract_rows(payload: Any, record_path: str) -> list[dict[str, Any]]:
        rows = RestletClient._extract_value(payload, record_path) if record_path else payload
        if rows is None:
            return []
        if isinstance(rows, list):
            return rows
        return [rows]

    @staticmethod
    def _extract_value(payload: Any, dotted_path: str) -> Any:
        """Resolve a dotted path (e.g. ``data.results``) into a nested dict payload."""
        current = payload
        for key in dotted_path.split("."):
            if not isinstance(current, dict):
                return None
            current = current.get(key)
        return current
