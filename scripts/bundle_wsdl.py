"""Mirror the NetSuite SuiteTalk WSDL + its imported XSD tree into the repo for offline SOAP replay.

The WSDL is version-pinned and account-agnostic (it contains no account id/host — the only endpoint
in it is the generic ``webservices.netsuite.com`` address, which the client overrides per account at
runtime). Its imports are RELATIVE paths, so mirroring the tree preserving the URL path layout means
zeep resolves every import from the local files with no rewriting. Public docs, no auth needed.

    uv run python scratchpad/bundle_wsdl.py
"""

import sys
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import re  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
_DEST = ROOT / "src" / "client" / "wsdl"
_VERSION = "2023_2"
# Public NetSuite webservices host (account-agnostic content); avoids embedding an account host.
_BASE = "https://webservices.netsuite.com"
_ROOT_WSDL = f"{_BASE}/wsdl/v{_VERSION}_0/netsuite.wsdl"

_REF_RE = re.compile(r'(?:schemaLocation|location)="([^"]+)"')


def _local_path(url: str) -> Path:
    """Map an absolute URL to a local path under _DEST, preserving the URL path layout."""
    return _DEST / urlsplit(url).path.lstrip("/")


def main() -> None:
    session = requests.Session()
    seen: set[str] = set()
    queue = [_ROOT_WSDL]
    total_bytes = 0
    while queue:
        url = queue.pop()
        if url in seen:
            continue
        seen.add(url)
        resp = session.get(url, timeout=60)
        resp.raise_for_status()
        text = resp.text
        total_bytes += len(text)
        dest = _local_path(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        for ref in _REF_RE.findall(text):
            if ref.startswith(("http://", "https://")):
                absolute = ref
            elif ref.startswith(("mailto:", "urn:")):
                continue
            else:
                absolute = urljoin(url, ref)
            # Only follow schema/wsdl documents — never the soap:address service endpoint (a
            # `location="…/NetSuitePort_…"` that is not a downloadable document, returns 410).
            if (
                urlsplit(absolute).scheme in ("http", "https")
                and absolute.rsplit("?", 1)[0].endswith((".xsd", ".wsdl"))
                and absolute not in seen
            ):
                queue.append(absolute)

    files = sorted(_DEST.rglob("*"))
    n = sum(1 for f in files if f.is_file())
    print(f"mirrored {n} files, {total_bytes / 1024:.0f} KB total, into {_DEST.relative_to(ROOT)}")
    # sanity: does any mirrored file contain a real account host/id? (must be NO)
    acct_present = any("suitetalk.api.netsuite.com" in f.read_text(errors="ignore") for f in files if f.is_file())
    print(f"any account-specific host in bundled files: {acct_present}")
    root_local = _DEST / "wsdl" / f"v{_VERSION}_0" / "netsuite.wsdl"
    print(f"root WSDL local: {root_local.relative_to(ROOT)} exists={root_local.exists()}")


if __name__ == "__main__":
    main()
