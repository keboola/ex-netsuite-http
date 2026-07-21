"""Pydantic configuration models for the NetSuite HTTP extractor.

The platform delivers a single **merged** ``config.json`` (root ``parameters`` merged with the
row's ``parameters``), so connection fields and per-mode row fields arrive in one flat dict.
``Configuration`` splits that dict into a always-present :class:`Connection` and an optional,
mode-discriminated row model (absent for config-level contexts such as ``testConnection``).
"""

from enum import StrEnum
from typing import Annotated, Any, Literal

from keboola.component.exceptions import UserException
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    ValidationInfo,
    computed_field,
    model_validator,
)

# Runtime-only validation context flag. Rows are constructed leniently during sync actions (the user
# has not filled every field yet); the run-start path re-validates with this context so the
# runtime-only rules below (required fields, incremental+PK) fire only for an actual extraction run.
_RUNTIME_CONTEXT = {"runtime": True}


def _is_runtime(info: ValidationInfo) -> bool:
    return bool(info.context) and bool(info.context.get("runtime"))


class LoadType(StrEnum):
    full_load = "full_load"
    incremental_load = "incremental_load"


class SublistHandling(StrEnum):
    flatten = "flatten"
    child_table = "child_table"


class HttpMethod(StrEnum):
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    DELETE = "DELETE"


class Connection(BaseModel):
    """Config-level connection + TBA credentials, shared by all rows."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    account_id: str
    consumer_key: str = Field(alias="#consumer_key")
    consumer_secret: str = Field(alias="#consumer_secret")
    token_id: str = Field(alias="#token_id")
    token_secret: str = Field(alias="#token_secret")


class BaseRow(BaseModel):
    """Common row-level fields shared by every mode.

    Mode-specific fields default to empty so mode sync actions (which run before the user has
    filled every field) never crash on construction; presence is validated where actually used.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    output_table_name: str = ""
    primary_key: list[str] = Field(default_factory=list)
    load_type: LoadType = LoadType.incremental_load
    incremental_field: str = "lastmodifieddate"

    @computed_field
    @property
    def incremental(self) -> bool:
        return self.load_type == LoadType.incremental_load

    @model_validator(mode="after")
    def _validate_incremental_pk(self, info: ValidationInfo) -> BaseRow:
        # Guard against unbounded append: an incremental run with no primary key never upserts, so
        # every run blindly appends the whole batch. Only enforced at run start (see _RUNTIME_CONTEXT)
        # so partially-filled rows in sync actions still construct.
        if _is_runtime(info) and self.incremental and not self.primary_key:
            raise UserException(
                "Incremental load requires a primary key so Storage can upsert; without one every "
                "run appends the full batch. Set 'primary_key' or switch 'load_type' to full_load."
            )
        return self


class RecordRow(BaseRow):
    mode: Literal["record"]
    record_type: str = ""
    fields: list[str] = Field(default_factory=list)
    query_filter: str = ""
    sublist_handling: SublistHandling = SublistHandling.flatten
    page_limit: int = 1000

    @model_validator(mode="after")
    def _validate_required(self, info: ValidationInfo) -> RecordRow:
        if _is_runtime(info) and not self.record_type:
            raise UserException("record mode requires 'record_type'.")
        return self


class SuiteQLRow(BaseRow):
    mode: Literal["suiteql"]
    query: str = ""
    page_limit: int = 1000
    # Date windowing keeps large pulls under NetSuite's ~100k-row result ceiling by running the
    # query once per window. The user drives it by placing ':window_start'/':window_end' placeholders
    # in their query's WHERE clause; window_size is the span (in days) of each window.
    window_size: int = 0

    @model_validator(mode="after")
    def _validate_required(self, info: ValidationInfo) -> SuiteQLRow:
        if not _is_runtime(info):
            return self
        if not self.query:
            raise UserException("suiteql mode requires a 'query'.")
        # Windowing is placeholder-driven; a window_size with no placeholders would silently do nothing.
        if self.window_size > 0 and not (":window_start" in self.query and ":window_end" in self.query):
            raise UserException(
                "window_size enables date windowing, which requires ':window_start' and ':window_end' "
                "placeholders in the query WHERE clause (e.g. WHERE lastmodifieddate BETWEEN "
                "':window_start' AND ':window_end'). Add both placeholders or set window_size to 0."
            )
        return self


class SavedSearchRow(BaseRow):
    mode: Literal["saved_search"]
    saved_search_id: str = ""
    # The SuiteTalk SOAP mechanism runs a saved search via a typed ``<RecordType>SearchAdvanced``
    # record, so the saved search's underlying record type must be known (e.g. Transaction, Customer,
    # Item). It drives which SearchAdvanced type the SOAP client instantiates.
    search_record_type: str = "Transaction"
    page_size: int = 1000
    extra_filters: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_required(self, info: ValidationInfo) -> SavedSearchRow:
        if _is_runtime(info):
            if not self.saved_search_id:
                raise UserException("saved_search mode requires a 'saved_search_id'.")
            if not self.search_record_type:
                raise UserException(
                    "saved_search mode requires 'search_record_type' (the saved search's underlying "
                    "record type, e.g. Transaction, Customer, Item) — it selects the SuiteTalk "
                    "SearchAdvanced request type."
                )
        return self


class RestletRow(BaseRow):
    mode: Literal["restlet"]
    script_id: str = ""
    deploy_id: str = ""
    method: HttpMethod = HttpMethod.GET
    query_params: dict[str, Any] = Field(default_factory=dict)
    request_body: dict[str, Any] | None = None
    record_path: str = ""
    pagination_cursor_field: str = ""

    @model_validator(mode="after")
    def _validate_required(self, info: ValidationInfo) -> RestletRow:
        if _is_runtime(info) and (not self.script_id or not self.deploy_id):
            raise UserException("restlet mode requires both 'script_id' and 'deploy_id'.")
        return self


Row = Annotated[
    RecordRow | SuiteQLRow | SavedSearchRow | RestletRow,
    Field(discriminator="mode"),
]

_ROW_ADAPTER: TypeAdapter[RecordRow | SuiteQLRow | SavedSearchRow | RestletRow] = TypeAdapter(Row)


class Configuration:
    """Top-level configuration composed of a connection and (optionally) a per-mode row.

    Not a ``BaseModel`` itself: it composes a :class:`Connection` and a discriminated-union row
    parsed from the same flat merged dict. Any Pydantic ``ValidationError`` surfaces as a clean
    :class:`UserException`.
    """

    def __init__(self, **data):
        # Lenient construction: sync actions build the config before every field is filled, so
        # required-field / incremental rules are deferred to validate_for_run() at run start.
        self._raw: dict[str, Any] = dict(data)
        try:
            self.connection = Connection(**data)
            self.row: RecordRow | SuiteQLRow | SavedSearchRow | RestletRow | None = None
            if data.get("mode") is not None:
                self.row = _ROW_ADAPTER.validate_python(data)
        except ValidationError as e:
            error_messages = [f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()]
            raise UserException(f"Validation Error: {', '.join(error_messages)}") from e

    def validate_for_run(self) -> RecordRow | SuiteQLRow | SavedSearchRow | RestletRow:
        """Re-validate the row for an actual run, enforcing required-field and incremental rules.

        Returns the validated row. Raises :class:`UserException` when ``mode`` is absent or a
        mode-essential field / primary key is missing (these are intentionally not enforced at
        lenient construction time so sync actions keep working).
        """
        if self.row is None:
            raise UserException("Missing required parameter 'mode' (the extraction target).")
        try:
            self.row = _ROW_ADAPTER.validate_python(self._raw, context=_RUNTIME_CONTEXT)
        except ValidationError as e:
            error_messages = [f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()]
            raise UserException(f"Validation Error: {', '.join(error_messages)}") from e
        return self.row
