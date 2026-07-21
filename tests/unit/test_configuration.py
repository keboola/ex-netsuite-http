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


# ---- run-start validation (B1 incremental+PK, I4 required fields) --------


def test_incremental_without_primary_key_rejected_at_run():
    # B1: lenient construction must succeed (sync actions), run-start validation must reject.
    cfg = _cfg(mode="suiteql", query="SELECT 1", load_type="incremental_load", primary_key=[])
    assert cfg.row is not None  # constructs leniently
    with pytest.raises(UserException, match="primary key"):
        cfg.validate_for_run()


def test_incremental_with_primary_key_passes_run_validation():
    cfg = _cfg(mode="suiteql", query="SELECT 1", load_type="incremental_load", primary_key=["id"])
    row = cfg.validate_for_run()
    assert row.incremental is True


def test_full_load_without_primary_key_ok_at_run():
    cfg = _cfg(mode="suiteql", query="SELECT 1", load_type="full_load", primary_key=[])
    cfg.validate_for_run()  # no raise


def test_missing_mode_essential_field_rejected_at_run():
    # I4: record_type/query/saved_search_id/script_id default "" and construct leniently, but a run
    # must fail fast with a clear UserException.
    cfg = _cfg(mode="record", load_type="full_load")  # no record_type
    assert cfg.row is not None
    with pytest.raises(UserException, match="record_type"):
        cfg.validate_for_run()


def test_restlet_missing_deploy_id_rejected_at_run():
    cfg = _cfg(mode="restlet", script_id="123", load_type="full_load")  # no deploy_id
    with pytest.raises(UserException, match="deploy_id"):
        cfg.validate_for_run()


def test_saved_search_missing_id_rejected_at_run():
    cfg = _cfg(mode="saved_search", load_type="full_load")
    with pytest.raises(UserException, match="saved_search_id"):
        cfg.validate_for_run()


def test_missing_mode_rejected_at_run():
    cfg = Configuration(**CONNECTION)
    with pytest.raises(UserException, match="mode"):
        cfg.validate_for_run()


def test_sync_action_construction_stays_lenient():
    # A record row with default incremental load and no record_type/PK must still CONSTRUCT so
    # sync actions (listFields etc.) run before the user has filled every field.
    cfg = _cfg(mode="record")
    assert isinstance(cfg.row, RecordRow)


def test_extra_filters_typed_as_list_of_dict():
    cfg = _cfg(mode="saved_search", saved_search_id="cs", extra_filters=[{"field": "status"}])
    assert cfg.row.extra_filters == [{"field": "status"}]


# ---- NTH5: window_size requires the windowing placeholders -----------------


def test_window_size_without_placeholders_rejected_at_run():
    cfg = _cfg(mode="suiteql", query="SELECT id FROM tx", load_type="full_load", window_size=30)
    with pytest.raises(UserException, match="window_start"):
        cfg.validate_for_run()


def test_window_size_with_placeholders_passes():
    cfg = _cfg(
        mode="suiteql",
        query="SELECT id FROM tx WHERE trandate BETWEEN ':window_start' AND ':window_end'",
        load_type="full_load",
        window_size=30,
    )
    cfg.validate_for_run()  # no raise


def test_no_windowing_when_window_size_zero():
    cfg = _cfg(mode="suiteql", query="SELECT id FROM tx", load_type="full_load", window_size=0)
    cfg.validate_for_run()  # no placeholder requirement


def test_window_column_field_removed():
    cfg = _cfg(mode="suiteql", query="SELECT 1", load_type="full_load")
    assert not hasattr(cfg.row, "window_column")
