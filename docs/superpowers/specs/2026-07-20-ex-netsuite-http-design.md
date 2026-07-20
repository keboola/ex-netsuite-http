# NetSuite HTTP Extractor — Design Spec

> Type: extractor
> Component ID: `keboola.ex-netsuite-http`
> Status: draft (awaiting user approval)
> Date: 2026-07-20
> Branch: `initial-implementation`

## 1. Overview & source system

The NetSuite HTTP Extractor pulls data from Oracle NetSuite over its **HTTP integration
surfaces only** — REST Record API, SuiteQL over REST, SOAP/SuiteTalk Web Services, and
customer-deployed RESTlets. It is the HTTP-based complement to the sibling PHP component
`ex-netsuite`, which reaches NetSuite over JDBC/ODBC via SuiteAnalytics Connect; this component
gives customers feature parity with that driver without ever needing JDBC.

- **Source system:** Oracle NetSuite (SuiteTalk / SuiteQL / RESTlets).
  Docs: NetSuite REST Web Services, SuiteQL, SuiteTalk SOAP, RESTlets
  (`https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/`).
- **Primary use case:** load any NetSuite record type, arbitrary SuiteQL, any saved search, or any
  RESTlet response into Keboola Storage, with incremental loading and configurable output.

The customer selects **what** to pull (via a `mode` enum). The component decides **how** (REST vs
SOAP is an internal implementation detail, never surfaced in the UI).

## 2. Keboola mapping

| NetSuite concept | Keboola construct |
|---|---|
| One extraction target (a record type / a SuiteQL query / a saved search / a RESTlet) | **one config row** |
| TBA credentials + account id | **config-level** `#`-encrypted parameters, shared by all rows |
| Per-target options (record type, query text, saved-search id, filters, incremental column, output table name, PK) | **row-level** parameters |
| Record/query result set | one **output table** in `data/out/tables/` + manifest |
| Sublist (e.g. invoice lines) | flattened into parent rows **or** a separate child output table keyed to parent id |
| Last-seen `lastmodifieddate` watermark | per-row `state.json` (`{"last_run": "<ISO-8601 UTC>"}`) |
| Credential validation, dropdowns, query dry-run | **sync actions** (6, see §5) |

**Config vs config rows.** This is a **config-row component** (`configRowSchema.json` non-empty).
Connection/auth lives at config level; each extraction target is one row. This matches the sibling
JDBC extractor's config-row model and the Keboola convention (multiple objects → rows, one per
object). Per the platform, the component always receives a **single merged `config.json`** (root
`parameters` merged with the row's `parameters`) — it never sees the root/row split.

**Execution model (grounded correction).** Config rows run **sequentially by default** in
`rowsSortOrder`; each row does a full input→run→output cycle and commits before the next row starts.
Parallelism is **opt-in** (`parallelism` > 1 spins up multiple identical containers, each with its
own merged config). The spec does not assume rows run in parallel; we neither require nor forbid
parallelism, and because state is not thread-safe we document that enabling parallelism on the same
config is last-writer-wins for state (irrelevant to the default sequential mode).

**Incremental → output mapping + state.** Incremental is driven by a watermark column
(`lastmodifieddate` / `lastModifiedDate`), default `incremental_load`. On incremental runs we set
`incremental=True` on the output table and require a primary key so Storage **upserts** (PK match →
replace). State is **per-row and automatic**: before a row runs, its state loads into
`/data/in/state.json`; after success we `write_state_file({"last_run": "<max watermark seen, UTC
ISO-8601>"})` to `/data/out/state.json`, which the platform saves back to that row only. The
watermark is written **only after a successful output write**, so a failed run is safely retried.

**Secrets.** The four TBA secrets are `#`-prefixed config keys (`#consumer_key`, `#consumer_secret`,
`#token_id`, `#token_secret`); the platform encrypts them on save (`KBC::ProjectSecure::…`) and the
component receives decrypted plaintext at runtime. `account_id` is plain.

**Output bucket / table naming.** Output table name is a row-level parameter (parity with the JDBC
extractor's `outputTable`); default derived from the target (e.g. `customer`, `suiteql_result`, the
saved-search id, the RESTlet script id). We do **not** enable `default_bucket` in the portal (it
would silently override destinations); tables route to the config's normal output bucket.

**Native types.** Emit an authoritative `schema` manifest (CF default) and set the Dev Portal
`dataTypeSupport=authoritative` in Phase 6. The component must fall back to legacy `columns` behaviour
when `KBC_DATA_TYPE_SUPPORT` is absent (the python-component library auto-detects). CSV written with a
header row must set `has_header=True` on `create_out_table_definition` (verified only by a live smoke
test, Phase 7).

## 3. Authentication & connection

**Chosen auth: TBA — Token-Based Authentication, OAuth 1.0a, HMAC-SHA256.** This is NetSuite's
mandated token auth for server-to-server integration and the method the brief specifies. HMAC-SHA1
support ended in 2023.1, so HMAC-SHA256 is the only signature method.

**The 5 config fields:**

| Field | Encrypted | Role |
|---|---|---|
| `account_id` | plain | realm + host derivation |
| `#consumer_key` | yes | Integration consumer key |
| `#consumer_secret` | yes | signing key part 1 |
| `#token_id` | yes | Access Token id (`oauth_token`) |
| `#token_secret` | yes | signing key part 2 |

**`sign_request` strategy interface — two shapes.** Auth is a strategy interface so a future OAuth
2.0 signer drops in without touching extractor code. The TBA signer must produce **two distinct
shapes** because REST/RESTlet and SOAP sign differently:

1. **OAuth `Authorization` header (REST + RESTlet).** RFC 5849 signature base string =
   `<HTTP-METHOD> & <pct-encoded-URL> & <pct-encoded-normalized-params>`. Normalized params = all
   OAuth params **except `realm`** plus query-string params, sorted, percent-encoded. Required OAuth
   params: `oauth_consumer_key`, `oauth_token`, `oauth_signature_method=HMAC-SHA256`,
   `oauth_timestamp`, `oauth_nonce`, `oauth_version=1.0`. **`realm` is NOT in the base string**
   (RFC 5849 §3.4.1.3.1) — it appears only in the header and equals the account id. Signing key =
   `rawurlencode(consumer_secret) + "&" + rawurlencode(token_secret)`. Signature =
   `base64(HMAC-SHA256(base_string, signing_key))`.
2. **SOAP `TokenPassport` element (SOAP SuiteTalk).** Different base string — a `&`-joined
   concatenation of `account & consumerKey & tokenKey & nonce & timestamp` (all rawurlencoded),
   carried in the SOAP `TokenPassport` header, not an HTTP `Authorization` header. Same signing key +
   HMAC-SHA256. `zeep` sends this in the SOAP envelope header.

**Host derivation from `account_id`.** Production REST/SOAP host
`https://<account>.suitetalk.api.netsuite.com`; RESTlet host
`https://<account>.restlets.api.netsuite.com`. Sandbox transform: `_` → `-`, lowercase (e.g.
`XXXXXX_SB1` → `xxxxxx-sb1.suitetalk.api.netsuite.com`). We derive the host from `account_id` with
this rule (matches nearly every client); the DataCenterUrls REST service is noted as a future
robustness improvement, not built now.

**Provisioning (manual, admin, per-customer).** Credentials **cannot** be obtained headlessly. Each
customer must, in their own NetSuite UI: (1) enable the SuiteTalk + TBA features, (2) create an
Integration record → consumer key/secret, (3) create an Access Token for a user+role → token
id/secret, (4) grant the role permission to the records/searches they intend to pull. The README
(Phase 8) documents these steps. This is a per-account manual setup, not a blocker for *our* build
because a verified sandbox already exists (below).

**Blockers / access.** A NetSuite sandbox has been verified reachable with TBA creds in
`secrets.env` (REST metadata catalog + SuiteQL returned 200 during research). `secrets.env` is
gitignored. See §9 for the one real blocker (missing `probe_tba.py`).

**Future-proofing.** Oracle has announced that **as of 2027.1 no new TBA integrations can be
created** (existing keep working; direction is OAuth 2.0). The `sign_request` strategy interface is
exactly the seam that lets an OAuth 2.0 signer be added later without disturbing the clients. We ship
TBA now and document OAuth 2.0 as the planned future signer.

## 4. Capability inventory & scope

Built from Phase 2 research §7 (the full capability inventory). **Every capability is In scope** —
this is a full-scope-by-default greenfield build; no capability from the research inventory is
silently dropped. A handful of *variant behaviours* are deferred with a recorded reason and flagged
for the user's sign-off in the Phase 6 review gate; they are marked **In scope (core) / deferred
(variant)** so the capability itself still ships.

### Mode `record` — REST Record API (SOAP fallback)

| Capability | Verdict | Rationale |
|---|---|---|
| Standard record types (~280, role/feature-dependent: entities, transactions, items, lists/setup) | In scope | core of record mode; live list from metadata catalog |
| Custom records (`customrecord_*`) | In scope | customer-defined types, first-class |
| Sublists (invoice lines, addressbook, contacts) → flatten into parent rows | In scope | default sublist handling |
| Sublists → separate child output table keyed to parent id | In scope | alternative sublist handling, row option |
| Collection list with `q` filter | In scope | filtering per brief |
| GET by internal id | In scope | single-record read |
| `expandSubResources` on GET | In scope | needed for field-rich single reads |
| `fields=` projection (custom column selection) | In scope | JDBC-parity knob |
| `limit`/`offset` paging (≤1000, offset÷limit) | In scope | REST collection paging |
| Incremental via `lastModifiedDate ON_OR_AFTER` | In scope | incremental parity |
| **REST→SOAP fallback** (record type not REST-exposed; join needs SOAP) | In scope (core) / **deferred (variant)** | transparent fallback is designed into the client seam; the *automatic* trigger set beyond "type absent from metadata catalog" (e.g. join-driven fallback) is deferred pending sandbox coverage — user sign-off |

### Mode `suiteql` — SuiteQL over REST

| Capability | Verdict | Rationale |
|---|---|---|
| Arbitrary SuiteQL (`SELECT`, joins, `GROUP BY`, aggregations, analytics views, custom fields, functions, `WHERE`/`ORDER BY`) | In scope | the JDBC-parity workhorse; equivalent to custom `query` |
| `Prefer: transient` header | In scope | required by NetSuite |
| `limit`/`offset` + `hasMore` paging | In scope | standard SuiteQL paging |
| **100k-row ceiling** handling via date/id windowing | In scope | hard governance limit; windowing built in for large/incremental pulls |
| Incremental via `WHERE lastmodifieddate > :state` | In scope | primary incremental path |
| Typed columns → typed output columns | In scope | native-types manifest |
| Output table name, PK, incremental watermark column, row/page limit | In scope | JDBC-parity knobs |

### Mode `saved_search` — SOAP SuiteTalk

| Capability | Verdict | Rationale |
|---|---|---|
| Execute any saved search by id (`customsearch_*`), incl. formulas/summary/grouping/joins | In scope | single highest-value SOAP feature |
| `searchMoreWithId` paging with configurable `pageSize` | In scope | large result sets |
| Additional search filters layered on the saved search | In scope | brief "support filters" |
| Incremental via `lastModifiedDate` criterion / date-parameterized search | In scope | incremental parity |
| **Async execution** for very large searches | In scope (core) / **deferred (variant)** | synchronous `searchMoreWithId` ships first; async (`asyncSearch`/job poll) is a variant deferred pending a sandbox search large enough to exercise it — user sign-off |

### Mode `restlet` — customer RESTlets

| Capability | Verdict | Rationale |
|---|---|---|
| Call any deployed RESTlet (`script=<id>&deploy=<id>`), methods GET/POST/PUT/DELETE | In scope | generic caller |
| Configurable query params + JSON request body | In scope | brief |
| Response → output table; customer describes response shape / record path | In scope | brief |
| Customer-defined marker/cursor pagination (loop on config-named field) | In scope | brief |
| Error responses surfaced with status + body | In scope | reliability |

### Mode `metadata` (REST, supporting — not a user mode)

| Capability | Verdict | Rationale |
|---|---|---|
| Metadata catalog (`/metadata-catalog`, `application/schema+json`) | In scope | powers `listRecordTypes` / `listFields` sync actions; not a standalone extraction mode |

### Cross-cutting (all modes)

| Capability | Verdict | Rationale |
|---|---|---|
| TBA OAuth 1.0a HMAC-SHA256 (strategy iface; OAuth 2.0 future) | In scope | §3 |
| Output: configurable table name, PK, custom column selection | In scope | JDBC parity |
| Incremental: per-row state-file watermark | In scope | §2 |
| Retry + exponential backoff on 5xx + 429; honor `Retry-After` | In scope | §4 mechanics |
| Conservative concurrency (< default tier 15) | In scope | shared account budget |
| Config-row model, `mode` enum, `options.dependencies` per-mode field hiding | In scope | §5 |

**Deferred-variant sign-off:** the three "deferred (variant)" rows (record→SOAP automatic join
fallback, saved-search async, and — implicitly — DataCenterUrls host discovery from §3) are the only
narrowing in this spec. They are surfaced to the user at the Phase 6 review gate; the capabilities
themselves (record mode, saved-search mode, host derivation) all ship.

### Mechanics for the in-scope surface

- **Pagination:** REST Record collection `limit`/`offset` (default 100, max 1000, offset÷limit,
  loop `links.next`); SuiteQL `limit`/`offset` + `hasMore` (fresh OAuth nonce/timestamp per page);
  SOAP `searchMoreWithId(searchId, pageIndex)` with `searchPreferences.pageSize`; RESTlet
  customer-defined marker loop.
- **Rate limits:** shared REST+SOAP concurrency per account (default tier 15 → up to 55). On **429**
  honor the `Retry-After` header when present, else exponential backoff with jitter; retry transient
  5xx; surface 401/403 as `UserException` (config error). Keep concurrency conservative.
- **SuiteQL ceiling:** ~100,000 rows per result set — cannot paginate past it. Large/incremental
  pulls window by date (or id) range to stay under the cap.
- **Nested data:** REST record collection returns **IDs + HATEOAS links only** (non-expanded) and
  only body fields are usable in `q`; field-rich bulk extraction therefore prefers SuiteQL or
  per-id GET with `expandSubResources`. Sublists → flatten or child table (row option).

## 5. Configuration & schema

Actual `configSchema.json` / `configRowSchema.json` are built by **`component-build-ui`** in Phase 4.
This section describes the fields.

**Config-level (`configSchema.json`) — connection, shared by all rows:**
- `account_id` (string, required)
- `#consumer_key`, `#consumer_secret`, `#token_id`, `#token_secret` (password fields, required)
- Sync action button: **testConnection**.

**Row-level (`configRowSchema.json`) — one extraction target:**
- `mode` — enum `record` | `suiteql` | `saved_search` | `restlet` (required); drives
  `options.dependencies` so each mode shows only its fields.
- Common: `output_table_name` (string), `primary_key` (array), `load_type` (enum
  `full_load` | `incremental_load`, default `incremental_load`), `incremental_field` (string,
  the watermark column, shown when incremental).
- `mode=record`: `record_type` (string, populated by `listRecordTypes` dropdown), `fields` (array,
  from `listFields` dropdown), `query_filter` (string, `q` expression), `sublist_handling`
  (enum `flatten` | `child_table`), `page_limit` (int ≤ 1000).
- `mode=suiteql`: `query` (SQL textarea), `page_limit` (int ≤ 1000), date-window options for the
  100k ceiling (`window_column`, `window_size` — optional).
- `mode=saved_search`: `saved_search_id` (string, from `listSavedSearches` dropdown),
  `page_size` (int), `extra_filters` (optional).
- `mode=restlet`: `script_id`, `deploy_id`, `method` (enum GET/POST/PUT/DELETE), `query_params`
  (object), `request_body` (object/textarea), `record_path` (string — where rows live in the
  response), `pagination_cursor_field` (string, optional).

**Sync actions (6, wired to real client calls):**
1. `testConnection` — signed metadata-catalog ping; validates TBA + host reachability (200 vs
   401/403).
2. `listRecordTypes` — from `/services/rest/record/v1/metadata-catalog`.
3. `listFields` — metadata catalog for the chosen `record_type`.
4. `listSavedSearches` — **SOAP** (search/get of saved searches).
5. `validateSuiteQL` — dry-run the `query` with `LIMIT 0` (or `WHERE 1=0`) to validate syntax cheaply.
6. `previewRestlet` — single RESTlet call, sampled response.

Sync actions are declared in the component config and dispatched by the python-component library's
`execute_action()` (already the scaffold entrypoint); each action method is decorated/registered and
returns a JSON result to the UI.

## 6. Code architecture

Matches the brief's suggested layout:

```
src/
├── component.py           # Entry; thin run() orchestrator; routes by mode; sync-action dispatch
├── configuration.py       # Pydantic Configuration + per-mode row submodels
├── client/
│   ├── auth.py            # sign_request strategy iface + TBA signer (2 shapes: header + TokenPassport)
│   ├── rest.py            # REST Record API + SuiteQL + Metadata catalog; retry/backoff/429
│   ├── soap.py            # zeep-based SOAP: record search + saved searches; searchMoreWithId
│   └── restlet.py         # RESTlet caller; marker pagination
├── extractor/
│   ├── record.py          # mode=record — REST-first, SOAP fallback
│   ├── suiteql.py         # mode=suiteql — windowing + incremental
│   ├── saved_search.py    # mode=saved_search — SOAP
│   └── restlet.py         # mode=restlet
└── sync_actions.py        # 6 sync actions calling the clients
```

- **Client separation:** all HTTP/SOAP lives under `client/`, distinct from `component.py`. The
  auth signer is injected into each client (strategy pattern).
- **`run()` is a thin orchestrator:** parse+validate config (Pydantic) → build signer → select
  extractor by `mode` → run extractor → write manifest + state. No business logic in `run()`.
- **Pydantic config:** `Configuration` (connection) + a discriminated union of per-mode row models
  (`RecordRow`, `SuiteQLRow`, `SavedSearchRow`, `RestletRow`) keyed on `mode`. Validation errors →
  `UserException`. `incremental` is a `@computed_field` derived from `load_type`.
- **Error handling:** `UserException` (exit 1, message shown) for bad config, 401/403 auth failure,
  unknown record type, invalid SuiteQL, missing required row fields; unexpected errors propagate to
  exit 2 (hidden). Scratch files (e.g. WSDL cache, paging temp) go to `/tmp`, never
  `data/out/tables/`.
- **Key dependencies:** `keboola.component` (base/CI), `requests` (REST/RESTlet), `zeep` (SOAP),
  `pydantic` (config). OAuth signing implemented in-house (no oauth lib needed for HMAC-SHA256 base
  string) to control the two-shape signer.

## 7. Testing

**Datadir tests** (fixtures = single merged `config.json` with `parameters` only; row-scoped state):
- happy path per mode; validation failure (bad config → exit 1); auth failure (401 → exit 1);
  unknown record type (exit 1); incremental run restoring/writing `state.json`.

**VCR strategy — target ~30 cassettes** (recorded against the verified sandbox; use
`component-developer:generate-vcr-tests`). Distribution from the brief:
- **Sync actions (6):** testConnection ok / 401, listRecordTypes, listFields(customer),
  listSavedSearches, validateSuiteQL ok / bad-sql.
- **REST Record (6):** customer basic, invoice + pagination, query filter, field selection, custom
  record, incremental via state.
- **SuiteQL (5):** simple, multi-page pagination, JOIN, incremental via state, typed columns.
- **SOAP saved search (4):** basic, `searchMoreWithId` paging, filters, async.
- **SOAP record (3):** search customers, search with join, getRecord by id.
- **RESTlet (4):** GET basic, POST with body, marker-based pagination, error response.
- **Edge cases (3):** 429 retry, token expiry, unknown record type.

**Sanitizers (mandatory):** scrub `Authorization` header, `OAuth realm`/account id, `oauth_nonce`,
`oauth_timestamp`, SOAP `TokenPassport` (signature/nonce/tokenId), and any PII in bodies from every
cassette. OAuth signatures are time/nonce-dependent → **match on method+path+query, not on the
signed header**. Account ids and signatures must never be committed.

**Prerequisite — `probe_tba.py` must be (re)written before recording (§9 blocker).** It signs a
request with the TBA creds and hits the REST metadata catalog + a trivial SuiteQL (`SELECT 1`) to
confirm 200s and the correct host. No sample payloads have been captured yet (research did not read
`secrets.env`), so the first VCR recording pass depends on the probe proving reachability and on the
sandbox containing at least one custom record, a saved search id, and a deployed RESTlet (create
fixtures if absent).

## 8. Deployment & validation (cf-dev)

- **Phase 6 (Dev Portal):** `component-dev-portal` via `kbagent` sets configSchema, configRowSchema,
  6 sync actions, `dataTypeSupport=authoritative`, and portal-owned properties — after the bootstrap
  release so CI-sync doesn't overwrite it.
- **Phase 7 (smoke test):** build an `initial-implementation` branch image; create a cf-dev config
  via `kbagent` with `runtime.tag` overridden to the branch build; run a real job. Success =
  job `success` + resolved image tag matching the branch build + expected output tables written
  (e.g. a `customer` SuiteQL pull with plausible row count, incremental state persisted). A live run
  is the only thing that catches the `has_header` native-types gotcha.
- **Phase 8:** open PR `initial-implementation → main`, run full `component-checklist-review`, then
  `babysit-pr` loop with Copilot; hand clean PR to maintainer (factory never merges).

## 9. Open risks & blockers

1. **BLOCKER — `scratchpad/probe_tba.py` does not exist** (brief references it as if it does). Must
   be (re)written before Phase 5 VCR recording; it is the gate proving the creds/host still work.
   Owner: Phase 5 (`component-test`). Highest priority.
2. **Sandbox fixture coverage** — recording needs at least one custom record, a saved search
   (`customsearch_*`), and a deployed RESTlet (`script`/`deploy` ids). If the sandbox lacks any,
   fixtures must be created first. Owner: Phase 5.
3. **TBA 2027.1 deprecation** — no *new* TBA integrations after 2027.1 (existing keep working;
   OAuth 2.0 is the successor). Mitigated by the `sign_request` strategy interface; document clearly.
4. **SuiteQL 100k-row ceiling** — large single-query pulls silently cap at ~100k rows. Mitigated by
   date/id windowing; must be documented so users don't assume unbounded extraction.
5. **REST record collection is ID-only** — the collection endpoint returns IDs + HATEOAS links, not
   field values; field-rich record-mode extraction leans on SuiteQL or per-id GET (N+1 cost).
   Design decision, documented, not a defect.
6. **Result drift in SOAP paging** — records changing mid-pull can shift `searchMoreWithId` pages;
   note as a known NetSuite limitation.

## 10. Grounding reconciliation

Fresh-context reconciliation of this spec against the behaviour-relevant `keboola-context`
references (Phase 1 read + fresh-subagent gate before the plan). Each reference → `correct` or
`corrected: …` with the fix folded into the spec above.

- `architecture-conventions.md` → **correct** — config rows per target, config/row param split,
  `#`-secrets, incremental+PK, `test-connection` sync action, client separated from `component.py`,
  thin `run()`, Pydantic config: all reflected in §2/§5/§6.
- `config-rows.md` → **corrected:** initial draft implied rows could be assumed parallel. Fixed in
  §2 — rows run **sequentially by default**, parallelism opt-in, state per-row and not thread-safe,
  component receives a single merged `config.json`, input mapping belongs on the row.
- `incremental-state.md` → **corrected:** standardized on `write_state_file`/`get_state_file` with
  key `last_run`, watermark written **only after successful output**, UTC ISO-8601 timestamps,
  `load_type` enum (not a bare boolean) with `incremental` as a computed field. Reflected in §2/§5.
- `encryption.md` → **correct** — 4 TBA secrets `#`-prefixed at config level, decrypted at runtime,
  `KBC::ProjectSecure` default scope noted in §3/§2.
- `output-mapping.md` → **corrected:** made explicit that `incremental=True` **requires a PK** to
  upsert (else unbounded growth), and that scratch files must go to `/tmp` (everything under
  `data/out/tables/` uploads). Reflected in §2/§6.
- `native-data-types.md` → **corrected:** added authoritative `schema` manifest as CF default with
  legacy fallback when `KBC_DATA_TYPE_SUPPORT` absent, the Dev Portal `dataTypeSupport` switch
  (Phase 6), and the `has_header=True` gotcha (live smoke test only). Reflected in §2/§8.
- `default-bucket.md` → **corrected:** explicitly decided **not** to enable `default_bucket` (it
  would override row-level output destinations). Reflected in §2.
- `exit-codes.md` → **correct** — `UserException`→exit 1 (shown) for user-fixable errors; unexpected
  → exit 2 (hidden). Reflected in §6.
- `environment-variables.md` → **correct** — datadir layout, `/tmp` for scratch, `KBC_CONFIGROWID`
  absent (None) on non-row runs handled gracefully, `KBC_DATA_TYPE_SUPPORT` may be unset. Reflected
  in §2/§6.
