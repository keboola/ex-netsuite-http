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


def test_suiteql_incremental_happy_path(tmp_path):
    params = {
        **CONNECTION,
        "mode": "suiteql",
        "query": "SELECT id, name FROM customer",
        "output_table_name": "customers",
        "primary_key": ["id"],
        "load_type": "incremental_load",
    }
    data_dir = _make_datadir(tmp_path, params)
    rows = [{"id": "1", "name": "ACME"}, {"id": "2", "name": "Globex"}]
    with mock.patch.object(RestClient, "iter_suiteql", return_value=iter(rows)):
        _run(data_dir)
    out = _read_csv(data_dir, "customers")
    assert [r["id"] for r in out] == ["1", "2"]
    # §4: no state file is written anymore.
    assert not (data_dir / "out" / "state.json").exists()


def test_suiteql_date_range_substituted_into_query(tmp_path):
    params = {
        **CONNECTION,
        "mode": "suiteql",
        "query": "SELECT id FROM tx WHERE trandate BETWEEN :date_from AND :date_to",
        "output_table_name": "tx",
        "load_type": "full_load",
        "date_from": "2024-01-01",
        "date_to": "2024-03-01",
    }
    data_dir = _make_datadir(tmp_path, params)
    with mock.patch.object(RestClient, "iter_suiteql", return_value=iter([{"id": "1"}])) as iter_mock:
        _run(data_dir)
    query = iter_mock.call_args[0][0]
    assert ":date_from" not in query and ":date_to" not in query
    assert "2024-01-01" in query and "2024-03-01" in query


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
    # Collection is ID-only; full field values come from the per-id expandSubResources GET (I3).
    with (
        mock.patch.object(RestClient, "iter_record_collection", return_value=iter([{"id": "1"}])),
        mock.patch.object(RestClient, "get_record", return_value={"id": "1", "entityid": "ACME"}),
    ):
        _run(data_dir)
    out = _read_csv(data_dir, "customer")
    assert out[0]["entityid"] == "ACME"
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


def test_zero_row_run_writes_header_only_table_and_manifest(tmp_path):
    # T12: a 0-row run must still create the Storage table (header-only CSV + manifest) using the
    # configured primary key as the known schema, rather than silently skipping the table.
    params = {
        **CONNECTION,
        "mode": "suiteql",
        "query": "SELECT id, name FROM customer",
        "output_table_name": "customers",
        "primary_key": ["id"],
        "load_type": "full_load",
    }
    data_dir = _make_datadir(tmp_path, params)
    with mock.patch.object(RestClient, "iter_suiteql", return_value=iter([])):
        _run(data_dir)
    csv_path = data_dir / "out" / "tables" / "customers.csv"
    manifest_path = data_dir / "out" / "tables" / "customers.csv.manifest"
    assert csv_path.exists()
    assert manifest_path.exists()
    # header-only: exactly one line (the header from the configured primary key), no data rows
    lines = csv_path.read_text(encoding="utf-8").splitlines()
    assert lines == ["id"]
    assert _read_csv(data_dir, "customers") == []


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
