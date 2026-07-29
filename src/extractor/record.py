"""``record`` mode extractor — REST Record API first.

Fetches a Record collection with an optional ``fields`` projection and ``q`` filter, then handles
sublists either by flattening them into a JSON column on the parent row or by splitting them into a
child table keyed to the parent id.

Deferred variant (spec §4): a transparent REST→SOAP fallback when a record type is not REST-exposed
or a join is required. The seam is present (:meth:`_soap_fallback`) but the automatic trigger is not
wired pending sandbox coverage and user sign-off.
"""

import json
import logging
from typing import Any

from keboola.component.exceptions import UserException

from client.rest import RestClient
from configuration import RecordRow, SublistHandling
from extractor.base import (
    ExtractionResult,
    Extractor,
    OutputTable,
    collect_columns,
    infer_column_types,
)

# Candidate per-line identifier columns used to form a child-table composite PK, in priority order.
_CHILD_KEY_CANDIDATES = ("line", "lineuniquekey", "id", "key", "sequence", "seq")

# Keys never emitted as output columns / child tables. ``links`` is the HATEOAS navigation array
# NetSuite attaches to every record and sublist item — it is not user data.
_RESERVED_KEYS = frozenset({"links"})


def _strip_links(value: Any) -> Any:
    """Recursively drop every reserved (``links``) key at any depth.

    NetSuite attaches a HATEOAS ``links`` array not only to the top-level record but to every nested
    sublist item and reference (``addressBook.items[i].links``, ``…country.links``, …). Stripping only
    the top level would leave that navigation metadata in child-table columns and buried inside the
    JSON-serialized sublist blobs in flatten mode, so we walk dicts and lists to remove it everywhere.
    """
    if isinstance(value, dict):
        return {k: _strip_links(v) for k, v in value.items() if k not in _RESERVED_KEYS}
    if isinstance(value, list):
        return [_strip_links(v) for v in value]
    return value


class RecordExtractor(Extractor):
    def __init__(self, row: RecordRow, rest_client: RestClient):
        self.row = row
        self.rest_client = rest_client

    def extract(self) -> ExtractionResult:
        q = self._build_q()
        # The q filter is user free text (potential PII / log injection): keep it out of the INFO
        # line and log it at DEBUG only, with newlines stripped so it cannot forge extra log lines.
        logging.info("Fetching record collection '%s'", self.row.record_type)
        if q:
            logging.debug("Record filter q=%s", q.replace("\r", " ").replace("\n", " "))
        records = self._fetch_records(q)

        table_name = self.row.output_table_name or self.row.record_type
        if self.row.sublist_handling == SublistHandling.child_table:
            tables = self._split_child_tables(records, table_name)
        else:
            tables = [self._flatten_table(records, table_name)]

        return ExtractionResult(tables=tables)

    # ---- fetch -----------------------------------------------------------

    def _fetch_records(self, q: str | None) -> list[dict[str, Any]]:
        """Fetch the full records eagerly (no state exists post-overhaul; the eager fetch just
        surfaces a fetch failure before any output table is written).

        The REST record collection is ID-only (spec §9 risk 5): it returns ids + HATEOAS links, no
        field values or sublists, and supports no server-side field projection. So every record is
        GET'd individually with ``expandSubResources`` (accepting the documented N+1 cost, spec §9
        risk 5); the Fields picker is then applied client-side (see ``_project``).
        """
        collection = self.rest_client.iter_record_collection(
            self.row.record_type,
            q=q,
            limit=self.row.page_limit,
        )
        records: list[dict[str, Any]] = []
        for item in collection:
            record_id = item.get("id")
            if record_id is None:
                records.append(item)
                continue
            records.append(self.rest_client.get_record(self.row.record_type, str(record_id), expand_sub_resources=True))
        return records

    # ---- filter ----------------------------------------------------------

    def _build_q(self) -> str | None:
        if not self.row.query_filter:
            return None
        return self.row.query_filter

    # ---- fields projection -------------------------------------------------

    def _project(self, record: dict[str, Any]) -> dict[str, Any]:
        """Shape a fetched record to the Fields picker (client-side; NetSuite's GET has no reliable
        field projection). ``links`` (HATEOAS navigation) is always dropped. When ``fields`` is
        empty, every remaining column passes through unchanged. When ``fields`` is set, only the
        selected columns are kept, in the user's selected order, plus any ``primary_key`` columns so
        the writer's PK-subset-of-columns check still holds.
        """
        stripped = _strip_links(record)
        if not self.row.fields:
            return stripped
        projected = {key: stripped[key] for key in self.row.fields if key in stripped}
        for key in self.row.primary_key:
            if key not in projected and key in stripped:
                projected[key] = stripped[key]
        return projected

    # ---- sublist handling ------------------------------------------------

    def _flatten_table(self, records: Any, table_name: str) -> OutputTable:
        rows = list(self._flatten_rows(records))
        return OutputTable(
            name=table_name,
            rows=rows,
            primary_key=self.row.primary_key,
            incremental=self.row.incremental,
            columns=collect_columns(rows) or None,
            column_types=infer_column_types(rows) or None,
        )

    def _flatten_rows(self, records: Any):
        for record in records:
            row = {}
            for key, value in self._project(record).items():
                if self._is_sublist(value):
                    row[key] = json.dumps(self._sublist_items(value))
                else:
                    row[key] = value
            yield row

    def _split_child_tables(self, records: Any, table_name: str) -> list[OutputTable]:
        # Child tables are streamed together, so materialize once and fan out.
        parent_rows: list[dict[str, Any]] = []
        child_rows: dict[str, list[dict[str, Any]]] = {}
        fields = self.row.fields
        for record in records:
            parent = {}
            record_id = record.get("id")
            for key, value in self._project(record).items():
                if self._is_sublist(value) and (not fields or key in fields):
                    for item in self._sublist_items(value):
                        child_rows.setdefault(key, []).append({"_parent_id": record_id, **item})
                else:
                    parent[key] = value
            parent_rows.append(parent)

        tables = [
            OutputTable(
                name=table_name,
                rows=parent_rows,
                primary_key=self.row.primary_key,
                incremental=self.row.incremental,
                columns=collect_columns(parent_rows) or None,
                column_types=infer_column_types(parent_rows) or None,
            )
        ]
        for sublist_name, rows in child_rows.items():
            # Child tables must share the parent's load semantics: on an incremental run a child table
            # left as full-load with no PK would be truncated to the current batch every run (data
            # loss). Propagate incremental and a composite PK [_parent_id, <line key>].
            tables.append(
                OutputTable(
                    name=f"{table_name}_{sublist_name}",
                    rows=rows,
                    primary_key=self._child_primary_key(sublist_name, rows),
                    incremental=self.row.incremental,
                    columns=collect_columns(rows) or None,
                    column_types=infer_column_types(rows) or None,
                )
            )
        return tables

    def _child_primary_key(self, sublist_name: str, rows: list[dict[str, Any]]) -> list[str]:
        """Derive [_parent_id, <line key>] for a child table; reject incremental if none is sound."""
        child_key = self._derive_child_key(rows)
        if child_key is not None:
            return ["_parent_id", child_key]
        if self.row.incremental:
            raise UserException(
                f"Incremental child-table extraction of sublist '{sublist_name}' needs a per-line key "
                f"to form a composite primary key, but none of {list(_CHILD_KEY_CANDIDATES)} was found. "
                "Use load_type=full_load, or switch sublist_handling to flatten."
            )
        # Full load replaces the whole child table each run, so a PK is not required.
        return []

    @staticmethod
    def _derive_child_key(rows: list[dict[str, Any]]) -> str | None:
        # Intersection semantics: a candidate is a sound per-line key only if it is present AND
        # non-null on EVERY child row. A candidate present on only some rows (or null on some) would
        # let those rows collide on the composite PK and be silently dropped on the incremental
        # upsert. Try candidates in priority order; fall through to the next when one fails.
        if not rows:
            return None
        for candidate in _CHILD_KEY_CANDIDATES:
            if candidate == "_parent_id":
                continue
            if all(row.get(candidate) is not None for row in rows):
                return candidate
        return None

    @staticmethod
    def _is_sublist(value: Any) -> bool:
        if isinstance(value, dict) and isinstance(value.get("items"), list):
            return True
        return isinstance(value, list) and bool(value) and all(isinstance(v, dict) for v in value)

    @staticmethod
    def _sublist_items(value: Any) -> list[dict[str, Any]]:
        if isinstance(value, dict):
            return value.get("items", [])
        return value

    # ---- deferred SOAP fallback seam -------------------------------------

    def _soap_fallback(self) -> ExtractionResult:
        """Extension seam for record types not exposed over REST (deferred variant, spec §4)."""
        raise UserException(
            f"Record type '{self.row.record_type}' is not available over REST and the SOAP fallback "
            "is not enabled in this version."
        )
