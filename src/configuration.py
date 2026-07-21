"""Pydantic configuration models for the NetSuite HTTP extractor.

The platform delivers a single **merged** ``config.json`` (root ``parameters`` merged with the
row's ``parameters``), so connection fields and per-mode row fields arrive in one flat dict.
``Configuration`` splits that dict into a always-present :class:`Connection` and an optional,
mode-discriminated row model (absent for config-level contexts such as ``testConnection``).
"""

from enum import StrEnum
from typing import Annotated, Literal

from keboola.component.exceptions import UserException
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, computed_field


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


class RecordRow(BaseRow):
    mode: Literal["record"]
    record_type: str = ""
    fields: list[str] = Field(default_factory=list)
    query_filter: str = ""
    sublist_handling: SublistHandling = SublistHandling.flatten
    page_limit: int = 1000


class SuiteQLRow(BaseRow):
    mode: Literal["suiteql"]
    query: str = ""
    page_limit: int = 1000
    window_column: str = ""
    window_size: int = 0


class SavedSearchRow(BaseRow):
    mode: Literal["saved_search"]
    saved_search_id: str = ""
    page_size: int = 1000
    extra_filters: list[dict] = Field(default_factory=list)


class RestletRow(BaseRow):
    mode: Literal["restlet"]
    script_id: str = ""
    deploy_id: str = ""
    method: HttpMethod = HttpMethod.GET
    query_params: dict = Field(default_factory=dict)
    request_body: dict | None = None
    record_path: str = ""
    pagination_cursor_field: str = ""


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
        try:
            self.connection = Connection(**data)
            self.row: RecordRow | SuiteQLRow | SavedSearchRow | RestletRow | None = None
            if data.get("mode") is not None:
                self.row = _ROW_ADAPTER.validate_python(data)
        except ValidationError as e:
            error_messages = [f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()]
            raise UserException(f"Validation Error: {', '.join(error_messages)}")
