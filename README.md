ex-netsuite-http
=============

Description

**Table of Contents:**

[TOC]

Functionality Notes
===================

Prerequisites
=============

Ensure you have the necessary API token, register the application, etc.

Features
========

| **Feature**             | **Description**                               |
|-------------------------|-----------------------------------------------|
| Generic UI Form         | Dynamic UI form for easy configuration.       |
| Row-Based Configuration | Allows structuring the configuration in rows. |
| OAuth                   | OAuth authentication enabled.                 |
| Incremental Loading     | Fetch data in new increments.                 |
| Backfill Mode           | Supports seamless backfill setup.             |
| Date Range Filter       | Specify the date range for data retrieval.    |

Supported Endpoints
===================

If you need additional endpoints, please submit your request to
[ideas.keboola.com](https://ideas.keboola.com/).

Configuration
=============

Param 1
-------
Details about parameter 1.

Param 2
-------
Details about parameter 2.

Output
======

Provides a list of tables, foreign keys, and schema.

Development
-----------

To customize the local data folder path, replace the `CUSTOM_FOLDER` placeholder with your desired path in the `docker-compose.yml` file:

~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    volumes:
      - ./:/code
      - ./CUSTOM_FOLDER:/data
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Clone this repository, initialize the workspace, and run the component using the following
commands:

~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
git clone  component-ex-netsuite-http
cd component-ex-netsuite-http
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
  RESTlet in the sandbox, then run `scratchpad/record_cassettes.py` with the corresponding
  config (the SuiteTalk WSDL is bundled in `src/client/wsdl/`, so SOAP cassettes replay offline).
  See `tests/functional/README.md` for details.

Integration
===========

For details about deployment and integration with Keboola, refer to the
[deployment section of the developer
documentation](https://developers.keboola.com/extend/component/deployment/).
