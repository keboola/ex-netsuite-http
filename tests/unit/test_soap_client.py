"""Thin unit tests for the SOAP client.

Full SOAP behaviour (search/searchMoreWithId/get) is covered by VCR in Phase 5 — recording a real
envelope is the only sane way to exercise zeep. Here we only assert the module imports and that the
TokenPassport SOAP header is built from the signer (the auth wiring, which VCR cannot easily verify
because signatures are scrubbed)."""

from unittest import mock

import lxml.etree as etree
import pytest
from keboola.component.exceptions import UserException

from client.auth import TBASigner
from client.soap import SoapClient


def _client():
    signer = TBASigner("1234567_SB1", "ck", "cs", "ti", "ts")
    return SoapClient(signer)


def test_token_passport_header_built_from_signer():
    client = _client()
    element = client.token_passport_element(nonce="abc123", timestamp="1600000000")
    xml = etree.tostring(element, encoding="unicode")
    assert "1234567_SB1" in xml  # account
    assert "ck" in xml  # consumerKey
    assert "ti" in xml  # token
    assert "abc123" in xml  # nonce
    assert "1600000000" in xml  # timestamp
    assert 'algorithm="HMAC-SHA256"' in xml
    # the signature value is present (computed by the signer)
    assert client.signer.token_passport(nonce="abc123", timestamp="1600000000")["signature"] in xml


def test_wsdl_url_prefers_bundled_local_copy():
    """The version-pinned WSDL is bundled in the repo so SOAP cassettes replay offline (no network)."""
    client = _client()
    url = client.wsdl_url
    assert url.startswith("file://")  # loaded from disk, not the network
    assert url.endswith("/wsdl/v2023_2_0/netsuite.wsdl")
    # the bundled tree must not embed any account-specific host
    assert "suitetalk.api.netsuite.com" not in url


class _FakeBinding:
    def __init__(self, name):
        self.name = name


class _FakePort:
    def __init__(self, binding):
        self.binding = binding


class _FakeService:
    def __init__(self, ports):
        self.ports = ports


class _FakeWsdl:
    def __init__(self, services):
        self.services = services


class _FakeZeepClient:
    def __init__(self):
        self.wsdl = _FakeWsdl({"svc": _FakeService({"port": _FakePort(_FakeBinding("{ns}NetSuiteBinding"))})})
        self.created_with = None

    def create_service(self, binding_name, address):
        self.created_with = (binding_name, address)
        return f"service@{address}"


def test_service_rebinds_to_account_specific_endpoint():
    """The pinned WSDL advertises the generic host; the client must rebind to the account host.

    Regression guard for the live-sandbox defect: NetSuite rejects the WSDL's default
    ``webservices.netsuite.com`` endpoint with "you must use account-specific domains".
    """
    client = _client()
    fake = _FakeZeepClient()
    # Short-circuit the cached_property so no real WSDL is loaded.
    client.__dict__["_client"] = fake

    service = client._service

    assert fake.created_with is not None
    binding_name, address = fake.created_with
    assert binding_name == "{ns}NetSuiteBinding"
    assert address == "https://1234567-sb1.suitetalk.api.netsuite.com/services/NetSuitePort_2023_2"
    assert "webservices.netsuite.com" not in address
    assert service == f"service@{address}"


def test_saved_search_request_builds_and_validates_offline():
    """Regression guard for the run_saved_search request SHAPE.

    NetSuite runs a saved search via a typed ``<RecordType>SearchAdvanced`` record carrying
    ``savedSearchId`` — the earlier ``SearchRequest(savedSearchId=...)`` shape was invalid (that type
    has no such field). zeep validates request objects against the schema at serialization time, so
    building + serializing the request against the bundled WSDL (offline, no network) catches exactly
    that signature-mismatch class of bug.
    """
    client = _client()
    advanced = client._advanced_search_type("Transaction")
    record = advanced(savedSearchId="customsearch_example")
    record.criteria = client._build_criteria("Transaction", "2024-01-01T00:00:00Z")
    node = client._client.create_message(client._client.service, "search", searchRecord=record)
    xml = etree.tostring(node, encoding="unicode")
    assert "customsearch_example" in xml  # savedSearchId serialized
    assert "SearchAdvanced" in xml  # the SearchAdvanced record type is used
    assert "onOrAfter" in xml  # incremental lastModifiedDate criterion serialized


def test_advanced_search_type_resolves_across_namespaces():
    client = _client()
    for record_type in ("Transaction", "Customer", "Item"):
        advanced = client._advanced_search_type(record_type)
        assert advanced.name == f"{record_type}SearchAdvanced"


def test_unknown_search_record_type_raises():
    client = _client()
    with pytest.raises(UserException):
        client._advanced_search_type("NotARealRecordType")


# ---- T6: SOAP fault / transport error translation ------------------------


def test_soap_fault_translated_to_user_exception():
    """A zeep Fault (server-side rejection: bad creds / invalid search / missing permission) must
    surface as a clean UserException (exit 1), not an unhandled exit-2 crash."""
    from zeep.exceptions import Fault

    client = _client()
    fake_service = mock.Mock()
    fake_service.search.side_effect = Fault("INVALID_LOGIN: bad token")
    client.__dict__["_service"] = fake_service  # short-circuit the cached WSDL-backed service

    with pytest.raises(UserException) as exc:
        client.search(search_record=object(), page_size=10)
    assert "SOAP search was rejected" in str(exc.value)


def test_soap_transport_error_translated_to_user_exception():
    """A zeep TransportError (timeout / connection reset) is transient infra failure and must surface
    as a UserException with a retry hint, not an unhandled crash."""
    from zeep.exceptions import TransportError

    client = _client()
    fake_service = mock.Mock()
    fake_service.searchMoreWithId.side_effect = TransportError("read timed out", status_code=504)
    client.__dict__["_service"] = fake_service

    with pytest.raises(UserException) as exc:
        client.search_more_with_id("s1", 2)
    assert "failed to reach the server" in str(exc.value)
