The NetSuite HTTP extractor pulls data from NetSuite over its native HTTP interfaces — the REST Record API, SuiteQL, SuiteTalk SOAP saved searches, and custom RESTlets — authenticating with Token-Based Authentication (TBA).

It supports four extraction modes:

- **Record** — read a NetSuite record type through the REST Record API, with optional field projection, a `q` filter, and sublist flattening or a separate child table.
- **SuiteQL** — run an arbitrary SuiteQL query, with optional date windowing to stay under NetSuite's result-size ceiling.
- **Saved Search** — execute an existing saved search (`customsearch_*`) via SuiteTalk SOAP, selecting the underlying record type used to build the request.
- **RESTlet** — call a deployed RESTlet script (GET/POST/PUT/DELETE) and extract rows from its JSON response, with optional cursor-based pagination.

Both full and incremental loads are supported; incremental runs upsert on a configurable watermark column and primary key. The component emits authoritative table schema manifests describing the output columns and their data types.
