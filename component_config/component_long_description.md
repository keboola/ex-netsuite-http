The NetSuite HTTP extractor pulls data from NetSuite over its native HTTP interfaces, authenticating with Token-Based Authentication (TBA).

It supports four extraction modes:

- **Record** — read a NetSuite record type, with optional field projection, a `q` filter, and sublist flattening or a separate child table.
- **SuiteQL** — run an arbitrary SuiteQL query, with an optional date range substituted into the query so you can pull just a recent window.
- **Saved Search** — execute an existing saved search (`customsearch_*`), selecting the underlying record type used to build the request. Filtering is defined inside the saved search itself.
- **RESTlet** — call a deployed RESTlet script (GET/POST/PUT/DELETE) with an optional JSON body and query parameters, and extract rows from its JSON response, with optional cursor-based pagination.

Both full and incremental loads are supported: full load rewrites the table each run, while incremental load upserts the fetched rows on the configured primary key. The component emits authoritative table schema manifests describing the output columns and their data types.
