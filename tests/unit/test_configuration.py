"""Unit tests for the Pydantic configuration models."""

import pytest
from keboola.component.exceptions import UserException

from configuration import (
    Configuration,
    HttpMethod,
    LoadType,
    RecordRow,
    RestletRow,
    SavedSearchRow,
    SublistHandling,
    SuiteQLRow,
)

CONNECTION = {
    "account_id": "1234567_SB1",
    "#consumer_key": "ck",
    "#consumer_secret": "cs",
    "#token_id": "ti",
    "#token_secret": "ts",
}


def _cfg(**overrides):
    data = {**CONNECTION, **overrides}
    return Configuration(**data)


def test_connection_parsed_and_secrets_available():
    cfg = _cfg(mode="suiteql", query="SELECT 1")
    assert cfg.connection.account_id == "1234567_SB1"
    assert cfg.connection.consumer_key == "ck"
    assert cfg.connection.consumer_secret == "cs"
    assert cfg.connection.token_id == "ti"
    assert cfg.connection.token_secret == "ts"


def test_record_mode_parses_to_record_row():
    cfg = _cfg(mode="record", record_type="customer", fields=["id", "entityid"])
    assert isinstance(cfg.row, RecordRow)
    assert cfg.row.record_type == "customer"
    assert cfg.row.fields == ["id", "entityid"]
    assert cfg.row.sublist_handling == SublistHandling.flatten


def test_suiteql_mode_parses_to_suiteql_row():
    cfg = _cfg(mode="suiteql", query="SELECT id FROM customer")
    assert isinstance(cfg.row, SuiteQLRow)
    assert cfg.row.query == "SELECT id FROM customer"


def test_saved_search_mode_parses_to_saved_search_row():
    cfg = _cfg(mode="saved_search", saved_search_id="customsearch_x")
    assert isinstance(cfg.row, SavedSearchRow)
    assert cfg.row.saved_search_id == "customsearch_x"


def test_restlet_mode_parses_to_restlet_row():
    cfg = _cfg(mode="restlet", script_id="123", deploy_id="1", method="POST")
    assert isinstance(cfg.row, RestletRow)
    assert cfg.row.method == HttpMethod.POST


def test_missing_secret_raises_user_exception():
    data = {k: v for k, v in CONNECTION.items() if k != "#consumer_key"}
    data["mode"] = "suiteql"
    with pytest.raises(UserException):
        Configuration(**data)


def test_unknown_mode_raises_user_exception():
    with pytest.raises(UserException):
        _cfg(mode="not_a_mode")


def test_incremental_load_computes_incremental_true():
    cfg = _cfg(mode="suiteql", query="SELECT 1", load_type="incremental_load")
    assert cfg.row.load_type == LoadType.incremental_load
    assert cfg.row.incremental is True


def test_full_load_computes_incremental_false():
    cfg = _cfg(mode="suiteql", query="SELECT 1", load_type="full_load")
    assert cfg.row.incremental is False


def test_default_load_type_is_incremental():
    cfg = _cfg(mode="suiteql", query="SELECT 1")
    assert cfg.row.load_type == LoadType.incremental_load
    assert cfg.row.incremental is True


def test_row_absent_when_no_mode():
    # config-level context (e.g. testConnection) has connection but no row/mode
    cfg = Configuration(**CONNECTION)
    assert cfg.row is None
    assert cfg.connection.account_id == "1234567_SB1"
