"""``metadata`` mode extractor — dump NetSuite's schema catalog.

Produces two fixed output tables:

* ``record_types`` — every record type in the metadata catalog (``name``, ``href``); one catalog call.
* ``fields`` — the field definitions of every record type; one metadata-catalog call *per record
  type*, so a run issues as many calls as the account has record types (hundreds) and is API-heavy.

The initial catalog call is the auth/connection gate and is left unguarded — a failure there aborts
the run with a clear message. Per-type schema calls are guarded: one record type that 404s or is not
permitted is logged and skipped so the dump still completes. The ``fields`` rows are yielded lazily
(one type fetched at a time) so the whole catalog is never held in memory at once.
"""

import json
import logging
from collections.abc import Iterator
from typing import Any

from client.rest import RestClient
from configuration import MetadataRow
from extractor.base import ExtractionResult, Extractor, OutputTable

_RECORD_TYPES_COLUMNS = ["name", "href"]
_FIELDS_COLUMNS = ["record_type", "field_name", "type", "title", "format", "nullable", "enum", "definition"]
# Every fields column is a string except the boolean nullable flag.
_FIELDS_COLUMN_TYPES = {col: ("boolean" if col == "nullable" else "string") for col in _FIELDS_COLUMNS}
# How often to log progress while walking the (hundreds of) record types.
_PROGRESS_EVERY = 50


class MetadataExtractor(Extractor):
    def __init__(self, row: MetadataRow, rest_client: RestClient):
        self.row = row
        self.rest_client = rest_client

    def extract(self) -> ExtractionResult:
        # Fetch the catalog eagerly: it is the auth gate, it is small, and it gives both the
        # record_types rows and the list of types to walk for their fields. Build the rows once and
        # drop items with no resolvable name — an empty name would emit an empty-string primary key
        # (and two such items would collide) — then reuse that filtered list for the per-type walk so
        # the two tables stay consistent (every record_types row has its fields fetched, and vice
        # versa).
        catalog = self.rest_client.get_metadata_catalog()
        record_types = [
            {"name": name, "href": self._item_href(item)}
            for item in (catalog.get("items", []) or [])
            if (name := self._item_name(item))
        ]
        record_type_names = [row["name"] for row in record_types]
        logging.info("Found %s record types; fetching field definitions for each.", len(record_type_names))

        record_types_table = OutputTable(
            name="record_types",
            rows=record_types,
            primary_key=["name"],
            incremental=self.row.incremental,
            columns=_RECORD_TYPES_COLUMNS,
        )
        fields_table = OutputTable(
            name="fields",
            rows=self._iter_field_rows(record_type_names),
            primary_key=["record_type", "field_name"],
            incremental=self.row.incremental,
            columns=_FIELDS_COLUMNS,
            column_types=_FIELDS_COLUMN_TYPES,
        )
        return ExtractionResult(tables=[record_types_table, fields_table])

    def _iter_field_rows(self, record_type_names: list[str]) -> Iterator[dict[str, Any]]:
        """Yield one row per (record type, field), fetching each type's schema lazily.

        A per-type fetch failure is logged and skipped — one inaccessible record type must not abort
        the whole dump (the auth gate already passed on the catalog call).
        """
        total = len(record_type_names)
        for index, record_type in enumerate(record_type_names, start=1):
            try:
                schema = self.rest_client.get_metadata_catalog(record_type)
            except Exception as exc:  # noqa: BLE001 — skip this type, keep dumping the rest
                logging.warning("Skipping fields for record type '%s': %s", record_type, exc)
                continue
            for field_name, definition in (schema.get("properties", {}) or {}).items():
                yield self._field_row(record_type, field_name, definition)
            if index % _PROGRESS_EVERY == 0:
                # "Processed", not "Fetched": the index counts types skipped on a per-type error too.
                logging.info("Processed %s/%s record types.", index, total)

    @staticmethod
    def _field_row(record_type: str, field_name: str, definition: dict[str, Any]) -> dict[str, Any]:
        enum = definition.get("enum")
        return {
            "record_type": record_type,
            "field_name": field_name,
            "type": _type_str(definition),
            "title": definition.get("title"),
            "format": definition.get("format"),
            "nullable": _nullable(definition),
            "enum": json.dumps(enum) if enum is not None else None,
            "definition": json.dumps(definition),
        }

    @staticmethod
    def _item_name(item: dict[str, Any]) -> str:
        return item.get("name") or _href_tail(item)

    @staticmethod
    def _item_href(item: dict[str, Any]) -> str:
        for link in item.get("links", []) or []:
            if link.get("href"):
                return link["href"]
        return ""


def _type_str(definition: dict[str, Any]) -> str | None:
    """The JSON-Schema ``type``, normalized to a string. A ``["number", "null"]`` nullable-style list
    drops the ``null`` marker (reflected in ``nullable`` instead)."""
    type_value = definition.get("type")
    if isinstance(type_value, list):
        non_null = [t for t in type_value if t != "null"]
        return ",".join(non_null) or None
    return type_value


def _nullable(definition: dict[str, Any]) -> bool | None:
    """Whether the field is nullable, from either the OpenAPI ``nullable`` flag or a JSON-Schema
    ``type: [..., "null"]`` list. ``None`` when neither says (unknown, not asserted False)."""
    if "nullable" in definition:
        return bool(definition["nullable"])
    type_value = definition.get("type")
    if isinstance(type_value, list):
        return "null" in type_value
    return None


def _href_tail(item: dict[str, Any]) -> str:
    for link in item.get("links", []) or []:
        href = link.get("href", "")
        if href:
            return href.rstrip("/").rsplit("/", 1)[-1]
    return ""
