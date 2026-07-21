# Functional tests — coverage & the saved_search/restlet gap

Two kinds of functional tests exist, deliberately separated because of a sandbox limitation.

## Live-recorded VCR cassettes — `tests/functional/`

`record`, `suiteql`, and the sync actions (`testConnection`, `listRecordTypes`, `listFields`,
`validateSuiteQL`) are **real** interactions recorded against a NetSuite sandbox with
`keboola.datadirtest` and replayed deterministically in CI. Each test dir holds the recorded
`source/data/cassettes/requests.json`, the captured `logs.json`, and the `expected/` output.

Every cassette is sanitized by `VCR_SANITIZERS` in `src/component.py`: the `Authorization` header
(realm/nonce/timestamp/signature) is stripped, the four `#` TBA secrets are value-scrubbed, the
account-id host is rewritten to a fixed `account.*` placeholder in request URIs and response-body
links, the SOAP `TokenPassport` is redacted, and email PII is redacted. No account id, secret, or
PII is committed.

Re-record with `scratchpad/record_cassettes.py` (reads creds from `secrets.env`, never committed).

## Mock-based (SYNTHETIC) tests — `tests/functional_mock/`

`saved_search` (SOAP) and `restlet` are covered with **synthetic, hand-authored responses — not
live recordings.**

**Why:** the sandbox used for recording contained **no `customsearch_*` saved search** and **no
deployed RESTlet**, and the available TBA credentials had **no permission to create either**. Live
cassettes for these two modes therefore could not be recorded. This is an honest coverage gap, not
a passed-off live recording.

**What the mock tests do:** they drive the **real** `SavedSearchExtractor` / `RestletExtractor` (and
the real `RestletClient` HTTP path via the `responses` library) against synthetic response fixtures
modeled on NetSuite's documented shapes (`tests/functional_mock/fixtures/`). They assert row
mapping, `searchMoreWithId` / marker-cursor pagination, `extra_filters`/incremental forwarding,
error surfacing, and state — everything except the live wire format.

- saved_search: basic result, `searchMoreWithId` paging (2 pages), extra_filters + incremental +
  state. The SOAP client runs a saved search the real SuiteTalk way — a typed
  `<RecordType>SearchAdvanced` record (chosen by the row's `search_record_type`) carrying
  `savedSearchId`, with an incremental `lastModifiedDate onOrAfter` criterion layered on via the
  typed `<RecordType>SearchBasic`. That request **shape is validated offline against the bundled
  WSDL** by `tests/unit/test_soap_client.py::test_saved_search_request_builds_and_validates_offline`
  (zeep type-checks the request at serialization, which is what caught the earlier
  `SearchRequest(savedSearchId=...)` signature bug).
  - **Residual gap:** only the *live request/response round-trip* is still unverified — no sandbox
    saved search exists to execute it against. Also, arbitrary `extra_filters` are **not** applied
    server-side (per-field SuiteTalk typing is record-type specific); embed such filters in the
    saved search itself. The bundled WSDL (`src/client/wsdl/`) lets a real cassette replay in CI once
    a fixture exists.
- restlet: GET basic, POST with body, marker/cursor pagination (2 pages), non-2xx error surfaced
  with body (spec §4).
- **Deferred variant:** async saved-search execution (spec §4) is left as a seam
  (`SavedSearchExtractor._run_async`) and is intentionally not mocked.

## Recording these for real later

1. In the sandbox: create a `customsearch_*` saved search, and deploy a RESTlet (note its script +
   deployment internal ids and response shape).
2. Add `saved_search` / `restlet` entries to `tests/setup/configs.json`.
3. Run `uv run python scratchpad/record_cassettes.py` (it also regenerates `expected/` from a
   sanitized replay). Verify the sanitization + intent gate, then commit.
