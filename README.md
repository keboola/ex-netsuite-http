ex-netsuite-http
================

Keboola extractor that pulls data from **NetSuite over its native HTTP interfaces only** — the REST
Record API, SuiteQL, SuiteTalk SOAP saved searches, and custom RESTlets. It is the HTTP-based
complement to the JDBC/ODBC `ex-netsuite` extractor (SuiteAnalytics Connect): no JDBC driver is
required to reach any NetSuite data surface.

**Table of Contents:**

[TOC]

Functionality
=============

The component is a **row-based** configuration: connection + TBA credentials are set once at the
configuration level, and each row is one extraction target. A row picks one of four **modes** and
the component decides internally whether to call REST or SOAP:

| Mode           | Interface            | What it does                                                                                          |
|----------------|----------------------|-------------------------------------------------------------------------------------------------------|
| `record`       | REST Record API      | Read a record type with optional field projection and a `q` filter; sublists flattened or split out.  |
| `suiteql`      | REST SuiteQL         | Run an arbitrary SuiteQL query, with an optional date range substituted into the query.               |
| `saved_search` | SuiteTalk SOAP       | Execute an existing saved search (`customsearch_*`), paging with `searchMoreWithId`.                  |
| `restlet`      | Custom RESTlet       | Call a deployed RESTlet (GET/POST/PUT/DELETE) and map rows from its JSON response.                    |

Load Type is purely the Storage write mode: **full load** rewrites the whole table each run;
**incremental load** upserts the fetched rows on the primary key (so rows not in the current batch
are kept). There is no state-file watermark — bound the data you pull with the SuiteQL date range,
the record `q` filter, or the saved search definition itself. Output manifests carry native column
types (integer/numeric/boolean/string) inferred from the fetched data.

Prerequisites — NetSuite Token-Based Authentication (TBA)
=========================================================

Authentication is **TBA (OAuth 1.0a, HMAC-SHA256)** — not the interactive OAuth flow. Each customer
creates their own Integration record and Access Token inside NetSuite and pastes five values into the
Keboola configuration. One-time setup in the NetSuite UI:

1. **Enable the features.** Setup → Company → Enable Features → *SuiteCloud* tab: enable **Token-Based
   Authentication**, **REST Web Services**, and **SOAP Web Services** (the last only if you will use
   saved searches).
2. **Create an Integration record** → get the consumer key/secret. Setup → Integration → Manage
   Integrations → New. Give it a name, tick **Token-Based Authentication**, save. NetSuite shows the
   **Consumer Key** and **Consumer Secret** **once** — copy them now.
3. **Create an Access Token** → get the token id/secret. Setup → Users/Roles → Access Tokens → New.
   Pick the integration from step 2, a user, and a role that has the permissions you need (see step
   4). Save. NetSuite shows the **Token ID** and **Token Secret** **once** — copy them now.
4. **Grant role permissions.** The role tied to the token needs, at minimum: *Log in using Access
   Tokens*, *REST Web Services*, *SuiteAnalytics Workbook* / SuiteQL access, plus record-level view
   permissions for every record type you intend to extract (and *SOAP Web Services* for saved
   searches).
5. **Find your Account ID.** Setup → Company → Company Information → **Account ID** (e.g. `1234567`
   for production, `1234567_SB1` for a sandbox). This drives the account-specific API host.

Paste these five values into the connection configuration. No secret is ever written to the output or
committed to recorded tests.

Configuration
=============

Connection (configuration level)
--------------------------------

| Field              | Required | Description                                                          |
|--------------------|----------|----------------------------------------------------------------------|
| `account_id`       | yes      | NetSuite account id, e.g. `1234567_SB1` (realm + host derivation).   |
| `consumer_key`     | yes      | TBA integration consumer key (identifier, not encrypted).            |
| `#consumer_secret` | yes      | TBA integration consumer secret (encrypted).                         |
| `token_id`         | yes      | TBA access token id (identifier, not encrypted).                     |
| `#token_secret`    | yes      | TBA access token secret (encrypted).                                 |

Use the **Test Connection** button to verify the credentials against the metadata catalog.

Extraction row (per row)
------------------------

Common fields (all modes):

| Field                | Default            | Description                                                                               |
|----------------------|--------------------|-------------------------------------------------------------------------------------------|
| `mode`               | `suiteql`          | `record` / `suiteql` / `saved_search` / `restlet`.                                        |
| `output_table_name`  | derived from target| Destination Storage table name.                                                           |
| `primary_key`        | `[]`               | Columns that uniquely identify a row. Load suggestions via **Load columns** or type any name. **Required** for incremental (Storage upsert). |
| `load_type`          | `incremental_load` | `full_load` (rewrite the table) or `incremental_load` (upsert by primary key).            |

Mode `record`:

| Field              | Default   | Description                                                                              |
|--------------------|-----------|------------------------------------------------------------------------------------------|
| `record_type`      | —         | NetSuite record type to extract (e.g. `customer`, `invoice`, `customrecord_*`).          |
| `fields`           | `[]`      | Optional column projection; empty = all fields.                                          |
| `query_filter`     | `""`      | Optional record filter using NetSuite's [`q` syntax](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1545222128.html). |
| `sublist_handling` | `flatten` | `flatten` (sublist → JSON column) or `child_table` (separate table keyed to parent id).  |

Mode `suiteql`:

| Field       | Default | Description                                                                                              |
|-------------|---------|----------------------------------------------------------------------------------------------------------|
| `query`     | —       | SuiteQL query. Put `:date_from` / `:date_to` in the WHERE clause to use the date range below.            |
| `date_from` | `""`    | Start of the date range (absolute `2024-01-01` or relative `5 days ago`), parsed with Keboola's dateparser and substituted into `:date_from`. Empty disables substitution. |
| `date_to`   | `now`   | End of the date range (absolute or relative, e.g. `now`), substituted into `:date_to`.                   |

A single range is issued as-is (no automatic chunking); narrow a range that would exceed NetSuite's
~100k-row SuiteQL result ceiling.

Mode `saved_search`:

| Field                | Default       | Description                                                                                    |
|----------------------|---------------|------------------------------------------------------------------------------------------------|
| `saved_search_id`    | —             | Saved search to execute (`customsearch_*`). Define any filters inside the saved search itself. |
| `search_record_type` | `Transaction` | The saved search's underlying record type — selects the `SearchAdvanced` request type. Creatable: pick a standard type or type any value, including custom record types (`customrecord_*`). |
| `page_size`          | `1000`        | Result page size.                                                                               |

Mode `restlet`:

| Field                     | Default | Description                                                                    |
|---------------------------|---------|--------------------------------------------------------------------------------|
| `script_id`               | —       | RESTlet script internal id.                                                    |
| `deploy_id`               | —       | RESTlet deployment internal id.                                                |
| `method`                  | `GET`   | `GET` / `POST` / `PUT` / `DELETE`.                                             |
| `query_params`            | `""`    | Optional query parameters as a JSON object (e.g. `{"since": "2024-01-01"}`). Must be valid JSON. |
| `request_body`            | `""`    | Optional JSON request body for POST/PUT. Must be valid JSON.                    |
| `record_path`             | `""`    | Dotted path to the rows in the response (e.g. `data.results`). Empty = top level. |
| `pagination_cursor_field` | `""`    | Optional response field holding the next-page cursor.                          |

Sync actions
============

Seven UI actions call the real client so you can build a row interactively:

- **testConnection** — validates the TBA credentials and reachability (metadata-catalog ping).
- **listRecordTypes** — record types from the metadata catalog (populates `record_type`).
- **listFields** — fields for the chosen record type (populates `fields`).
- **getColumns** — primary-key column suggestions for the creatable PK picker: record fields
  (record mode) or the columns a SuiteQL query returns (probed with a single row). Saved search /
  RESTlet columns are not knowable ahead of a run, so the picker is type-your-own there.
- **listSavedSearches** — saved searches (populates `saved_search_id`).
- **validateSuiteQL** — dry-runs the query (`LIMIT`ed) and reports errors.
- **previewRestlet** — makes a single RESTlet call and returns a sampled response.

Output
======

Each row writes one primary output table (record `child_table` mode also writes one child table per
sublist, named `<table>_<sublist>` and keyed to the parent id). Tables are created in the
configuration's **default bucket** unless an output mapping specifies otherwise. Manifests declare the
primary key, load type (incremental → upsert), and native column types inferred from the data. A run
that returns zero rows still creates the table (header-only) from the configured primary key.

The component consumes **no input tables** (`storage.input` is empty).

Development
-----------

To customize the local data folder path, replace the `CUSTOM_FOLDER` placeholder with your desired
path in the `docker-compose.yml` file:

~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    volumes:
      - ./:/code
      - ./CUSTOM_FOLDER:/data
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Clone this repository, initialize the workspace, and run the component using the following
commands:

~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
git clone https://github.com/keboola/ex-netsuite-http.git
cd ex-netsuite-http
docker-compose build
docker-compose run --rm dev
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Run the test suite and perform lint checks using this command:

~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
docker-compose run --rm test
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Test coverage
-------------

- **Unit / mock tests** (`tests/unit/`, `tests/test_*.py`) — config, auth (known-answer
  vectors), client and extractor logic.
- **Live-recorded VCR functional tests** (`tests/functional/`) — `record`, `suiteql`, and the
  sync actions are recorded against a real NetSuite sandbox with `keboola.datadirtest` and
  replayed deterministically in CI (no credentials needed). Cassettes are sanitized (see
  `VCR_SANITIZERS` in `src/component.py`): no account id, TBA secret, or PII is committed.
- **Mock-based functional tests** (`tests/functional_mock/`) — `saved_search` and `restlet`
  are covered with **synthetic** responses, **not** live recordings. The sandbox contained no
  `customsearch_*` saved search and no deployed RESTlet, and the available credentials had no
  permission to create either, so live cassettes could not be recorded. These tests drive the
  real extractors/clients against synthetic response fixtures modeled on NetSuite's documented
  shapes. To record real cassettes later: create a `customsearch_*` saved search / deploy a
  RESTlet in the sandbox, then run `scripts/record_cassettes.py` with the corresponding
  config (the SuiteTalk WSDL is bundled in `src/client/wsdl/`, so SOAP cassettes replay offline).
  See `tests/functional/README.md` for details.

Integration
===========

For details about deployment and integration with Keboola, refer to the
[deployment section of the developer
documentation](https://developers.keboola.com/extend/component/deployment/).
