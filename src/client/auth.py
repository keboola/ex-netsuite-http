"""Token-Based Authentication (TBA) signing for NetSuite.

TBA is OAuth 1.0a with HMAC-SHA256 (HMAC-SHA1 was removed in NetSuite 2023.1). NetSuite signs two
surfaces differently, so the signer produces **two shapes** from the same credentials:

1. an RFC 5849 ``Authorization: OAuth ...`` header for the REST Record API, SuiteQL and RESTlets;
2. a SOAP ``TokenPassport`` (a different base string, carried in the SOAP envelope header).

The signature is implemented in-house (stdlib ``hmac``/``hashlib``) to control both shapes; no OAuth
library is used. ``Signer`` is a strategy interface so a future OAuth 2.0 signer can drop in without
touching the clients (TBA cannot create new integrations after NetSuite 2027.1).
"""

import base64
import hashlib
import hmac
import secrets
import time
from abc import ABC, abstractmethod
from urllib.parse import quote, urlsplit

# RFC 3986 unreserved characters — everything else is percent-encoded.
_UNRESERVED = "-._~"


def _pct(value: str) -> str:
    """Percent-encode per RFC 3986 (used for both OAuth base strings and the TokenPassport)."""
    return quote(str(value), safe=_UNRESERVED)


def _derive_host(account_id: str, service: str) -> str:
    """Derive a NetSuite service host from the account id (``_`` -> ``-``, lowercased)."""
    normalized = account_id.replace("_", "-").lower()
    return f"{normalized}.{service}.api.netsuite.com"


class Signer(ABC):
    """Strategy interface for authenticating NetSuite requests."""

    @abstractmethod
    def authorization_header(
        self,
        method: str,
        url: str,
        query_params: dict[str, str] | None = None,
        nonce: str | None = None,
        timestamp: str | None = None,
    ) -> str:
        """Return the value for the HTTP ``Authorization`` header."""

    @property
    @abstractmethod
    def rest_base_url(self) -> str:
        """Base URL for the REST/SuiteTalk REST surfaces."""

    @property
    @abstractmethod
    def restlet_base_url(self) -> str:
        """Base URL for the RESTlet surface."""

    @abstractmethod
    def token_passport(self, nonce: str | None = None, timestamp: str | None = None) -> dict[str, str]:
        """Return the SOAP TokenPassport pieces (the second auth shape) for the SOAP client."""


class TBASigner(Signer):
    """NetSuite Token-Based Authentication signer (OAuth 1.0a, HMAC-SHA256)."""

    SIGNATURE_METHOD = "HMAC-SHA256"
    OAUTH_VERSION = "1.0"

    def __init__(
        self,
        account_id: str,
        consumer_key: str,
        consumer_secret: str,
        token_id: str,
        token_secret: str,
    ):
        self.account_id = account_id
        self._consumer_key = consumer_key
        self._consumer_secret = consumer_secret
        self._token_id = token_id
        self._token_secret = token_secret

    # ---- host derivation -------------------------------------------------

    @property
    def suitetalk_host(self) -> str:
        return _derive_host(self.account_id, "suitetalk")

    @property
    def restlet_host(self) -> str:
        return _derive_host(self.account_id, "restlets")

    @property
    def rest_base_url(self) -> str:
        return f"https://{self.suitetalk_host}"

    @property
    def restlet_base_url(self) -> str:
        return f"https://{self.restlet_host}"

    # ---- primitives ------------------------------------------------------

    @property
    def _signing_key(self) -> str:
        return f"{_pct(self._consumer_secret)}&{_pct(self._token_secret)}"

    def sign(self, base_string: str) -> str:
        """Return base64(HMAC-SHA256(base_string, signing_key))."""
        digest = hmac.new(self._signing_key.encode(), base_string.encode(), hashlib.sha256).digest()
        return base64.b64encode(digest).decode()

    @staticmethod
    def _new_nonce() -> str:
        return secrets.token_hex(16)

    @staticmethod
    def _new_timestamp() -> str:
        return str(int(time.time()))

    def _oauth_params(self, nonce: str, timestamp: str) -> dict[str, str]:
        return {
            "oauth_consumer_key": self._consumer_key,
            "oauth_token": self._token_id,
            "oauth_signature_method": self.SIGNATURE_METHOD,
            "oauth_timestamp": timestamp,
            "oauth_nonce": nonce,
            "oauth_version": self.OAUTH_VERSION,
        }

    # ---- shape 1: OAuth Authorization header (REST + RESTlet) ------------

    def signature_base_string(
        self,
        method: str,
        url: str,
        query_params: dict[str, str] | None = None,
        nonce: str = "",
        timestamp: str = "",
    ) -> str:
        """Build the RFC 5849 signature base string. ``realm`` is intentionally excluded."""
        params = self._oauth_params(nonce, timestamp)
        if query_params:
            params.update({k: str(v) for k, v in query_params.items()})
        normalized = "&".join(f"{_pct(k)}={_pct(v)}" for k, v in sorted(params.items()))
        split = urlsplit(url)
        base_url = f"{split.scheme}://{split.netloc}{split.path}"
        return f"{method.upper()}&{_pct(base_url)}&{_pct(normalized)}"

    def authorization_header(
        self,
        method: str,
        url: str,
        query_params: dict[str, str] | None = None,
        nonce: str | None = None,
        timestamp: str | None = None,
    ) -> str:
        nonce = nonce or self._new_nonce()
        timestamp = timestamp or self._new_timestamp()
        base_string = self.signature_base_string(method, url, query_params, nonce, timestamp)
        signature = self.sign(base_string)
        # realm is present ONLY in the header (not the base string) and equals the account id.
        header_params = {
            "realm": self.account_id,
            "oauth_consumer_key": self._consumer_key,
            "oauth_token": self._token_id,
            "oauth_signature_method": self.SIGNATURE_METHOD,
            "oauth_timestamp": timestamp,
            "oauth_nonce": nonce,
            "oauth_version": self.OAUTH_VERSION,
            "oauth_signature": signature,
        }
        rendered = ", ".join(f'{k}="{quote(str(v), safe="")}"' for k, v in header_params.items())
        return f"OAuth {rendered}"

    # ---- shape 2: SOAP TokenPassport -------------------------------------

    def token_passport_base_string(self, nonce: str = "", timestamp: str = "") -> str:
        """Build the SOAP TokenPassport base string: account & consumerKey & token & nonce & ts."""
        parts = [self.account_id, self._consumer_key, self._token_id, nonce, timestamp]
        return "&".join(_pct(p) for p in parts)

    def token_passport(self, nonce: str | None = None, timestamp: str | None = None) -> dict[str, str]:
        """Return the TokenPassport pieces for the SOAP client to assemble into a zeep header."""
        nonce = nonce or self._new_nonce()
        timestamp = timestamp or self._new_timestamp()
        signature = self.sign(self.token_passport_base_string(nonce, timestamp))
        return {
            "account": self.account_id,
            "consumerKey": self._consumer_key,
            "token": self._token_id,
            "nonce": nonce,
            "timestamp": timestamp,
            "signature": signature,
            "algorithm": self.SIGNATURE_METHOD,
        }
