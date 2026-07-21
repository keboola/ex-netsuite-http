"""Unit tests for the RESTlet client: call construction, cursor pagination, error surfacing."""

import pytest
import responses
from keboola.component.exceptions import UserException

from client.auth import TBASigner
from client.restlet import RestletClient

RESTLET_URL = "https://1234567-sb1.restlets.api.netsuite.com/app/site/hosting/restlet.nl"


def _client():
    signer = TBASigner("1234567_SB1", "ck", "cs", "ti", "ts")
    return RestletClient(signer)


@responses.activate
def test_call_sends_script_and_deploy_params():
    responses.add(responses.GET, RESTLET_URL, json={"rows": []}, status=200)
    client = _client()
    client.call("123", "1")
    sent_url = responses.calls[0].request.url or ""
    assert "script=123" in sent_url
    assert "deploy=1" in sent_url


@responses.activate
def test_iter_records_extracts_record_path():
    responses.add(
        responses.GET,
        RESTLET_URL,
        json={"data": {"results": [{"id": "1"}, {"id": "2"}]}},
        status=200,
    )
    client = _client()
    rows = list(client.iter_records("123", "1", record_path="data.results"))
    assert [r["id"] for r in rows] == ["1", "2"]


@responses.activate
def test_iter_records_top_level_list():
    responses.add(responses.GET, RESTLET_URL, json=[{"id": "1"}], status=200)
    client = _client()
    rows = list(client.iter_records("123", "1", record_path=""))
    assert rows == [{"id": "1"}]


@responses.activate
def test_cursor_pagination_loops_until_cursor_absent():
    responses.add(
        responses.GET,
        RESTLET_URL,
        json={"rows": [{"id": "1"}], "next_cursor": "c2"},
        status=200,
    )
    responses.add(
        responses.GET,
        RESTLET_URL,
        json={"rows": [{"id": "2"}], "next_cursor": "c3"},
        status=200,
    )
    responses.add(
        responses.GET,
        RESTLET_URL,
        json={"rows": [{"id": "3"}]},
        status=200,
    )
    client = _client()
    rows = list(client.iter_records("123", "1", record_path="rows", cursor_field="next_cursor"))
    assert [r["id"] for r in rows] == ["1", "2", "3"]
    assert len(responses.calls) == 3
    # cursor value from page 1 must be forwarded on page 2's request
    assert "cursor=c2" in (responses.calls[1].request.url or "")


@responses.activate
def test_post_with_body():
    responses.add(responses.POST, RESTLET_URL, json={"rows": []}, status=200)
    client = _client()
    client.call("123", "1", method="POST", body={"foo": "bar"})
    body = responses.calls[0].request.body
    assert isinstance(body, bytes)
    assert b"foo" in body


@responses.activate
def test_error_response_surfaced_with_status_and_body():
    responses.add(responses.GET, RESTLET_URL, status=400, json={"error": "bad script"})
    client = _client()
    with pytest.raises(UserException) as exc:
        client.call("123", "1")
    assert "400" in str(exc.value)
    assert "bad script" in str(exc.value)


@responses.activate
def test_auth_error_surfaced():
    responses.add(responses.GET, RESTLET_URL, status=401, json={"error": "bad token"})
    client = _client()
    with pytest.raises(UserException):
        client.call("123", "1")
