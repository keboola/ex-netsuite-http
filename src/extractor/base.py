"""Shared contract for the per-mode extractors.

An extractor turns one configured row into an :class:`ExtractionResult`: a list of output tables
(streamed rows + resolved name/PK/incremental) plus the new state to persist. Extractors own all
NetSuite interaction and row mapping; ``component.py`` owns the platform I/O (writing CSVs, manifests
and the state file). Keeping the two apart makes extractors unit-testable with mocked clients and no
data directory.
"""

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

# Single source of truth for the incremental watermark key in the state file (imported by
# component.py and every extractor so the key can never drift between writer and readers).
STATE_LAST_RUN = "last_run"


@dataclass
class OutputTable:
    """One output table: streamed rows plus the metadata needed to write its manifest."""

    name: str
    rows: Iterable[dict[str, Any]]
    primary_key: list[str] = field(default_factory=list)
    incremental: bool = False
    # Optional column ordering and per-column base types (colname -> "string"/"integer"/...).
    columns: list[str] | None = None
    column_types: dict[str, str] | None = None


@dataclass
class ExtractionResult:
    """Everything a run produces: the tables to write and the state to persist on success."""

    tables: list[OutputTable]
    state: dict[str, Any] = field(default_factory=dict)


class Extractor(ABC):
    """Base interface for a single-row extraction."""

    @abstractmethod
    def extract(self) -> ExtractionResult:
        """Fetch data and return the tables + new state (state written only after success)."""


def infer_base_type(value: Any) -> str:
    """Best-effort base type for native-types manifests, inferred from a Python value."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "numeric"
    return "string"


def collect_columns(rows: Iterable[dict[str, Any]]) -> list[str]:
    """Return the ordered union of column names across ``rows`` (first-seen order preserved)."""
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    return columns


def infer_column_types(rows: Iterable[dict[str, Any]]) -> dict[str, str]:
    """Infer a base type per column from the first non-null value seen for that column.

    Dict/list values (sublists serialized to JSON at write time) stay ``string``.
    """
    types: dict[str, str] = {}
    for row in rows:
        for key, value in row.items():
            if key not in types and value is not None and not isinstance(value, (dict, list)):
                types[key] = infer_base_type(value)
    return types
