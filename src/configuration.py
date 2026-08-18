"""Pydantic configuration models for the NetSuite HTTP extractor.

The platform delivers a single **merged** ``config.json`` (root ``parameters`` merged with the
row's ``parameters``), so connection fields and per-mode row fields arrive in one flat dict.
``Configuration`` splits that dict into a always-present :class:`Connection` and an optional,
mode-discriminated row model (absent for config-level contexts such as ``testConnection``).

Load Type is purely the Storage write mode: full load rewrites the table (``incremental=False``),
incremental load upserts by the primary key (``incremental=True``). There is no state-file watermark;
"recent data" is controlled by the SuiteQL date range, the record ``q`` filter, or the saved search
definition itself.
"""

import json
import re
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

# SuiteQL date placeholders substituted at run time from the parsed date range (see extractor.suiteql).
_DATE_FROM_PLACEHOLDER = ":date_from"
_DATE_TO_PLACEHOLDER = ":date_to"

# Output table name pattern. The value becomes ``<output_table_name>.csv`` under ``data/out/tables``
# (and child tables splice an API-supplied sublist name onto it), so it must be a bare filesystem-safe
# slug — letters, digits, underscores, hyphens, dots — with no path separator or '..', so a crafted
# name cannot escape the output directory. Matched with ``fullmatch`` (not ``$``) so a trailing
# newline cannot slip past into the filename.
_SAFE_TABLE_NAME = re.compile(r"[A-Za-z0-9_.-]+")


def _is_runtime(info: ValidationInfo) -> bool:
    return bool(info.context) and bool(info.context.get("runtime"))


def parse_json_field(field_name: str, raw: str, *, require_object: bool) -> Any:
    """Parse a user-authored JSON string field into Python, with a clean UserException on failure.

    Returns ``{}`` (object fields) / ``None`` (body) for an empty value. Used for the RESTlet
    ``query_params`` (must be a JSON object) and ``request_body`` (any JSON value) fields, which the
    user types as free text in the UI and which are parsed at run time.
    """
    text = (raw or "").strip()
    if not text:
        return {} if require_object else None
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise UserException(f"'{field_name}' must be valid JSON: {exc}") from exc
    if require_object and not isinstance(value, dict):
        raise UserException(f"'{field_name}' must be a JSON object (got {type(value).__name__}).")
    return value


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
    # consumer_key and token_id are TBA *identifiers*, not secrets, so they are plain (unencrypted)
    # fields; only the two secrets carry the encrypted ``#`` alias.
    consumer_key: str
    consumer_secret: str = Field(alias="#consumer_secret")
    token_id: str
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

    @computed_field
    @property
    def incremental(self) -> bool:
        return self.load_type == LoadType.incremental_load

    @model_validator(mode="after")
    def _validate_output_table_name(self, info: ValidationInfo) -> BaseRow:
        # Runtime-gated like _validate_incremental_pk: sync actions build partially-filled rows before
        # the user has finished, so only enforce at run start. Empty is allowed (modes fall back to a
        # derived name); a supplied value can't smuggle a path separator or '..' into the output path.
        name = self.output_table_name
        if _is_runtime(info) and name and (".." in name or not _SAFE_TABLE_NAME.fullmatch(name)):
            raise UserException(
                f"output_table_name '{name}' is not a valid table name; use letters, digits, "
                "underscores, hyphens or dots only (no path separators or '..')."
            )
        return self

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
    # Optional date range. When the query contains the ':date_from' / ':date_to' placeholders, these
    # are parsed with Keboola's dateparser (relative strings like "5 days ago" or absolute dates) and
    # substituted into the query at run time (see extractor.suiteql). Leaving date_from empty disables
    # substitution. A single range is issued verbatim — no auto sub-chunking — so a range that would
    # exceed NetSuite's ~100k SuiteQL row ceiling should be narrowed.
    date_from: str = ""
    date_to: str = "now"

    @model_validator(mode="after")
    def _validate_required(self, info: ValidationInfo) -> SuiteQLRow:
        if not _is_runtime(info):
            return self
        if not self.query:
            raise UserException("suiteql mode requires a 'query'.")
        has_placeholders = _DATE_FROM_PLACEHOLDER in self.query or _DATE_TO_PLACEHOLDER in self.query
        if self.date_from and not has_placeholders:
            raise UserException(
                "A date range (Start Date) is set but the query has no ':date_from'/':date_to' "
                "placeholders, so it would do nothing. Add the placeholders to the WHERE clause "
                "(e.g. WHERE lastmodifieddate BETWEEN :date_from AND :date_to) or clear Start Date."
            )
        if has_placeholders and not self.date_from:
            raise UserException(
                "The query uses ':date_from'/':date_to' placeholders but Start Date (date_from) is "
                "empty. Provide a start date (e.g. '5 days ago' or '2024-01-01')."
            )
        return self


class SavedSearchRow(BaseRow):
    mode: Literal["saved_search"]
    saved_search_id: str = ""
    # The saved search runs via a typed ``<RecordType>SearchAdvanced`` record, so the underlying
    # record type must be known (Transaction, Customer, Item, …). Creatable in the UI: any value is
    # allowed, including custom record types (customrecord_* -> their SearchAdvanced type).
    search_record_type: str = "Transaction"
    page_size: int = 1000

    @model_validator(mode="after")
    def _validate_required(self, info: ValidationInfo) -> SavedSearchRow:
        if _is_runtime(info):
            if not self.saved_search_id:
                raise UserException("saved_search mode requires a 'saved_search_id'.")
            if not self.search_record_type:
                raise UserException(
                    "saved_search mode requires 'search_record_type' (the saved search's underlying "
                    "record type, e.g. Transaction, Customer, Item) — it selects the request type "
                    "used to run the search."
                )
        return self


class RestletRow(BaseRow):
    mode: Literal["restlet"]
    script_id: str = ""
    deploy_id: str = ""
    method: HttpMethod = HttpMethod.GET
    # query_params and request_body are authored as free-text JSON in the UI and parsed at run time
    # (see parsed_query_params / parsed_request_body). query_params must be a JSON object; request_body
    # may be any JSON value (object or array).
    query_params: str = ""
    request_body: str = ""
    record_path: str = ""
    pagination_cursor_field: str = ""

    def parsed_query_params(self) -> dict[str, Any]:
        return parse_json_field("query_params", self.query_params, require_object=True)

    def parsed_request_body(self) -> Any:
        return parse_json_field("request_body", self.request_body, require_object=False)

    @model_validator(mode="after")
    def _validate_required(self, info: ValidationInfo) -> RestletRow:
        if _is_runtime(info):
            if not self.script_id or not self.deploy_id:
                raise UserException("restlet mode requires both 'script_id' and 'deploy_id'.")
            # Fail fast on malformed JSON at run start rather than mid-fetch.
            self.parsed_query_params()
            self.parsed_request_body()
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
            # `from None`: the chained ValidationError's str embeds a truncated ``input_value`` of the
            # merged params (including a prefix of the TBA secrets), which logging.exception would
            # otherwise print. error_messages above already gives a clear, secret-free message.
            raise UserException(f"Validation Error: {', '.join(error_messages)}") from None

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
            # See Configuration.__init__: `from None` keeps the secret-bearing ValidationError cause
            # out of the log.
            raise UserException(f"Validation Error: {', '.join(error_messages)}") from None
        return self.row
