"""NetSuite HTTP Extractor — component entrypoint.

``run()`` is a thin orchestrator: validate config → build the TBA signer → select the extractor for
the row's mode → run it → write the output tables, manifests and state. All HTTP/SOAP lives under
``client/`` and all mapping under ``extractor/``; no business logic lives here.
"""

import csv
import json
import logging
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from keboola.component.base import ComponentBase
from keboola.component.dao import BaseType, ColumnDefinition, TableDefinition
from keboola.component.exceptions import UserException

from client.auth import TBASigner
from client.rest import RestClient
from client.restlet import RestletClient
from client.soap import SoapClient
from configuration import Configuration, RecordRow, RestletRow, SavedSearchRow, SuiteQLRow
from extractor.base import ExtractionResult, Extractor, OutputTable
from extractor.record import RecordExtractor
from extractor.restlet import RestletExtractor
from extractor.saved_search import SavedSearchExtractor
from extractor.suiteql import SuiteQLExtractor
from sync_actions import SyncActionsMixin

_STATE_LAST_RUN = "last_run"

_BASE_TYPES = {
    "integer": BaseType.integer,
    "numeric": BaseType.numeric,
    "boolean": BaseType.boolean,
    "string": BaseType.string,
}


class Component(SyncActionsMixin, ComponentBase):
    """NetSuite HTTP extractor component."""

    def __init__(self):
        super().__init__()
        self.params = Configuration(**self.configuration.parameters)

    # ---- orchestration ---------------------------------------------------

    def run(self):
        """Validate → build signer → select extractor by mode → run → write output + state."""
        row = self._require_row()
        signer = self.build_signer()
        rest_client = RestClient(signer)
        since = self._load_since(row.incremental)
        server_time: Callable[[], str] = rest_client.server_time

        extractor = self._select_extractor(row, signer, rest_client, since, server_time)
        result = extractor.extract()

        self._write_result(result)
        self._write_state(result)

    def _select_extractor(
        self,
        row: RecordRow | SuiteQLRow | SavedSearchRow | RestletRow,
        signer: TBASigner,
        rest_client: RestClient,
        since: str | None,
        server_time: Callable[[], str],
    ) -> Extractor:
        if isinstance(row, RecordRow):
            return RecordExtractor(row, rest_client, since=since, server_time_provider=server_time)
        if isinstance(row, SuiteQLRow):
            return SuiteQLExtractor(row, rest_client, since=since, server_time_provider=server_time)
        if isinstance(row, SavedSearchRow):
            return SavedSearchExtractor(row, SoapClient(signer), since=since, server_time_provider=server_time)
        if isinstance(row, RestletRow):
            return RestletExtractor(row, RestletClient(signer), since=since, server_time_provider=server_time)
        raise UserException(f"Unsupported mode: {getattr(row, 'mode', None)}")

    # ---- config / signer helpers ----------------------------------------

    def _require_row(self) -> RecordRow | SuiteQLRow | SavedSearchRow | RestletRow:
        if self.params.row is None:
            raise UserException("Missing required parameter 'mode' (the extraction target).")
        return self.params.row

    def _load_since(self, incremental: bool) -> str | None:
        if not incremental:
            return None
        state = self.get_state_file() or {}
        return state.get(_STATE_LAST_RUN)

    # ---- output / state --------------------------------------------------

    def _write_result(self, result: ExtractionResult) -> None:
        for table in result.tables:
            self._write_table(table)

    def _write_table(self, table: OutputTable) -> None:
        rows = list(table.rows)
        columns = table.columns or self._collect_columns(rows)
        if not columns:
            logging.warning("Table '%s' produced no columns; skipping.", table.name)
            return

        table_def = self.create_out_table_definition(
            f"{table.name}.csv",
            primary_key=table.primary_key,
            incremental=table.incremental,
            schema=self._build_schema(columns, table.column_types, table.primary_key),
            has_header=True,
        )
        with open(table_def.full_path, "w", encoding="utf-8", newline="") as out_file:
            writer = csv.DictWriter(out_file, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({col: self._serialize_value(row.get(col)) for col in columns})
        self.write_manifest(table_def)
        logging.info("Wrote %s rows to table '%s'.", len(rows), table.name)

    @staticmethod
    def _build_schema(
        columns: list[str],
        column_types: dict[str, str] | None,
        primary_key: list[str],
    ) -> TableDefinition.SCHEMA_TYPE:
        column_types = column_types or {}
        schema: OrderedDict[str, ColumnDefinition] = OrderedDict()
        for name in columns:
            base_type = _BASE_TYPES.get(column_types.get(name, "string"), BaseType.string)()
            schema[name] = ColumnDefinition(data_types=base_type, primary_key=name in primary_key)
        return schema

    @staticmethod
    def _collect_columns(rows: list[dict[str, Any]]) -> list[str]:
        columns: list[str] = []
        for row in rows:
            for key in row:
                if key not in columns:
                    columns.append(key)
        return columns

    @staticmethod
    def _serialize_value(value: Any) -> Any:
        if isinstance(value, (dict, list)):
            return json.dumps(value)
        return value

    def _write_state(self, result: ExtractionResult) -> None:
        # State is advanced ONLY after a successful output write (spec §2): a failed run keeps the
        # old watermark and is safely retried.
        if result.state:
            self.write_state_file(result.state)


if __name__ == "__main__":
    try:
        comp = Component()
        comp.execute_action()
    except UserException as exc:
        logging.exception(exc)
        exit(1)
    except Exception as exc:
        logging.exception(exc)
        exit(2)
