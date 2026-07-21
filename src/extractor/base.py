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
