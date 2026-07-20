# Component Brief: NetSuite HTTP Extractor

**Suggested kickoff:** `/component-developer:component-factory` — use this brief as the input.

## Goal

Build a new Keboola Python component that extracts data from NetSuite over **HTTP APIs only** — REST, SOAP, and RESTlets. The component must offer feature parity with a direct DB driver (SuiteAnalytics Connect) so customers never need JDBC to reach any NetSuite data surface.

A sibling PHP component (`ex-netsuite`) already covers the JDBC/ODBC path via SuiteAnalytics Connect. This new component is the HTTP-based complement.

## Authentication

**TBA (Token-Based Authentication, OAuth 1.0a HMAC-SHA256).** Every customer creates their own Integration record and Access Token in their NetSuite UI, then pastes 5 values into the Keboola config:

- `account_id` (plain)
- `#consumer_key` (encrypted)
- `#consumer_secret` (encrypted)
- `#token_id` (encrypted)
- `#token_secret` (encrypted)

Auth is implemented as a strategy interface (`sign_request(session)`) so additional signers can be added later without disturbing the extractor code.

Sandbox credentials for TBA are in `secrets.env` (already verified against the sandbox — see `scratchpad/probe_tba.py` — REST metadata catalog and SuiteQL both return 200).

## Data surfaces to cover

The customer selects **what** to pull. The component decides **how** to pull it (REST vs SOAP is an implementation detail, never surfaced in the UI).

### 1. Records (REST + SOAP fallback)
- Pull any standard NetSuite record type: `customer`, `vendor`, `invoice`, `salesOrder`, `item`, `journalEntry`, `employee`, `transaction`, and the ~280 others exposed to the token's role.
- Pull custom records (`customrecord_*`).
- Pull sublists (e.g., invoice lines) either flattened into rows or written to a separate output table.
- Route to REST Record API by default; **transparently fall back to SOAP** for record types REST does not expose or for join queries that require SOAP.
- Support field selection, query filters, and pagination (REST `limit`/`offset` up to 1000 per page; SOAP `searchMoreWithId` for large pulls).

### 2. SuiteQL (REST)
- Run arbitrary SuiteQL against `/services/rest/query/v1/suiteql` with `Prefer: transient` header and `limit`/`offset` pagination.
- Full SQL-92 support: joins, aggregations, custom fields, analytics views.
- Incremental loads via user-provided timestamp column (`WHERE lastmodifieddate > :state`).

### 3. Saved Searches (SOAP)
- Execute any saved search by ID (`customsearch_*`).
- Handle `searchMoreWithId` pagination for large result sets.
- Support async execution for very large searches.
- This is the single highest-value SOAP feature — NetSuite power users define most of their reporting as saved searches, and many customers will not accept a NetSuite connector without it.

### 4. RESTlets
- Call arbitrary customer-deployed RESTlets at `/app/site/hosting/restlet.nl?script=<id>&deploy=<id>`.
- Support GET / POST / PUT / DELETE.
- Configurable query params and request body.
- Response mapped to output table (customer describes the response shape in config).
- Pagination via a customer-defined pattern (marker/cursor field in body or params).

### 5. Metadata (REST)
- Metadata catalog endpoint at `/services/rest/record/v1/metadata-catalog` to discover record types and their fields — powers UI sync actions.

## Config-row modes

One config-row component, four modes selected by a `mode` enum:

- `record` — standard or custom NetSuite record
- `suiteql` — SQL query
- `saved_search` — saved search by ID
- `restlet` — customer RESTlet

`options.dependencies` in the schema hides irrelevant fields per mode.

## Sync actions (required)

1. `testConnection` — validates TBA + reachability
2. `listRecordTypes` — from metadata catalog
3. `listFields` for a chosen record type
4. `listSavedSearches` (SOAP)
5. `validateSuiteQL` — dry-run with `LIMIT 0`
6. `previewRestlet` — single call, sampled response

## Feature-parity with JDBC extractor (must-haves)

- Configurable output table name and primary key
- Incremental fetching (state file with last-seen watermark)
- Custom column selection
- Row-level or table-level configs (config rows)
- Retry with exponential backoff on transient errors
- Rate-limit handling (respect NetSuite `Retry-After`)

## Tests — target ~30 VCR cassettes

Recorded against the verified sandbox with the TBA creds in `secrets.env`. Distribution:

- Sync actions (6): testConnection ok / 401, listRecordTypes, listFields(customer), listSavedSearches, validateSuiteQL ok / bad-sql
- REST Record (6): customer basic, invoice + pagination, query filter, field selection, custom record, incremental via state
- SuiteQL (5): simple, multi-page pagination, JOIN, incremental via state, typed columns
- SOAP saved search (4): basic, `searchMoreWithId` paging, filters, async
- SOAP record (3): search customers, search with join, getRecord by id
- RESTlet (4): GET basic, POST with body, marker-based pagination, error response
- Edge cases (3): 429 retry, token expiry, unknown record type

Use `component-developer:generate-vcr-tests` for setup.

## Architecture (suggested)

```
src/
├── component.py           # Entry; routes by mode
├── configuration.py       # Pydantic Configuration + per-mode submodels
├── client/
│   ├── auth.py            # TBA OAuth 1.0a HMAC-SHA256 signer (strategy iface)
│   ├── rest.py            # Record API + SuiteQL + Metadata
│   ├── soap.py            # zeep-based SOAP for records + saved searches
│   └── restlet.py         # RESTlet caller
├── extractor/
│   ├── record.py          # mode=record  — REST-first, SOAP fallback
│   ├── suiteql.py         # mode=suiteql
│   ├── saved_search.py    # mode=saved_search — SOAP
│   └── restlet.py         # mode=restlet
└── sync_actions.py
```

## Reference files in the repo

- `secrets.env` — verified TBA + JDBC creds (JDBC used only by the sibling `ex-netsuite`, ignore for this build)
- `scratchpad/probe_tba.py` — TBA probe script; run it to confirm sandbox is still reachable before recording cassettes
- Sibling repo: `../ex-netsuite/` — the JDBC-based PHP extractor whose capabilities we are matching

## Deliverables

1. Scaffolded Python component (uv, Ruff, standard CF layout)
2. Working code for all four modes + auth
3. Full configSchema + configRowSchema with mode-conditional fields
4. All six sync actions wired to real client calls
5. ~30 VCR-recorded functional tests, all green
6. README with setup steps for customer (NetSuite Integration + Access Token creation, config field reference)
7. Component registered in Keboola Developer Portal (dev-portal skill)
