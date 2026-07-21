# Phase 2 Research — NetSuite HTTP Extractor

Research-only deliverable for the Component Factory build of a new Keboola component:
a **NetSuite HTTP Extractor** (REST + SOAP/SuiteTalk + RESTlets), the HTTP complement to
the JDBC-based sibling `ex-netsuite`. This settles the API surface, auth, pagination,
rate limits, incremental strategy, feasibility, and the full capability inventory that the
Phase 3 scope gate will check the spec against.

> Scope note: this is research, NOT the spec and NOT implementation. No scaffolding, no commits.

---

## 0. Sibling parity baseline (`../ex-netsuite`)

`ex-netsuite` is the PHP JDBC/ODBC extractor over **SuiteAnalytics Connect** (CDATA driver).
Its README is largely the shared `db-extractor-*` template (Informix boilerplate), but the
real, must-match capability surface it exposes is the standard Keboola DB-extractor contract:

- Table mode (`table.tableName` + `table.schema`) **or** custom `query` (advanced mode) — one required.
- `columns[]` custom column selection (default all).
- `outputTable` configurable output table name.
- `primaryKey[]` on the output table.
- `incremental` (incremental loading) + `incrementalFetchingColumn` + `incrementalFetchingLimit`
  (incremental fetching with a watermark column).
- `retries` count for transient errors.
- Row/table configs (standard DB extractor config-row model).

**Implication:** the new HTTP extractor must reach *any* NetSuite data surface without JDBC and
preserve these knobs: output table name, PK, custom column selection, incremental fetching via a
watermark, config rows, and retry/backoff. SuiteAnalytics Connect exposes essentially every record
as a relational table; the HTTP extractor reaches the same data through 4 API styles (below).

---

## 1. API styles

NetSuite exposes four HTTP integration surfaces. The customer picks **what** to pull (via `mode`);
the component decides **how** (REST vs SOAP is an internal implementation detail, never in the UI).

### 1a. REST Record API — `/services/rest/record/v1/{recordType}`
- CRUD + collection listing over standard and custom record types, JSON payloads.
- Collection filtering via the **`q` query parameter**: `field operator value`, joinable with
  `AND`/`OR` and `()` precedence. Operators include `START_WITH`, `IS`, `ON_OR_AFTER`, `BEFORE`,
  `GREATER_OR_EQUAL`, `LESS_OR_EQUAL`, etc. Example:
  `?q=dateCreated ON_OR_AFTER "1/1/2019" AND dateCreated BEFORE "1/1/2020"`.
- **Key limitation:** the collection/query operation returns only **record IDs + HATEOAS links**
  (non-expanded references), and only **body fields** can be used in `q` conditions. To get full
  field values you must either (a) follow each link / GET each record by id (expensive, N+1), or
  (b) use `expandSubResources=true` on a single-record GET, or (c) pull the data via SuiteQL instead.
- Field selection: `fields=` limits returned fields on record GET; sublists are sub-resources.
- **Best for:** typed single-record reads, custom-record CRUD, and driving UI sync actions.
  For bulk field-rich extraction, SuiteQL is almost always the better REST path than the Record API
  collection endpoint (which is ID-only).
- Docs: Record API browser (2024.1), Record Collection Filtering
  (`section_1545222128.html`), Record Filtering and Query (`chapter_1540810947.html`).

### 1b. SuiteQL over REST — `POST /services/rest/query/v1/suiteql`
- **Required header `Prefer: transient`** — tells NetSuite not to persist query metadata server-side
  (faster, lower memory). Query text goes in the JSON body under `q`.
- Full SQL-92-ish: joins, aggregations, custom fields (`custentity_*`, `custbody_*`), analytics views.
- `limit`/`offset` passed as **URL query params** on the POST; response carries `hasMore`,
  `totalResults`, `count`, `offset`, and a `links` array (`next`/`last`).
- **Best for:** the highest-value, most flexible REST extraction — joins, column projection, filters,
  aggregation, and incremental watermarks. This is the workhorse mode.
- Docs: "Executing SuiteQL Queries Through REST Web Services" (`section_157909186990.html`),
  "Using SuiteQL with SuiteTalk REST Web Services" (`section_158394344595.html`).

### 1c. SOAP SuiteTalk (Web Services) — `https://<account>.suitetalk.api.netsuite.com/services/NetSuitePort_<version>`
- Versioned WSDL (e.g. `2024_1`, `2023_2`, …). Pin an endpoint version; `zeep` is the standard Python
  client. NetSuite deprecates old WSDL versions on a rolling schedule, so version is a config concern.
- Core ops: `search` / `searchMoreWithId` / `searchNext`, `get` / `getList`, plus `add`/`update` (unused here).
- **Saved searches:** execute by referencing the saved-search id (`savedSearchId` / `customsearch_*`).
- **When REST must fall back to SOAP:**
  1. **Saved searches** — not available over REST at all (SOAP only). Highest-value SOAP feature.
  2. Record types **not exposed by the REST Record API** (REST coverage < SOAP coverage historically).
  3. Certain **join-heavy searches** and search columns that SuiteQL/REST cannot express.
- Docs: SuiteTalk Platform Guide; "Advanced Searches in SOAP Web Services" (`section_N3516862.html`);
  "Search Issues and Best Practices" (`section_1519647409.html`).

### 1d. RESTlets — `GET/POST/PUT/DELETE /app/site/hosting/restlet.nl?script=<id>&deploy=<id>`
- Arbitrary customer-deployed SuiteScript endpoints. The component is a generic caller: configurable
  method, query params, and request body; response mapped to an output table (customer describes shape).
- Pagination is **customer-defined** (a marker/cursor field in the body or params, looped by the client).
- **Best for:** bespoke business logic the standard surfaces can't express; escape hatch.
- Account-specific host for RESTlets is `https://<account>.restlets.api.netsuite.com` (the
  `restlet.nl` path also works on the app domain; prefer the account-specific `restlets.api` host).
- Docs: "RESTlet Governance and Security" (`section_4640094112.html`).

### 1e. Metadata catalog (REST, supporting) — `/services/rest/record/v1/metadata-catalog`
- Returns the metadata schema for all exposed records (or selected record types via query).
  `Accept: application/schema+json` or `application/swagger+json`. Powers `listRecordTypes` / `listFields`.
- Docs: "Getting Metadata" (`section_1540810174.html`), "Working with Resource Metadata"
  (`chapter_1540810168.html`).

---

## 2. Authentication — TBA (OAuth 1.0a, HMAC-SHA256)

TBA is the mandated auth. Customer creates an Integration record (→ consumer key/secret) and an
Access Token (→ token id/secret) in their NetSuite UI, then pastes **5 values** into Keboola.

### 2a. The 5 config fields (confirmed)
| Field | Encrypted? | Role |
|---|---|---|
| `account_id` | plain | realm + host derivation |
| `#consumer_key` | yes | Integration consumer key |
| `#consumer_secret` | yes | Integration consumer secret (signing key part 1) |
| `#token_id` | yes | Access Token id (`oauth_token`) |
| `#token_secret` | yes | Access Token secret (signing key part 2) |

### 2b. Signature base string (REST + RESTlets — the RFC5849 form)
Base string = three components joined by `&`:
```
<HTTP-METHOD> & <percent-encoded-URL> & <percent-encoded-normalized-params>
```
- **Normalized params**: all OAuth params **except `realm`**, plus any query-string params, sorted
  alphabetically, percent-encoded, joined with `&` (then the whole blob percent-encoded into the base string).
- **Required OAuth params** (all in the base string, and in the `Authorization` header):
  `oauth_consumer_key`, `oauth_token`, `oauth_signature_method` (`HMAC-SHA256`),
  `oauth_timestamp`, `oauth_nonce`, `oauth_version` (`1.0`).
- **`realm` is NOT part of the base string** (RFC5849 §3.4.1.3.1) — it only appears in the header,
  and `realm` = the **account id** (e.g. `realm="1234567"`).
- **Signing key** = `rawurlencode(consumer_secret) + "&" + rawurlencode(token_secret)`.
- **Signature** = `base64( HMAC-SHA256( base_string, signing_key ) )`.
- **HMAC-SHA1 support ended in 2023.1 → HMAC-SHA256 only.**

> SOAP SuiteTalk uses a *different* base string (a `&`-joined concatenation of
> `realm & consumerKey & tokenKey & nonce & timestamp`, all rawurlencoded) carried in the SOAP
> `TokenPassport` header, not an HTTP `Authorization` header. Same signing key + HMAC-SHA256.
> The auth strategy interface (`sign_request(session)`) must therefore produce **two shapes**:
> an OAuth `Authorization` header for REST/RESTlet, and a `TokenPassport` element for SOAP.

### 2c. account_id → host mapping
- Production REST/SOAP host: `https://<account>.suitetalk.api.netsuite.com`.
- RESTlet host: `https://<account>.restlets.api.netsuite.com`.
- **Sandbox transform:** the `_` in the account id becomes `-` and letters lowercased, e.g.
  account `XXXXXX_SB1` → host `https://xxxxxx-sb1.suitetalk.api.netsuite.com`.
- Oracle guidance: *do not hand-construct the host in production* — the authoritative per-account
  URLs live on the Company Information → Company URLs subtab, discoverable programmatically via the
  **DataCenterUrls REST service** (`chapter_157011836591.html`). For our purposes: derive from
  `account_id` with the `_`→`-` + lowercase rule (matches how nearly every client does it), and
  keep DataCenterUrls as a possible future robustness improvement.

### 2c-note. Deprecation horizon
Oracle has announced that **as of 2027.1 no new TBA integrations can be created** for
SOAP/REST/RESTlets (existing ones keep working; direction is OAuth 2.0). The brief's strategy-interface
`sign_request(session)` design is exactly right — it lets an OAuth 2.0 signer be added later without
touching extractor code. Flag for the spec: document TBA as current, note OAuth 2.0 as the future signer.

---

## 3. Pagination (per mode)

| Mode | Mechanism | Details |
|---|---|---|
| REST Record collection | `limit`/`offset` | default 100, **max 1000** per page; `offset` must be divisible by `limit`; response `links[]` carries `self/first/prev/next/last`; loop `next` until absent. |
| SuiteQL (REST) | `limit`/`offset` + `hasMore` | `limit` max 1000; POST each page (fresh OAuth nonce/timestamp per call); loop while `hasMore==true`, advancing `offset`; `links.next` also available. |
| SOAP search | `searchMoreWithId` / `searchNext` + `pageSize` | set `searchPreferences.pageSize`; initial `search` returns `searchId` + `totalRecords` + `totalPages`; iterate pages with `searchMoreWithId(searchId, pageIndex)`. Beware result drift if records change mid-pull. Later pages may be shorter than `pageSize`. |
| RESTlet | customer-defined marker/cursor | no standard paging; client loops on a config-specified cursor field in body/params. |

---

## 4. Rate limits & governance

- **Concurrency** (shared REST + SOAP, per account): default tier **15** simultaneous requests;
  Tier 2 = 25, Tier 3 = 35, Tier 4 = 45, Tier 5 = 55 (SuiteCloud Plus / service-tier upgrades raise it).
  Exceeding it → **HTTP 429**.
- **429 handling:** honor the **`Retry-After`** response header when present; otherwise exponential
  backoff with jitter. This is the core of the brief's "respect NetSuite `Retry-After`" requirement.
- **SuiteQL governance:** a **hard ~100,000-row ceiling** per query result set — you cannot paginate
  past it. Large extractions must be windowed (e.g. by date range / id range) to stay under the cap.
  This directly shapes incremental design (below).
- **RESTlet governance:** RESTlet scripts get up to **5,000 usage units** per invocation (5× a Suitelet),
  but individual API calls inside the script still cost standard units; the *component* only sees
  HTTP status + body, so from our side it's the same 429/`Retry-After` + backoff contract.
- Practical: keep concurrency conservative (well under 15, since the customer's other integrations
  share the account budget), retry transient 5xx + 429, surface 401/403 as config errors.

---

## 5. Incremental extraction

- **Watermark column:** NetSuite records carry system datetime fields — `lastmodifieddate`
  (also `datecreated`) on most record tables; SuiteQL exposes them directly.
- **SuiteQL mode (primary incremental path):**
  `... WHERE lastmodifieddate > TO_DATE(:state, 'YYYY-MM-DD HH24:MI:SS') ORDER BY lastmodifieddate`
  where `:state` is the last-seen watermark from the Keboola state file. Combine with the 100k-row
  ceiling → window by date if a single incremental slice could exceed it.
- **REST Record mode:** use the `q` filter with `lastModifiedDate ON_OR_AFTER "<state>"` (body-field,
  supported operator). Note the collection endpoint returns IDs only, so field-rich record-mode
  incremental typically still leans on SuiteQL or per-id GETs.
- **SOAP / saved search:** filter by `lastModifiedDate` search criterion, or let the saved search
  itself define the incremental window; watermark still tracked in state.
- **State file:** store `{ "last_value": "<max lastmodifieddate seen>" }` per config row, matching the
  JDBC sibling's incremental-fetching semantics (watermark column + optional row limit). Watch the
  classic watermark hazards: timezone consistency (NetSuite returns account-timezone datetimes),
  `>` vs `>=` boundary duplicates, and clock/commit skew.

---

## 6. Feasibility & provisioning verdict

**Verdict: FEASIBLE.** All four surfaces are reachable over HTTPS with TBA; the brief states the
sandbox was verified via TBA (REST metadata catalog + SuiteQL both returned 200). The auth math
(HMAC-SHA256 base string, realm=account_id, `_`→`-` sandbox host) is well-documented and standard.

**BLOCKER / discrepancy to flag:**
- The brief (lines 23 & 122) references `scratchpad/probe_tba.py` as an existing verification script
  ("run it to confirm the sandbox is still reachable before recording cassettes"). **It does not
  exist.** The `scratchpad/` directory was absent until this research step created it, and a repo-wide
  `find` for `probe_tba.py` returns nothing. Someone must (re)write this probe before Phase 5 VCR
  recording — it is the gate that proves the creds/host still work.
- `secrets.env` exists (378 bytes) — I did **not** read its values (per guardrails). It reportedly
  holds the verified TBA creds (+ JDBC creds that belong to the sibling and are irrelevant here).

**What's needed to record VCR cassettes (Phase 5):**
1. Rewrite `scratchpad/probe_tba.py` (or equivalent) — sign a request with the TBA creds and hit the
   REST metadata catalog + a trivial SuiteQL (`SELECT 1`) to confirm 200s and the correct host.
2. Live sandbox reachability with the current `secrets.env` creds (token not expired/revoked).
3. A VCR setup that **scrubs the `Authorization` header, `OAuth realm`/account id, nonce, timestamp,
   and any PII in bodies** from cassettes — OAuth signatures and account ids must never be committed.
   (Signatures are time/nonce-dependent, so match on method+path+query, not on the signed header.)
4. Sandbox data that exercises each mode: at least one standard record (customer/invoice), a custom
   record, a saved search id (`customsearch_*`), and a deployed RESTlet (`script`/`deploy` ids).
   If the sandbox lacks a saved search or RESTlet, those cassettes need fixtures created first.
5. `component-developer:generate-vcr-tests` for the harness (per brief).

---

## 7. FULL CAPABILITY INVENTORY (baseline for the Phase 3 scope gate)

The complete menu a NetSuite HTTP extractor *can* expose, organized by the 4 config-row modes.
This is intentionally exhaustive — the Phase 3 spec is checked against it, not the reverse.

### Mode `record` — REST Record API (SOAP fallback)
- **Standard record types:** the full set exposed to the token's role — **~280** standard record
  types (approximate and role/feature-dependent; the exact live list comes from the metadata catalog).
  Includes entities (`customer`, `vendor`, `employee`, `contact`, `partner`, `subsidiary`),
  transactions (`invoice`, `salesOrder`, `purchaseOrder`, `journalEntry`, `creditMemo`, `payment`,
  `cashSale`, `vendorBill`, `transaction` generic, …), items (`inventoryItem`, `serviceItem`,
  `assemblyItem`, `item` generic), and lists/setup records (`account`, `location`, `department`,
  `classification`, `currency`, `pricingGroup`, …).
- **Custom records:** `customrecord_*` — any customer-defined record type.
- **Sublists:** e.g. invoice/sales-order **lines**, addressbook, contacts — exposed as sub-resources.
  Output options: **flatten into parent rows** or **write to a separate output table** (child table
  keyed to parent id).
- **Per-type operations:** list/collection with `q` filter; GET by internal id; `expandSubResources`;
  `fields=` projection; `limit`/`offset` paging (≤1000).
- **REST→SOAP fallback triggers:** record type not REST-exposed; join queries REST can't express;
  search-column needs SOAP.
- **Knobs:** output table name, primary key, custom column/field selection, query filter, pagination,
  incremental via `lastModifiedDate`.

### Mode `suiteql` — SuiteQL over REST
- Arbitrary SuiteQL: `SELECT` with joins, aggregations, `GROUP BY`, analytics views, custom fields,
  function calls, `WHERE`/`ORDER BY`.
- `Prefer: transient`; `limit`/`offset` + `hasMore` paging; **100k-row ceiling** per result set.
- Incremental: `WHERE lastmodifieddate > :state`, windowed by date for large sets.
- Typed columns (SuiteQL returns typed values; map to output columns).
- **Knobs:** output table name, PK, incremental watermark column, row/page limit.
- Highest-flexibility mode — effectively the JDBC-parity path (custom `query` equivalent).

### Mode `saved_search` — SOAP SuiteTalk
- Execute any saved search by id (`customsearch_*`) — including complex, customer-authored searches
  with formulas, summary/grouping, and joins that REST/SuiteQL can't replicate.
- `searchMoreWithId` paging with configurable `pageSize`; total pages/records reported.
- Optional **async** execution for very large searches.
- Optional additional search filters layered on top of the saved search.
- Incremental: `lastModifiedDate` search criterion or a date-parameterized saved search.
- **Single highest-value SOAP feature** — many customers gate adoption on saved-search support.

### Mode `restlet` — customer RESTlets
- Call any deployed RESTlet: `script=<id>&deploy=<id>`, methods GET/POST/PUT/DELETE.
- Configurable query params + request body (JSON).
- Response → output table; customer describes the response shape / record path in config.
- Customer-defined marker/cursor pagination (loop on a config-named field).
- Error responses surfaced with status + body.

### Cross-cutting (all modes)
- **Auth:** TBA OAuth 1.0a HMAC-SHA256 (strategy interface; OAuth 2.0 future-proofed).
- **Output:** configurable output table name, primary key, custom column selection.
- **Incremental:** Keboola state-file watermark (`lastmodifieddate`/`lastModifiedDate`).
- **Reliability:** retry with exponential backoff on 5xx + 429; honor `Retry-After`; conservative
  concurrency (< 15 default tier).
- **Config model:** one config-row component, `mode` enum {`record`,`suiteql`,`saved_search`,`restlet`},
  `options.dependencies` hides irrelevant fields per mode.

### Sync actions (UI-supporting, from the brief — all feasible)
1. `testConnection` — signed no-op / metadata ping; validates TBA + host reachability (200 vs 401/403).
2. `listRecordTypes` — from `/metadata-catalog`.
3. `listFields` — metadata catalog for a chosen record type.
4. `listSavedSearches` — **SOAP** (`getSavedSearch` / search of saved searches).
5. `validateSuiteQL` — dry-run with `LIMIT 0` (or a `WHERE 1=0`) to validate syntax cheaply.
6. `previewRestlet` — single RESTlet call, sampled response.

---

## Sources
- REST collection paging / limit-offset (max 1000, offset÷limit): https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_156414087576.html
- Record collection filtering (`q`, operators): https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1545222128.html — and https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_1540810947.html
- SuiteQL over REST (`Prefer: transient`, hasMore): https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_157909186990.html — and https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_158394344595.html
- TBA signature base string (HMAC-SHA256, realm not in base string): https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1534941088.html
- Account-specific domains + sandbox `_`→`-` transform: https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1498251763.html
- DataCenterUrls REST service: https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_157011836591.html
- SOAP search / searchMoreWithId / pageSize / saved searches: https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_N3516862.html — and https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1519647409.html
- Concurrency tiers + 429/Retry-After governance: https://www.houseblend.io/articles/netsuite-api-rate-limits-concurrency
- RESTlet governance: https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4640094112.html
- Metadata catalog / getting metadata: https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1540810174.html — and https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/chapter_1540810168.html
- REST Record API browser (2024.1, record type list): https://system.netsuite.com/help/helpcenter/en_US/APIs/REST_API_Browser/record/v1/2024.1/index.html
