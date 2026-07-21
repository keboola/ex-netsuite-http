Configure the NetSuite connection (account ID and Token-Based Authentication credentials) at the configuration level, then add a row per extraction target.

### Connection

Set the **Account ID** and the four TBA secrets (consumer key/secret and token ID/secret). Use **Test Connection** to verify the credentials.

### Extraction row

Each row picks a **Mode** — Record, SuiteQL, Saved Search, or RESTlet — and exposes only the fields relevant to that mode. Set the output table name, primary key, and load type (full or incremental) per row.
