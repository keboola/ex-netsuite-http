# NetSuite HTTP Extractor — Implementation Plan

> Spec: `docs/superpowers/specs/2026-07-20-ex-netsuite-http-design.md`
> Component: `keboola.ex-netsuite-http` (extractor, config rows) · Branch: `initial-implementation`
> Execute via `superpowers:subagent-driven-development` — one fresh subagent per task, review between
> tasks. Owner skills noted per task (implementation → `component-develop`; schema/UI →
> `component-build-ui`; tests → `component-test` / `generate-vcr-tests`). TDD: write the failing test
> first where a task has testable behaviour.

## Conventions (apply to every task)
- Secrets are `#`-prefixed config keys; runtime value is decrypted plaintext.
- `UserException` (exit 1) for user-fixable errors; unexpected → exit 2. Scratch files → `/tmp`.
- `run()` is a thin orchestrator; all HTTP/SOAP in `client/`; Pydantic config validated early.
- Per-row state via `get_state_file`/`write_state_file`, key `last_run`; write only after success.
- Datadir fixtures = single merged `config.json` (`parameters` only); state fixtures row-scoped.
- `ruff check` + `ruff format` clean after every task.

---

## Phase A — Foundations

### Task 1 — Dependencies & project skeleton  (owner: component-develop)
- Add `zeep`, `requests`, `pydantic` (and keep `keboola.component`) to `pyproject.toml`; `uv sync`.
- Create empty `src/client/__init__.py`, `src/extractor/__init__.py`.
- Remove the cookiecutter example logic from `component.py` and the placeholder `Configuration`
  fields (`print_hello`, `api_token`), leaving a clean shell.
- **Done when:** `uv run ruff check` clean; `python -c "import zeep, requests, pydantic"` works.

### Task 2 — Configuration models  (owner: component-develop, TDD)
- Write `tests/unit/test_configuration.py` first: valid config per mode parses; missing secret →
  `UserException`; unknown `mode` → `UserException`; `load_type=incremental_load` → `incremental`
  computed True.
- Implement `src/configuration.py`: `Configuration` (connection: `account_id`, 4 `#` secrets) +
  discriminated union of `RecordRow`/`SuiteQLRow`/`SavedSearchRow`/`RestletRow` on `mode`; common
  fields (`output_table_name`, `primary_key`, `load_type`, `incremental_field`); `incremental` as a
  `@computed_field`. ValidationError → `UserException`.
- **Done when:** unit tests green.

---

## Phase B — Auth (the linchpin)

### Task 3 — TBA signer strategy interface  (owner: component-develop, TDD)
- Write `tests/unit/test_auth.py` first with **known-answer vectors**: given fixed
  consumer/token/nonce/timestamp/method/url, assert the exact RFC 5849 base string, signing key, and
  HMAC-SHA256 `Authorization` header; assert `realm` is present in the header but **absent from the
  base string**; assert the SOAP `TokenPassport` base string
  (`account & consumerKey & tokenKey & nonce & timestamp`, rawurlencoded) and its signature.
- Implement `src/client/auth.py`: `Signer` interface with `sign_request(...)`; `TBASigner`
  producing **two shapes** — (a) OAuth `Authorization` header for REST/RESTlet, (b) `TokenPassport`
  dict/element for SOAP. HMAC-SHA256 only. Host derivation helper from `account_id`
  (`_`→`-`, lowercase) for `suitetalk.api` and `restlets.api` hosts.
- **Done when:** known-answer unit tests green (no network).

---

## Phase C — Clients

### Task 4 — REST client (Record + SuiteQL + Metadata)  (owner: component-develop, TDD)
- `src/client/rest.py`: signed `requests.Session`; methods for Record collection (`q`, `fields`,
  `limit`/`offset`≤1000, follow `links.next`), Record GET by id (`expandSubResources`), SuiteQL POST
  (`Prefer: transient`, `limit`/`offset`, loop `hasMore`, fresh nonce/timestamp per page), metadata
  catalog GET.
- **Retry/backoff:** honor `Retry-After` on 429, exponential backoff + jitter on transient 5xx;
  401/403 → `UserException`.
- Tests: mock/`responses`-level unit tests for pagination loop, 429 `Retry-After` honoring, and
  `hasMore` termination.
- **Done when:** unit tests green.

### Task 5 — SOAP client (zeep)  (owner: component-develop)
- `src/client/soap.py`: `zeep` client against pinned `NetSuitePort_<version>` WSDL; inject
  `TokenPassport` header from `TBASigner`; `search`/`searchMoreWithId` with `searchPreferences.pageSize`;
  `get`/`getList`; saved-search execution by id; list-saved-searches op. WSDL cached to `/tmp`.
- Tests: deferred to VCR (Phase 5) — SOAP is impractical to unit-test without a recorded envelope;
  add a thin unit test that the `TokenPassport` header is attached.
- **Done when:** module imports, header-injection unit test green.

### Task 6 — RESTlet client  (owner: component-develop, TDD)
- `src/client/restlet.py`: signed calls to `restlets.api` host with `script`/`deploy`, methods
  GET/POST/PUT/DELETE, query params + JSON body, marker/cursor pagination loop on a config-named
  field, error responses surfaced with status+body.
- Tests: unit test the cursor pagination loop and error surfacing (mocked).
- **Done when:** unit tests green.

---

## Phase D — Extractors (one per mode)

### Task 7 — `record` extractor  (owner: component-develop, TDD)
- `src/extractor/record.py`: REST-first; `fields` projection; `q` filter incl.
  `lastModifiedDate ON_OR_AFTER :last_run` for incremental; sublist handling (flatten | child table);
  SOAP fallback seam when record type absent from metadata catalog. Writes output table + manifest
  (schema/native types, `has_header=True` if header written); writes `last_run` state after success.
- **Done when:** unit tests (mocked client) green for flatten vs child-table and incremental filter.

### Task 8 — `suiteql` extractor  (owner: component-develop, TDD)
- `src/extractor/suiteql.py`: run query, paginate, map typed columns; incremental
  `WHERE lastmodifieddate > :last_run` with pre-fetch `run_started_at` from NetSuite `SELECT SYSDATE`;
  **date/id windowing** to stay under the 100k ceiling. State after success.
- **Done when:** unit tests green (windowing splits a large range; watermark captured pre-fetch).

### Task 9 — `saved_search` extractor  (owner: component-develop)
- `src/extractor/saved_search.py`: execute saved search by id via SOAP; `searchMoreWithId` paging;
  extra filters; incremental via `lastModifiedDate` criterion. Async execution is **deferred
  (variant)** per spec §4 — leave a clearly-marked extension seam, do not implement.
- **Done when:** module wired; covered by VCR in Phase 5.

### Task 10 — `restlet` extractor  (owner: component-develop, TDD)
- `src/extractor/restlet.py`: call RESTlet, extract rows at `record_path`, cursor pagination, map to
  output table. State handling if the RESTlet exposes an incremental cursor.
- **Done when:** unit tests green (record_path extraction, pagination).

---

## Phase E — Orchestration & sync actions

### Task 11 — `component.py` router  (owner: component-develop, TDD)
- Thin `run()`: parse+validate `Configuration` → build `TBASigner` → select extractor by `mode` →
  run → write manifest + state. No logic in `run()`. Route sync actions via `execute_action()`.
- **Done when:** datadir happy-path test per mode (with a mocked/monkeypatched client) green; bad
  config → exit 1.

### Task 12 — Sync actions  (owner: component-develop + component-build-ui, TDD)
- `src/sync_actions.py`: `testConnection`, `listRecordTypes`, `listFields`, `listSavedSearches`,
  `validateSuiteQL` (dry-run `LIMIT 0`), `previewRestlet`. Each calls the real client and returns a
  JSON UI result; failures → `UserException`.
- **Done when:** unit tests green (mocked clients); actions registered/dispatchable.

### Task 13 — configSchema + configRowSchema  (owner: component-build-ui)
- Build `component_config/configSchema.json` (connection: `account_id` + 4 password fields;
  testConnection button) and `configRowSchema.json` (`mode` enum + `options.dependencies` hiding
  per-mode fields; common output/PK/load_type/incremental fields; the sync-action-backed dropdowns
  `listRecordTypes`/`listFields`/`listSavedSearches`; `validateSuiteQL` and `previewRestlet`
  buttons). Provide `sample-config`.
- **Done when:** schema-tester passes; fields hide/show correctly per mode.

---

## Phase F — Tests (owner: component-test / generate-vcr-tests)

### Task 14 — (Re)write `scratchpad/probe_tba.py` — BLOCKER, do first in this phase
- Sign a request with the `secrets.env` TBA creds; hit REST metadata catalog + `SELECT 1` SuiteQL;
  confirm 200s and correct host. Gate before any recording. Verify sandbox has a custom record, a
  saved search (`customsearch_*`), and a deployed RESTlet; create fixtures if missing.

### Task 15 — VCR harness + sanitizers
- Set up `keboola.datadirtest` + VCR. **Sanitizers (mandatory):** scrub `Authorization`, account
  id/`realm`, `oauth_nonce`/`oauth_timestamp`, SOAP `TokenPassport` fields, body PII. **Match on
  method+path+query, not the signed header** (signatures are nonce/time-dependent).

### Task 16 — Record ~30 cassettes (per spec §7 distribution)
- Sync actions (6), REST Record (6), SuiteQL (5), SOAP saved search (4), SOAP record (3),
  RESTlet (4), edge cases (3: 429 retry, token expiry, unknown record type).
- **Done when:** full `pytest` suite green; cassettes sanitized (no secrets/account id committed).

---

## Phase G — Deploy & review (tracker Phases 6–8)
- **Task 17 (Phase 6, component-dev-portal):** push schemas + 6 sync actions + portal properties +
  `dataTypeSupport=authoritative` via `kbagent` (after bootstrap release).
- **Task 18 (Phase 7, component-test):** build `initial-implementation` image; cf-dev config via
  `kbagent` with `runtime.tag` = branch build; real job → `success`; verify output tables +
  incremental state + `has_header` correctness (live-only check).
- **Task 19 (Phase 8):** open PR → full `component-checklist-review` → `babysit-pr` loop with
  Copilot until converged; hand clean PR to maintainer (factory never merges).

## Capability coverage check (plan ↔ spec §4)
Every in-scope capability maps to a task: record mode → T7/T13; suiteql → T8/T13; saved_search →
T9/T13; restlet → T6/T10/T13; metadata/sync actions → T4/T12/T13; TBA auth (2 shapes) → T3;
retry/429 → T4; incremental/state → T7/T8; pagination (all 4 mechanisms) → T4/T5/T6; native types +
output → T7–T10/T18. Deferred variants (record→SOAP join fallback, saved-search async, DataCenterUrls
host discovery) carry marked extension seams (T7/T9/T3), pending user sign-off at the review gate.
