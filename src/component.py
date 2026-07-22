"""NetSuite HTTP Extractor — component entrypoint.

``run()`` is a thin orchestrator: validate config → build the TBA signer → select the extractor for
the row's mode → run it → write the output tables and manifests. All HTTP/SOAP lives under
``client/`` and all mapping under ``extractor/``; no business logic lives here.
"""

import csv
import json
import logging
import re
from collections import OrderedDict
from typing import Any

from keboola.component.base import ComponentBase
from keboola.component.dao import BaseType, ColumnDefinition, TableDefinition
from keboola.component.exceptions import UserException
from keboola.vcr import CallbackSanitizer, DefaultSanitizer, UrlPatternSanitizer

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

# --------------------------------------------------------------------------------------------------
# VCR sanitizers — scrub every secret/PII/nonce from recorded cassettes (spec §7).
#
# The framework always prepends a DefaultSanitizer built from the recording secrets, which (a) strips
# every non-whitelisted header — so the whole ``Authorization: OAuth …`` header (realm, oauth_nonce,
# oauth_timestamp, oauth_signature) disappears — and (b) exact-value replaces the four ``#`` TBA
# secrets wherever they appear (incl. the SOAP ``consumerKey``/``token`` elements). The sanitizers
# below add what that cannot know statically:
#   * the NetSuite account id, which is embedded in EVERY host (``<acct>.suitetalk|restlets.api…``)
#     and in the HATEOAS ``links`` of REST response bodies — rewritten to a fixed ``account`` label
#     by regex so no real account id is ever committed, and so replay still matches (the same rewrite
#     runs on the live request before matching);
#   * the SOAP ``TokenPassport`` fields (account/nonce/timestamp/signature) carried in the request
#     XML body — redacted by element;
#   * email addresses in response/output bodies — redacted as PII.
# Matching is method+scheme+host+port+path+query (never the signed header); the host rewrite is
# deterministic so it matches on replay.
# --------------------------------------------------------------------------------------------------

_NS_HOST_RE = re.compile(r"//[A-Za-z0-9-]+\.(suitetalk|restlets)\.api\.netsuite\.com")
_NS_HOST_REPL = r"//account.\1.api.netsuite.com"
# TokenPassport element contents (optionally namespace-prefixed), keeping the open/close tags intact.
_SOAP_TP_RE = re.compile(
    r"(<(?:\w+:)?(account|consumerKey|token|nonce|timestamp|signature)(?:\s[^>]*)?>)(.*?)(</(?:\w+:)?\2>)",
    re.DOTALL,
)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _as_text(value: Any) -> tuple[str | None, str | None]:
    """Return (text, encoding) for a str/bytes body, or (None, None) if not textual."""
    if isinstance(value, str):
        return value, None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore"), "utf-8"
    return None, None


def _scrub_request(request: Any) -> Any:
    """Rewrite the account host in the URI and redact SOAP TokenPassport fields in the body."""
    if getattr(request, "uri", None):
        request.uri = _NS_HOST_RE.sub(_NS_HOST_REPL, request.uri)
    body = getattr(request, "body", None)
    text, encoding = _as_text(body)
    if text is not None and "<" in text:
        scrubbed = _SOAP_TP_RE.sub(r"\1REDACTED\4", text)
        request.body = scrubbed.encode(encoding) if encoding else scrubbed
    return request


def _scrub_response(response: dict) -> dict:
    """Rewrite the account host and redact email PII in the response body (record-time only)."""
    body = response.get("body") if isinstance(response, dict) else None
    if not isinstance(body, dict) or "string" not in body:
        return response
    text, encoding = _as_text(body["string"])
    if text is None:
        return response
    scrubbed = _EMAIL_RE.sub("REDACTED@example.com", _NS_HOST_RE.sub(_NS_HOST_REPL, text))
    body["string"] = scrubbed.encode(encoding) if encoding else scrubbed
    return response


VCR_SANITIZERS = [
    DefaultSanitizer(
        additional_sensitive_fields=[
            "consumer_key",
            "consumer_secret",
            "token_id",
            "token_secret",
            "oauth_signature",
            "oauth_nonce",
            "signature",
            "nonce",
        ]
    ),
    UrlPatternSanitizer(patterns=[(r"//[A-Za-z0-9-]+\.(suitetalk|restlets)\.api\.netsuite\.com", _NS_HOST_REPL)]),
    CallbackSanitizer(before_request=_scrub_request, before_response=_scrub_response),
]

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
        """Validate → build signer → select extractor by mode → run → write output tables."""
        row = self.params.validate_for_run()
        signer = self.build_signer()
        rest_client = RestClient(signer)

        extractor = self._select_extractor(row, signer, rest_client)
        result = extractor.extract()

        self._write_result(result)

    def _select_extractor(
        self,
        row: RecordRow | SuiteQLRow | SavedSearchRow | RestletRow,
        signer: TBASigner,
        rest_client: RestClient,
    ) -> Extractor:
        if isinstance(row, RecordRow):
            return RecordExtractor(row, rest_client)
        if isinstance(row, SuiteQLRow):
            return SuiteQLExtractor(row, rest_client)
        if isinstance(row, SavedSearchRow):
            return SavedSearchExtractor(row, SoapClient(signer))
        if isinstance(row, RestletRow):
            return RestletExtractor(row, RestletClient(signer))
        raise UserException(f"Unsupported mode: {getattr(row, 'mode', None)}")

    # ---- output ----------------------------------------------------------

    def _write_result(self, result: ExtractionResult) -> None:
        for table in result.tables:
            self._write_table(table)

    def _write_table(self, table: OutputTable) -> None:
        # Stream rows straight to the CSV writer instead of materializing them (spec: avoid buffering
        # a whole large pull in memory). The CSV header needs the column set up front, so extractors
        # declare ``columns``; when they don't (a schema-less table), we fall back to buffering once
        # to collect the column union — the only path that holds all rows in memory.
        rows_iter = iter(table.rows)
        columns = table.columns
        if columns is None:
            buffered = list(rows_iter)
            columns = self._collect_columns(buffered)
            rows_iter = iter(buffered)

        # T12: a 0-row run must still create the Storage table. When the row data yielded no columns
        # (columns are data-derived), fall back to the configured primary key as the known header so a
        # header-only CSV + manifest are still written; if even that is empty there is no known schema.
        if not columns:
            columns = list(table.primary_key)
        if not columns:
            logging.warning(
                "Table '%s' produced 0 rows and no known schema (columns are derived from the data); "
                "no Storage table written this run.",
                table.name,
            )
            return

        table_def = self.create_out_table_definition(
            f"{table.name}.csv",
            primary_key=table.primary_key,
            incremental=table.incremental,
            schema=self._build_schema(columns, table.column_types, table.primary_key),
            has_header=True,
        )
        written = 0
        with open(table_def.full_path, "w", encoding="utf-8", newline="") as out_file:
            writer = csv.DictWriter(out_file, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for row in rows_iter:
                writer.writerow({col: self._serialize_value(row.get(col)) for col in columns})
                written += 1
        self.write_manifest(table_def)
        if written == 0:
            logging.info("Table '%s': first run produced 0 rows; wrote a header-only table.", table.name)
        else:
            logging.info("Wrote %s rows to table '%s'.", written, table.name)

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
