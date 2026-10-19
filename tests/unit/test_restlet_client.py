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
def test_call_sends_json_content_type_and_accept():
    # NetSuite serializes a RESTlet's return value only when the request declares a JSON
    # Content-Type — also on GET, where requests sends none by default. Without it every script
    # answers INVALID_RETURN_DATA_FORMAT (400), so the headers must always be present.
    responses.add(responses.GET, RESTLET_URL, json={"rows": []}, status=200)
    _client().call("123", "1")
    sent = responses.calls[0].request.headers
    assert sent["Content-Type"] == "application/json"
    assert sent["Accept"] == "application/json"


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
def test_scalar_at_record_path_raises_user_exception():
    # record_path resolving to a scalar (not a record or list of records) must fail fast with a
    # clear UserException, not yield a non-dict row that crashes the CSV writer downstream.
    responses.add(responses.GET, RESTLET_URL, json={"data": {"results": "oops"}}, status=200)
    client = _client()
    with pytest.raises(UserException) as exc:
        list(client.iter_records("123", "1", record_path="data.results"))
    assert "data.results" in str(exc.value)


@responses.activate
def test_list_of_scalars_at_record_path_raises_user_exception():
    responses.add(responses.GET, RESTLET_URL, json={"rows": [1, 2, 3]}, status=200)
    client = _client()
    with pytest.raises(UserException):
        list(client.iter_records("123", "1", record_path="rows"))


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
def test_error_reports_status_without_body():
    # The RESTlet error body can carry arbitrary customer data (the script's own error text, echoed
    # field values), so it is never put into the user-facing message; only the status code and the
    # request path are reported.
    responses.add(responses.GET, RESTLET_URL, status=400, json={"error": "bad script for admin@acme.com"})
    client = _client()
    with pytest.raises(UserException) as exc:
        client.call("123", "1")
    msg = str(exc.value)
    assert "400" in msg
    assert "bad script" not in msg
    assert "admin@acme.com" not in msg


@responses.activate
def test_non_json_response_raises_user_exception():
    # F5: a 200 with a non-JSON body (e.g. an HTML maintenance page) must raise a clean UserException
    # naming the script/deploy, not crash with a JSONDecodeError.
    responses.add(
        responses.GET,
        RESTLET_URL,
        body="<html>maintenance</html>",
        status=200,
        content_type="text/html",
    )
    client = _client()
    with pytest.raises(UserException) as exc:
        client.call("123", "1")
    message = str(exc.value)
    assert "non-JSON" in message
    assert "script=123" in message and "deploy=1" in message


@responses.activate
def test_non_json_response_on_preview_path_raises_user_exception():
    # F5: the previewRestlet path also goes through call(); a non-JSON 200 must surface cleanly.
    responses.add(responses.POST, RESTLET_URL, body="not json", status=200, content_type="text/plain")
    client = _client()
    with pytest.raises(UserException) as exc:
        client.call("123", "1", method="POST", body={"x": 1})
    assert "non-JSON" in str(exc.value)


@responses.activate
def test_auth_error_surfaced():
    responses.add(responses.GET, RESTLET_URL, status=401, json={"error": "bad token"})
    client = _client()
    with pytest.raises(UserException):
        client.call("123", "1")
