"""Thin unit tests for the SOAP client.

Full SOAP behaviour (search/searchMoreWithId/get) is covered by VCR in Phase 5 — recording a real
envelope is the only sane way to exercise zeep. Here we only assert the module imports and that the
TokenPassport SOAP header is built from the signer (the auth wiring, which VCR cannot easily verify
because signatures are scrubbed)."""

import lxml.etree as etree

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


def test_wsdl_url_derived_from_account():
    client = _client()
    assert client.wsdl_url.startswith("https://1234567-sb1.suitetalk.api.netsuite.com/wsdl/")
    assert client.wsdl_url.endswith("/netsuite.wsdl")


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
