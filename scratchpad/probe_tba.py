"""TBA sandbox reachability probe (Phase F, Task 14 — the recording BLOCKER).

Signs two requests with the live TBA creds from ``secrets.env`` and confirms the NetSuite
sandbox answers 200 on:
  (a) the REST metadata-catalog  (GET, application/schema+json)
  (b) a trivial SuiteQL          (POST "SELECT 1 ... FROM dual", Prefer: transient)

It NEVER prints, echoes, or commits any secret value. The derived host is printed with the
account-id portion masked (the account id is itself a secret / must-scrub value). Run it BEFORE
recording any cassette; if the sandbox is unreachable, STOP and report — do not fabricate cassettes.

    uv run python scratchpad/probe_tba.py
"""

import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from client.auth import TBASigner  # noqa: E402

_SECRETS = Path(__file__).resolve().parents[1] / "secrets.env"
_REQUIRED = (
    "NETSUITE_ACCOUNT_ID",
    "NETSUITE_CONSUMER_KEY",
    "NETSUITE_CONSUMER_SECRET",
    "NETSUITE_TOKEN_ID",
    "NETSUITE_TOKEN_SECRET",
)


def _load_secrets() -> dict[str, str]:
    if not _SECRETS.exists():
        sys.exit(f"secrets.env not found at {_SECRETS}")
    values: dict[str, str] = {}
    for line in _SECRETS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip()] = val.strip().strip('"').strip("'")
    missing = [k for k in _REQUIRED if not values.get(k)]
    if missing:
        sys.exit(f"secrets.env is missing keys: {', '.join(missing)}")
    return values


def _mask_host(host: str) -> str:
    """Mask the account-id prefix so the host can be shown without leaking the account id."""
    prefix, _, suffix = host.partition(".")
    return f"***.{suffix}"


def main() -> None:
    s = _load_secrets()
    signer = TBASigner(
        account_id=s["NETSUITE_ACCOUNT_ID"],
        consumer_key=s["NETSUITE_CONSUMER_KEY"],
        consumer_secret=s["NETSUITE_CONSUMER_SECRET"],
        token_id=s["NETSUITE_TOKEN_ID"],
        token_secret=s["NETSUITE_TOKEN_SECRET"],
    )
    host = signer.suitetalk_host
    is_sandbox = "-sb" in host.lower()
    print(f"Resolved SuiteTalk host : {_mask_host(host)}")
    print(f"Looks like a sandbox    : {is_sandbox}  (host contains '-sb')")

    session = requests.Session()
    failures = []

    # (a) REST metadata catalog -------------------------------------------------
    meta_url = f"https://{host}/services/rest/record/v1/metadata-catalog"
    meta_headers = {
        "Authorization": signer.authorization_header("GET", meta_url),
        "Accept": "application/schema+json",
    }
    r1 = session.get(meta_url, headers=meta_headers, timeout=60)
    item_count = None
    if r1.ok:
        try:
            item_count = len(r1.json().get("items", []) or [])
        except ValueError:
            item_count = "?"
    print(f"(a) metadata-catalog GET : HTTP {r1.status_code}  (record types listed: {item_count})")
    if not r1.ok:
        failures.append(f"metadata-catalog returned {r1.status_code}: {r1.text[:300]}")

    # (b) trivial SuiteQL -------------------------------------------------------
    sql_url = f"https://{host}/services/rest/query/v1/suiteql"
    sql_headers = {
        "Authorization": signer.authorization_header("POST", sql_url, query_params={"limit": "1", "offset": "0"}),
        "Prefer": "transient",
        "Content-Type": "application/json",
    }
    r2 = session.post(
        sql_url,
        params={"limit": "1", "offset": "0"},
        json={"q": "SELECT 1 AS probe FROM dual"},
        headers=sql_headers,
        timeout=60,
    )
    sql_rows = None
    if r2.ok:
        try:
            sql_rows = r2.json().get("items")
        except ValueError:
            sql_rows = "?"
    print(f"(b) SuiteQL SELECT 1 POST: HTTP {r2.status_code}  (rows: {sql_rows})")
    if not r2.ok:
        failures.append(f"SuiteQL returned {r2.status_code}: {r2.text[:300]}")

    print("-" * 60)
    if failures:
        print("PROBE FAILED — sandbox NOT confirmed reachable:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("PROBE OK — sandbox reachable, both endpoints returned 200. Safe to record cassettes.")


if __name__ == "__main__":
    main()
