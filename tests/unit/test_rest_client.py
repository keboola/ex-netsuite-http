"""Unit tests for the REST client: pagination, 429 Retry-After, hasMore, auth errors."""

from unittest import mock

import pytest
import requests
import responses
from keboola.component.exceptions import UserException

from client.auth import TBASigner
from client.rest import RestClient

BASE = "https://1234567-sb1.suitetalk.api.netsuite.com"
RECORD_URL = f"{BASE}/services/rest/record/v1/customer"
SUITEQL_URL = f"{BASE}/services/rest/query/v1/suiteql"


def _client():
    signer = TBASigner("1234567_SB1", "ck", "cs", "ti", "ts")
    return RestClient(signer)


@responses.activate
def test_record_collection_follows_links_next():
    responses.add(
        responses.GET,
        RECORD_URL,
        json={
            "items": [{"id": "1"}, {"id": "2"}],
            "hasMore": True,
            "links": [{"rel": "next", "href": f"{RECORD_URL}?limit=2&offset=2"}],
        },
        status=200,
    )
    responses.add(
        responses.GET,
        RECORD_URL,
        json={"items": [{"id": "3"}], "hasMore": False, "links": []},
        status=200,
    )
    client = _client()
    records = list(client.iter_record_collection("customer", limit=2))
    assert [r["id"] for r in records] == ["1", "2", "3"]
    assert len(responses.calls) == 2


@responses.activate
def test_suiteql_paginates_until_has_more_false():
    responses.add(
        responses.POST,
        SUITEQL_URL,
        json={"items": [{"id": "1"}, {"id": "2"}], "hasMore": True},
        status=200,
    )
    responses.add(
        responses.POST,
        SUITEQL_URL,
        json={"items": [{"id": "3"}], "hasMore": False},
        status=200,
    )
    client = _client()
    rows = list(client.iter_suiteql("SELECT id FROM customer", limit=2))
    assert [r["id"] for r in rows] == ["1", "2", "3"]
    assert len(responses.calls) == 2
    # Each SuiteQL page must be signed freshly (unique nonce per page).
    nonces = [call.request.headers["Authorization"] for call in responses.calls]
    assert nonces[0] != nonces[1]


@responses.activate
def test_429_retry_after_is_honored():
    responses.add(responses.POST, SUITEQL_URL, status=429, headers={"Retry-After": "7"})
    responses.add(responses.POST, SUITEQL_URL, json={"items": [{"id": "1"}], "hasMore": False}, status=200)
    client = _client()
    with mock.patch("client.http_base.time.sleep") as sleep:
        rows = list(client.iter_suiteql("SELECT id FROM customer"))
    assert [r["id"] for r in rows] == ["1"]
    sleep.assert_called_once_with(7.0)


@responses.activate
def test_transient_5xx_retried_with_backoff():
    responses.add(responses.POST, SUITEQL_URL, status=503)
    responses.add(responses.POST, SUITEQL_URL, json={"items": [], "hasMore": False}, status=200)
    client = _client()
    with mock.patch("client.http_base.time.sleep") as sleep:
        rows = list(client.iter_suiteql("SELECT 1"))
    assert rows == []
    assert sleep.call_count == 1


@responses.activate
def test_401_raises_user_exception():
    responses.add(responses.GET, RECORD_URL, status=401, json={"error": "invalid token"})
    client = _client()
    with pytest.raises(UserException):
        list(client.iter_record_collection("customer"))


@responses.activate
def test_403_raises_user_exception():
    responses.add(responses.POST, SUITEQL_URL, status=403, json={"error": "no permission"})
    client = _client()
    with pytest.raises(UserException):
        list(client.iter_suiteql("SELECT 1"))


@responses.activate
def test_5xx_gives_up_after_max_retries():
    for _ in range(6):
        responses.add(responses.POST, SUITEQL_URL, status=500)
    client = _client()
    client.max_retries = 2
    with mock.patch("client.http_base.time.sleep"):
        with pytest.raises(UserException):
            list(client.iter_suiteql("SELECT 1"))


@responses.activate
def test_metadata_catalog_returns_json():
    responses.add(
        responses.GET,
        f"{BASE}/services/rest/record/v1/metadata-catalog",
        json={"items": [{"name": "customer"}, {"name": "invoice"}]},
        status=200,
    )
    client = _client()
    catalog = client.get_metadata_catalog()
    assert catalog["items"][0]["name"] == "customer"


@responses.activate
def test_suiteql_prefer_transient_header_sent():
    responses.add(responses.POST, SUITEQL_URL, json={"items": [], "hasMore": False}, status=200)
    client = _client()
    list(client.iter_suiteql("SELECT 1"))
    assert responses.calls[0].request.headers["Prefer"] == "transient"


# ---- I5: transient network errors folded into the retry/backoff path ------


@responses.activate
def test_network_error_retried_then_recovers():
    responses.add(responses.POST, SUITEQL_URL, body=requests.exceptions.ConnectionError("reset"))
    responses.add(responses.POST, SUITEQL_URL, json={"items": [{"id": "1"}], "hasMore": False}, status=200)
    client = _client()
    with mock.patch("client.http_base.time.sleep") as sleep:
        rows = list(client.iter_suiteql("SELECT 1"))
    assert [r["id"] for r in rows] == ["1"]
    assert sleep.call_count == 1


@responses.activate
def test_network_error_exhausts_to_user_exception():
    for _ in range(6):
        responses.add(responses.POST, SUITEQL_URL, body=requests.exceptions.ReadTimeout("slow"))
    client = _client()
    client.max_retries = 2
    with mock.patch("client.http_base.time.sleep"):
        with pytest.raises(UserException):
            list(client.iter_suiteql("SELECT 1"))


# ---- NTH1: RFC-7231 HTTP-date Retry-After --------------------------------


@responses.activate
def test_retry_after_http_date_does_not_crash():
    responses.add(
        responses.POST,
        SUITEQL_URL,
        status=429,
        headers={"Retry-After": "Wed, 21 Oct 2099 07:28:00 GMT"},
    )
    responses.add(responses.POST, SUITEQL_URL, json={"items": [{"id": "1"}], "hasMore": False}, status=200)
    client = _client()
    with mock.patch("client.http_base.time.sleep") as sleep:
        rows = list(client.iter_suiteql("SELECT 1"))
    assert [r["id"] for r in rows] == ["1"]
    sleep.assert_called_once()
    assert sleep.call_args[0][0] > 0  # a positive delay derived from the future date


# ---- NTH3: raw response body must not leak into the user-facing message ----


@responses.activate
def test_auth_failure_message_excludes_response_body():
    responses.add(responses.GET, RECORD_URL, status=401, json={"error": "secret-internal-detail"})
    client = _client()
    with pytest.raises(UserException) as exc:
        list(client.iter_record_collection("customer"))
    assert "secret-internal-detail" not in str(exc.value)


@responses.activate
def test_error_message_excludes_response_body():
    responses.add(responses.POST, SUITEQL_URL, status=400, json={"error": "secret-internal-detail"})
    client = _client()
    with pytest.raises(UserException) as exc:
        list(client.iter_suiteql("SELECT 1"))
    assert "secret-internal-detail" not in str(exc.value)
