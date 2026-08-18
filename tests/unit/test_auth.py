"""Known-answer unit tests for the TBA signer (RFC 5849 header + SOAP TokenPassport)."""

from urllib.parse import quote

from client.auth import TBASigner

# Fixed inputs shared with the independently-computed vectors (see scratchpad/kav.py).
ACCOUNT_ID = "1234567_SB1"
CONSUMER_KEY = "ck"
CONSUMER_SECRET = "cs"
TOKEN_ID = "ti"
TOKEN_SECRET = "ts"
NONCE = "abc123"
TIMESTAMP = "1600000000"
URL = "https://1234567-sb1.suitetalk.api.netsuite.com/services/rest/record/v1/customer"

# Expected values computed independently with stdlib hmac/hashlib per RFC 5849 / NetSuite spec.
OAUTH_BASE_STRING = (
    "GET&https%3A%2F%2F1234567-sb1.suitetalk.api.netsuite.com%2Fservices%2Frest%2Frecord%2Fv1%2Fcustomer"
    "&oauth_consumer_key%3Dck%26oauth_nonce%3Dabc123%26oauth_signature_method%3DHMAC-SHA256"
    "%26oauth_timestamp%3D1600000000%26oauth_token%3Dti%26oauth_version%3D1.0"
)
OAUTH_SIGNATURE = "rmGlNRf38nuAbKjSd+4Fuq9XeVbZro95lzU7ojkxKOs="
SOAP_BASE_STRING = "1234567_SB1&ck&ti&abc123&1600000000"
SOAP_SIGNATURE = "qvvKayfZbtGhxR6TZNN6xDxf+RHHlpQ+ei0y9XZxUdU="
Q_BASE_STRING = (
    "GET&https%3A%2F%2F1234567-sb1.suitetalk.api.netsuite.com%2Fservices%2Frest%2Frecord%2Fv1%2Fcustomer"
    "&limit%3D10%26oauth_consumer_key%3Dck%26oauth_nonce%3Dabc123%26oauth_signature_method%3DHMAC-SHA256"
    "%26oauth_timestamp%3D1600000000%26oauth_token%3Dti%26oauth_version%3D1.0%26offset%3D20"
)
Q_SIGNATURE = "7tY+5aPfwfjyLaksiAL0IQoOmhYqW2ryhSOlzzZAyxg="


def _signer():
    return TBASigner(ACCOUNT_ID, CONSUMER_KEY, CONSUMER_SECRET, TOKEN_ID, TOKEN_SECRET)


def test_oauth_base_string_matches_and_excludes_realm():
    signer = _signer()
    base = signer.signature_base_string("GET", URL, nonce=NONCE, timestamp=TIMESTAMP)
    assert base == OAUTH_BASE_STRING
    # RFC 5849 §3.4.1.3.1: realm must NOT appear in the base string.
    assert "realm" not in base
    assert ACCOUNT_ID not in base


def test_oauth_signature_matches_known_answer():
    signer = _signer()
    sig = signer.sign(OAUTH_BASE_STRING)
    assert sig == OAUTH_SIGNATURE


def test_authorization_header_carries_realm_and_encoded_signature():
    signer = _signer()
    header = signer.authorization_header("GET", URL, nonce=NONCE, timestamp=TIMESTAMP)
    assert header.startswith("OAuth ")
    # realm IS present in the header, equals the account id.
    assert f'realm="{ACCOUNT_ID}"' in header
    assert 'oauth_signature_method="HMAC-SHA256"' in header
    assert f'oauth_signature="{quote(OAUTH_SIGNATURE, safe="")}"' in header
    assert f'oauth_nonce="{NONCE}"' in header
    assert f'oauth_timestamp="{TIMESTAMP}"' in header


def test_oauth_query_params_folded_into_base_string():
    signer = _signer()
    base = signer.signature_base_string(
        "GET", URL, query_params={"limit": "10", "offset": "20"}, nonce=NONCE, timestamp=TIMESTAMP
    )
    assert base == Q_BASE_STRING
    assert signer.sign(base) == Q_SIGNATURE


def test_soap_token_passport_base_string_and_signature():
    signer = _signer()
    base = signer.token_passport_base_string(nonce=NONCE, timestamp=TIMESTAMP)
    assert base == SOAP_BASE_STRING
    assert signer.sign(base) == SOAP_SIGNATURE


def test_token_passport_shape():
    signer = _signer()
    tp = signer.token_passport(nonce=NONCE, timestamp=TIMESTAMP)
    assert tp["account"] == ACCOUNT_ID
    assert tp["consumerKey"] == CONSUMER_KEY
    assert tp["token"] == TOKEN_ID
    assert tp["nonce"] == NONCE
    assert tp["timestamp"] == TIMESTAMP
    assert tp["signature"] == SOAP_SIGNATURE
    assert tp["algorithm"] == "HMAC-SHA256"


def test_host_derivation_from_account_id():
    signer = _signer()
    assert signer.suitetalk_host == "1234567-sb1.suitetalk.api.netsuite.com"
    assert signer.restlet_host == "1234567-sb1.restlets.api.netsuite.com"
    assert signer.rest_base_url == "https://1234567-sb1.suitetalk.api.netsuite.com"
    assert signer.restlet_base_url == "https://1234567-sb1.restlets.api.netsuite.com"
