"""Unit tests for the 6 sync actions (mocked clients).

Actions are called with config action="run" so the @sync_action wrapper returns the result object
(rather than serialising to stdout), letting us assert the returned UI payload directly."""

import json
import os
from pathlib import Path
from unittest import mock

import pytest
from keboola.component.base import _SYNC_ACTION_MAPPING
from keboola.component.exceptions import UserException
from keboola.component.sync_actions import SelectElement, ValidationResult

from client.rest import RestClient
from client.restlet import RestletClient
from client.soap import SoapClient
from component import Component

CONNECTION = {
    "account_id": "1234567_SB1",
    "#consumer_key": "ck",
    "#consumer_secret": "cs",
    "#token_id": "ti",
    "#token_secret": "ts",
}


def _component(tmp_path: Path, parameters: dict) -> Component:
    data_dir = tmp_path / "data"
    (data_dir / "in" / "tables").mkdir(parents=True)
    (data_dir / "out" / "tables").mkdir(parents=True)
    (data_dir / "config.json").write_text(json.dumps({"parameters": parameters, "action": "run"}))
    (data_dir / "in" / "state.json").write_text("{}")
    with mock.patch.dict(os.environ, {"KBC_DATADIR": str(data_dir)}):
        return Component()


def test_all_actions_registered():
    for name in (
        "testConnection",
        "listRecordTypes",
        "listFields",
        "getColumns",
        "listSavedSearches",
        "validateSuiteQL",
        "previewRestlet",
    ):
        assert name in _SYNC_ACTION_MAPPING


def test_test_connection_success(tmp_path):
    comp = _component(tmp_path, {**CONNECTION})
    with mock.patch.object(RestClient, "get_metadata_catalog", return_value={"items": []}):
        result = comp.test_connection()
    assert isinstance(result, ValidationResult)
    assert "successful" in result.message.lower()


def test_test_connection_failure_raises(tmp_path):
    comp = _component(tmp_path, {**CONNECTION})
    with mock.patch.object(RestClient, "get_metadata_catalog", side_effect=UserException("401")):
        with pytest.raises(UserException):
            comp.test_connection()


def test_list_record_types(tmp_path):
    comp = _component(tmp_path, {**CONNECTION})
    catalog = {"items": [{"name": "customer"}, {"name": "invoice"}]}
    with mock.patch.object(RestClient, "get_metadata_catalog", return_value=catalog):
        result = comp.list_record_types()
    assert all(isinstance(e, SelectElement) for e in result)
    assert [e.value for e in result] == ["customer", "invoice"]


def test_list_fields_requires_record_type(tmp_path):
    comp = _component(tmp_path, {**CONNECTION, "mode": "record", "record_type": ""})
    with pytest.raises(UserException):
        comp.list_fields()


def test_list_fields(tmp_path):
    comp = _component(tmp_path, {**CONNECTION, "mode": "record", "record_type": "customer"})
    schema = {"properties": {"id": {}, "entityid": {}}}
    with mock.patch.object(RestClient, "get_metadata_catalog", return_value=schema):
        result = comp.list_fields()
    assert {e.value for e in result} == {"id", "entityid"}


def test_get_columns_record_mode_uses_metadata_fields(tmp_path):
    comp = _component(tmp_path, {**CONNECTION, "mode": "record", "record_type": "customer"})
    schema = {"properties": {"id": {}, "entityid": {}}}
    with mock.patch.object(RestClient, "get_metadata_catalog", return_value=schema):
        result = comp.get_columns()
    assert {e.value for e in result} == {"id", "entityid"}


def test_get_columns_suiteql_mode_probes_query(tmp_path):
    comp = _component(tmp_path, {**CONNECTION, "mode": "suiteql", "query": "SELECT id, companyname FROM customer"})
    with mock.patch.object(RestClient, "suiteql_page", return_value={"items": [{"id": "1", "companyname": "ACME"}]}):
        result = comp.get_columns()
    assert [e.value for e in result] == ["id", "companyname"]


def test_get_columns_suiteql_substitutes_date_placeholders_before_probe(tmp_path):
    # A date-filtered query must not reach NetSuite with unbound :date_from/:date_to (it would 400
    # and yield no suggestions); the probe substitutes dummy literals first.
    query = "SELECT id FROM tx WHERE trandate BETWEEN :date_from AND :date_to"
    comp = _component(tmp_path, {**CONNECTION, "mode": "suiteql", "query": query})
    with mock.patch.object(RestClient, "suiteql_page", return_value={"items": [{"id": "1"}]}) as probe:
        result = comp.get_columns()
    probed_query = probe.call_args.args[0]
    assert ":date_from" not in probed_query and ":date_to" not in probed_query
    assert "TO_TIMESTAMP" in probed_query
    assert [e.value for e in result] == ["id"]


def test_get_columns_suiteql_probe_failure_returns_empty(tmp_path):
    comp = _component(tmp_path, {**CONNECTION, "mode": "suiteql", "query": "SELECT bad"})
    with mock.patch.object(RestClient, "suiteql_page", side_effect=UserException("400 syntax")):
        result = comp.get_columns()
    assert result == []


def test_get_columns_saved_search_mode_returns_empty(tmp_path):
    comp = _component(tmp_path, {**CONNECTION, "mode": "saved_search", "saved_search_id": "customsearch_x"})
    assert comp.get_columns() == []


def test_get_columns_record_without_record_type_returns_empty(tmp_path):
    comp = _component(tmp_path, {**CONNECTION, "mode": "record", "record_type": ""})
    assert comp.get_columns() == []


def test_list_saved_searches(tmp_path):
    comp = _component(tmp_path, {**CONNECTION, "mode": "saved_search"})
    searches = [{"scriptId": "customsearch_1", "name": "My Search"}]
    with mock.patch.object(SoapClient, "list_saved_searches", return_value=searches):
        result = comp.list_saved_searches()
    assert result[0].value == "customsearch_1"
    assert result[0].label == "My Search"


def test_validate_suiteql_ok(tmp_path):
    comp = _component(tmp_path, {**CONNECTION, "mode": "suiteql", "query": "SELECT id FROM customer"})
    with mock.patch.object(RestClient, "suiteql_page", return_value={"items": []}) as page:
        result = comp.validate_suiteql()
    assert isinstance(result, ValidationResult)
    assert page.call_args.kwargs.get("limit") == 1 or page.call_args[0][1] == 1


def test_validate_suiteql_requires_query(tmp_path):
    comp = _component(tmp_path, {**CONNECTION, "mode": "suiteql", "query": ""})
    with pytest.raises(UserException):
        comp.validate_suiteql()


def test_validate_suiteql_bad_sql_raises(tmp_path):
    comp = _component(tmp_path, {**CONNECTION, "mode": "suiteql", "query": "SELECT bad"})
    with mock.patch.object(RestClient, "suiteql_page", side_effect=UserException("syntax error")):
        with pytest.raises(UserException):
            comp.validate_suiteql()


def test_preview_restlet(tmp_path):
    comp = _component(
        tmp_path,
        {**CONNECTION, "mode": "restlet", "script_id": "123", "deploy_id": "1"},
    )
    with mock.patch.object(RestletClient, "call", return_value={"rows": [{"id": "1"}]}):
        result = comp.preview_restlet()
    assert isinstance(result, ValidationResult)
    assert "rows" in result.message
