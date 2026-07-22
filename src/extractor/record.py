"""``record`` mode extractor — REST Record API first.

Fetches a Record collection with an optional ``fields`` projection and ``q`` filter (the incremental
``lastModifiedDate ON_OR_AFTER`` clause is folded in for incremental runs), then handles sublists
either by flattening them into a JSON column on the parent row or by splitting them into a child
table keyed to the parent id.

Deferred variant (spec §4): a transparent REST→SOAP fallback when a record type is not REST-exposed
or a join is required. The seam is present (:meth:`_soap_fallback`) but the automatic trigger is not
wired pending sandbox coverage and user sign-off.
"""

import json
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from keboola.component.exceptions import UserException

from client.rest import RestClient
from configuration import RecordRow, SublistHandling
from extractor.base import (
    STATE_LAST_RUN,
    ExtractionResult,
    Extractor,
    OutputTable,
    collect_columns,
    infer_column_types,
)

# Candidate per-line identifier columns used to form a child-table composite PK, in priority order.
_CHILD_KEY_CANDIDATES = ("line", "lineuniquekey", "id", "key", "sequence", "seq")


def _ns_date(iso: str) -> str:
    """Render an ISO-8601 UTC watermark as the ``M/D/YYYY`` date the REST record ``q`` grammar wants.

    NetSuite's Record collection ``q`` date operators (``ON_OR_AFTER`` …) reject an ISO-8601
    timestamp (400) — confirmed against the live sandbox — and accept ``M/D/YYYY``. The watermark is
    therefore truncated to date granularity for the ``q`` filter (a day of overlap is re-pulled and
    de-duplicated by the primary key on the incremental upsert).
    """
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    return f"{dt.month}/{dt.day}/{dt.year}"


class RecordExtractor(Extractor):
    def __init__(
        self,
        row: RecordRow,
        rest_client: RestClient,
        since: str | None = None,
        server_time_provider: Callable[[], str] | None = None,
    ):
        self.row = row
        self.rest_client = rest_client
        self.since = since
        self.server_time_provider = server_time_provider

    def extract(self) -> ExtractionResult:
        # Capture the watermark from NetSuite's clock BEFORE fetching (spec §2).
        new_watermark = self._capture_watermark()

        q = self._build_q()
        logging.info("Fetching record collection '%s' (q=%s)", self.row.record_type, q)
        records = self._fetch_records(q)

        table_name = self.row.output_table_name or self.row.record_type
        if self.row.sublist_handling == SublistHandling.child_table:
            tables = self._split_child_tables(records, table_name)
        else:
            tables = [self._flatten_table(records, table_name)]

        state = {STATE_LAST_RUN: new_watermark} if new_watermark else {}
        return ExtractionResult(tables=tables, state=state)

    # ---- fetch -----------------------------------------------------------

    def _fetch_records(self, q: str | None) -> list[dict[str, Any]]:
        """Fetch the full records (eagerly, so a fetch failure surfaces before state is written).

        The REST record collection is ID-only (spec §9 risk 5): it returns ids + HATEOAS links, not
        field values or sublists. So whenever field values or sublist data are wanted we GET each
        record with ``expandSubResources`` (accepting the documented N+1 cost, spec §9 risk 5).
        """
        collection = self.rest_client.iter_record_collection(
            self.row.record_type,
            q=q,
            fields=self.row.fields or None,
            limit=self.row.page_limit,
        )
        if not self._needs_detail():
            return list(collection)
        records: list[dict[str, Any]] = []
        for item in collection:
            record_id = item.get("id")
            if record_id is None:
                records.append(item)
                continue
            records.append(self.rest_client.get_record(self.row.record_type, str(record_id), expand_sub_resources=True))
        return records

    def _needs_detail(self) -> bool:
        # Record mode always needs more than the ID-only collection: either specific field values or
        # sublist data (flatten/child_table). The per-id GET with expandSubResources supplies both.
        return bool(self.row.fields) or self.row.sublist_handling in (
            SublistHandling.flatten,
            SublistHandling.child_table,
        )

    # ---- watermark / filter ---------------------------------------------

    def _capture_watermark(self) -> str | None:
        # Persist a watermark on every successful run (incl. full loads) so a later full->incremental
        # switch resumes from this run instead of re-pulling all history (spec §2).
        if self.server_time_provider is None:
            return None
        return self.server_time_provider()

    def _build_q(self) -> str | None:
        clauses: list[str] = []
        if self.row.query_filter:
            clauses.append(self.row.query_filter)
        if self.row.incremental and self.since:
            clauses.append(f'{self.row.incremental_field} ON_OR_AFTER "{_ns_date(self.since)}"')
        if not clauses:
            return None
        return " AND ".join(f"({c})" for c in clauses)

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
            for key, value in record.items():
                if self._is_sublist(value):
                    row[key] = json.dumps(self._sublist_items(value))
                else:
                    row[key] = value
            yield row

    def _split_child_tables(self, records: Any, table_name: str) -> list[OutputTable]:
        # Child tables are streamed together, so materialize once and fan out.
        parent_rows: list[dict[str, Any]] = []
        child_rows: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            parent = {}
            record_id = record.get("id")
            for key, value in record.items():
                if self._is_sublist(value):
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
