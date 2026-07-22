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
| `suiteql`      | REST SuiteQL         | Run an arbitrary SuiteQL query, with optional date windowing for large result sets.                   |
| `saved_search` | SuiteTalk SOAP       | Execute an existing saved search (`customsearch_*`), paging with `searchMoreWithId`.                  |
| `restlet`      | Custom RESTlet       | Call a deployed RESTlet (GET/POST/PUT/DELETE) and map rows from its JSON response.                    |

Both full and incremental loads are supported. Incremental runs upsert on a configurable primary key
using a watermark taken from NetSuite's own server clock (captured before the fetch, so a failed run
keeps the previous watermark and is safely retried). Output manifests carry native column types
(integer/numeric/boolean/string) inferred from the fetched data.

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
| `#consumer_key`    | yes      | TBA integration consumer key (encrypted).                            |
| `#consumer_secret` | yes      | TBA integration consumer secret (encrypted).                         |
| `#token_id`        | yes      | TBA access token id (encrypted).                                     |
| `#token_secret`    | yes      | TBA access token secret (encrypted).                                 |

Use the **Test Connection** button to verify the credentials against the metadata catalog.

Extraction row (per row)
------------------------

Common fields (all modes):

| Field                | Default            | Description                                                                               |
|----------------------|--------------------|-------------------------------------------------------------------------------------------|
| `mode`               | `suiteql`          | `record` / `suiteql` / `saved_search` / `restlet`.                                        |
| `output_table_name`  | derived from target| Destination Storage table name.                                                           |
| `primary_key`        | `[]`               | Columns forming the primary key. **Required** for incremental (Storage upsert).           |
| `load_type`          | `incremental_load` | `full_load` or `incremental_load`.                                                        |
| `incremental_field`  | `lastmodifieddate` | Watermark column used for incremental filtering (shown only for incremental load).        |

Mode `record`:

| Field              | Default   | Description                                                                              |
|--------------------|-----------|------------------------------------------------------------------------------------------|
| `record_type`      | —         | NetSuite record type to extract (e.g. `customer`, `invoice`, `customrecord_*`).          |
| `fields`           | `[]`      | Optional column projection; empty = all fields.                                          |
| `query_filter`     | `""`      | Optional REST `q` filter expression (the incremental clause is AND-combined into it).    |
| `sublist_handling` | `flatten` | `flatten` (sublist → JSON column) or `child_table` (separate table keyed to parent id).  |

Mode `suiteql`:

| Field         | Default | Description                                                                                              |
|---------------|---------|----------------------------------------------------------------------------------------------------------|
| `query`       | —       | SuiteQL query. Use `:state` for the incremental lower bound.                                              |
| `window_size` | `0`     | Days per date window (to stay under NetSuite's ~100k-row ceiling). `0` disables. When > 0 the query must contain `:window_start` and `:window_end` placeholders. |

Mode `saved_search`:

| Field                | Default       | Description                                                                                    |
|----------------------|---------------|------------------------------------------------------------------------------------------------|
| `saved_search_id`    | —             | Saved search to execute (`customsearch_*`).                                                    |
| `search_record_type` | `Transaction` | The saved search's underlying record type — selects the SuiteTalk `SearchAdvanced` request type (e.g. `Transaction`, `Customer`, `Item`, `Contact`, `Employee`, `Vendor`). |
| `page_size`          | `1000`        | SOAP result page size.                                                                          |
| `extra_filters`      | `[]`          | Advanced; not applied server-side (embed such filters in the saved search itself).             |

Mode `restlet`:

| Field                     | Default | Description                                                                    |
|---------------------------|---------|--------------------------------------------------------------------------------|
| `script_id`               | —       | RESTlet script internal id.                                                    |
| `deploy_id`               | —       | RESTlet deployment internal id.                                                |
| `method`                  | `GET`   | `GET` / `POST` / `PUT` / `DELETE`.                                             |
| `query_params`            | `{}`    | Optional query parameters sent to the RESTlet.                                 |
| `request_body`            | `null`  | Optional JSON body (for POST/PUT).                                             |
| `record_path`             | `""`    | Dotted path to the rows in the response (e.g. `data.results`). Empty = top level. |
| `pagination_cursor_field` | `""`    | Optional response field holding the next-page cursor.                          |

Sync actions
============

Six UI actions call the real client so you can build a row interactively:

- **testConnection** — validates the TBA credentials and reachability (metadata-catalog ping).
- **listRecordTypes** — record types from the metadata catalog (populates `record_type`).
- **listFields** — fields for the chosen record type (populates `fields`).
- **listSavedSearches** — saved searches via SOAP (populates `saved_search_id`).
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
