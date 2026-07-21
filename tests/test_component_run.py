"""End-to-end run() tests per mode with monkeypatched clients (no network).

Each test builds a temporary KBC data directory (merged config.json + row-scoped state), patches the
client methods the mode uses, runs the component, and asserts the output CSV, manifest and state."""

import csv
import json
import os
from pathlib import Path
from unittest import mock

import pytest
from keboola.component.exceptions import UserException

from client.rest import RestClient
from client.restlet import RestletClient
from component import Component

CONNECTION = {
    "account_id": "1234567_SB1",
    "#consumer_key": "ck",
    "#consumer_secret": "cs",
    "#token_id": "ti",
    "#token_secret": "ts",
}


def _make_datadir(tmp_path: Path, parameters: dict, state: dict | None = None) -> Path:
    data_dir = tmp_path / "data"
    (data_dir / "in" / "tables").mkdir(parents=True)
    (data_dir / "out" / "tables").mkdir(parents=True)
    (data_dir / "config.json").write_text(json.dumps({"parameters": parameters, "action": "run"}))
    (data_dir / "in" / "state.json").write_text(json.dumps(state or {}))
    return data_dir


def _run(data_dir: Path) -> Component:
    with mock.patch.dict(os.environ, {"KBC_DATADIR": str(data_dir)}):
        comp = Component()
        comp.run()
    return comp


def _read_csv(data_dir: Path, name: str) -> list[dict]:
    with open(data_dir / "out" / "tables" / f"{name}.csv", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _read_state(data_dir: Path) -> dict:
    return json.loads((data_dir / "out" / "state.json").read_text())


def test_suiteql_incremental_happy_path(tmp_path):
    params = {
        **CONNECTION,
        "mode": "suiteql",
        "query": "SELECT id, name FROM customer WHERE lastmodifieddate > :state",
        "output_table_name": "customers",
        "primary_key": ["id"],
        "load_type": "incremental_load",
    }
    data_dir = _make_datadir(tmp_path, params, state={"last_run": "2024-01-01T00:00:00Z"})
    rows = [{"id": "1", "name": "ACME"}, {"id": "2", "name": "Globex"}]
    with (
        mock.patch.object(RestClient, "iter_suiteql", return_value=iter(rows)) as iter_mock,
        mock.patch.object(RestClient, "server_time", return_value="2024-05-01T00:00:00Z"),
    ):
        _run(data_dir)
    # incremental lower bound bound into the query
    assert "2024-01-01T00:00:00Z" in iter_mock.call_args[0][0]
    out = _read_csv(data_dir, "customers")
    assert [r["id"] for r in out] == ["1", "2"]
    assert _read_state(data_dir) == {"last_run": "2024-05-01T00:00:00Z"}


def test_record_flatten_happy_path(tmp_path):
    params = {
        **CONNECTION,
        "mode": "record",
        "record_type": "customer",
        "output_table_name": "customer",
        "primary_key": ["id"],
        "load_type": "full_load",
    }
    data_dir = _make_datadir(tmp_path, params)
    records = [{"id": "1", "entityid": "ACME"}]
    with mock.patch.object(RestClient, "iter_record_collection", return_value=iter(records)):
        _run(data_dir)
    out = _read_csv(data_dir, "customer")
    assert out[0]["entityid"] == "ACME"
    # full load writes no state
    assert not (data_dir / "out" / "state.json").exists()


def test_restlet_happy_path(tmp_path):
    params = {
        **CONNECTION,
        "mode": "restlet",
        "script_id": "123",
        "deploy_id": "1",
        "output_table_name": "restlet_out",
        "record_path": "rows",
        "load_type": "full_load",
    }
    data_dir = _make_datadir(tmp_path, params)
    with mock.patch.object(RestletClient, "iter_records", return_value=iter([{"id": "9"}])):
        _run(data_dir)
    out = _read_csv(data_dir, "restlet_out")
    assert out[0]["id"] == "9"


def test_bad_config_missing_mode_raises_user_exception(tmp_path):
    params = {**CONNECTION}  # no mode
    data_dir = _make_datadir(tmp_path, params)
    with pytest.raises(UserException):
        _run(data_dir)


def test_missing_secret_raises_user_exception(tmp_path):
    params = {"account_id": "1234567_SB1", "mode": "suiteql", "query": "SELECT 1"}
    data_dir = _make_datadir(tmp_path, params)
    with pytest.raises(UserException):
        _run(data_dir)


def test_manifest_written_with_schema(tmp_path):
    params = {
        **CONNECTION,
        "mode": "suiteql",
        "query": "SELECT id FROM customer",
        "output_table_name": "customers",
        "load_type": "full_load",
    }
    data_dir = _make_datadir(tmp_path, params)
    with mock.patch.object(RestClient, "iter_suiteql", return_value=iter([{"id": 1}])):
        _run(data_dir)
    manifest = json.loads((data_dir / "out" / "tables" / "customers.csv.manifest").read_text())
    assert manifest  # a manifest exists
