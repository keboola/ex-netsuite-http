"""SYNTHETIC / mock-based functional tests for the `saved_search` (SOAP) mode.

NOT live recordings. The sandbox contained no `customsearch_*` saved search and the credentials
had no permission to create one, so these tests stand in for live VCR cassettes. They drive the REAL
``SavedSearchExtractor`` against synthetic SOAP ``search`` / ``searchMoreWithId`` result objects
parsed from the documented-shape envelope fixtures in ``fixtures/saved_search_page*.xml`` (the SOAP
client boundary is faked here to feed the extractor synthetic result objects). They assert the
extractor maps records, follows searchMoreWithId paging, forwards
search_record_type/extra_filters/incremental criteria, and writes state.

The SOAP request SHAPE (``<RecordType>SearchAdvanced`` with ``savedSearchId`` + a typed
``lastModifiedDate`` criterion) is separately validated offline against the bundled WSDL in
``tests/unit/test_soap_client.py``; what remains unverified is only the live request/response
round-trip (no sandbox saved search).

Async saved-search execution is a spec §4 deferred variant (SavedSearchExtractor._run_async seam) and
is intentionally NOT mocked here.

To record REAL cassettes later: create a `customsearch_*` saved search in the sandbox, then run
``scratchpad/record_cassettes.py`` with a saved_search config (the bundled WSDL already lets the SOAP
client build offline for replay).
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import lxml.etree as etree

from configuration import SavedSearchRow
from extractor.saved_search import SavedSearchExtractor

_FIXTURES = Path(__file__).parent / "fixtures"


def _first(el: Any, localname: str) -> Any:
    """Return the first descendant element with the given local name (namespace-agnostic)."""
    return next((e for e in el.iter() if etree.QName(e).localname == localname), None)


def _text(el: Any, localname: str, default: str) -> str:
    found = _first(el, localname)
    return found.text if found is not None and found.text else default


def _load_search_result(fixture: str) -> SimpleNamespace:
    """Parse a synthetic SOAP envelope fixture into the object shape the extractor reads.

    Matches by local name so it is robust to the platformMsgs/platformCore namespace split in the
    NetSuite envelope (searchResult lives in the messages ns; its children in the core ns).
    """
    root = etree.fromstring((_FIXTURES / fixture).read_bytes())
    result_el = _first(root, "searchResult")
    assert result_el is not None
    records = []
    for rec in result_el.iter():
        if etree.QName(rec).localname != "record":
            continue
        row: dict[str, Any] = {}
        if rec.get("internalId"):
            row["internalId"] = rec.get("internalId")
        for child in rec:
            row[etree.QName(child).localname] = child.text
        records.append(row)
    return SimpleNamespace(
        searchResult=SimpleNamespace(
            totalPages=int(_text(result_el, "totalPages", "1")),
            pageIndex=int(_text(result_el, "pageIndex", "1")),
            searchId=_text(result_el, "searchId", ""),
            recordList=SimpleNamespace(record=records),
        )
    )


def _row(**kw) -> SavedSearchRow:
    params: dict[str, Any] = {
        "mode": "saved_search",
        "saved_search_id": "customsearch_synth",
        "search_record_type": "Transaction",
        "output_table_name": "ss",
        **kw,
    }
    return SavedSearchRow.model_validate(params)


def _fake_client(page1: str, page2: str | None = None) -> mock.Mock:
    client = mock.Mock()
    client.run_saved_search.return_value = _load_search_result(page1)
    if page2 is not None:
        client.search_more_with_id.return_value = _load_search_result(page2)
    return client


def test_basic_single_page_maps_records():
    client = _fake_client("saved_search_page1.xml")
    # page1 fixture declares totalPages=2; for the single-page case force totalPages=1
    client.run_saved_search.return_value.searchResult.totalPages = 1
    ext = SavedSearchExtractor(row=_row(load_type="full_load"), soap_client=client)
    rows = list(ext.extract().tables[0].rows)
    assert [r["companyName"] for r in rows] == ["ACME Inc"]
    assert rows[0]["internalId"] == "101"
    client.search_more_with_id.assert_not_called()


def test_search_more_with_id_paging_two_pages():
    client = _fake_client("saved_search_page1.xml", "saved_search_page2.xml")
    ext = SavedSearchExtractor(row=_row(load_type="full_load"), soap_client=client)
    rows = list(ext.extract().tables[0].rows)
    assert [r["companyName"] for r in rows] == ["ACME Inc", "Globex Corporation"]
    client.search_more_with_id.assert_called_once()
    assert client.search_more_with_id.call_args[0] == ("WEBSERVICES_SYNTHETIC_SEARCH_1", 2)


def test_extra_filters_and_incremental_forwarded_and_state_written():
    client = _fake_client("saved_search_page1.xml")
    client.run_saved_search.return_value.searchResult.totalPages = 1
    filters = [{"field": "status", "operator": "anyOf", "value": "open"}]
    ext = SavedSearchExtractor(
        row=_row(load_type="incremental_load", extra_filters=filters),
        soap_client=client,
        since="2024-01-01T00:00:00Z",
        server_time_provider=lambda: "2024-05-01T00:00:00Z",
    )
    result = ext.extract()
    _, kwargs = client.run_saved_search.call_args
    assert kwargs["search_record_type"] == "Transaction"  # forwarded -> selects the SearchAdvanced type
    assert kwargs["extra_filters"] == filters
    assert kwargs["since"] == "2024-01-01T00:00:00Z"
    assert result.state == {"last_run": "2024-05-01T00:00:00Z"}
    assert result.tables[0].incremental is True
